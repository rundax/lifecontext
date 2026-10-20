"""Immutable original-byte custody below the private LifeContext home."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from lifecontext.config import LIMIT_CEILINGS, JournalLimits, load_settings
from lifecontext.paths import LifeContextPaths


_SOURCE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_MANIFEST_SCHEMA = 1
_MAX_MANIFEST_BYTES = 64 * 1024


class CustodyError(ValueError):
    """Stable, redacted local-custody failure."""


class CustodyConflict(CustodyError):
    """The requested immutable source version already exists."""


def _validate_identity(source_id: str, source_version: int) -> tuple[str, int]:
    if not isinstance(source_id, str) or not _SOURCE_ID.fullmatch(source_id):
        raise CustodyError("invalid source identity")
    if (
        isinstance(source_version, bool)
        or not isinstance(source_version, int)
        or not 1 <= source_version <= 2_147_483_647
    ):
        raise CustodyError("invalid source identity")
    return source_id, source_version


def _freeze_json(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze_json(child) for key, child in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(child) for child in value)
    return value


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(child) for child in value]
    return value


def _provenance_copy(value: Mapping[str, Any]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise CustodyError("invalid provenance")
    try:
        raw = json.dumps(
            _thaw_json(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if len(raw) > _MAX_MANIFEST_BYTES // 2:
            raise CustodyError("provenance too large")
        copied = json.loads(raw.decode("utf-8"))
    except CustodyError:
        raise
    except (TypeError, ValueError, UnicodeError):
        raise CustodyError("invalid provenance") from None
    return _freeze_json(copied)


@dataclass(frozen=True)
class SourceManifest:
    """Immutable metadata identifying one exact original source version."""

    source_id: str
    source_version: int
    sha256: str
    size_bytes: int
    provenance: Mapping[str, Any]

    def __post_init__(self) -> None:
        _validate_identity(self.source_id, self.source_version)
        if not isinstance(self.sha256, str) or not _SHA256.fullmatch(self.sha256):
            raise CustodyError("invalid source manifest")
        if (
            isinstance(self.size_bytes, bool)
            or not isinstance(self.size_bytes, int)
            or self.size_bytes < 0
        ):
            raise CustodyError("invalid source manifest")
        object.__setattr__(self, "provenance", _provenance_copy(self.provenance))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": _MANIFEST_SCHEMA,
            "source_id": self.source_id,
            "source_version": self.source_version,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "provenance": _thaw_json(self.provenance),
        }

    def to_bytes(self) -> bytes:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")

    @classmethod
    def from_bytes(cls, raw: bytes) -> "SourceManifest":
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError):
            raise CustodyError("invalid source manifest") from None
        if (
            not isinstance(value, dict)
            or set(value)
            != {
                "schema_version",
                "source_id",
                "source_version",
                "sha256",
                "size_bytes",
                "provenance",
            }
            or value.get("schema_version") != _MANIFEST_SCHEMA
        ):
            raise CustodyError("invalid source manifest")
        try:
            return cls(
                source_id=value["source_id"],
                source_version=value["source_version"],
                sha256=value["sha256"],
                size_bytes=value["size_bytes"],
                provenance=value["provenance"],
            )
        except (KeyError, TypeError):
            raise CustodyError("invalid source manifest") from None


@dataclass(frozen=True)
class SourceEvidence:
    """Verified local bytes and manifest for one exact source version."""

    manifest: SourceManifest
    original_bytes: bytes


def _dir_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)


def _file_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)


def _check_private_dir(info: os.stat_result) -> None:
    if not stat.S_ISDIR(info.st_mode):
        raise CustodyError("unsafe custody path")
    if os.name == "posix" and (
        info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise CustodyError("unsafe custody path")


def _open_child_dir(parent_fd: int, name: str, *, create: bool) -> int:
    if create:
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
        except OSError:
            raise CustodyError("unsafe custody path") from None
    try:
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        _check_private_dir(before)
        child_fd = os.open(name, _dir_flags(), dir_fd=parent_fd)
    except (OSError, CustodyError):
        raise CustodyError("unsafe custody path") from None
    try:
        opened = os.fstat(child_fd)
        _check_private_dir(opened)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise CustodyError("unsafe custody path")
        return child_fd
    except Exception:
        os.close(child_fd)
        raise


def _open_home(paths: LifeContextPaths) -> int:
    try:
        paths.ensure_private_home()
        before = paths.lifeweave_home.lstat()
        _check_private_dir(before)
        descriptor = os.open(paths.lifeweave_home, _dir_flags())
        opened = os.fstat(descriptor)
        _check_private_dir(opened)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            os.close(descriptor)
            raise CustodyError("unsafe custody path")
        return descriptor
    except (OSError, CustodyError):
        raise CustodyError("unsafe custody path") from None


def _read_private_file(parent_fd: int, name: str, max_bytes: int) -> bytes:
    try:
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode):
            raise CustodyError("unsafe custody file")
        if os.name == "posix" and (
            before.st_uid != os.geteuid() or stat.S_IMODE(before.st_mode) != 0o600
        ):
            raise CustodyError("unsafe custody file")
        descriptor = os.open(name, _file_flags(), dir_fd=parent_fd)
    except (OSError, CustodyError):
        raise CustodyError("unsafe custody file") from None
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            or (
                os.name == "posix"
                and (opened.st_uid != os.geteuid() or stat.S_IMODE(opened.st_mode) != 0o600)
            )
        ):
            raise CustodyError("unsafe custody file")
        chunks = bytearray()
        while len(chunks) <= max_bytes:
            chunk = os.read(descriptor, min(64 * 1024, max_bytes + 1 - len(chunks)))
            if not chunk:
                break
            chunks.extend(chunk)
        if len(chunks) > max_bytes:
            raise CustodyError("custody file too large")
        return bytes(chunks)
    except OSError:
        raise CustodyError("custody read failed") from None
    finally:
        os.close(descriptor)


def _write_private_file(parent_fd: int, name: str, raw: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(name, flags, 0o600, dir_fd=parent_fd)
    except OSError:
        raise CustodyError("custody write failed") from None
    try:
        if os.name == "posix":
            os.fchmod(descriptor, 0o600)
        offset = 0
        while offset < len(raw):
            offset += os.write(descriptor, raw[offset:])
        os.fsync(descriptor)
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or (os.name == "posix" and (info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600))
        ):
            raise CustodyError("unsafe custody file")
    except OSError:
        raise CustodyError("custody write failed") from None
    finally:
        os.close(descriptor)


class LifeContextCustody:
    """Store and verify immutable original bytes under ``LifeContextPaths``."""

    def __init__(
        self,
        paths: LifeContextPaths | None = None,
        *,
        max_bytes: int | None = None,
    ) -> None:
        self.paths = paths or LifeContextPaths.current()
        if max_bytes is None:
            max_bytes = JournalLimits.from_config(load_settings()).max_file_bytes
        if (
            isinstance(max_bytes, bool)
            or not isinstance(max_bytes, int)
            or not 1 <= max_bytes <= LIMIT_CEILINGS["max_file_bytes"]
        ):
            raise CustodyError("invalid custody size limit")
        self.max_bytes = max_bytes

    @staticmethod
    def _version_directory(source_version: int) -> str:
        return f"v{source_version}"

    def original_path(self, source_id: str, source_version: int) -> Path:
        source_id, source_version = _validate_identity(source_id, source_version)
        return self.paths.archive_dir / "sources" / source_id / self._version_directory(source_version) / "original.bin"

    def manifest_path(self, source_id: str, source_version: int) -> Path:
        source_id, source_version = _validate_identity(source_id, source_version)
        return self.paths.archive_dir / "sources" / source_id / self._version_directory(source_version) / "manifest.json"

    def relative_manifest_path(self, source_id: str, source_version: int) -> str:
        source_id, source_version = _validate_identity(source_id, source_version)
        return f"sources/{source_id}/{self._version_directory(source_version)}/manifest.json"

    def write_version(
        self,
        source_id: str,
        source_version: int,
        original_bytes: bytes,
        *,
        provenance: Mapping[str, Any],
    ) -> SourceManifest:
        source_id, source_version = _validate_identity(source_id, source_version)
        if not isinstance(original_bytes, bytes):
            raise CustodyError("source content must be bytes")
        if len(original_bytes) > self.max_bytes:
            raise CustodyError("source size limit exceeded")
        copied_provenance = _provenance_copy(provenance)
        manifest = SourceManifest(
            source_id=source_id,
            source_version=source_version,
            sha256=hashlib.sha256(original_bytes).hexdigest(),
            size_bytes=len(original_bytes),
            provenance=copied_provenance,
        )
        manifest_raw = manifest.to_bytes()
        if len(manifest_raw) > _MAX_MANIFEST_BYTES:
            raise CustodyError("source manifest too large")

        home_fd = _open_home(self.paths)
        archive_fd = sources_fd = source_fd = staging_fd = -1
        staging_name = f".staging-{secrets.token_hex(12)}"
        staging_created = False
        try:
            archive_fd = _open_child_dir(home_fd, "archive", create=True)
            sources_fd = _open_child_dir(archive_fd, "sources", create=True)
            source_fd = _open_child_dir(sources_fd, source_id, create=True)
            version_name = self._version_directory(source_version)
            try:
                os.stat(version_name, dir_fd=source_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            except OSError:
                raise CustodyError("unsafe custody path") from None
            else:
                raise CustodyConflict("source version already exists")

            os.mkdir(staging_name, mode=0o700, dir_fd=source_fd)
            staging_created = True
            staging_fd = _open_child_dir(source_fd, staging_name, create=False)
            _write_private_file(staging_fd, "original.bin", original_bytes)
            _write_private_file(staging_fd, "manifest.json", manifest_raw)
            os.fsync(staging_fd)
            try:
                os.stat(version_name, dir_fd=source_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            except OSError:
                raise CustodyError("unsafe custody path") from None
            else:
                raise CustodyConflict("source version already exists")
            try:
                os.rename(
                    staging_name,
                    version_name,
                    src_dir_fd=source_fd,
                    dst_dir_fd=source_fd,
                )
            except FileExistsError:
                raise CustodyConflict("source version already exists") from None
            except OSError:
                # POSIX may report an occupied non-empty destination as ENOTEMPTY.
                try:
                    os.stat(version_name, dir_fd=source_fd, follow_symlinks=False)
                except OSError:
                    raise CustodyError("custody write failed") from None
                raise CustodyConflict("source version already exists") from None
            staging_created = False
            os.fsync(source_fd)
            return manifest
        except (CustodyConflict, CustodyError):
            raise
        except OSError:
            raise CustodyError("custody write failed") from None
        finally:
            if staging_fd >= 0:
                os.close(staging_fd)
            if staging_created and source_fd >= 0:
                try:
                    cleanup_fd = os.open(staging_name, _dir_flags(), dir_fd=source_fd)
                    try:
                        for filename in ("original.bin", "manifest.json"):
                            try:
                                os.unlink(filename, dir_fd=cleanup_fd)
                            except FileNotFoundError:
                                pass
                        os.fsync(cleanup_fd)
                    finally:
                        os.close(cleanup_fd)
                    os.rmdir(staging_name, dir_fd=source_fd)
                except OSError:
                    pass
            for descriptor in (source_fd, sources_fd, archive_fd, home_fd):
                if descriptor >= 0:
                    os.close(descriptor)

    def resolve_exact(self, source_id: str, source_version: int) -> SourceEvidence:
        source_id, source_version = _validate_identity(source_id, source_version)
        home_fd = _open_home(self.paths)
        descriptors: list[int] = [home_fd]
        try:
            archive_fd = _open_child_dir(home_fd, "archive", create=False)
            descriptors.append(archive_fd)
            sources_fd = _open_child_dir(archive_fd, "sources", create=False)
            descriptors.append(sources_fd)
            source_fd = _open_child_dir(sources_fd, source_id, create=False)
            descriptors.append(source_fd)
            version_fd = _open_child_dir(
                source_fd, self._version_directory(source_version), create=False
            )
            descriptors.append(version_fd)
            manifest_raw = _read_private_file(version_fd, "manifest.json", _MAX_MANIFEST_BYTES)
            manifest = SourceManifest.from_bytes(manifest_raw)
            if manifest.source_id != source_id or manifest.source_version != source_version:
                raise CustodyError("source manifest identity mismatch")
            original = _read_private_file(version_fd, "original.bin", self.max_bytes)
            digest = hashlib.sha256(original).hexdigest()
            if len(original) != manifest.size_bytes or digest != manifest.sha256:
                raise CustodyError("source checksum mismatch")
            return SourceEvidence(manifest=manifest, original_bytes=original)
        except CustodyError:
            raise
        except OSError:
            raise CustodyError("source version not found") from None
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    def list_inventory(self, *, limit: int = 4096) -> list[tuple[str, int]]:
        """List every complete version directory beneath the private archive."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 4096:
            raise CustodyError("invalid custody inventory limit")
        descriptors: list[int] = []
        try:
            home_fd = _open_home(self.paths)
            descriptors.append(home_fd)
            try:
                os.stat("archive", dir_fd=home_fd, follow_symlinks=False)
            except FileNotFoundError:
                return []
            archive_fd = _open_child_dir(home_fd, "archive", create=False)
            descriptors.append(archive_fd)
            try:
                os.stat("sources", dir_fd=archive_fd, follow_symlinks=False)
            except FileNotFoundError:
                return []
            sources_fd = _open_child_dir(archive_fd, "sources", create=False)
            descriptors.append(sources_fd)
            try:
                source_names = sorted(os.listdir(sources_fd))
            except OSError:
                raise CustodyError("custody read failed") from None
            if len(source_names) > limit:
                raise CustodyError("custody inventory limit exceeded")
            inventory: list[tuple[str, int]] = []
            for source_id in source_names:
                _validate_identity(source_id, 1)
                source_fd = _open_child_dir(sources_fd, source_id, create=False)
                descriptors.append(source_fd)
                try:
                    version_names = sorted(os.listdir(source_fd))
                except OSError:
                    raise CustodyError("custody read failed") from None
                if len(inventory) + len(version_names) > limit:
                    raise CustodyError("custody inventory limit exceeded")
                for version_name in version_names:
                    match = re.fullmatch(r"v([1-9][0-9]*)", version_name)
                    if match is None:
                        raise CustodyError("unexpected custody entry")
                    version = int(match.group(1))
                    _validate_identity(source_id, version)
                    version_fd = _open_child_dir(source_fd, version_name, create=False)
                    descriptors.append(version_fd)
                    try:
                        names = set(os.listdir(version_fd))
                    except OSError:
                        raise CustodyError("custody read failed") from None
                    if names != {"original.bin", "manifest.json"}:
                        raise CustodyError("incomplete custody version")
                    inventory.append((source_id, version))
                    os.close(version_fd)
                    descriptors.remove(version_fd)
                    if len(inventory) > limit:
                        raise CustodyError("custody inventory limit exceeded")
                os.close(source_fd)
                descriptors.remove(source_fd)
            return inventory
        except CustodyError:
            raise
        except (OSError, ValueError, OverflowError):
            raise CustodyError("custody inventory unavailable") from None
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    def remove_source(
        self, source_id: str, expected_hashes: Mapping[int, str]
    ) -> None:
        """Remove only a complete source tree whose remaining files match custody facts."""
        if not isinstance(expected_hashes, Mapping) or not expected_hashes:
            raise CustodyError("invalid source manifest set")
        expected: dict[int, str] = {}
        for version, digest in expected_hashes.items():
            _validate_identity(source_id, version)
            if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
                raise CustodyError("invalid source manifest set")
            expected[version] = digest

        home_fd = archive_fd = sources_fd = source_fd = -1
        versions: list[tuple[int, int, set[str]]] = []
        try:
            home_fd = _open_home(self.paths)
            archive_fd = _open_child_dir(home_fd, "archive", create=False)
            sources_fd = _open_child_dir(archive_fd, "sources", create=False)
            try:
                os.stat(source_id, dir_fd=sources_fd, follow_symlinks=False)
            except FileNotFoundError:
                return
            except OSError:
                raise CustodyError("unsafe custody path") from None
            source_fd = _open_child_dir(sources_fd, source_id, create=False)
            try:
                source_names = set(os.listdir(source_fd))
            except OSError:
                raise CustodyError("custody read failed") from None
            expected_names = {self._version_directory(version) for version in expected}
            if source_names - expected_names:
                raise CustodyError("unexpected custody entry")

            # Verify every remaining version before deleting any of them.
            for version, digest in sorted(expected.items()):
                name = self._version_directory(version)
                if name not in source_names:
                    continue
                version_fd = _open_child_dir(source_fd, name, create=False)
                try:
                    try:
                        names = set(os.listdir(version_fd))
                    except OSError:
                        raise CustodyError("custody read failed") from None
                    if names - {"original.bin", "manifest.json"}:
                        raise CustodyError("unexpected custody entry")
                    if "original.bin" in names:
                        raw = _read_private_file(version_fd, "original.bin", self.max_bytes)
                        if hashlib.sha256(raw).hexdigest() != digest:
                            raise CustodyError("source checksum mismatch")
                    if "manifest.json" in names:
                        manifest = SourceManifest.from_bytes(
                            _read_private_file(version_fd, "manifest.json", _MAX_MANIFEST_BYTES)
                        )
                        if (
                            manifest.source_id != source_id
                            or manifest.source_version != version
                            or manifest.sha256 != digest
                        ):
                            raise CustodyError("source manifest identity mismatch")
                    versions.append((version, version_fd, names))
                    version_fd = -1
                finally:
                    if version_fd >= 0:
                        os.close(version_fd)

            for _version, version_fd, names in versions:
                for filename in ("original.bin", "manifest.json"):
                    if filename in names:
                        os.unlink(filename, dir_fd=version_fd)
                os.fsync(version_fd)
            for version, version_fd, _names in versions:
                os.close(version_fd)
                os.rmdir(self._version_directory(version), dir_fd=source_fd)
            versions.clear()
            os.fsync(source_fd)
            os.close(source_fd)
            source_fd = -1
            os.rmdir(source_id, dir_fd=sources_fd)
            os.fsync(sources_fd)
        except (CustodyConflict, CustodyError):
            raise
        except OSError:
            raise CustodyError("custody removal failed") from None
        finally:
            for _version, version_fd, _names in versions:
                try:
                    os.close(version_fd)
                except OSError:
                    pass
            for descriptor in (source_fd, sources_fd, archive_fd, home_fd):
                if descriptor >= 0:
                    os.close(descriptor)
