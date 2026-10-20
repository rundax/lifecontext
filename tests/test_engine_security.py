"""Engine auth for the `lifeweave` Hindsight daemon: its token, its port and who listens there."""

from __future__ import annotations

import http.server
import os
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from local_paths import scratch_root

from lifecontext import hindsight
from lifecontext.hindsight import (
    HindsightStartupError,
    _allocate_port,
    _daemon_owner,
    _proc_listeners,
    _profile_values,
    _require_engine_auth,
    engine_report,
    engine_token,
    listener_owner,
    stop_profile,
)
from lifecontext.paths import LifeContextPaths

LINUX = Path("/proc/net/tcp").exists()


@contextmanager
def _home():
    with tempfile.TemporaryDirectory(prefix="lifecontext-engine-test.", dir=scratch_root()) as temporary:
        home = Path(temporary).resolve(strict=True)
        with patch.dict(os.environ, {"HOME": str(home)}):
            paths = LifeContextPaths.current()
            paths.ensure_private_home()
            yield paths


@contextmanager
def _daemon(check):
    """A loopback HTTP server that answers like the daemon's list-banks route, given the Authorization header."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append((self.path, self.headers.get("Authorization")))
            self.send_response(check(self.headers.get("Authorization")))
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args):
            pass

    seen: list = []
    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", seen
    finally:
        server.shutdown()
        server.server_close()


class EngineTokenTests(unittest.TestCase):
    def test_token_is_made_once_private_and_random(self) -> None:
        with _home() as paths:
            with self.assertRaises(HindsightStartupError) as caught:
                engine_token(paths)
            self.assertEqual(caught.exception.code, "engine_token_missing")
            token = engine_token(paths, create=True)
            self.assertRegex(token, r"\A[0-9a-f]{64}\Z")
            self.assertEqual(engine_token(paths, create=True), token)
            self.assertEqual(engine_token(paths), token)
            self.assertEqual(stat.S_IMODE(paths.hindsight_api_token_path.stat().st_mode), 0o600)
        with _home() as other:
            self.assertNotEqual(engine_token(other, create=True), token)

    def test_unsafe_or_malformed_tokens_are_refused(self) -> None:
        with _home() as paths:
            path = paths.hindsight_api_token_path
            for label, prepare in (
                ("readable", lambda: (path.write_text("a" * 64), os.chmod(path, 0o644))),
                ("short", lambda: (path.write_text("abc"), os.chmod(path, 0o600))),
                ("symlink", lambda: path.symlink_to(paths.lifeweave_home / "elsewhere")),
            ):
                with self.subTest(label):
                    prepare()
                    with self.assertRaises(HindsightStartupError) as caught:
                        engine_token(paths, create=True)
                    self.assertEqual(caught.exception.code, "unsafe_engine_token")
                    path.unlink()


class ProfileAndPortTests(unittest.TestCase):
    def test_profile_values_are_read_like_hindsight_embed_reads_them(self) -> None:
        with _home() as paths:
            self.assertEqual(_profile_values(paths), {})
            paths.hindsight_profiles_dir.mkdir(mode=0o700, parents=True)
            paths.hindsight_profile_env_path.write_text(
                "# comment\nexport HINDSIGHT_API_PORT = 24001\n\nHINDSIGHT_EMBED_API_DATABASE_URL="
                "postgresql://u:p@/db?host=/s&port=5\nnot a setting\n")
            self.assertEqual(_profile_values(paths), {
                "HINDSIGHT_API_PORT": "24001",
                "HINDSIGHT_EMBED_API_DATABASE_URL": "postgresql://u:p@/db?host=/s&port=5"})

    def test_port_is_chosen_per_home_and_skips_busy_ports(self) -> None:
        with _home() as first:
            with patch.object(hindsight, "_port_is_free", return_value=True):
                port = _allocate_port(first)
                self.assertEqual(_allocate_port(first), port)
            self.assertTrue(20000 <= port < 30000)
            self.assertNotEqual(port, 9855)
            busy = {port, port + 1}
            with patch.object(hindsight, "_port_is_free", side_effect=lambda candidate: candidate not in busy):
                self.assertEqual(_allocate_port(first), port + 2 if port + 2 < 30000 else 20000 + (port + 2 - 30000))
            with patch.object(hindsight, "_port_is_free", return_value=False), \
                    self.assertRaises(HindsightStartupError) as caught:
                _allocate_port(first)
            self.assertEqual(caught.exception.code, "daemon_port_unavailable")
        with _home() as second, patch.object(hindsight, "_port_is_free", return_value=True):
            self.assertNotEqual(_allocate_port(second), port)


class ListenerOwnerTests(unittest.TestCase):
    def test_proc_tables_give_each_listeners_owner(self) -> None:
        with tempfile.TemporaryDirectory(dir=scratch_root()) as temporary:
            tcp = Path(temporary) / "tcp"
            header = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
            tcp.write_text(header
                           + "   0: 0100007F:2673 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1001        0 4242\n"
                           + "   1: 0100007F:2674 0100007F:9C40 01 00000000:00000000 00:00000000 00000000  1000        0 4343\n")
            tables = (tcp, Path(temporary) / "tcp6-missing")
            with patch.object(hindsight, "_PROC_TCP", tables):
                self.assertEqual(_proc_listeners(9843), [(1001, 4242)])
                self.assertEqual(_proc_listeners(9844), [])
                with patch.object(hindsight.os, "geteuid", return_value=1001):
                    self.assertEqual(listener_owner(9843), "ours")
                with patch.object(hindsight.os, "geteuid", return_value=1000):
                    self.assertEqual(listener_owner(9843), "foreign")
                self.assertEqual(listener_owner(9844), "none")

    def test_without_procfs_an_unlisted_listener_is_someone_elses(self) -> None:
        missing = (Path("/nonexistent/tcp"), Path("/nonexistent/tcp6"))
        with patch.object(hindsight, "_PROC_TCP", missing):
            with patch.object(hindsight, "_lsof_listeners", return_value={}), \
                    patch.object(hindsight, "_port_answers", return_value=True):
                self.assertEqual(listener_owner(24001), "foreign")
            with patch.object(hindsight, "_lsof_listeners", return_value=None), \
                    patch.object(hindsight, "_port_answers", return_value=False):
                self.assertEqual(listener_owner(24001), "none")
            with patch.object(hindsight, "_lsof_listeners", return_value={4242: os.geteuid()}):
                self.assertEqual(listener_owner(24001), "ours")
                self.assertEqual(hindsight._listener_pids(24001), {4242})
            with patch.object(hindsight, "_lsof_listeners", return_value={4242: os.geteuid() + 1}):
                self.assertEqual(listener_owner(24001), "foreign")
                self.assertEqual(hindsight._listener_pids(24001), set())

    @unittest.skipUnless(LINUX, "procfs sockets and environments are Linux")
    def test_a_real_listener_is_this_homes_daemon_only_with_this_homes_log(self) -> None:
        with _home() as paths:
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            self.assertEqual(_daemon_owner(port, paths), "none")
            script = ("import socket, sys, time\ns = socket.socket()\ns.bind(('127.0.0.1', int(sys.argv[1])))\n"
                      "s.listen()\nprint('ready', flush=True)\ntime.sleep(60)\n")
            for log, expected in ((paths.hindsight_profiles_dir / "lifeweave.log", "here"),
                                  (paths.home / "other-home" / ".hindsight/profiles/lifeweave.log", "elsewhere"),
                                  (None, "elsewhere")):
                environment = {"PATH": os.defpath}
                if log is not None:
                    environment["HINDSIGHT_API_DAEMON_LOG"] = str(log)
                with self.subTest(expected=expected, log=log), subprocess.Popen(
                        [sys.executable, "-c", script, str(port)], env=environment,
                        stdout=subprocess.PIPE, text=True) as process:
                    try:
                        self.assertEqual(process.stdout.readline().strip(), "ready")
                        self.assertEqual(listener_owner(port), "ours")
                        self.assertEqual(_daemon_owner(port, paths), expected)
                        self.assertEqual(hindsight._listener_pids(port), {process.pid})
                    finally:
                        process.kill()


class StopDaemonTests(unittest.TestCase):
    """hindsight-embed waits 5 s for the daemon; LifeContext waits for the process to exit."""

    def test_a_slow_daemon_counts_as_stopped_once_its_process_is_gone(self) -> None:
        alive = [True, True, False]
        with patch.object(hindsight, "_listener_pids", return_value={4242}), \
                patch.object(hindsight, "_process_exists", side_effect=lambda pid: alive.pop(0)), \
                patch.object(hindsight.time, "sleep"):
            hindsight._stop_daemon(lambda profile=None: False, 24001)
        self.assertEqual(alive, [])

    def test_a_daemon_that_does_not_exit_fails_with_a_stable_code(self) -> None:
        for pids, stop_result, port in (({4242}, True, 24001), (set(), False, None)):
            with self.subTest(pids=pids, port=port), \
                    patch.object(hindsight, "_listener_pids", return_value=pids), \
                    patch.object(hindsight, "_process_exists", return_value=True), \
                    patch.object(hindsight, "_DAEMON_STOP_TIMEOUT", 0.01), \
                    self.assertRaises(HindsightStartupError) as caught:
                hindsight._stop_daemon(lambda profile=None, result=stop_result: result, port)
            self.assertEqual(caught.exception.code, "daemon_stop_failed")
        with patch.object(hindsight, "_listener_pids", return_value=set()), \
                patch.object(hindsight, "_port_answers", return_value=False):
            hindsight._stop_daemon(lambda profile=None: False, 24001)

    def test_an_exited_child_is_reaped_rather_than_waited_for(self) -> None:
        process = subprocess.Popen([sys.executable, "-c", "pass"])
        try:
            deadline = 50
            while hindsight._process_exists(process.pid) and deadline:
                deadline -= 1
                threading.Event().wait(0.1)
            self.assertFalse(hindsight._process_exists(process.pid))
        finally:
            process.returncode = 0  # Already reaped; keeps Popen from waiting again.


class EngineAuthProbeTests(unittest.TestCase):
    TOKEN = "f" * 64

    def test_a_daemon_must_refuse_without_the_token_and_accept_it(self) -> None:
        good = lambda header: 200 if header == f"Bearer {self.TOKEN}" else 401
        with _daemon(good) as (base_url, seen):
            _require_engine_auth(base_url, self.TOKEN)
        self.assertEqual(seen, [("/v1/default/banks?limit=0", None),
                                ("/v1/default/banks?limit=0", f"Bearer {self.TOKEN}")])
        for label, answer, code in (("open", lambda header: 200, "engine_auth_disabled"),
                                    ("other token", lambda header: 401, "engine_auth_rejected")):
            with self.subTest(label), _daemon(answer) as (base_url, _seen), \
                    self.assertRaises(HindsightStartupError) as caught:
                _require_engine_auth(base_url, self.TOKEN)
            self.assertEqual(caught.exception.code, code)

    def test_the_probe_never_goes_through_a_proxy(self) -> None:
        good = lambda header: 200 if header == f"Bearer {self.TOKEN}" else 401
        with _daemon(good) as (base_url, seen), patch.dict(os.environ, {
                "http_proxy": "http://127.0.0.1:9", "HTTP_PROXY": "http://127.0.0.1:9", "no_proxy": ""}):
            _require_engine_auth(base_url, self.TOKEN)
        self.assertEqual(len(seen), 2)


class StopAndReportTests(unittest.TestCase):
    def _profile(self, paths: LifeContextPaths, text: str) -> None:
        paths.hindsight_profiles_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(paths.hindsight_home, 0o700)
        paths.hindsight_profile_env_path.write_text(text)
        os.chmod(paths.hindsight_profile_env_path, 0o600)

    def test_stop_leaves_another_owners_daemon_alone_and_stops_postgres(self) -> None:
        with _home() as paths:
            self._profile(paths, "HINDSIGHT_API_PORT=24001\n")
            asked, stopped = [], []
            client = SimpleNamespace(stop_daemon=lambda profile=None: asked.append(profile) or True)
            for owner, expected in (("elsewhere", []), ("here", ["lifeweave"]), ("none", [])):
                asked.clear()
                with self.subTest(owner=owner), patch.object(hindsight, "_load_runtime"), \
                        patch.dict(sys.modules, {"hindsight_embed.daemon_client": client}), \
                        patch.object(hindsight, "_daemon_owner", return_value=owner), \
                        patch.object(hindsight, "_listener_pids", return_value=set()), \
                        patch.object(hindsight.hindsight_postgres, "stop", side_effect=lambda p: stopped.append(p)):
                    self.assertEqual(stop_profile(paths), "stopped")
                    self.assertEqual(asked, expected)
            self.assertEqual(len(stopped), 3)

    def test_report_names_auth_owner_and_postgres_without_secrets(self) -> None:
        with _home() as paths:
            self.assertEqual(engine_report(paths)["profile"], "missing")
            token = engine_token(paths, create=True)
            self._profile(paths, "HINDSIGHT_API_PORT=24001\nHINDSIGHT_API_LLM_API_KEY=provider-secret\n")
            with patch.object(hindsight, "_daemon_owner", return_value="none"):
                legacy = engine_report(paths)
            self.assertEqual((legacy["auth_configured"], legacy["daemon"], legacy["port"]),
                             (False, "not_running", 24001))
            self.assertFalse(legacy["postgres"]["configured_socket_only"])
            self._profile(paths, (
                "HINDSIGHT_API_PORT=24001\n"
                "HINDSIGHT_API_TENANT_EXTENSION=hindsight_api.extensions.builtin.tenant:ApiKeyTenantExtension\n"
                f"HINDSIGHT_API_TENANT_API_KEY={token}\nHINDSIGHT_API_LLM_API_KEY=provider-secret\n"
                f"HINDSIGHT_EMBED_API_DATABASE_URL=postgresql://hindsight:pg-secret@/hindsight?"
                f"host={paths.postgres_socket_dir}&port=5432\n"))
            with patch.object(hindsight, "_daemon_owner", return_value="here"), \
                    patch.object(hindsight, "_http_status", return_value=401) as status:
                report = engine_report(paths)
            status.assert_called_once_with("http://127.0.0.1:24001/v1/default/banks?limit=0", None)
            self.assertEqual((report["auth_configured"], report["token_matches_profile"], report["auth_enforced"],
                              report["daemon"]), (True, True, True, "this_home"))
            self.assertTrue(report["postgres"]["configured_socket_only"])
            for secret in (token, "pg-secret", "provider-secret"):
                self.assertNotIn(secret, repr(report))
            with patch.object(hindsight, "_daemon_owner", return_value="elsewhere"):
                self.assertEqual(engine_report(paths)["daemon"], "other_owner")


if __name__ == "__main__":
    unittest.main()
