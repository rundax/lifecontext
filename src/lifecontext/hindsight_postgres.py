"""Socket-only PostgreSQL for the ``lifeweave`` Hindsight profile.

Left to itself, Hindsight starts its embedded Postgres through pg0, which listens
on 127.0.0.1 for every local user and passes the database password on pg0's
command line, where every local user can read it. LifeContext runs the same pg0
instance itself instead: pg0's own PostgreSQL binaries and server settings, but
``listen_addresses=''``, so the only way in is a Unix socket in
``~/.lifeweave/postgres`` (mode 0700). Other local users cannot connect, whatever
password they know. Hindsight gets a ``postgresql://`` URL for that socket
instead of ``pg0://``, so it never starts or stops this Postgres.

pg0 itself still runs once per HOME: to create a new instance, or to unpack the
PostgreSQL binaries after a restore (into a throwaway instance). That one start
listens on 127.0.0.1 with a random one-time password for a few seconds.
"""

from __future__ import annotations

import importlib
import io
import json
import os
import re
import secrets
import shlex
import shutil
import signal
import stat
import subprocess
import time
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from lifecontext.paths import LifeContextPaths

PG0_INSTANCE = "hindsight-embed-lifeweave"
USERNAME = "hindsight"
DATABASE = "hindsight"
_UNPACK_INSTANCE = "lifecontext-unpack"
_START_TIMEOUT = 120
# The settings pg0 0.15.1 starts PostgreSQL with (its `postgres` command line).
# LifeContext keeps them, replaces the listen addresses and the socket directory,
# and leaves out pg0's `-F`, which turns fsync off.
_SERVER_SETTINGS = (
    ("logging_collector", "on"),
    ("log_timezone", "UTC"),
    ("shared_buffers", "256MB"),
    ("log_rotation_size", "100MB"),
    ("log_rotation_age", "1d"),
    ("maintenance_work_mem", "512MB"),
    ("work_mem", "64MB"),
    ("log_filename", "postgresql-%Y-%m-%d.log"),
    ("timezone", "UTC"),
    ("max_parallel_maintenance_workers", "4"),
    ("effective_cache_size", "1GB"),
    ("log_directory", "log"),
)
# A Unix socket path holds at most 103 bytes on macOS and 107 on Linux.
_SOCKET_PATH_LIMIT = 103
_MAX_INSTANCE_BYTES = 64 * 1024
_VERSION = re.compile(r"[0-9]+(?:\.[0-9]+){0,3}\Z")


class PostgresError(RuntimeError):
    """A stable failure code; never pg0's or PostgreSQL's own message."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class Instance:
    port: int
    username: str
    password: str
    database: str
    data_dir: Path
    bin_dir: Path


def _printable(value: Any) -> bool:
    return isinstance(value, str) and bool(value) and all(32 <= ord(c) < 127 for c in value)


def _inside(path: Path, directory: Path) -> bool:
    return Path(os.path.realpath(path)).is_relative_to(os.path.realpath(directory))


def read_instance(paths: LifeContextPaths) -> Instance | None:
    """pg0's ``instance.json`` for the profile, checked; ``None`` when there is none."""
    path = paths.pg0_instance_dir / "instance.json"
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    except OSError:
        raise PostgresError("postgres_instance_unsafe") from None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            raise PostgresError("postgres_instance_unsafe")
        raw = os.read(descriptor, _MAX_INSTANCE_BYTES + 1)
    finally:
        os.close(descriptor)
    try:
        if len(raw) > _MAX_INSTANCE_BYTES:
            raise ValueError
        data = json.loads(raw.decode("utf-8"))
        port = data["port"]
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError
        if not all(_printable(data.get(key)) for key in ("username", "password", "database")):
            raise ValueError
        if not isinstance(data.get("version"), str) or not _VERSION.fullmatch(data["version"]):
            raise ValueError
        data_dir, installation = Path(data["data_dir"]), Path(data["installation_dir"])
    except (KeyError, TypeError, ValueError, UnicodeError, RecursionError):
        raise PostgresError("postgres_instance_invalid") from None
    pg0_home = paths.home / ".pg0"
    if (not data_dir.is_absolute() or not installation.is_absolute()
            or os.path.realpath(data_dir) != os.path.realpath(paths.pg0_instance_dir / "data")
            or not _inside(installation, pg0_home)):
        raise PostgresError("postgres_instance_invalid")
    return Instance(port=port, username=data["username"], password=data["password"],
                    database=data["database"], data_dir=paths.pg0_instance_dir / "data",
                    bin_dir=installation / data["version"] / "bin")


