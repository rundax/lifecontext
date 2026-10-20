"""Socket-only Postgres for the `lifeweave` profile, with pg0 and pg_ctl replaced by fakes."""

from __future__ import annotations

import json
import os
import shlex
import stat
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from local_paths import scratch_root

from lifecontext import hindsight_postgres as postgres
from lifecontext.hindsight_postgres import PostgresError
from lifecontext.paths import LifeContextPaths

PASSWORD = "0123456789abcdef" * 4


@contextmanager
def _home():
    with tempfile.TemporaryDirectory(prefix="pg.", dir=scratch_root()) as temporary:
        home = Path(temporary).resolve(strict=True)
        # No socket is made here, so a checkout's long path may exceed the socket limit.
        with patch.dict(os.environ, {"HOME": str(home)}), patch.object(postgres, "_SOCKET_PATH_LIMIT", 4096):
            paths = LifeContextPaths.current()
            paths.ensure_private_home()
            yield paths


def _instance(paths: LifeContextPaths, *, password: str = PASSWORD, binaries: bool = True, **changes) -> Path:
    """What pg0 leaves behind: instance.json (umask modes), a data directory and the unpacked binaries."""
    instance = paths.pg0_instance_dir
    (instance / "data").mkdir(parents=True, exist_ok=True)
    os.chmod(instance, 0o775)
    os.chmod(instance / "data", 0o700)
    installation = paths.home / ".pg0" / "installation"
    if binaries:
        (installation / "18.1.0" / "bin").mkdir(parents=True, exist_ok=True)
        (installation / "18.1.0" / "bin" / "pg_ctl").write_text("")
    record = {"pid": 1, "port": 54321, "data_dir": str(instance / "data"), "installation_dir": str(installation),
              "username": "hindsight", "password": password, "database": "hindsight", "version": "18.1.0", **changes}
    (instance / "instance.json").write_text(json.dumps(record))
    os.chmod(instance / "instance.json", 0o664)
    return instance


def _postmaster(paths: LifeContextPaths, socket_dir: str, listen: str, pid: int | None = None) -> None:
    lines = [str(pid or os.getpid()), str(paths.pg0_instance_dir / "data"), "1792437321", "54321", socket_dir,
             listen, "  3284663    393247", "ready   "]
    (paths.pg0_instance_dir / "data" / "postmaster.pid").write_text("\n".join(lines) + "\n")


class _PgCtl:
    """Stands in for pg_ctl: start writes a socket-only postmaster.pid, stop removes it."""

    def __init__(self, paths: LifeContextPaths):
        self.paths, self.calls = paths, []

    def __call__(self, instance, *arguments):
        self.calls.append(arguments)
        if arguments[0] == "start":
            options = shlex.split(arguments[arguments.index("-o") + 1])
            directory = next(value.split("=", 1)[1] for value in options if value.startswith("unix_socket_directories="))
            listen = next(value.split("=", 1)[1] for value in options if value.startswith("listen_addresses="))
            _postmaster(self.paths, directory.strip('"'), listen)
        elif arguments[0] == "stop":
            (self.paths.pg0_instance_dir / "data" / "postmaster.pid").unlink(missing_ok=True)


