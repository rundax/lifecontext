"""``lifecontext backup`` and ``lifecontext restore``: one verified archive of a LifeContext home.

The archive holds ``~/.lifeweave`` (custody archive, catalog with its operations,
derived store, wiki revisions, vision cache, config) without ``.env``, the API
token, the Hindsight daemon's token, locks, caches and the Postgres socket
directory; the ``lifeweave`` Hindsight profile's embedded Postgres instance,
whose ``instance.json`` holds the local Postgres password, so the restored
database opens with the password it has; and the vault. The next start makes
a new daemon token. ``manifest.json`` lists every entry with its mode, size and SHA-256.
Backups refuse to run while the service or its Postgres runs, unless told to
stop them first. Restores go into a HOME without LifeContext state only, verify
everything before writing, extract into a staging directory, validate the
catalog, then move each tree into place.

Archives are trusted input. Restore keeps every path inside the new HOME (or the
``--vault`` given) and refuses links, ``.env`` and both tokens, but what it
restores still acts: ``config.yaml`` chooses where model traffic goes, and the
Postgres instance (``instance.json``, ``postgresql.auto.conf``) can run commands
when Postgres starts. Restore only archives you made.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import posixpath
import re
import shutil
import sqlite3
import stat
import tarfile
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any

from lifecontext import __version__
from lifecontext.hindsight_postgres import PG0_INSTANCE
from lifecontext.paths import LifeContextPaths, atomic_write

FORMAT = "lifecontext-backup"
FORMAT_VERSION = 1
_HOME_STATE = ".lifeweave"
_PG_STATE = f".pg0/instances/{PG0_INSTANCE}"
_SKIPPED_IN_HOME = frozenset({".env", "api.token", "hindsight-api.token", "lifeweave.lock", "service.lock",
                              "hindsight-start.json"})
_SKIPPED_DIRECTORIES_IN_HOME = frozenset({"cache", "postgres"})
_MAX_MANIFEST_BYTES = 256 * 1024 * 1024
_MAX_SETTINGS_BYTES = 1024 * 1024
_CHUNK = 1024 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_MODE = re.compile(r"[0-7]{3,4}\Z")
# How long --stop-service waits for the API and Postgres to go away, and how long
# Postgres gets to stop with its daemon before it is stopped by name.
_STOP_TIMEOUT = 30.0
_ORPHAN_GRACE = 10.0
EXCLUDED = (
    "~/.lifeweave/.env (provider keys; keep a copy elsewhere)",
    "~/.lifeweave/api.token, ~/.lifeweave/hindsight-api.token and lock files (recreated)",
    "~/.lifeweave/postgres (the Postgres socket directory, recreated)",
    "~/.lifeweave/cache (tokenizer, downloaded again)",
    "Hindsight profile env under ~/.hindsight/profiles (regenerated from config.yaml and .env; it holds the key)",
    "Postgres binaries in ~/.pg0/installation and the Hugging Face model cache (installed or downloaded again)",
    "symbolic links and special files",
)


class BackupError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _vault_paths(paths: LifeContextPaths) -> tuple[Path, Path]:
    from lifecontext.config import ConfigError, load_settings
    from lifecontext.vault import default_journal_paths

    try:
        vault = load_settings().get("vault")
    except ConfigError:
        raise BackupError("invalid_config") from None
    if vault:
        return Path(vault["root"]), Path(vault["journal_path"])
    return default_journal_paths(paths.home)


def api_answers(port: int) -> bool:
    """True when something answers ``/healthz`` on the loopback port."""
    import http.client

    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        connection.request("GET", "/healthz")
        return connection.getresponse().status == 200
    except (OSError, http.client.HTTPException):
        return False
    finally:
        connection.close()


def postgres_running(instance: Path) -> bool:
    """The embedded Postgres keeps ``postmaster.pid`` while it runs."""
    try:
        pid = int((instance / "data" / "postmaster.pid").read_text(encoding="ascii").splitlines()[0])
    except (OSError, ValueError, IndexError, UnicodeError):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@contextmanager
def _service_claim(paths: LifeContextPaths):
    """Hold the service's own lock, so ``serve`` cannot start while the backup reads."""
    try:
        paths.ensure_private_home()
        descriptor = os.open(paths.service_lock_path,
                             os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError:
        raise BackupError("unsafe_home") from None
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BackupError("service_running") from None
        yield
    finally:
        os.close(descriptor)


class _Writers:
    """What ``backup --stop-service`` stops and starts again: the user service, then the ``lifeweave`` profile."""

    def __init__(self) -> None:
        import platform

        from lifecontext import service

        self._service, self._system = service, platform.system()

    def active_service(self) -> list[str]:
        try:
            return self._service.active_units(self._system)
        except self._service.ServiceError as exc:
            raise BackupError(exc.code) from None

    def stop_service(self, units: list[str]) -> None:
        try:
            self._service.stop_units(self._system, units)
        except self._service.ServiceError as exc:
            raise BackupError(exc.code) from None

    def start_service(self, units: list[str]) -> None:
        try:
            self._service.start_again(self._system, self._service.account_home(), units)
        except self._service.ServiceError as exc:
            raise BackupError(exc.code) from None

    def stop_hindsight(self, paths: LifeContextPaths) -> str:
        from lifecontext import hindsight_postgres
        from lifecontext.hindsight import HindsightStartupError, stop_profile

        try:
            state = stop_profile(paths)
        except HindsightStartupError as exc:
            raise BackupError(exc.code) from None
        instance = paths.home / _PG_STATE
        if not _wait_until(lambda: not postgres_running(instance), _ORPHAN_GRACE):
            # Without a profile env stop_profile stops nothing; stop this instance's Postgres only.
            try:
                hindsight_postgres.stop(paths)
            except hindsight_postgres.PostgresError:
                raise BackupError("postgres_stop_failed") from None
            state = "stopped"
        return state


def _wait_until(condition: Callable[[], bool], timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.2)
    return True


def stop_hindsight(*, port: int, probe: Callable[[int], bool] = api_answers, writers=None) -> dict[str, Any]:
    """Stop only the ``lifeweave`` profile's daemon and its Postgres, while no API runs in this HOME."""
    paths = LifeContextPaths.current()
    instance = paths.home / _PG_STATE
    if probe(port):
        raise BackupError("service_running")
    if not paths.hindsight_profile_env_path.is_file():
        # Nothing of ours to stop; and taking the lock would create ~/.lifeweave in a fresh HOME.
        return {"command": "hindsight stop", "profile": "lifeweave", "state": "not_configured"}
    with _service_claim(paths):
        if probe(port):
            raise BackupError("service_running")
        state = (writers or _Writers()).stop_hindsight(paths)
        if not _wait_until(lambda: not postgres_running(instance), _STOP_TIMEOUT):
            raise BackupError("hindsight_running")
    return {"command": "hindsight stop", "profile": "lifeweave", "state": state}


def _tree(root: Path, prefix: str, skip: Callable[[tuple[str, ...], bool], bool]) -> Iterator[tuple[Path, str, os.stat_result]]:
    """Directories and regular files below ``root``, sorted; links and special files are skipped."""
    for current, directories, files in os.walk(root):
        relative = Path(current).relative_to(root).parts
        kept = []
        for name in sorted(directories):
            full = Path(current) / name
            if not full.is_symlink() and not skip((*relative, name), True):
                kept.append(name)
            elif full.is_symlink():
                yield full, "", full.lstat()
        directories[:] = kept
        info = Path(current).lstat()
        yield Path(current), "/".join((prefix, *relative)), info
        for name in sorted(files):
            if not skip((*relative, name), False):
                full = Path(current) / name
                yield full, "/".join((prefix, *relative, name)), full.lstat()


class _HashingReader:
    def __init__(self, stream, size: int):
        self.stream, self.remaining, self.digest = stream, size, hashlib.sha256()

    def read(self, size: int = -1) -> bytes:
        size = self.remaining if size < 0 else min(size, self.remaining)
        data = self.stream.read(size)
        self.remaining -= len(data)
        self.digest.update(data)
        return data


def _catalog_version(catalog: Path) -> int | None:
    if not catalog.is_file():
        return None
    try:
        connection = sqlite3.connect(f"{catalog.as_uri()}?mode=ro", uri=True)
        try:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            check = connection.execute("PRAGMA quick_check").fetchone()[0]
        finally:
            connection.close()
    except (sqlite3.Error, TypeError, ValueError):
        raise BackupError("catalog_unreadable") from None
    if check != "ok":
        raise BackupError("catalog_unreadable")
    return version


def _destination(paths: LifeContextPaths, given: str | None, protected: list[Path]) -> Path:
    name = f"lifecontext-backup-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.tar.gz"
    if given is None:
        target = paths.home / "LifeContext" / "backups" / name
    else:
        target = Path(os.path.abspath(os.path.expanduser(given)))
        if target.is_dir():
            target = target / name
    real = Path(os.path.realpath(target.parent)) / target.name
    if any(real.is_relative_to(os.path.realpath(path)) for path in protected):
        raise BackupError("destination_inside_backup")
    if given is None:
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.path.lexists(target):
        raise BackupError("destination_exists")
    return target


def backup(*, destination: str | None = None, port: int, probe: Callable[[int], bool] = api_answers,
           stop_service: bool = False, writers=None) -> dict[str, Any]:
    """Write the archive; with ``stop_service``, stop the user service and the profile first, then restart."""
    paths = LifeContextPaths.current()
    if not paths.lifeweave_home.is_dir():
        raise BackupError("nothing_to_back_up")
    root, journal = _vault_paths(paths)
    instance = paths.home / _PG_STATE
    if not stop_service:
        return _write_backup(paths, destination, port, probe, root, journal, instance)
    _destination(paths, destination, [paths.lifeweave_home, instance, root])  # fail before stopping anything
    writers = writers or _Writers()
    # What ran is recorded before anything stops, so every failure below, a
    # failing stop or Ctrl-C included, starts it again.
    units = writers.active_service()
    stopped: dict[str, Any] = {"service": units}
    try:
        if units:
            writers.stop_service(units)
            _wait_until(lambda: not probe(port), _STOP_TIMEOUT)
        if probe(port):
            # Not a service this command manages, such as a foreground `serve`.
            raise BackupError("service_running")
        stopped["hindsight"] = writers.stop_hindsight(paths)
        if not _wait_until(lambda: not postgres_running(instance), _STOP_TIMEOUT):
            raise BackupError("hindsight_running")
        report = _write_backup(paths, destination, port, probe, root, journal, instance)
    except BaseException:
        if units:
            try:
                writers.start_service(units)
            except BackupError:
                pass  # The failure that stopped the backup is the one to report.
        raise
    report.update({"stopped": stopped, "restarted": [], "warnings": []})
    if units:
        try:
            writers.start_service(units)
            report["restarted"] = units
        except BackupError:
            report["warnings"].append("service_restart_failed")
    return report


def _write_backup(paths: LifeContextPaths, destination: str | None, port: int, probe: Callable[[int], bool],
                  root: Path, journal: Path, instance: Path) -> dict[str, Any]:
    with _service_claim(paths):
        if probe(port):
            raise BackupError("service_running")
        if postgres_running(instance):
            raise BackupError("hindsight_running")
        target = _destination(paths, destination, [paths.lifeweave_home, instance, root])
        sources = [(paths.lifeweave_home, f"home/{_HOME_STATE}",
                    lambda parts, is_dir: (parts[0] in _SKIPPED_DIRECTORIES_IN_HOME if is_dir and len(parts) == 1
                                           else len(parts) == 1 and parts[0] in _SKIPPED_IN_HOME))]
        if instance.is_dir():
            sources.append((instance, f"home/{_PG_STATE}",
                            lambda parts, is_dir: not is_dir and parts == ("data", "postmaster.pid")))
        if root.is_dir():
            sources.append((root, "vault", lambda parts, is_dir: not is_dir and parts == (".journal.lock",)))
        entries: list[dict[str, Any]] = []
        skipped = 0
        given_home, real_home = str(paths.home), os.path.realpath(paths.home)
        descriptor, staged = tempfile.mkstemp(prefix=".lifecontext-backup-", dir=target.parent)
        archive_digest = hashlib.sha256()
        try:
            with os.fdopen(descriptor, "wb") as raw:
                os.fchmod(raw.fileno(), 0o600)

                class _Tee:
                    def write(self, data: bytes) -> int:
                        archive_digest.update(data)
                        return raw.write(data)

                    def flush(self) -> None:
                        raw.flush()

                with tarfile.open(fileobj=_Tee(), mode="w|gz") as archive:
                    for source, prefix, skip in sources:
                        for full, name, info in _tree(source, prefix, skip):
                            if not name or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                                skipped += 1
                                continue
                            entries.append(_add(archive, full, name, info))
                    entries_json = json.dumps({
                        "format": FORMAT, "format_version": FORMAT_VERSION, "lifecontext_version": __version__,
                        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "catalog_schema_version": _catalog_version(paths.catalog_path),
                        # init stores real paths; recording the real HOME lets restore rebase them.
                        "source_home": real_home,
                        **({"source_home_as_given": given_home} if given_home != real_home else {}),
                        "vault": {"root": _rebase(str(root), given_home, real_home),
                                  "journal_path": _rebase(str(journal), given_home, real_home)},
                        "excluded": list(EXCLUDED), "skipped_links": skipped, "entries": entries,
                    }, ensure_ascii=False, sort_keys=True).encode("utf-8")
                    info = tarfile.TarInfo("manifest.json")
                    info.size, info.mode, info.mtime = len(entries_json), 0o600, int(time.time())
                    import io
                    archive.addfile(info, io.BytesIO(entries_json))
                raw.flush()
                os.fsync(raw.fileno())
            try:
                os.link(staged, target)
            except FileExistsError:
                raise BackupError("destination_exists") from None
            except OSError:
                # FAT drives and some network shares have no hard links.
                _copy_exclusive(staged, target)
        except OSError:
            raise BackupError("backup_write_failed") from None
        finally:
            try:
                os.unlink(staged)
            except OSError:
                pass
    checksum = archive_digest.hexdigest()
    atomic_write(target.with_name(target.name + ".sha256"), f"{checksum}  {target.name}\n".encode())
    files = [entry for entry in entries if entry["type"] == "file"]
    return {"command": "backup", "archive": str(target), "sha256": checksum,
            "files": len(files), "bytes": sum(entry["size"] for entry in files),
            "skipped_links": skipped, "vault_root": str(root), "excluded": list(EXCLUDED)}


def _copy_exclusive(source: str, target: Path) -> None:
    """Publish ``source`` as ``target`` without replacing anything that appeared there meanwhile."""
    try:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except FileExistsError:
        raise BackupError("destination_exists") from None
    try:
        with os.fdopen(descriptor, "wb") as output, open(source, "rb") as stream:
            shutil.copyfileobj(stream, output, _CHUNK)
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        try:
            os.unlink(target)
        except OSError:
            pass
        raise


def _add(archive: tarfile.TarFile, full: Path, name: str, info: os.stat_result) -> dict[str, Any]:
    entry_info = tarfile.TarInfo(name)
    entry_info.mode, entry_info.mtime = stat.S_IMODE(info.st_mode), int(info.st_mtime)
    mode = f"{stat.S_IMODE(info.st_mode):04o}"
    if stat.S_ISDIR(info.st_mode):
        entry_info.type = tarfile.DIRTYPE
        archive.addfile(entry_info)
        return {"path": name, "type": "dir", "mode": mode}
    descriptor = os.open(full, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(descriptor, "rb") as stream:
        if os.fstat(stream.fileno()).st_size != info.st_size:
            raise BackupError("source_changed")
        entry_info.size = info.st_size
        reader = _HashingReader(stream, info.st_size)
        try:
            archive.addfile(entry_info, reader)
        except OSError:
            raise BackupError("source_changed") from None
    return {"path": name, "type": "file", "mode": mode, "size": info.st_size, "sha256": reader.digest.hexdigest()}


def _safe_name(name: str) -> bool:
    """Relative, normalised names below the three roots; never the secrets and locks backup leaves out."""
    parts = name.split("/")
    if any(part in {"", ".", ".."} for part in parts) or "\\" in name or "\x00" in name:
        return False
    # macOS and Windows file systems ignore case: API.TOKEN would land on api.token.
    if (parts[:2] == ["home", _HOME_STATE] and len(parts) == 3
            and parts[2].casefold() in {skipped.casefold() for skipped in _SKIPPED_IN_HOME}):
        return False
    return name == "manifest.json" or name == "vault" or name.startswith("vault/") or any(
        name == f"home/{root}" or name.startswith(f"home/{root}/") for root in (_HOME_STATE, _PG_STATE))


def _normal_absolute(value: Any) -> bool:
    return (isinstance(value, str) and value.startswith("/") and not value.startswith("//")
            and "\x00" not in value and posixpath.normpath(value) == value)


def _checked_manifest(manifest: Any) -> dict[str, Any]:
    """The manifest's shape, so later steps never meet a wrong type or a path that climbs out."""
    if (not isinstance(manifest, dict) or manifest.get("format") != FORMAT
            or manifest.get("format_version") != FORMAT_VERSION):
        raise BackupError("archive_invalid")
    entries, vault = manifest.get("entries"), manifest.get("vault")
    schema = manifest.get("catalog_schema_version")
    if (not isinstance(entries, list) or not isinstance(vault, dict)
            or not _normal_absolute(manifest.get("source_home"))
            or "source_home_as_given" in manifest and not _normal_absolute(manifest["source_home_as_given"])
            or not _normal_absolute(vault.get("root")) or not _normal_absolute(vault.get("journal_path"))
            or not PurePosixPath(vault["journal_path"]).is_relative_to(vault["root"])
            or vault["journal_path"] == vault["root"]
            or schema is not None and (type(schema) is not int or schema < 0)):
        raise BackupError("archive_invalid")
    for entry in entries:
        if (not isinstance(entry, dict) or not isinstance(entry.get("path"), str)
                or entry.get("type") not in {"dir", "file"}
                or not isinstance(entry.get("mode"), str) or not _MODE.fullmatch(entry["mode"])):
            raise BackupError("archive_invalid")
        if entry["type"] == "file" and (type(entry.get("size")) is not int or entry["size"] < 0
                                        or not isinstance(entry.get("sha256"), str)
                                        or not _SHA256.fullmatch(entry["sha256"])):
            raise BackupError("archive_invalid")
    if len({entry["path"] for entry in entries}) != len(entries):
        raise BackupError("archive_invalid")
    return manifest


def _members(path: Path) -> Iterator[tuple[tarfile.TarInfo, tarfile.TarFile]]:
    try:
        with tarfile.open(path, mode="r|gz") as archive:
            for member in archive:
                yield member, archive
    except (tarfile.TarError, OSError, EOFError):
        raise BackupError("archive_unreadable") from None


def _digest(member: tarfile.TarInfo, archive: tarfile.TarFile, sink=None) -> str:
    stream = archive.extractfile(member)
    digest = hashlib.sha256()
    while chunk := stream.read(_CHUNK):
        digest.update(chunk)
        if sink is not None:
            sink.write(chunk)
    return digest.hexdigest()


def verify(path: Path) -> dict[str, Any]:
    """Read the whole archive without writing anything; return its manifest when every entry matches."""
    sidecar = path.with_name(path.name + ".sha256")
    if sidecar.is_file():
        try:
            expected = sidecar.read_text(encoding="utf-8").split()[:1]
        except (OSError, UnicodeError):
            raise BackupError("archive_checksum_mismatch") from None
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(_CHUNK):
                digest.update(chunk)
        if expected != [digest.hexdigest()]:
            raise BackupError("archive_checksum_mismatch")
    seen: dict[str, dict[str, Any]] = {}
    manifest = None
    for member, archive in _members(path):
        if not _safe_name(member.name) or member.name in seen or not (member.isdir() or member.isreg()):
            raise BackupError("archive_invalid")
        if member.name == "manifest.json":
            if not member.isreg() or member.size > _MAX_MANIFEST_BYTES:
                raise BackupError("archive_invalid")
            try:
                manifest = json.loads(archive.extractfile(member).read().decode("utf-8"))
            except (UnicodeError, ValueError, RecursionError):
                raise BackupError("archive_invalid") from None
            seen[member.name] = {}
            continue
        seen[member.name] = ({"type": "dir"} if member.isdir() else
                             {"type": "file", "size": member.size, "sha256": _digest(member, archive)})
    manifest = _checked_manifest(manifest)
    listed = {entry["path"]: entry for entry in manifest["entries"]}
    if set(listed) != set(seen) - {"manifest.json"}:
        raise BackupError("archive_incomplete")
    for name, entry in listed.items():
        actual = seen[name]
        if entry.get("type") != actual["type"] or (actual["type"] == "file" and (
                entry.get("size") != actual["size"] or entry.get("sha256") != actual["sha256"])):
            raise BackupError("archive_checksum_mismatch")
    return manifest


def _restored_mode(entry: dict[str, Any]) -> int:
    """The archived mode, except in the vault: notes need no execute bits, and folders stay private."""
    if entry["path"] == "vault" or entry["path"].startswith("vault/"):
        return 0o700 if entry["type"] == "dir" else 0o600
    return int(entry["mode"], 8) & 0o777


def _extract(path: Path, staging: Path, manifest: dict[str, Any]) -> None:
    listed = {entry["path"]: entry for entry in manifest["entries"]}
    directories = []
    for member, archive in _members(path):
        if member.name == "manifest.json":
            continue
        entry = listed.get(member.name)
        if entry is None:
            raise BackupError("archive_changed")
        target = staging / member.name
        if member.isdir():
            target.mkdir(mode=0o700, parents=True, exist_ok=True)
            directories.append((target, _restored_mode(entry)))
            continue
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            if _digest(member, archive, stream) != entry["sha256"]:
                raise BackupError("archive_changed")
            os.fchmod(stream.fileno(), _restored_mode(entry))
    for directory, mode in sorted(directories, key=lambda item: len(item[0].parts), reverse=True):
        os.chmod(directory, mode)


def _rebase(value: Any, old: str, new: str) -> Any:
    return _rebase_any(value, [old], new)


def _rebase_any(value: Any, olds: list[str], new: str) -> Any:
    """Rebase from the first old HOME spelling that matches (the real path, then the one HOME gave)."""
    for old in olds:
        if isinstance(value, str) and (value == old or value.startswith(old.rstrip("/") + "/")):
            return new + value[len(old):]
    return value


def _move(source: Path, target: Path) -> None:
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.rename(source, target)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        temporary = target.with_name(f".{target.name}.lifecontext-restore")
        shutil.copytree(source, temporary, symlinks=True)
        os.rename(temporary, target)
        shutil.rmtree(source)


def _inside(path: Path, directory: Path) -> bool:
    """``path`` lies strictly below ``directory``, by name and after resolving links."""
    return (path != directory and path.is_relative_to(directory)
            and Path(os.path.realpath(path)).is_relative_to(os.path.realpath(directory)))


def _existing_vault(root: Path) -> bool:
    """False when nothing is at ``root``; True for an empty directory of this user (a volume mount point)."""
    try:
        info = root.lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        raise BackupError("unsafe_vault")
    with os.scandir(root) as entries:
        if next(entries, None) is not None:
            raise BackupError("vault_not_empty")
    return True


def _remove(path: Path) -> None:
    """Delete a tree this restore created, even below directories it made read-only."""
    if path.is_dir() and not path.is_symlink():
        os.chmod(path, 0o700)
        for child in path.iterdir():
            _remove(child)
        path.rmdir()
    else:
        path.unlink()


def _place_file(source: Path, target: Path, entry: dict[str, Any]) -> None:
    """A new name for verified bytes: a hard link on the same filesystem, else a checked copy."""
    try:
        os.link(source, target, follow_symlinks=False)
        return
    except FileExistsError:
        raise
    except (OSError, NotImplementedError):
        pass  # Another filesystem, or one without hard links.
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as output:
            digest = hashlib.sha256()
            with open(source, "rb") as stream:
                while chunk := stream.read(_CHUNK):
                    digest.update(chunk)
                    output.write(chunk)
            if digest.hexdigest() != entry["sha256"]:
                raise BackupError("archive_changed")
            os.fchmod(output.fileno(), _restored_mode(entry))
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        try:
            os.unlink(target)
        except OSError:
            pass
        raise


def _fill(source: Path, target: Path, listed: dict[str, dict[str, Any]], created: list[Path]) -> None:
    """Recreate the staged vault inside the existing ``target``, entry by entry, never replacing anything."""
    directories = []
    for current, names, files in os.walk(source):
        relative = Path(current).relative_to(source)
        for name in sorted(names):
            os.mkdir(target / relative / name, 0o700)
            if not relative.parts:
                created.append(target / name)
            directories.append((target / relative / name, _restored_mode(listed[f"vault/{relative / name}"])))
        for name in sorted(files):
            entry = listed[f"vault/{relative / name}"]
            _place_file(Path(current) / name, target / relative / name, entry)
            if not relative.parts:
                created.append(target / name)
    for directory, mode in sorted(directories, key=lambda item: len(item[0].parts), reverse=True):
        os.chmod(directory, mode)


def _place_vault(source: Path, target: Path, existed: bool, listed: dict[str, dict[str, Any]]):
    """Move the staged vault to ``target``; return how to take it back out again.

    A missing target on the same filesystem is renamed into place. An existing empty
    directory (a volume mount point), or a target on another filesystem, is filled
    instead, and taking it back leaves it as it was.
    """
    if not existed:
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            os.rename(source, target)
            return lambda: os.rename(target, source)
        except OSError as exc:
            if exc.errno not in (errno.EXDEV, errno.EBUSY):
                raise
        os.mkdir(target, 0o700)
    created: list[Path] = []

    def undo() -> None:
        for path in reversed(created):
            _remove(path)
        if not existed:
            os.rmdir(target)

    try:
        _fill(source, target, listed, created)
    except BaseException:
        try:
            undo()
        except OSError:
            pass
        raise
    return undo


def _restored_settings(config: Path, root: Path, journal: Path, home: Path) -> None:
    """Point the restored ``config.yaml`` at the vault's new place; refuse one that does not parse."""
    import yaml

    from lifecontext.vault import default_journal_paths

    try:
        if config.stat().st_size > _MAX_SETTINGS_BYTES:
            raise ValueError
        settings = yaml.safe_load(config.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, RecursionError, yaml.YAMLError):
        raise BackupError("archive_config_invalid") from None
    settings = {} if settings is None else settings
    if not isinstance(settings, dict) or not isinstance(settings.get("vault", {}), dict):
        raise BackupError("archive_config_invalid")
    wanted = {"root": str(root), "journal_path": str(journal)}
    vault = settings.get("vault")
    if vault is None and (root, journal) == default_journal_paths(home):
        return
    if vault is None or any(vault.get(key) != value for key, value in wanted.items()):
        settings["vault"] = {**(vault or {}), **wanted}
        atomic_write(config, yaml.safe_dump(settings, sort_keys=False, allow_unicode=True).encode())


def _restored_instance(instance_file: Path, old_homes: list[str], home: Path) -> None:
    try:
        if instance_file.stat().st_size > _MAX_SETTINGS_BYTES:
            raise ValueError
        instance = json.loads(instance_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, RecursionError):
        raise BackupError("archive_instance_invalid") from None
    if not isinstance(instance, dict):
        raise BackupError("archive_instance_invalid")
    instance = {key: _rebase_any(value, old_homes, str(home)) for key, value in instance.items()}
    mode = stat.S_IMODE(instance_file.stat().st_mode)
    atomic_write(instance_file, json.dumps(instance, indent=2).encode(), mode=mode)


def restore(archive: str, *, port: int, vault: str | None = None,
            probe: Callable[[int], bool] = api_answers) -> dict[str, Any]:
    from lifecontext.catalog import _SCHEMA_VERSION

    paths = LifeContextPaths.current()
    home = paths.home
    if not home.is_absolute():
        raise BackupError("unsafe_home")
    path = Path(os.path.abspath(os.path.expanduser(archive)))
    if not path.is_file():
        raise BackupError("archive_unreadable")
    manifest = verify(path)
    old_homes = [manifest["source_home"], *([manifest["source_home_as_given"]]
                                            if "source_home_as_given" in manifest else [])]
    real_home = Path(os.path.realpath(home))
    old_root, old_journal = Path(manifest["vault"]["root"]), Path(manifest["vault"]["journal_path"])
    if vault is not None:
        given = Path(os.path.abspath(os.path.expanduser(vault)))
    else:
        given = Path(_rebase_any(str(old_root), old_homes, str(real_home)))
    # The service opens every vault folder without following links, so config.yaml gets the
    # real path; the last component stays as given, so a link there is still refused.
    root = Path(os.path.realpath(given.parent)) / given.name
    if vault is None:
        # The archive names the vault. Without --vault only a folder below ~/LifeContext, the
        # default vault's parent, is accepted, judged after resolving links: elsewhere in HOME
        # (~/bin, ~/.local/bin, ~/.config, ~/Library) restored files could be run.
        real_root = Path(os.path.realpath(root))
        if not _inside(real_root, real_home):
            raise BackupError("vault_outside_home")
        if not _inside(real_root, real_home / "LifeContext"):
            raise BackupError("vault_location_refused")
    journal = root / old_journal.relative_to(old_root)
    pg_home = home / ".pg0"
    if any(root == place or root.is_relative_to(place) or place.is_relative_to(root)
           for place in (paths.lifeweave_home, pg_home, real_home / _HOME_STATE, real_home / ".pg0")):
        raise BackupError("invalid_vault")
    targets = {f"home/{_HOME_STATE}": paths.lifeweave_home, f"home/{_PG_STATE}": home / _PG_STATE}
    if any(os.path.lexists(target) for target in targets.values()):
        raise BackupError("home_not_empty")
    vault_existed = _existing_vault(root)
    if probe(port):
        raise BackupError("service_running")
    if (manifest.get("catalog_schema_version") or 0) > _SCHEMA_VERSION:
        raise BackupError("schema_unsupported")

    staging = Path(tempfile.mkdtemp(prefix=".lifecontext-restore-", dir=home))
    undo: list[Callable[[], None]] = []
    try:
        _extract(path, staging, manifest)
        state = staging / "home" / _HOME_STATE
        version = _catalog_version(state / "catalog.sqlite3")
        if version is not None and version > _SCHEMA_VERSION:
            raise BackupError("schema_unsupported")
        if (state / "config.yaml").is_file():
            _restored_settings(state / "config.yaml", root, journal, real_home)
        if (staging / "home" / _PG_STATE / "instance.json").is_file():
            _restored_instance(staging / "home" / _PG_STATE / "instance.json", old_homes, home)
        if (staging / "vault").exists():
            listed = {entry["path"]: entry for entry in manifest["entries"]}
            undo.append(_place_vault(staging / "vault", root, vault_existed, listed))
        for name in (f"home/{_PG_STATE}", f"home/{_HOME_STATE}"):
            if (staging / name).exists():
                _move(staging / name, targets[name])
                undo.append(lambda target=targets[name], source=staging / name: os.rename(target, source))
        # Whatever modes the archive carried, the private roots end up private.
        for directory in (paths.lifeweave_home, pg_home, pg_home / "instances", home / _PG_STATE, root):
            if directory.is_dir() and not directory.is_symlink():
                os.chmod(directory, 0o700)
    except BaseException:
        for step in reversed(undo):
            try:
                step()
            except OSError:
                pass
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    files = [entry for entry in manifest["entries"] if entry["type"] == "file"]
    return {"command": "restore", "archive": str(path), "created_at": manifest.get("created_at"),
            "lifecontext_version": manifest.get("lifecontext_version"), "catalog_schema_version": version,
            "files": len(files), "vault_root": str(root), "journal_path": str(journal),
            "next_steps": [
                "lifecontext init --api-key-stdin < FILE  (or lifecontext init in a terminal): the key was not in the backup",
                "lifecontext preflight  (regenerates the Hindsight profile; downloads the models if they are missing)",
                "lifecontext doctor",
            ]}


def render(report: dict[str, Any]) -> str:
    if report["command"] == "backup":
        lines = [f"Backup written: {report['archive']}",
                 f"- {report['files']} files, {report['bytes']} bytes; SHA-256 {report['sha256']}",
                 f"- Vault: {report['vault_root']}",
                 "- Not included: " + "; ".join(report["excluded"])]
        if report["skipped_links"]:
            lines.append(f"- Skipped {report['skipped_links']} symbolic links or special files")
        if "stopped" in report:
            stopped = list(report["stopped"]["service"])
            if report["stopped"].get("hindsight") == "stopped":
                stopped.append("the lifeweave Hindsight profile")
            lines.append("- Stopped first: " + (", ".join(stopped) or "nothing was running"))
            if report["restarted"]:
                lines.append("- Started again: " + ", ".join(report["restarted"]))
        if "service_restart_failed" in report.get("warnings", []):
            lines.append("Warning: the service did not start again; run lifecontext service install, "
                         "or systemctl --user start lifecontext (launchd: launchctl bootstrap).")
        return "\n".join(lines)
    if report["command"] == "hindsight stop":
        return ("Stopped the lifeweave Hindsight profile and its Postgres." if report["state"] == "stopped"
                else "No lifeweave Hindsight profile in this HOME; nothing to stop.")
    lines = [f"Restored {report['files']} files from {report['archive']} (made {report['created_at']})",
             f"- Vault: {report['vault_root']}", "Next steps:"]
    lines += [f"  {number}. {step}" for number, step in enumerate(report["next_steps"], 1)]
    return "\n".join(lines)