def status(paths: LifeContextPaths) -> dict[str, Any]:
    """Whether the profile's Postgres runs and where it listens, from ``postmaster.pid``; reads only."""
    data_dir = paths.pg0_instance_dir / "data"
    if not data_dir.is_dir():
        return {"state": "absent"}
    try:
        lines = (data_dir / "postmaster.pid").read_text(encoding="ascii").splitlines()
        pid = int(lines[0])
    except FileNotFoundError:
        return {"state": "stopped"}
    except (OSError, UnicodeError, ValueError, IndexError):
        return {"state": "unknown"}
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return {"state": "stopped"}
    except PermissionError:
        return {"state": "unknown"}
    # Lines 5 and 6: the first socket directory and the first listen address,
    # empty when PostgreSQL listens on no TCP address at all.
    socket_dir = lines[4] if len(lines) > 4 else None
    listen = lines[5].strip() if len(lines) > 5 else None
    return {"state": "running", "pid": pid, "socket_dir": socket_dir,
            "listen": None if listen is None else ("socket_only" if listen == "" else "tcp")}


def database_url(paths: LifeContextPaths, instance: Instance) -> str:
    """The libpq URL Hindsight connects with: the private socket, never TCP."""
    return (f"postgresql://{quote(instance.username, safe='')}:{quote(instance.password, safe='')}@/"
            f"{quote(instance.database, safe='')}?host={quote(str(paths.postgres_socket_dir), safe='/')}"
            f"&port={instance.port}")


def _shell_safe(*paths: Path) -> None:
    """pg_ctl runs PostgreSQL through ``sh -c`` with ``-D`` and ``-l`` only in double quotes."""
    if any(character in str(path) for path in paths for character in '"\'`$,\\') or not all(
            str(path).isprintable() for path in paths):
        raise PostgresError("postgres_socket_path_unsupported")


def _socket_dir(paths: LifeContextPaths, port: int) -> Path:
    directory = paths.postgres_socket_dir
    if len(str(directory / f".s.PGSQL.{port}").encode()) > _SOCKET_PATH_LIMIT:
        raise PostgresError("postgres_socket_path_too_long")
    _shell_safe(directory, paths.pg0_instance_dir)
    try:
        directory.mkdir(mode=0o700)
    except FileExistsError:
        pass
    except OSError:
        raise PostgresError("postgres_socket_dir_unsafe") from None
    try:
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
            raise OSError
        if stat.S_IMODE(info.st_mode) != 0o700:
            os.chmod(directory, 0o700)
    except OSError:
        raise PostgresError("postgres_socket_dir_unsafe") from None
    return directory


def _make_private(paths: LifeContextPaths) -> None:
    """pg0 writes ``instance.json`` (with the password) and its directory with the umask's modes."""
    instance = paths.pg0_instance_dir
    try:
        for path, mode in ((instance, 0o700), (instance / "instance.json", 0o600)):
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or info.st_uid != os.geteuid():
                raise PostgresError("postgres_instance_unsafe")
            if stat.S_IMODE(info.st_mode) != mode:
                os.chmod(path, mode)
    except OSError:
        raise PostgresError("postgres_instance_unsafe") from None


def _pg0(name: str, password: str, **extra: Any):
    try:
        server = importlib.import_module("pg0").Pg0(
            name=name, username=USERNAME, password=password, database=DATABASE, **extra)
    except Exception:  # noqa: BLE001 -- a stable code, never pg0's own message
        raise PostgresError("postgres_setup_failed") from None
    return server


