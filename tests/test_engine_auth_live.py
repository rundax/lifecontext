"""Engine auth and socket-only Postgres against the real Hindsight daemon and pg0 (CI, or opt in).

Starts real daemons in throwaway HOMEs: GitHub Actions runs this in the test job;
elsewhere set LIFECONTEXT_ENGINE_LIVE=1. No LLM is called: the daemon skips its
LLM check and reranks without a model; it downloads the embedding model once.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import socket
import stat
import tempfile
import unittest
import urllib.error
import urllib.request
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from local_paths import scratch_root

from lifecontext import hindsight, hindsight_postgres
from lifecontext.hindsight import (
    PROFILE,
    _load_runtime,
    _validated_profile_config,
    engine_report,
    engine_token,
    preflight,
    stop_profile,
)
from lifecontext.paths import LifeContextPaths

LIVE = os.environ.get("GITHUB_ACTIONS") == "true" or os.environ.get("LIFECONTEXT_ENGINE_LIVE") == "1"
SETTINGS = {"llm": {"provider": "openai_compatible", "model": "live-test", "base_url": "http://127.0.0.1:9/v1"}}
BANKS = "/v1/default/banks"


def _request(url: str, *, token: str | None = None, method: str = "GET", body: bytes | None = None
             ) -> tuple[int | None, bytes]:
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(urllib.request.Request(url, data=body, method=method, headers=headers), timeout=60) as reply:
            return reply.status, reply.read()
    except urllib.error.HTTPError as exc:
        with exc:
            return exc.code, exc.read()
    except OSError:
        return None, b""


def _refuses_tcp(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(2)
        return probe.connect_ex(("127.0.0.1", port)) != 0


def _command_lines() -> list[bytes]:
    lines = []
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            try:
                if entry.stat().st_uid == os.geteuid():
                    lines.append((entry / "cmdline").read_bytes())
            except OSError:
                continue
    return lines


@unittest.skipUnless(LIVE, "starts real Hindsight daemons; runs in CI or with LIFECONTEXT_ENGINE_LIVE=1")
@unittest.skipUnless(Path("/proc/net/tcp").exists(), "checks listeners and command lines through procfs")
class EngineAuthLiveTests(unittest.TestCase):
    @contextmanager
    def _home(self):
        # Short: the Postgres socket path must stay under 104 bytes.
        home = Path(tempfile.mkdtemp(prefix="lc.", dir=scratch_root())).resolve(strict=True)
        # One model download for both tests; the cache is not part of what is checked.
        models = scratch_root() / "engine-live-hf"
        models.mkdir(mode=0o700, exist_ok=True)
        environment = {"HOME": str(home), "HERMES_HOME": str(home / "no-hermes"), "HF_HOME": str(models),
                       "HINDSIGHT_API_SKIP_LLM_VERIFICATION": "true", "HINDSIGHT_API_RERANKER_PROVIDER": "rrf"}
        try:
            with patch.dict(os.environ, environment):
                paths = LifeContextPaths.current()
                paths.ensure_private_home()
                try:
                    yield paths
                finally:
                    try:
                        stop_profile(paths)
                    finally:
                        hindsight_postgres.stop(paths)
        finally:
            shutil.rmtree(home, ignore_errors=True)

    def _assert_locked_down(self, paths: LifeContextPaths, base_url: str) -> str:
        token = engine_token(paths)
        self.assertEqual(_request(base_url + "/health")[0], 200)
        self.assertEqual(_request(base_url + BANKS)[0], 401)
        self.assertEqual(_request(base_url + BANKS, token="0" * 64)[0], 401)
        self.assertEqual(_request(base_url + BANKS + "/x/memories/recall", method="POST",
                                  body=b'{"query": "q"}')[0], 401)
        status, _body = _request(base_url + BANKS, token=token)
        self.assertEqual(status, 200)
        postgres = hindsight_postgres.status(paths)
        self.assertEqual((postgres["state"], postgres["listen"], postgres["socket_dir"]),
                         ("running", "socket_only", str(paths.postgres_socket_dir)))
        instance = hindsight_postgres.read_instance(paths)
        self.assertTrue(_refuses_tcp(instance.port))
        for path, mode in ((paths.postgres_socket_dir, 0o700), (paths.pg0_instance_dir, 0o700),
                           (paths.pg0_instance_dir / "instance.json", 0o600),
                           (paths.hindsight_api_token_path, 0o600), (paths.hindsight_profile_env_path, 0o600)):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), mode, path)
        report = engine_report(paths)
        self.assertEqual((report["auth_enforced"], report["daemon"], report["postgres"]["listen"]),
                         (True, "this_home", "socket_only"))
        self.assertNotIn(token, json.dumps(report))
        return token

    def test_a_new_install_requires_the_token_and_keeps_postgres_off_tcp(self) -> None:
        with self._home() as paths:
            report = preflight(SETTINGS)
            self.assertEqual((report["engine_auth"], report["postgres"]), ("enforced", "socket_only"))
            port = int(report["base_url"].rsplit(":", 1)[1])
            self.assertTrue(20000 <= port < 30000)
            token = self._assert_locked_down(paths, report["base_url"])
            password = hindsight_postgres.read_instance(paths).password
            self.assertRegex(password, r"\A[0-9a-f]{64}\Z")
            for line in _command_lines():
                self.assertNotIn(token.encode(), line)
                self.assertNotIn(password.encode(), line)

            runtime = _load_runtime()
            client = runtime.Hindsight(base_url=report["base_url"], api_key=token, timeout=60)
            try:
                client.create_bank("live-client")
            finally:
                client.close()
            self.assertIn(b"live-client", _request(report["base_url"] + BANKS, token=token)[1])

            again = preflight(SETTINGS)
            self.assertEqual(again["base_url"], report["base_url"])
            self.assertEqual(engine_token(paths), token)

            self.assertEqual(stop_profile(paths), "stopped")
            self.assertTrue(_refuses_tcp(port))
            self.assertEqual(hindsight_postgres.status(paths)["state"], "stopped")

    def test_an_install_from_before_engine_auth_is_migrated_with_its_data(self) -> None:
        with self._home() as paths:
            # What LifeContext 1.1 left: hindsight-embed's own pg0 Postgres on TCP, no engine auth.
            runtime = _load_runtime()
            hindsight._ensure_private_profile_directories(paths)
            hindsight._prepare_profile_file(paths)
            legacy = _validated_profile_config(SETTINGS, {})
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                runtime.ProfileManager().create_profile(PROFILE, legacy)
                self.assertTrue(runtime.ensure_daemon_running(legacy, profile=PROFILE))
            old_url = runtime.get_daemon_url(profile=PROFILE)
            self.assertEqual(_request(old_url + BANKS)[0], 200)  # Open to every local user.
            created = _request(old_url + BANKS + "/live-legacy", method="PUT", body=b"{}")
            self.assertEqual(created[0], 200, created[1][:300])
            self.assertEqual(hindsight_postgres.status(paths)["listen"], "tcp")

            report = preflight(SETTINGS)
            self.assertNotEqual(report["base_url"], old_url)
            self.assertTrue(_refuses_tcp(int(old_url.rsplit(":", 1)[1])))
            token = self._assert_locked_down(paths, report["base_url"])
            self.assertIn(b"live-legacy", _request(report["base_url"] + BANKS, token=token)[1])
            self.assertEqual(hindsight_postgres.read_instance(paths).password, "hindsight")


if __name__ == "__main__":
    unittest.main()