class InstanceTests(unittest.TestCase):
    def test_instance_json_is_checked(self) -> None:
        with _home() as paths:
            self.assertIsNone(postgres.read_instance(paths))
            _instance(paths)
            instance = postgres.read_instance(paths)
            self.assertEqual((instance.port, instance.username, instance.password, instance.database),
                             (54321, "hindsight", PASSWORD, "hindsight"))
            self.assertEqual(instance.bin_dir, paths.home / ".pg0" / "installation" / "18.1.0" / "bin")
            for label, changes in (("port", {"port": "5432"}), ("no password", {"password": ""}),
                                   ("version", {"version": "../../bin"}),
                                   ("other data", {"data_dir": "/var/lib/postgresql"}),
                                   ("binaries elsewhere", {"installation_dir": "/usr/local"})):
                with self.subTest(label):
                    _instance(paths, **changes)
                    with self.assertRaises(PostgresError) as caught:
                        postgres.read_instance(paths)
                    self.assertEqual(caught.exception.code, "postgres_instance_invalid")
            (paths.pg0_instance_dir / "instance.json").unlink()
            (paths.pg0_instance_dir / "instance.json").symlink_to(paths.home / "elsewhere.json")
            with self.assertRaises(PostgresError) as caught:
                postgres.read_instance(paths)
            self.assertEqual(caught.exception.code, "postgres_instance_unsafe")

    def test_status_reads_where_postgres_listens(self) -> None:
        with _home() as paths:
            self.assertEqual(postgres.status(paths), {"state": "absent"})
            _instance(paths)
            self.assertEqual(postgres.status(paths), {"state": "stopped"})
            _postmaster(paths, "/tmp", "localhost")
            self.assertEqual(postgres.status(paths)["listen"], "tcp")
            _postmaster(paths, str(paths.postgres_socket_dir), "")
            self.assertEqual(postgres.status(paths), {"state": "running", "pid": os.getpid(), "listen": "socket_only",
                                                      "socket_dir": str(paths.postgres_socket_dir)})
            _postmaster(paths, "/tmp", "", pid=2**22 + 12345)
            self.assertEqual(postgres.status(paths), {"state": "stopped"})

    def test_database_url_names_the_socket_and_quotes_the_parts(self) -> None:
        with _home() as paths:
            _instance(paths, password="p@ss/word")
            url = postgres.database_url(paths, postgres.read_instance(paths))
            self.assertEqual(url, f"postgresql://hindsight:p%40ss%2Fword@/hindsight?host={paths.postgres_socket_dir}"
                                  "&port=54321")