def _quietly(call) -> None:
    """A pg0 call: no output, and what pg0 writes (``instance.json`` with the password) private from the start."""
    previous = os.umask(0o077)
    try:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            call()
    finally:
        os.umask(previous)


def _is_postmaster(pid: int, data_dir: Path) -> bool:
    """``pid`` is this account's PostgreSQL started on ``data_dir`` (pg0 and pg_ctl both pass ``-D``)."""
    try:
        command = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
    except FileNotFoundError:
        try:
            command = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True,
                                     timeout=10, check=False).stdout
        except (OSError, subprocess.SubprocessError):
            return False
    except OSError:
        return False
    return "postgres" in command and str(data_dir) in command


def _halt(data_dir: Path) -> None:
    """Fast-stop whatever postmaster runs on ``data_dir`` (a pg0 start that went wrong), by its pid."""
    try:
        pid = int((data_dir / "postmaster.pid").read_text(encoding="ascii").splitlines()[0])
        if not _is_postmaster(pid, data_dir):
            return  # A stale pid file; never signal some other process.
        os.kill(pid, signal.SIGINT)
    except (OSError, ValueError, IndexError, UnicodeError):
        return
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        except OSError:
            return
        time.sleep(0.2)


def _create(paths: LifeContextPaths) -> None:
    """A new instance through pg0, with a random password, stopped again at once."""
    server = _pg0(PG0_INSTANCE, secrets.token_hex(32))
    try:
        _quietly(server.start)
    except Exception:  # noqa: BLE001
        raise PostgresError("postgres_setup_failed") from None
    finally:
        try:
            _quietly(server.stop)
        except Exception:  # noqa: BLE001, S110 -- status() below decides
            pass
        # pg0 ignores its own stop failing: never leave this TCP start behind.
        if status(paths)["state"] == "running":
            _halt(paths.pg0_instance_dir / "data")
    if status(paths)["state"] == "running":
        raise PostgresError("postgres_setup_failed")


def _unpack(paths: LifeContextPaths, instance: Instance) -> None:
    """pg0 unpacks PostgreSQL only when it starts an instance, so start and drop a throwaway one."""
    # No TCP and a private socket: pg0's own user step then fails, after the unpacking.
    server = _pg0(_UNPACK_INSTANCE, secrets.token_hex(32), config={
        "listen_addresses": "", "unix_socket_directories": str(paths.postgres_socket_dir)})
    throwaway = paths.home / ".pg0" / "instances" / _UNPACK_INSTANCE
    try:
        _quietly(server.start)
    except Exception:  # noqa: BLE001, S110 -- the binaries may be there even so
        pass
    finally:
        try:
            _quietly(lambda: server.drop(force=True))
        except Exception:  # noqa: BLE001, S110
            pass
        # pg0 ignores its own drop failing: never leave the throwaway running on TCP.
        _halt(throwaway / "data")
        shutil.rmtree(throwaway, ignore_errors=True)
    if not (instance.bin_dir / "pg_ctl").is_file():
        raise PostgresError("postgres_binaries_missing")


def _pg_ctl(instance: Instance, *arguments: str) -> None:
    library = str(instance.bin_dir.parent / "lib")
    environment = dict(os.environ)
    for name in ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH"):
        environment[name] = os.pathsep.join(item for item in (library, environment.get(name)) if item)
    try:
        subprocess.run([str(instance.bin_dir / "pg_ctl"), *arguments], env=environment,
                       stdin=subprocess.DEVNULL, capture_output=True, timeout=_START_TIMEOUT + 30, check=False)
    except (OSError, subprocess.SubprocessError):
        raise PostgresError("postgres_control_failed") from None


