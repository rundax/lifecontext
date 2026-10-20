"""Descriptor-pinned, disposable Journal projection paths and writer lock."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import secrets
import stat
import time
import threading
from contextlib import AbstractContextManager, contextmanager, nullcontext
from pathlib import Path
from typing import TypeAlias


PathLike: TypeAlias = str | os.PathLike[str]

LOCK_FILENAME = ".journal.lock"
_WRITABLE_DIRECTORIES = frozenset(
    {"entries", "relationships", "indices", "reports", "wiki", "documents"}
)
_READ_ONLY_DIRECTORIES = frozenset({"expectations"})
_READ_ONLY_PATHS = frozenset({("wiki", "SCHEMA.md")})
_MANAGED_DIRECTORIES = (
    "entries",
    "relationships",
    "indices",
    "reports",
    "expectations",
    "wiki",
    "documents",
)
_FILE_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
_MAX_RELATIONSHIP_LEDGER_BYTES = 64 * 1024
_PROJECTION_NAME = re.compile(r"([A-Za-z0-9][A-Za-z0-9_-]{0,127})-v([1-9][0-9]*)\.md\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class VaultError(ValueError):
    """Stable managed-vault failure without exposing filesystem contents."""

    def __init__(self, code: str, *, retryable: bool = False):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


def default_journal_paths(home: PathLike) -> tuple[Path, Path]:
    """Return ``~/LifeContext/vault`` and its ``Journal`` without inspecting the filesystem."""
    root = Path(home) / "LifeContext" / "vault"
    return root, root / "Journal"


def _raw_absolute(value: PathLike) -> tuple[str, tuple[str, ...]]:
    try:
        raw = os.fspath(value)
    except TypeError as exc:
        raise VaultError("unsafe_path") from exc
    if not isinstance(raw, str) or not raw or "\x00" in raw or "\\" in raw:
        raise VaultError("unsafe_path")
    if not raw.startswith("/"):
        raise VaultError("unsafe_path")
    components = raw.split("/")[1:]
    if not components or any(part in ("", ".", "..") for part in components):
        raise VaultError("unsafe_path")
    return raw, tuple(components)


def _raw_relative(value: PathLike) -> tuple[str, ...]:
    try:
        raw = os.fspath(value)
    except TypeError as exc:
        raise VaultError("unsafe_path") from exc
    if not isinstance(raw, str) or not raw or "\x00" in raw or "\\" in raw:
        raise VaultError("unsafe_path")
    if raw.startswith("/"):
        raise VaultError("unsafe_path")
    components = raw.split("/")
    if any(part in ("", ".", "..") for part in components):
        raise VaultError("unsafe_path")
    return tuple(components)


def _directory_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )


def _current_uid() -> int | None:
    getter = getattr(os, "getuid", None)
    return getter() if getter is not None else None


def _check_owner_and_mode(info: os.stat_result, *, directory: bool) -> None:
    if os.name != "posix":
        return
    owner = _current_uid()
    expected_mode = 0o700 if directory else 0o600
    if (owner is not None and info.st_uid != owner) or stat.S_IMODE(info.st_mode) != expected_mode:
        raise VaultError("unsafe_permissions")


def _check_directory_fd(descriptor: int) -> os.stat_result:
    try:
        info = os.fstat(descriptor)
    except OSError as exc:
        raise VaultError("unsafe_path") from exc
    if not stat.S_ISDIR(info.st_mode):
        raise VaultError("unsafe_path")
    _check_owner_and_mode(info, directory=True)
    return info


def _check_file_fd(descriptor: int) -> os.stat_result:
    try:
        info = os.fstat(descriptor)
    except OSError as exc:
        raise VaultError("unsafe_path") from exc
    if not stat.S_ISREG(info.st_mode):
        raise VaultError("unsafe_path")
    _check_owner_and_mode(info, directory=False)
    return info


def _read_fd(descriptor: int) -> bytes:
    chunks: list[bytes] = []
    while True:
        try:
            chunk = os.read(descriptor, 1024 * 1024)
        except OSError as exc:
            raise VaultError("unsafe_path") from exc
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _marker_owned(content: str) -> bool:
    if not isinstance(content, str):
        return False
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return False
    try:
        end = next(index for index, line in enumerate(lines[1:], 1) if line.strip() == "---")
    except StopIteration:
        return False
    fields: dict[str, str] = {}
    for line in lines[1:end]:
        key, separator, value = line.partition(":")
        if separator:
            fields[key.strip()] = value.strip().strip("'\"")
    return (
        fields.get("ownership_marker") in {"hermes-journal", "lifeweave-journal"}
        or fields.get("source_id", "").startswith("jrnl_src_")
        or (
            fields.get("schema_version") == "1"
            and fields.get("edge_id", "").startswith("edge_")
        )
    )


def _read_target(parent_fd: int, name: str) -> tuple[int, os.stat_result, bytes]:
    try:
        descriptor = os.open(name, _FILE_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError as exc:
        raise VaultError("not_found") from exc
    except OSError as exc:
        code = "unsafe_permissions" if exc.errno in (errno.EACCES, errno.EPERM) else "unsafe_path"
        raise VaultError(code) from exc
    try:
        info = _check_file_fd(descriptor)
        return descriptor, info, _read_fd(descriptor)
    except Exception:
        os.close(descriptor)
        raise


def _open_parent(journal_fd: int, components: tuple[str, ...]) -> int:
    descriptor = os.dup(journal_fd)
    try:
        _check_directory_fd(descriptor)
        for component in components:
            child = os.open(component, _directory_flags(), dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            _check_directory_fd(descriptor)
        return descriptor
    except VaultError:
        os.close(descriptor)
        raise
    except OSError as exc:
        os.close(descriptor)
        code = "unsafe_permissions" if exc.errno in (errno.EACCES, errno.EPERM) else "unsafe_path"
        raise VaultError(code) from exc


def _validate_write_path(value: PathLike) -> tuple[tuple[str, ...], str]:
    parts = _raw_relative(value)
    if parts == ("README.md",):
        return (), parts[0]
    first = parts[0]
    if first not in _WRITABLE_DIRECTORIES and first not in _READ_ONLY_DIRECTORIES:
        raise VaultError("unsafe_path")
    if first in _READ_ONLY_DIRECTORIES or parts[:2] in _READ_ONLY_PATHS:
        raise VaultError("read_only_path")
    return parts[:-1], parts[-1]


def _check_managed_tree(directory_fd: int) -> None:
    try:
        names = os.listdir(directory_fd)
    except OSError as exc:
        raise VaultError("unsafe_path") from exc
    for name in names:
        try:
            child_fd = os.open(name, _FILE_FLAGS, dir_fd=directory_fd)
        except OSError as exc:
            code = "unsafe_permissions" if exc.errno in (errno.EACCES, errno.EPERM) else "unsafe_path"
            raise VaultError(code) from exc
        try:
            info = os.fstat(child_fd)
            if stat.S_ISDIR(info.st_mode):
                _check_owner_and_mode(info, directory=True)
                _check_managed_tree(child_fd)
            elif stat.S_ISREG(info.st_mode):
                if ".sync-conflict-" not in name:
                    _check_owner_and_mode(info, directory=False)
            else:
                raise VaultError("unsafe_path")
        finally:
            os.close(child_fd)


def _open_file_at(directory_fd: int, name: str) -> int:
    try:
        return os.open(name, _FILE_FLAGS, dir_fd=directory_fd)
    except FileNotFoundError:
        raise
    except OSError as exc:
        code = "unsafe_permissions" if exc.errno in (errno.EACCES, errno.EPERM) else "unsafe_path"
        raise VaultError(code) from exc


def _open_absolute_parts(components: tuple[str, ...]) -> int:
    descriptor = -1
    try:
        descriptor = os.open("/", _directory_flags())
        for component in components:
            child = os.open(component, _directory_flags(), dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except OSError as exc:
        if descriptor >= 0:
            os.close(descriptor)
        code = "unsafe_permissions" if exc.errno in (errno.EACCES, errno.EPERM) else "unsafe_path"
        raise VaultError(code) from exc


class VaultWriterLock:
    """Bounded advisory lock stored beneath a descriptor-pinned vault root."""

    def __init__(self, root_fd: int, timeout: float = 10.0):
        self._parent_fd = os.dup(root_fd)
        self.timeout = max(0.0, float(timeout))
        self._handle = -1

    def __enter__(self) -> "VaultWriterLock":
        try:
            _check_directory_fd(self._parent_fd)
            try:
                existing = os.stat(
                    LOCK_FILENAME, dir_fd=self._parent_fd, follow_symlinks=False
                )
            except FileNotFoundError:
                existed = False
            else:
                existed = True
                if not stat.S_ISREG(existing.st_mode):
                    raise VaultError("unsafe_path")

            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
            flags |= getattr(os, "O_CLOEXEC", 0)
            self._handle = os.open(
                LOCK_FILENAME, flags, 0o600, dir_fd=self._parent_fd
            )
            info = os.fstat(self._handle)
            if not stat.S_ISREG(info.st_mode):
                raise VaultError("unsafe_path")
            if not existed and os.name == "posix":
                os.fchmod(self._handle, 0o600)
                info = os.fstat(self._handle)
            _check_owner_and_mode(info, directory=False)
        except VaultError:
            self._close_fds()
            raise
        except OSError as exc:
            self._close_fds()
            code = "unsafe_permissions" if exc.errno in (errno.EACCES, errno.EPERM) else "unsafe_path"
            raise VaultError(code) from exc

        try:
            import fcntl
        except ImportError:
            self._close_fds()
            raise VaultError("locking_unavailable") from None

        deadline = time.monotonic() + self.timeout
        while True:
            try:
                fcntl.flock(self._handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    self._close_fds()
                    raise VaultError("lock_timeout", retryable=True) from None
                time.sleep(min(0.05, max(0.001, deadline - time.monotonic())))
            except OSError as exc:
                if exc.errno in (errno.EAGAIN, errno.EACCES):
                    if time.monotonic() >= deadline:
                        self._close_fds()
                        raise VaultError("lock_timeout", retryable=True) from None
                    time.sleep(min(0.05, max(0.001, deadline - time.monotonic())))
                    continue
                self._close_fds()
                raise VaultError("unsafe_path") from exc

    def _close_fds(self) -> None:
        if self._handle >= 0:
            try:
                os.close(self._handle)
            except OSError:
                pass
            self._handle = -1
        if self._parent_fd >= 0:
            try:
                os.close(self._parent_fd)
            except OSError:
                pass
            self._parent_fd = -1

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self._handle >= 0:
            try:
                import fcntl

                fcntl.flock(self._handle, fcntl.LOCK_UN)
            except (ImportError, OSError):
                pass
        self._close_fds()
        return False


class ManagedVault:
    """Pinned authority for writes below one disposable Journal directory."""

    def __init__(self, vault_root: PathLike, journal_path: PathLike):
        raw_root, root_parts = _raw_absolute(vault_root)
        raw_journal, journal_parts = _raw_absolute(journal_path)
        if len(journal_parts) <= len(root_parts) or journal_parts[: len(root_parts)] != root_parts:
            raise VaultError("unsafe_path")

        root_path = Path(raw_root)
        journal = Path(raw_journal)
        self.vault_root = root_path
        self.journal_path = journal
        self.lock_path = root_path / LOCK_FILENAME
        self._root_fd = -1
        self._journal_fd = -1
        self._closed = False
        self._transaction_lock = threading.RLock()
        self._transaction_state = threading.local()
        try:
            self._root_fd = _open_absolute_parts(root_parts)
            _check_directory_fd(self._root_fd)
            current_fd = os.dup(self._root_fd)
            try:
                for component in journal_parts[len(root_parts) :]:
                    child_fd = os.open(component, _directory_flags(), dir_fd=current_fd)
                    os.close(current_fd)
                    current_fd = child_fd
                _check_directory_fd(current_fd)
                self._journal_fd = current_fd
            except Exception:
                os.close(current_fd)
                raise
        except VaultError:
            self.close()
            raise
        except OSError as exc:
            self.close()
            code = "unsafe_permissions" if exc.errno in (errno.EACCES, errno.EPERM) else "unsafe_path"
            raise VaultError(code) from exc

    def __enter__(self) -> "ManagedVault":
        self._ensure_open()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.close()
        return False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for name in ("_journal_fd", "_root_fd"):
            descriptor = getattr(self, name, -1)
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                setattr(self, name, -1)

    def _ensure_open(self) -> None:
        if self._closed or self._root_fd < 0 or self._journal_fd < 0:
            raise VaultError("unsafe_path")

    def verify_permissions(self) -> bool:
        self._ensure_open()
        _check_directory_fd(self._root_fd)
        _check_directory_fd(self._journal_fd)
        for name in _MANAGED_DIRECTORIES:
            try:
                directory_fd = os.open(name, _directory_flags(), dir_fd=self._journal_fd)
            except FileNotFoundError:
                continue
            except OSError as exc:
                code = "unsafe_permissions" if exc.errno in (errno.EACCES, errno.EPERM) else "unsafe_path"
                raise VaultError(code) from exc
            try:
                _check_directory_fd(directory_fd)
                _check_managed_tree(directory_fd)
            finally:
                os.close(directory_fd)
        try:
            readme_fd = _open_file_at(self._journal_fd, "README.md")
        except FileNotFoundError:
            pass
        else:
            try:
                _check_file_fd(readme_fd)
            finally:
                os.close(readme_fd)
        return True

    def writer_lock(self, timeout: float = 10.0) -> AbstractContextManager:
        self._ensure_open()
        if getattr(self._transaction_state, "active", False):
            return nullcontext()
        return VaultWriterLock(self._root_fd, timeout=timeout)

    @contextmanager
    def writer_transaction(self):
        """Hold one process lock across catalog reservations and managed writes."""
        with self._transaction_lock:
            if getattr(self._transaction_state, "active", False):
                yield
                return
            with self.writer_lock():
                self._transaction_state.active = True
                try:
                    yield
                finally:
                    self._transaction_state.active = False

    def list_vault_directory(
        self, relative_path: PathLike, *, limit: int = 10_000
    ) -> tuple[list[tuple[str, str]], bool]:
        """List regular files/directories without following vault symlinks."""
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise VaultError("invalid_request")
        components = _raw_relative(relative_path)
        descriptor = os.dup(self._root_fd)
        try:
            for component in components:
                child = os.open(component, _directory_flags(), dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
                if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
                    raise VaultError("unsafe_path")
            found: list[tuple[str, str]] = []
            truncated = False
            with os.scandir(descriptor) as entries:
                for entry in entries:
                    if len(found) >= limit:
                        truncated = True
                        break
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if stat.S_ISDIR(info.st_mode):
                        kind = "directory"
                    elif stat.S_ISREG(info.st_mode):
                        kind = "file"
                    else:
                        continue
                    found.append((entry.name, kind))
            found.sort()
            return found, truncated
        except FileNotFoundError as exc:
            raise VaultError("not_found") from exc
        except VaultError:
            raise
        except OSError as exc:
            code = "unsafe_permissions" if exc.errno in (errno.EACCES, errno.EPERM) else "unsafe_path"
            raise VaultError(code) from exc
        finally:
            os.close(descriptor)

    def read_vault_file(self, relative_path: PathLike, *, max_bytes: int) -> bytes:
        """Read one regular vault file through no-follow directory descriptors."""
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
            raise VaultError("invalid_request")
        components = _raw_relative(relative_path)
        parent_fd = os.dup(self._root_fd)
        descriptor = -1
        try:
            for component in components[:-1]:
                child = os.open(component, _directory_flags(), dir_fd=parent_fd)
                os.close(parent_fd)
                parent_fd = child
                if not stat.S_ISDIR(os.fstat(parent_fd).st_mode):
                    raise VaultError("unsafe_path")
            try:
                descriptor = os.open(components[-1], _FILE_FLAGS, dir_fd=parent_fd)
            except FileNotFoundError as exc:
                raise VaultError("not_found") from exc
            except OSError as exc:
                code = "unsafe_permissions" if exc.errno in (errno.EACCES, errno.EPERM) else "unsafe_path"
                raise VaultError(code) from exc
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise VaultError("unsafe_path")
            if info.st_size > max_bytes:
                raise VaultError("file_too_large")
            chunks: list[bytes] = []
            total = 0
            while total <= max_bytes:
                try:
                    chunk = os.read(descriptor, min(64 * 1024, max_bytes + 1 - total))
                except OSError as exc:
                    raise VaultError("unsafe_path") from exc
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
            if total > max_bytes:
                raise VaultError("file_too_large")
            return b"".join(chunks)
        except VaultError:
            raise
        except FileNotFoundError as exc:
            raise VaultError("not_found") from exc
        except OSError as exc:
            code = "unsafe_permissions" if exc.errno in (errno.EACCES, errno.EPERM) else "unsafe_path"
            raise VaultError(code) from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            os.close(parent_fd)

    @staticmethod
    def _relationship_frontmatter(content: str) -> dict[str, str] | None:
        lines = content.splitlines()
        if not lines or lines[0].strip() != "---":
            return None
        try:
            end = next(i for i, line in enumerate(lines[1:], 1) if line.strip() == "---")
        except StopIteration:
            return None
        fields: dict[str, str] = {}
        for line in lines[1:end]:
            key, separator, value = line.partition(":")
            if not separator or key.strip() in fields:
                return None
            fields[key.strip()] = value.strip().strip("'\"")
        return fields

    def safe_write_relationship_text(
        self,
        edge_id: str,
        content: str,
        *,
        expected_hash: str | None = None,
        expected_revision: int | None = None,
    ) -> bool:
        """CAS-write one marker-owned mutable relationship ledger."""
        if not isinstance(edge_id, str) or not re.fullmatch(r"edge_[0-9a-f]{64}", edge_id):
            raise VaultError("invalid_request")
        if not isinstance(content, str):
            raise TypeError("content must be text")
        new_fields = self._relationship_frontmatter(content)
        if (
            new_fields is None
            or new_fields.get("ownership_marker") != "lifeweave-relationship"
            or new_fields.get("schema_version") != "1"
            or new_fields.get("edge_id") != edge_id
            or new_fields.get("status") not in {"proposed", "approved", "rejected"}
            or new_fields.get("relation") not in {
                "references", "about", "same_topic", "follows", "supports", "contradicts", "supersedes"
            }
        ):
            raise VaultError("unmanaged_file")
        try:
            new_revision = int(new_fields.get("revision", ""))
            content_bytes = content.encode("utf-8")
        except (ValueError, UnicodeError):
            raise VaultError("invalid_file") from None
        if new_revision < 1 or str(new_revision) != new_fields.get("revision"):
            raise VaultError("invalid_file")
        if len(content_bytes) > _MAX_RELATIONSHIP_LEDGER_BYTES:
            raise VaultError("file_too_large")
        identity = new_fields.get("identity", "")
        try:
            expected_identity = hashlib.sha256(
                json.dumps(
                    [new_fields["from"], new_fields["to"], new_fields["relation"]],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
        except (KeyError, UnicodeError):
            raise VaultError("invalid_file") from None
        if identity != "sha256:" + expected_identity or edge_id != "edge_" + expected_identity:
            raise VaultError("invalid_file")
        if expected_revision is None:
            if expected_hash is not None or new_revision != 1:
                raise VaultError("conflict")
        elif (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 1
            or not isinstance(expected_hash, str)
            or not re.fullmatch(r"[0-9a-f]{64}", expected_hash)
            or new_revision != expected_revision + 1
        ):
            raise VaultError("conflict")

        name = edge_id + ".md"
        content_hash = hashlib.sha256(content_bytes).hexdigest()
        self.verify_permissions()
        with self.writer_lock():
            self.verify_permissions()
            try:
                os.mkdir("relationships", 0o700, dir_fd=self._journal_fd)
            except FileExistsError:
                pass
            except OSError as exc:
                code = "unsafe_permissions" if exc.errno in (errno.EACCES, errno.EPERM) else "unsafe_path"
                raise VaultError(code) from exc
            parent_fd = _open_parent(self._journal_fd, ("relationships",))
            temp_name = f".{name}.tmp.{secrets.token_hex(10)}"
            original_snapshot: tuple[int, int, int, int] | None = None
            try:
                try:
                    conflicts = os.listdir(parent_fd)
                except OSError as exc:
                    raise VaultError("unsafe_path") from exc
                if any(candidate.startswith(name + ".sync-conflict-") for candidate in conflicts):
                    raise VaultError("sync_conflict")

                try:
                    target_fd, target_info, target_data = _read_target(parent_fd, name)
                except VaultError as exc:
                    if exc.code != "not_found":
                        raise
                    if expected_revision is not None:
                        raise VaultError("conflict") from None
                else:
                    try:
                        old_text = target_data.decode("utf-8")
                        old_fields = self._relationship_frontmatter(old_text)
                        if (
                            old_fields is None
                            or old_fields.get("ownership_marker") != "lifeweave-relationship"
                            or old_fields.get("schema_version") != "1"
                            or old_fields.get("edge_id") != edge_id
                        ):
                            raise VaultError("unmanaged_file")
                        old_revision = old_fields.get("revision", "")
                        original_hash = hashlib.sha256(target_data).hexdigest()
                        if (
                            expected_revision is None
                            or old_revision != str(expected_revision)
                            or original_hash != expected_hash
                        ):
                            raise VaultError("conflict")
                        original_snapshot = (
                            target_info.st_dev,
                            target_info.st_ino,
                            target_info.st_mtime_ns,
                            target_info.st_size,
                        )
                    except UnicodeDecodeError:
                        raise VaultError("unmanaged_file") from None
                    finally:
                        os.close(target_fd)

                try:
                    temp_fd = os.open(
                        temp_name,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL
                        | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
                        0o600,
                        dir_fd=parent_fd,
                    )
                    try:
                        if os.name == "posix":
                            os.fchmod(temp_fd, 0o600)
                        offset = 0
                        while offset < len(content_bytes):
                            written = os.write(temp_fd, content_bytes[offset:])
                            if written <= 0:
                                raise OSError("short write")
                            offset += written
                        os.fsync(temp_fd)
                    finally:
                        os.close(temp_fd)
                except OSError as exc:
                    raise VaultError("export_durability") from exc

                if original_snapshot is None:
                    try:
                        os.link(temp_name, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd,
                                follow_symlinks=False)
                    except FileExistsError as exc:
                        raise VaultError("conflict") from exc
                    except OSError as exc:
                        raise VaultError("conflict") from exc
                else:
                    check_fd, check_info, check_data = _read_target(parent_fd, name)
                    try:
                        if (
                            (check_info.st_dev, check_info.st_ino, check_info.st_mtime_ns, check_info.st_size)
                            != original_snapshot
                            or hashlib.sha256(check_data).hexdigest() != expected_hash
                        ):
                            raise VaultError("conflict")
                    finally:
                        os.close(check_fd)
                    try:
                        os.replace(temp_name, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
                    except OSError as exc:
                        raise VaultError("conflict") from exc

                try:
                    os.fsync(parent_fd)
                except OSError as exc:
                    raise VaultError("export_durability") from exc
                result_fd, _result_info, result_data = _read_target(parent_fd, name)
                try:
                    if hashlib.sha256(result_data).hexdigest() != content_hash:
                        raise VaultError("conflict")
                finally:
                    os.close(result_fd)
                return True
            finally:
                try:
                    os.unlink(temp_name, dir_fd=parent_fd)
                except OSError:
                    pass
                os.close(parent_fd)

    def _artifact_parent(self, relative_path: PathLike, *, create: bool) -> tuple[int, str]:
        components, name = _validate_write_path(relative_path)
        if not components or components[0] not in {"documents", "wiki", "indices"}:
            raise VaultError("unsafe_path")
        if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", part)
               for part in components[1:]):
            raise VaultError("unsafe_path")
        if components[0] == "wiki":
            import unicodedata

            if (not name.endswith(".md") or name.startswith(".")
                    or name.endswith((" ", "."))
                    or any(character in '/\\:*?"<>|#[]^'
                           or unicodedata.category(character) in {"Cc", "Cf", "Cs"}
                           for character in name)
                    or len(name.encode("utf-8")) > 240
                    or name.split(".", 1)[0].rstrip(" ").upper() in {
                        "CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$",
                        *(f"{prefix}{digit}" for prefix in ("COM", "LPT")
                          for digit in "123456789¹²³"),
                    }):
                raise VaultError("unsafe_path")
        elif not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_. -]{0,239}", name):
            raise VaultError("unsafe_path")
        descriptor = os.dup(self._journal_fd)
        try:
            _check_directory_fd(descriptor)
            for component in components:
                if create:
                    try:
                        os.mkdir(component, 0o700, dir_fd=descriptor)
                    except FileExistsError:
                        pass
                child = _open_parent(descriptor, (component,))
                os.close(descriptor)
                descriptor = child
            return descriptor, name
        except Exception:
            os.close(descriptor)
            raise

    def read_artifact(self, relative_path: PathLike) -> bytes:
        """Read catalog-addressed projection bytes, without Markdown ownership rules."""
        return self.read_artifact_identity(relative_path)[0]

    def read_artifact_identity(self, relative_path: PathLike, *, staged_name=None):
        self._ensure_open()
        try:
            parent, name = self._artifact_parent(relative_path, create=False)
        except (FileNotFoundError, VaultError) as exc:
            # Missing parent is distinguished from unsafe/symlink parents below.
            if isinstance(exc, FileNotFoundError) or (isinstance(exc, VaultError)
                    and isinstance(exc.__cause__, FileNotFoundError)):
                raise VaultError("not_found") from exc
            raise
        try:
            if staged_name is not None:
                if not re.fullmatch(r"\.publication\.[0-9a-f]{20}\.tmp(?:\.hold)?", staged_name):
                    raise VaultError("unsafe_path")
                name = staged_name
            descriptor, info, data = _read_target(parent, name)
            os.close(descriptor)
            return data, (info.st_dev, info.st_ino)
        finally:
            os.close(parent)

    def safe_write_bytes(self, relative_path: PathLike, content: bytes, *,
                         expected_hash: str | None, artifact_id: str,
                         max_bytes: int = 64 * 1024 * 1024,
                         before_publish=None, before_stage=None) -> bool:
        """Catalog-owned binary/text CAS. None means create-only, never adopt a file."""
        if (not isinstance(content, bytes) or len(content) > max_bytes
                or not isinstance(artifact_id, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", artifact_id)
                or (expected_hash is not None and not _SHA256.fullmatch(expected_hash))):
            raise VaultError("invalid_request")
        self.verify_permissions()
        with self.writer_lock():
            parent, name = self._artifact_parent(relative_path, create=True)
            temporary = f".publication.{secrets.token_hex(10)}.tmp"
            held = temporary + ".hold"
            publication_pending = False
            staged_created = False
            held_created = False
            try:
                if any(n.startswith(name + ".sync-conflict-") for n in os.listdir(parent)):
                    raise VaultError("sync_conflict")
                snapshot = None
                try:
                    descriptor, info, old = _read_target(parent, name)
                except VaultError as exc:
                    if exc.code != "not_found":
                        raise
                    if expected_hash is not None:
                        raise VaultError("conflict") from None
                else:
                    os.close(descriptor)
                    if expected_hash is None or hashlib.sha256(old).hexdigest() != expected_hash:
                        raise VaultError("conflict")
                    if old == content:
                        return True
                    snapshot = (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size)
                if before_stage is not None:
                    before_stage(held)
                descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                     | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=parent)
                staged_created = True
                created_info = os.fstat(descriptor)
                try:
                    os.fchmod(descriptor, 0o600)
                    offset = 0
                    while offset < len(content):
                        written = os.write(descriptor, content[offset:])
                        if written <= 0:
                            raise VaultError("export_durability")
                        offset += written
                    os.fsync(descriptor)
                    staged_info = os.fstat(descriptor)
                finally:
                    os.close(descriptor)

                def record_publication():
                    nonlocal publication_pending, held_created
                    if before_publish is None:
                        return None
                    os.link(temporary, held, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
                    held_created = True
                    os.fsync(parent)
                    publication_pending = True
                    return before_publish(staged_info.st_dev, staged_info.st_ino, held)

                if snapshot is None:
                    rollback = record_publication()
                    try:
                        os.link(temporary, name, src_dir_fd=parent, dst_dir_fd=parent,
                                follow_symlinks=False)
                    except FileExistsError as exc:
                        if rollback is not None:
                            rollback()
                            publication_pending = False
                        raise VaultError("conflict") from exc
                else:
                    descriptor, info, old = _read_target(parent, name)
                    os.close(descriptor)
                    if ((info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size) != snapshot
                            or hashlib.sha256(old).hexdigest() != expected_hash):
                        raise VaultError("conflict")
                    record_publication()
                    os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
                os.fsync(parent)
                descriptor, actual_info, actual = _read_target(parent, name)
                os.close(descriptor)
                if actual != content or (actual_info.st_dev, actual_info.st_ino) != (staged_info.st_dev, staged_info.st_ino):
                    raise VaultError("conflict")
                return True
            finally:
                if not publication_pending:
                    for staged, created in ((temporary, staged_created), (held, held_created)):
                        if not created:
                            continue
                        try:
                            info = os.stat(staged, dir_fd=parent, follow_symlinks=False)
                            if (info.st_dev, info.st_ino) == (created_info.st_dev, created_info.st_ino):
                                os.unlink(staged, dir_fd=parent)
                        except FileNotFoundError:
                            pass
                os.close(parent)

    def safe_remove_artifact(self, relative_path: PathLike, *, expected_hash: str,
                             artifact_id: str, allow_missing: bool = False,
                             expected_identity=None, staged_name=None) -> bool:
        if (not isinstance(expected_hash, str) or not _SHA256.fullmatch(expected_hash)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", artifact_id)
                or not isinstance(allow_missing, bool)):
            raise VaultError("invalid_request")
        self.verify_permissions()
        with self.writer_lock():
            try:
                parent, name = self._artifact_parent(relative_path, create=False)
            except (FileNotFoundError, VaultError) as exc:
                if allow_missing and (isinstance(exc, FileNotFoundError)
                        or isinstance(exc.__cause__, FileNotFoundError)):
                    return True
                raise
            try:
                if staged_name is not None:
                    if not re.fullmatch(r"\.publication\.[0-9a-f]{20}\.tmp(?:\.hold)?", staged_name):
                        raise VaultError("unsafe_path")
                    name = staged_name
                if any(n.startswith(name + ".sync-conflict-") for n in os.listdir(parent)):
                    raise VaultError("sync_conflict")
                try:
                    descriptor, info, data = _read_target(parent, name)
                except VaultError as exc:
                    if allow_missing and exc.code == "not_found":
                        return True
                    raise
                os.close(descriptor)
                if (hashlib.sha256(data).hexdigest() != expected_hash
                        or expected_identity is not None and (info.st_dev, info.st_ino) != expected_identity):
                    raise VaultError("conflict")
                descriptor, check, data = _read_target(parent, name)
                os.close(descriptor)
                if ((info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size)
                        != (check.st_dev, check.st_ino, check.st_mtime_ns, check.st_size)
                        or hashlib.sha256(data).hexdigest() != expected_hash):
                    raise VaultError("conflict")
                try:
                    os.unlink(name, dir_fd=parent)
                    os.fsync(parent)
                except OSError as exc:
                    raise VaultError("vault_unavailable", retryable=True) from exc
                return True
            finally:
                os.close(parent)

    def safe_write_text(
        self,
        relative_path: PathLike,
        content: str,
        *,
        expected_hash: str | None = None,
        overwrite: bool = False,
    ) -> bool:
        """Write a versioned projection without replacing an existing version."""
        if not isinstance(content, str):
            raise TypeError("content must be text")
        components, name = _validate_write_path(relative_path)
        try:
            content_bytes = content.encode("utf-8")
        except UnicodeError as exc:
            raise VaultError("invalid_file") from exc
        content_hash = hashlib.sha256(content_bytes).hexdigest()

        self.verify_permissions()
        with self.writer_lock():
            self.verify_permissions()
            if components == ("entries",):
                # A new vault has no entries/ until its first Journal entry. The umask may
                # take bits from mkdir's mode, so the new folder is set to 0700 explicitly.
                try:
                    os.mkdir("entries", 0o700, dir_fd=self._journal_fd)
                    created = os.open("entries", _directory_flags(), dir_fd=self._journal_fd)
                    try:
                        os.fchmod(created, 0o700)
                    finally:
                        os.close(created)
                except FileExistsError:
                    pass
                except OSError as exc:
                    code = "unsafe_permissions" if exc.errno in (errno.EACCES, errno.EPERM) else "unsafe_path"
                    raise VaultError(code) from exc
            parent_fd = _open_parent(self._journal_fd, components)
            temp_name = f".{name}.tmp.{secrets.token_hex(10)}"
            original_hash: str | None = None
            try:
                try:
                    conflicts = os.listdir(parent_fd)
                except OSError as exc:
                    raise VaultError("unsafe_path") from exc
                if any(candidate.startswith(name + ".sync-conflict-") for candidate in conflicts):
                    raise VaultError("sync_conflict")

                try:
                    target_fd, _target_info, target_data = _read_target(parent_fd, name)
                except VaultError as exc:
                    if exc.code != "not_found":
                        raise
                else:
                    try:
                        if not overwrite:
                            raise VaultError("file_exists")
                        try:
                            old_text = target_data.decode("utf-8")
                        except UnicodeDecodeError as exc:
                            raise VaultError("unmanaged_file") from exc
                        if not _marker_owned(old_text):
                            raise VaultError("unmanaged_file")
                        original_hash = hashlib.sha256(target_data).hexdigest()
                        if expected_hash is not None and original_hash != expected_hash:
                            raise VaultError("conflict")
                        # Projection paths include an immutable source version.
                        # Replays can succeed when bytes already match, but a
                        # differing existing file may have been edited by an
                        # external vault writer. Without a filesystem CAS, never
                        # replace it after a hash check that such writers cannot
                        # share.
                        if target_data == content_bytes:
                            return True
                        raise VaultError("conflict")
                    finally:
                        os.close(target_fd)
                if original_hash is None and expected_hash is not None:
                    raise VaultError("conflict")

                try:
                    temp_fd = os.open(
                        temp_name,
                        os.O_WRONLY
                        | os.O_CREAT
                        | os.O_EXCL
                        | getattr(os, "O_NOFOLLOW", 0)
                        | getattr(os, "O_CLOEXEC", 0),
                        0o600,
                        dir_fd=parent_fd,
                    )
                    try:
                        if os.name == "posix":
                            os.fchmod(temp_fd, 0o600)
                        offset = 0
                        while offset < len(content_bytes):
                            written = os.write(temp_fd, content_bytes[offset:])
                            if written <= 0:
                                raise OSError("short write")
                            offset += written
                        os.fsync(temp_fd)
                    finally:
                        os.close(temp_fd)
                except OSError as exc:
                    raise VaultError("export_durability") from exc

                try:
                    os.link(
                        temp_name,
                        name,
                        src_dir_fd=parent_fd,
                        dst_dir_fd=parent_fd,
                        follow_symlinks=False,
                    )
                except FileExistsError as exc:
                    raise VaultError("conflict") from exc
                except OSError as exc:
                    raise VaultError("conflict") from exc

                try:
                    os.fsync(parent_fd)
                except OSError as exc:
                    raise VaultError("export_durability") from exc

                result_fd, _result_info, result_data = _read_target(parent_fd, name)
                try:
                    if hashlib.sha256(result_data).hexdigest() != content_hash:
                        raise VaultError("conflict")
                finally:
                    os.close(result_fd)
                return True
            finally:
                try:
                    os.unlink(temp_name, dir_fd=parent_fd)
                except OSError:
                    pass
                os.close(parent_fd)

    def safe_remove_projection(
        self,
        relative_path: PathLike,
        *,
        expected_hash: str,
        expected_source_hash: str,
        allow_missing: bool = False,
        expected_source_id: str | None = None,
        expected_source_version: int | None = None,
    ) -> bool:
        """Remove one unchanged LifeContext Journal projection through pinned paths."""
        components, name = _validate_write_path(relative_path)
        match = _PROJECTION_NAME.fullmatch(name)
        if (
            components != ("entries",)
            or not name.endswith(".md")
            or not isinstance(expected_hash, str)
            or not _SHA256.fullmatch(expected_hash)
            or not isinstance(expected_source_hash, str)
            or not _SHA256.fullmatch(expected_source_hash)
            or not isinstance(allow_missing, bool)
        ):
            raise VaultError("unsafe_path")

        if expected_source_id is None and expected_source_version is None and match is not None:
            source_id, source_version = match.group(1), int(match.group(2))
        else:
            source_id, source_version = expected_source_id, expected_source_version
        if (
            not isinstance(source_id, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", source_id) is None
            or isinstance(source_version, bool)
            or not isinstance(source_version, int)
            or source_version < 1
        ):
            raise VaultError("unsafe_path")
        self.verify_permissions()
        with self.writer_lock():
            self.verify_permissions()
            parent_fd = _open_parent(self._journal_fd, components)
            try:
                try:
                    conflicts = os.listdir(parent_fd)
                except OSError as exc:
                    raise VaultError("unsafe_path") from exc
                if any(candidate.startswith(name + ".sync-conflict-") for candidate in conflicts):
                    raise VaultError("sync_conflict")

                try:
                    target_fd, target_info, target_data = _read_target(parent_fd, name)
                except VaultError as exc:
                    if exc.code == "not_found":
                        if allow_missing:
                            return True
                        raise VaultError("conflict") from None
                    raise
                try:
                    if hashlib.sha256(target_data).hexdigest() != expected_hash:
                        raise VaultError("conflict")
                    try:
                        lines = target_data.decode("utf-8").splitlines()
                    except UnicodeDecodeError:
                        raise VaultError("unmanaged_file") from None
                    if not lines or lines[0].strip() != "---":
                        raise VaultError("unmanaged_file")
                    try:
                        end = next(
                            index for index, line in enumerate(lines[1:], 1)
                            if line.strip() == "---"
                        )
                    except StopIteration:
                        raise VaultError("unmanaged_file") from None
                    fields: dict[str, str] = {}
                    current_key = ""
                    for line in lines[1:end]:
                        if not line or line[0].isspace():
                            continue
                        if current_key == "aliases" and line.startswith("- "):
                            continue
                        key, separator, value = line.partition(":")
                        key = key.strip()
                        if not separator or not key or key in fields:
                            raise VaultError("unmanaged_file")
                        fields[key] = value.strip().strip("'\"")
                        current_key = key
                    if (
                        fields.get("ownership_marker") != "lifeweave-journal"
                        or fields.get("source_id") != source_id
                        or fields.get("source_version") != str(source_version)
                        or fields.get("source_checksum") != expected_source_hash
                    ):
                        raise VaultError("unmanaged_file")
                    original_snapshot = (
                        target_info.st_dev,
                        target_info.st_ino,
                        target_info.st_mtime_ns,
                        target_info.st_size,
                    )
                finally:
                    os.close(target_fd)

                check_fd, check_info, check_data = _read_target(parent_fd, name)
                try:
                    if (
                        (check_info.st_dev, check_info.st_ino, check_info.st_mtime_ns, check_info.st_size)
                        != original_snapshot
                        or hashlib.sha256(check_data).hexdigest() != expected_hash
                    ):
                        raise VaultError("conflict")
                finally:
                    os.close(check_fd)
                try:
                    os.unlink(name, dir_fd=parent_fd)
                    os.fsync(parent_fd)
                except OSError as exc:
                    raise VaultError("vault_unavailable", retryable=True) from exc
                return True
            finally:
                os.close(parent_fd)
