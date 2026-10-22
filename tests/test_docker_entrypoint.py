"""docker/entrypoint.sh with `lifecontext` stubbed: without a provider and a model it waits, never exits."""

from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = ROOT / "docker" / "entrypoint.sh"
REPORT = {"provider": "anthropic", "model": "claude-sonnet-5", "config_state": "written",
          "credentials_state": "missing", "vault_root": "/data/vault"}


class EntrypointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="lifecontext-entrypoint-test.")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.home = root / "home"
        self.home.mkdir()
        stubs = root / "stubs"
        stubs.mkdir()
        self.calls = root / "calls.log"
        lifecontext = stubs / "lifecontext"
        lifecontext.write_text(
            f'#!/bin/sh\necho "$*" >> "{self.calls}"\n'
            f"""case "$1" in init) printf '%s' '{json.dumps(REPORT)}' ;; hindsight) echo '{{}}' ;; esac\n""")
        lifecontext.chmod(0o755)
        os.symlink(sys.executable, stubs / "python")
        self.env = {"HOME": str(self.home), "PATH": f"{stubs}:/usr/bin:/bin",
                    "LIFECONTEXT_API_KEY_FILE": str(root / "no-such-secret")}

    def start(self, **environ: str) -> subprocess.Popen:
        process = subprocess.Popen(["/bin/sh", str(ENTRYPOINT)], env={**self.env, **environ},
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.addCleanup(process.stderr.close)
        self.addCleanup(lambda: process.poll() is not None or process.kill())
        return process

    def read_until(self, process: subprocess.Popen, text: str, seconds: float = 30) -> str:
        """stderr until ``text`` appears; fails if the process exits first."""
        seen = b""
        deadline = time.monotonic() + seconds
        while text.encode() not in seen:
            self.assertLess(time.monotonic(), deadline, seen)
            if select.select([process.stderr], [], [], 0.5)[0]:
                chunk = os.read(process.stderr.fileno(), 4096)
                self.assertTrue(chunk, f"the entrypoint exited: {seen!r}")
                seen += chunk
        return seen.decode()

    def logged(self) -> list[str]:
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def test_without_provider_and_model_it_waits_for_a_config_and_then_serves(self) -> None:
        process = self.start()
        log = self.read_until(process, "waiting for ~/.lifeweave/config.yaml")
        self.assertIn("LIFECONTEXT_PROVIDER and LIFECONTEXT_MODEL are not set", log)
        self.assertIn("docker exec -it CONTAINER lifecontext init", log)
        time.sleep(1)
        self.assertIsNone(process.poll())
        self.assertEqual(self.logged(), [])  # init never ran, so no restart loop
        (self.home / ".lifeweave").mkdir(mode=0o700)
        (self.home / ".lifeweave" / "config.yaml").write_text("llm: {}\n")
        self.assertEqual(process.wait(timeout=30), 0)
        self.assertEqual(self.logged(), ["serve --port 8765", "hindsight stop --json"])

    def test_a_missing_model_alone_is_named_and_docker_stop_ends_the_wait(self) -> None:
        for environ, missing in (({"LIFECONTEXT_PROVIDER": "anthropic"}, "LIFECONTEXT_MODEL is not set"),
                                 ({"LIFECONTEXT_MODEL": "claude-sonnet-5"}, "LIFECONTEXT_PROVIDER is not set")):
            with self.subTest(missing=missing):
                process = self.start(**environ)
                self.assertIn(missing, self.read_until(process, "waiting for"))
                process.send_signal(signal.SIGTERM)
                self.assertEqual(process.wait(timeout=30), 0)
                self.assertEqual(self.logged(), [])

    def test_with_provider_and_model_the_first_start_runs_init(self) -> None:
        process = self.start(LIFECONTEXT_PROVIDER="anthropic", LIFECONTEXT_MODEL="claude-sonnet-5")
        self.assertEqual(process.wait(timeout=30), 0, process.stderr.read())
        self.assertEqual(self.logged(), ["init --non-interactive --json --vault /data/vault",
                                         "serve --port 8765", "hindsight stop --json"])


if __name__ == "__main__":
    unittest.main()