def _server_options(instance: Instance, socket_dir: Path) -> str:
    """``pg_ctl -o``: a shell word list; the socket directory is a quoted list element."""
    settings = [("listen_addresses", ""), ("unix_socket_directories", f'"{socket_dir}"'), *_SERVER_SETTINGS]
    # pg0 also passes -F (fsync off); LifeContext keeps fsync on, so a host crash cannot corrupt the index.
    words = ["-p", str(instance.port)]
    for name, value in settings:
        words += ["-c", f"{name}={value}"]
    return " ".join(shlex.quote(word) for word in words)


def _start(paths: LifeContextPaths, instance: Instance, socket_dir: Path) -> None:
    _pg_ctl(instance, "start", "-D", str(instance.data_dir), "-w", "-t", str(_START_TIMEOUT), "-s",
            "-l", str(instance.data_dir / "start.log"), "-o", _server_options(instance, socket_dir))
    current = status(paths)
    if current["state"] != "running" or current["listen"] != "socket_only":
        raise PostgresError("postgres_start_failed")


def _stop(paths: LifeContextPaths, instance: Instance) -> None:
    _pg_ctl(instance, "stop", "-D", str(instance.data_dir), "-m", "fast", "-w", "-t", "60", "-s")
    if status(paths)["state"] == "running":
        raise PostgresError("postgres_stop_failed")


def ensure_socket_only(paths: LifeContextPaths) -> str:
    """Run the profile's Postgres on its private socket only; return Hindsight's database URL.

    Creates the instance on first use, unpacks the binaries after a restore, and
    restarts a Postgres that pg0 started (on 127.0.0.1) on the socket instead.
    Never changes the data or the password.
    """
    instance = read_instance(paths)
    if instance is None:
        if os.path.lexists(paths.pg0_instance_dir / "data"):
            # A data directory without pg0's record of its password; pg0 deletes
            # instance.json when one of its starts fails. Whatever runs there stops.
            if status(paths)["state"] == "running":
                _halt(paths.pg0_instance_dir / "data")
            raise PostgresError("postgres_instance_missing")
        _create(paths)
        instance = read_instance(paths)
        if instance is None:
            raise PostgresError("postgres_setup_failed")
    _make_private(paths)
    socket_dir = _socket_dir(paths, instance.port)
    if not (instance.bin_dir / "pg_ctl").is_file():
        _unpack(paths, instance)
    current = status(paths)
    if current["state"] == "unknown":
        raise PostgresError("postgres_state_unknown")
    if current["state"] == "running":
        if current["listen"] == "socket_only" and current["socket_dir"] == str(socket_dir):
            return database_url(paths, instance)
        _stop(paths, instance)
    _start(paths, instance, socket_dir)
    return database_url(paths, instance)


def stop(paths: LifeContextPaths) -> bool:
    """Stop the profile's Postgres if it runs; ``True`` when it was running."""
    if status(paths)["state"] != "running":
        return False
    instance = read_instance(paths)
    if instance is None or not (instance.bin_dir / "pg_ctl").is_file():
        raise PostgresError("postgres_stop_failed")
    _stop(paths, instance)
    return True


def report(paths: LifeContextPaths, configured_url: str | None) -> dict[str, Any]:
    """For ``doctor``: where Postgres listens and whether its files are private; no secrets."""
    current = status(paths)
    result: dict[str, Any] = {"state": current["state"], "listen": current.get("listen")}
    if current["state"] == "running" and current.get("socket_dir") is not None:
        result["socket_dir_is_private"] = current["socket_dir"] == str(paths.postgres_socket_dir)
    result["configured_socket_only"] = bool(configured_url) and configured_url.startswith("postgresql://") and (
        f"host={quote(str(paths.postgres_socket_dir), safe='/')}" in configured_url)
    try:
        directory = paths.postgres_socket_dir.lstat()
        result["socket_dir_mode"] = f"{stat.S_IMODE(directory.st_mode):04o}"
    except OSError:
        result["socket_dir_mode"] = None
    try:
        info = (paths.pg0_instance_dir / "instance.json").lstat()
        result["instance_file_private"] = (stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
                                           and stat.S_IMODE(info.st_mode) == 0o600)
    except OSError:
        result["instance_file_private"] = None
    return result