class EnsureSocketOnlyTests(unittest.TestCase):
    def test_a_new_instance_is_made_with_a_random_password_and_runs_on_the_socket_only(self) -> None:
        with _home() as paths:
            made = []

            class Pg0:
                def __init__(self, name, username, password, database):
                    made.append((name, username, password, database))

                def start(self):
                    _instance(paths, password=made[-1][2])
                    _postmaster(paths, "/tmp", "localhost")

                def stop(self):
                    (paths.pg0_instance_dir / "data" / "postmaster.pid").unlink()

            pg_ctl = _PgCtl(paths)
            with patch.dict(sys.modules, {"pg0": SimpleNamespace(Pg0=Pg0)}), \
                    patch.object(postgres, "_pg_ctl", side_effect=pg_ctl):
                url = postgres.ensure_socket_only(paths)
            (name, username, password, database), = made
            self.assertEqual((name, username, database), ("hindsight-embed-lifeweave", "hindsight", "hindsight"))
            self.assertRegex(password, r"\A[0-9a-f]{64}\Z")
            self.assertEqual(url, f"postgresql://hindsight:{password}@/hindsight?host={paths.postgres_socket_dir}"
                                  "&port=54321")
            self.assertEqual([call[0] for call in pg_ctl.calls], ["start"])
            self.assertTrue(all(password not in " ".join(call) for call in pg_ctl.calls))
            options = shlex.split(pg_ctl.calls[0][pg_ctl.calls[0].index("-o") + 1])
            self.assertIn("listen_addresses=", options)
            self.assertIn(f'unix_socket_directories="{paths.postgres_socket_dir}"', options)
            self.assertEqual(postgres.status(paths)["listen"], "socket_only")
            for path, mode in ((paths.pg0_instance_dir, 0o700), (paths.pg0_instance_dir / "instance.json", 0o600),
                               (paths.postgres_socket_dir, 0o700)):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), mode, path)

    def test_a_postgres_that_pg0_started_on_tcp_moves_to_the_socket_and_keeps_its_password(self) -> None:
        with _home() as paths:
            _instance(paths, password="hindsight")
            _postmaster(paths, "/tmp", "localhost")
            pg_ctl = _PgCtl(paths)
            with patch.object(postgres, "_pg_ctl", side_effect=pg_ctl):
                url = postgres.ensure_socket_only(paths)
                self.assertEqual([call[0] for call in pg_ctl.calls], ["stop", "start"])
                self.assertIn("hindsight:hindsight@/hindsight", url)
                self.assertEqual(postgres.ensure_socket_only(paths), url)
                self.assertEqual(len(pg_ctl.calls), 2)
            self.assertEqual(json.loads((paths.pg0_instance_dir / "instance.json").read_text())["password"],
                             "hindsight")

    def test_restored_data_gets_its_binaries_from_a_throwaway_instance(self) -> None:
        with _home() as paths:
            _instance(paths, binaries=False)
            asked = []

            class Pg0:
                def __init__(self, name, username, password, database, config=None):
                    self.name = name
                    asked.append(("new", name, password, config))

                def start(self):
                    asked.append(("start", self.name))
                    (paths.home / ".pg0" / "installation" / "18.1.0" / "bin").mkdir(parents=True)
                    (paths.home / ".pg0" / "installation" / "18.1.0" / "bin" / "pg_ctl").write_text("")

                def drop(self, force=False):
                    asked.append(("drop", self.name, force))

            with patch.dict(sys.modules, {"pg0": SimpleNamespace(Pg0=Pg0)}), \
                    patch.object(postgres, "_pg_ctl", side_effect=_PgCtl(paths)):
                postgres.ensure_socket_only(paths)
            self.assertEqual([step[:2] for step in asked],
                             [("new", "lifecontext-unpack"), ("start", "lifecontext-unpack"),
                              ("drop", "lifecontext-unpack")])
            self.assertNotEqual(asked[0][2], PASSWORD)
            # The throwaway listens on no TCP address, only on the private socket directory.
            self.assertEqual(asked[0][3], {"listen_addresses": "",
                                           "unix_socket_directories": str(paths.postgres_socket_dir)})

    def test_failures_have_stable_codes_and_leave_the_data_alone(self) -> None:
        with _home() as paths:
            (paths.pg0_instance_dir / "data").mkdir(parents=True)
            with self.assertRaises(PostgresError) as caught:
                postgres.ensure_socket_only(paths)
            self.assertEqual(caught.exception.code, "postgres_instance_missing")
            _instance(paths)
            with patch.object(postgres, "_pg_ctl"), self.assertRaises(PostgresError) as caught:
                postgres.ensure_socket_only(paths)
            self.assertEqual(caught.exception.code, "postgres_start_failed")
            with patch.object(postgres, "_SOCKET_PATH_LIMIT", 103), \
                    patch.object(LifeContextPaths, "postgres_socket_dir", Path("/") / ("x" * 90)), \
                    self.assertRaises(PostgresError) as caught:
                postgres.ensure_socket_only(paths)
            self.assertEqual(caught.exception.code, "postgres_socket_path_too_long")
            # pg_ctl hands -D and -l to `sh -c` in double quotes only.
            with patch.object(LifeContextPaths, "postgres_socket_dir", paths.home / "a$(id)b"), \
                    self.assertRaises(PostgresError) as caught:
                postgres.ensure_socket_only(paths)
            self.assertEqual(caught.exception.code, "postgres_socket_path_unsupported")

    def test_a_pg0_start_whose_stop_fails_is_halted_not_left_on_tcp(self) -> None:
        with _home() as paths:
            class Pg0:
                def __init__(self, name, username, password, database):
                    pass

                def start(self):
                    _instance(paths)
                    _postmaster(paths, "/tmp", "localhost")

                def stop(self):
                    raise RuntimeError("pg0 could not stop it")

            halted = []
            with patch.dict(sys.modules, {"pg0": SimpleNamespace(Pg0=Pg0)}), \
                    patch.object(postgres, "_halt", side_effect=halted.append), \
                    self.assertRaises(PostgresError) as caught:
                postgres.ensure_socket_only(paths)
            self.assertEqual(caught.exception.code, "postgres_setup_failed")
            self.assertEqual(halted, [paths.pg0_instance_dir / "data"])
            # A pid file naming some other process is never signalled.
            self.assertFalse(postgres._is_postmaster(os.getpid(), paths.pg0_instance_dir / "data"))

    def test_stop_uses_pg_ctl_only_while_it_runs(self) -> None:
        with _home() as paths:
            self.assertFalse(postgres.stop(paths))
            _instance(paths)
            _postmaster(paths, str(paths.postgres_socket_dir), "")
            pg_ctl = _PgCtl(paths)
            with patch.object(postgres, "_pg_ctl", side_effect=pg_ctl):
                self.assertTrue(postgres.stop(paths))
                self.assertFalse(postgres.stop(paths))
            self.assertEqual([call[0] for call in pg_ctl.calls], ["stop"])


if __name__ == "__main__":
    unittest.main()
