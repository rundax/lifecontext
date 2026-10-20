"""Private immutable extraction JSON, deliberately separate from original custody."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lifecontext.custody import (
    CustodyError,
    _dir_flags,
    _open_child_dir,
    _read_private_file,
    _validate_identity,
)
from lifecontext.paths import LifeContextPaths

MAX_ARTIFACT_BYTES = 16 * 1024 * 1024


class DerivedError(ValueError):
    """Redacted failure accessing derived evidence."""


class DerivedConflict(DerivedError):
    """An immutable extraction identity already has different content."""


@dataclass(frozen=True)
class StoredArtifact:
    path: Path
    sha256: str


def _identity(source_id: str, source_version: int, extraction_revision: int) -> None:
    try:
        _validate_identity(source_id, source_version)
        _validate_identity(source_id, extraction_revision)
    except CustodyError:
        raise DerivedError("invalid derived identity") from None


def _json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise DerivedError("invalid derived payload")
        return {key: _json_value(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_json_value(child) for child in value]
    if value is None or type(value) in (str, int, float, bool):
        return value
    raise DerivedError("invalid derived payload")


def _canonical(payload: Mapping[str, Any]) -> bytes:
    if not isinstance(payload, Mapping):
        raise DerivedError("invalid derived payload")
    try:
        raw = json.dumps(
            _json_value(payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise DerivedError("invalid derived payload") from None
    if len(raw) > MAX_ARTIFACT_BYTES:
        raise DerivedError("derived artifact size limit exceeded")
    return raw


class DerivedStore:
    """Immutable JSON artifacts keyed by source, original version and extraction revision.

    Checksums cover canonical payload bytes. The envelope records that checksum and
    identity; callers may additionally pin the checksum from their catalog on read.
    """

    def __init__(self, paths: LifeContextPaths) -> None:
        self.paths = paths

    def artifact_path(
        self, source_id: str, source_version: int, extraction_revision: int
    ) -> Path:
        _identity(source_id, source_version, extraction_revision)
        return (
            self.paths.lifeweave_home
            / "derived"
            / source_id
            / f"v{source_version}"
            / f"r{extraction_revision}.json"
        )

    @contextmanager
    def _directories(
        self, names: tuple[str, ...], *, create: bool
    ) -> Iterator[list[int]]:
        descriptors: list[int] = []
        links: list[tuple[int, str, int]] = []
        try:
            home = self.paths.lifeweave_home
            if not home.is_absolute() or ".." in home.parts:
                raise DerivedError("unsafe derived path")
            current = os.open(home.anchor, _dir_flags())
            descriptors.append(current)
            # Pin every ancestor, not just the final home component.
            for name in home.parts[1:-1]:
                child = os.open(name, _dir_flags(), dir_fd=current)
                descriptors.append(child)
                links.append((current, name, child))
                current = child
            for name in (home.name, *names):
                child = _open_child_dir(current, name, create=create)
                descriptors.append(child)
                links.append((current, name, child))
                if create:
                    os.fsync(current)
                current = child
            self._check_links(links)
            yield descriptors
            self._check_links(links)
        except (OSError, CustodyError):
            raise DerivedError("unsafe derived storage") from None
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    @staticmethod
    def _check_links(links: list[tuple[int, str, int]]) -> None:
        for parent, name, child in links:
            linked = os.stat(name, dir_fd=parent, follow_symlinks=False)
            opened = os.fstat(child)
            if (linked.st_dev, linked.st_ino) != (opened.st_dev, opened.st_ino):
                raise DerivedError("unsafe derived path")

    def _read(
        self,
        directory: int,
        source_id: str,
        version: int,
        revision: int,
        expected_sha256: str | None = None,
    ) -> tuple[dict[str, Any], str]:
        raw = _read_private_file(directory, f"r{revision}.json", MAX_ARTIFACT_BYTES)
        try:
            value = json.loads(raw)
            if not isinstance(value, dict) or set(value) != {
                "schema_version",
                "source_id",
                "source_version",
                "extraction_revision",
                "sha256",
                "payload",
            }:
                raise DerivedError("invalid derived artifact")
            if (
                type(value["schema_version"]) is not int
                or value["schema_version"] != 1
                or value["source_id"] != source_id
                or type(value["source_version"]) is not int
                or value["source_version"] != version
                or type(value["extraction_revision"]) is not int
                or value["extraction_revision"] != revision
            ):
                raise DerivedError("derived identity mismatch")
            canonical = _canonical(value["payload"])
            checksum = hashlib.sha256(canonical).hexdigest()
            if value["sha256"] != checksum or (
                expected_sha256 is not None and checksum != expected_sha256
            ):
                raise DerivedError("derived checksum mismatch")
            if raw != _canonical(value):
                raise DerivedError("noncanonical derived artifact")
            return value["payload"], checksum
        except (ValueError, TypeError, UnicodeError, RecursionError) as error:
            if isinstance(error, DerivedError):
                raise
            raise DerivedError("invalid derived artifact") from None

    def write(
        self,
        source_id: str,
        source_version: int,
        extraction_revision: int,
        payload: Mapping[str, Any],
    ) -> StoredArtifact:
        path = self.artifact_path(source_id, source_version, extraction_revision)
        canonical = _canonical(payload)
        checksum = hashlib.sha256(canonical).hexdigest()
        raw = _canonical(
            {
                "schema_version": 1,
                "source_id": source_id,
                "source_version": source_version,
                "extraction_revision": extraction_revision,
                "sha256": checksum,
                "payload": json.loads(canonical),
            }
        )
        with self._directories(
            ("derived", source_id, f"v{source_version}"), create=True
        ) as descriptors:
            directory = descriptors[-1]
            name = path.name
            try:
                os.stat(name, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                _, existing = self._read(
                    directory, source_id, source_version, extraction_revision
                )
                if existing != checksum:
                    raise DerivedConflict("derived identity already exists")
                os.fsync(directory)
                return StoredArtifact(path, checksum)
            temporary = f".staging-{secrets.token_hex(16)}"
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
            descriptor = os.open(temporary, flags, 0o600, dir_fd=directory)
            staged = os.fstat(descriptor)
            try:
                os.fchmod(descriptor, 0o600)
                offset = 0
                while offset < len(raw):
                    written = os.write(descriptor, raw[offset:])
                    if written <= 0:
                        raise DerivedError("derived write failed")
                    offset += written
                os.fsync(descriptor)
                current = os.stat(temporary, dir_fd=directory, follow_symlinks=False)
                if (current.st_dev, current.st_ino) != (staged.st_dev, staged.st_ino):
                    raise DerivedError("unsafe derived staging file")
                # Hard-link publication is atomic and never replaces an occupied name.
                os.link(
                    temporary,
                    name,
                    src_dir_fd=directory,
                    dst_dir_fd=directory,
                    follow_symlinks=False,
                )
                published_info = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if (published_info.st_dev, published_info.st_ino) != (
                    staged.st_dev,
                    staged.st_ino,
                ):
                    raise DerivedError("unsafe derived publication")
                _, published = self._read(
                    directory, source_id, source_version, extraction_revision
                )
                if published != checksum:
                    raise DerivedError("derived publication mismatch")
            except FileExistsError:
                _, existing = self._read(
                    directory, source_id, source_version, extraction_revision
                )
                if existing != checksum:
                    raise DerivedConflict("derived identity already exists") from None
            finally:
                try:
                    # Keep the inode pinned through cleanup to prevent inode reuse.
                    # Never unlink a substituted staging entry.
                    current = os.stat(
                        temporary, dir_fd=directory, follow_symlinks=False
                    )
                    if (current.st_dev, current.st_ino) != (
                        staged.st_dev,
                        staged.st_ino,
                    ):
                        raise DerivedError("unsafe derived staging file")
                    os.unlink(temporary, dir_fd=directory)
                    os.fsync(directory)
                finally:
                    os.close(descriptor)
        return StoredArtifact(path, checksum)

    def read(
        self,
        source_id: str,
        source_version: int,
        extraction_revision: int,
        *,
        expected_sha256: str | None = None,
    ) -> dict[str, Any]:
        _identity(source_id, source_version, extraction_revision)
        with self._directories(
            ("derived", source_id, f"v{source_version}"), create=False
        ) as descriptors:
            payload, _ = self._read(
                descriptors[-1],
                source_id,
                source_version,
                extraction_revision,
                expected_sha256,
            )
            return payload

    def list_inventory(self, *, limit: int = 4096) -> list[tuple[str, int, int]]:
        """Return verified extraction identities; unexpected entries fail closed."""
        if type(limit) is not int or not 1 <= limit <= 4096:
            raise DerivedError("invalid derived inventory limit")
        inventory: list[tuple[str, int, int]] = []
        with self._directories((), create=True) as descriptors:
            home = descriptors[-1]
            try:
                os.stat("derived", dir_fd=home, follow_symlinks=False)
            except FileNotFoundError:
                return []
            derived = _open_child_dir(home, "derived", create=False)
            try:
                sources = sorted(os.listdir(derived))
                if len(sources) > limit:
                    raise DerivedError("derived inventory limit exceeded")
                for source in sources:
                    _identity(source, 1, 1)
                    source_fd = _open_child_dir(derived, source, create=False)
                    try:
                        versions = sorted(os.listdir(source_fd))
                        if len(versions) > limit:
                            raise DerivedError("derived inventory limit exceeded")
                        for version_name in versions:
                            match = re.fullmatch(r"v([1-9][0-9]{0,9})", version_name)
                            if match is None:
                                raise DerivedError("unexpected derived entry")
                            version = int(match[1])
                            _identity(source, version, 1)
                            version_fd = _open_child_dir(
                                source_fd, version_name, create=False
                            )
                            try:
                                revisions = sorted(os.listdir(version_fd))
                                if len(inventory) + len(revisions) > limit:
                                    raise DerivedError(
                                        "derived inventory limit exceeded"
                                    )
                                for filename in revisions:
                                    match = re.fullmatch(
                                        r"r([1-9][0-9]{0,9})\.json", filename
                                    )
                                    if match is None:
                                        raise DerivedError("unexpected derived entry")
                                    revision = int(match[1])
                                    _identity(source, version, revision)
                                    self._read(version_fd, source, version, revision)
                                    inventory.append((source, version, revision))
                                self._check_links(
                                    [(source_fd, version_name, version_fd)]
                                )
                            finally:
                                os.close(version_fd)
                        self._check_links([(derived, source, source_fd)])
                    finally:
                        os.close(source_fd)
                self._check_links([(home, "derived", derived)])
            finally:
                os.close(derived)
        return inventory
