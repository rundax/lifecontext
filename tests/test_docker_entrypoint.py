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
REFUSED = {"error": "model_unsupported", "message": "claude-opus-5-5 ... do not work with LifeContext yet"}


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
        # What `lifecontext init` answers, and the key state `lifecontext llm show` reports.
        self.init_report = root / "init.json"
        self.init_report.write_text(json.dumps(REPORT))
        self.key_state = root / "llm.json"
        lifecontext = stubs / "lifecontext"
        # `init --replace-key` stores the key it is given.
        lifecontext.write_text(
            f'#!/bin/sh\ncase "$1" in llm) cat "{self.key_state}" 2>/dev/null; exit 0 ;; esac\n'
            f'echo "$*" >> "{self.calls}"\n'
            f"""case "$*" in *--replace-key*) echo '{{"api_key": "set"}}' > "{self.key_state}" ;; esac\n"""
            f"""case "$1" in init) cat "{self.init_report}" ;; hindsight) echo '{{}}' ;; esac\n""")
        lifecontext.chmod(0o755)
        os.symlink(sys.executable, stubs / "python")
        self.env = {"HOME": str(self.home), "PATH": f"{stubs}:/usr/bin:/bin",
                    "LIFECONTEXT_API_KEY_FILE": str(root / "no-such-secret")}
        self.secret = root / "secret"

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

    def write_config(self) -> None:
        (self.home / ".lifeweave").mkdir(mode=0o700, exist_ok=True)
        (self.home / ".lifeweave" / "config.yaml").write_text("llm: {}\n")

    def test_without_provider_and_model_it_waits_for_the_config_and_the_key_then_serves(self) -> None:
        process = self.start()
        log = self.read_until(process, "waiting for ~/.lifeweave/config.yaml")
        self.assertIn("LIFECONTEXT_PROVIDER and LIFECONTEXT_MODEL are not set", log)
        self.assertIn("docker exec -it CONTAINER lifecontext init", log)
        time.sleep(1)
        self.assertIsNone(process.poll())
        self.assertEqual(self.logged(), [])  # init never ran, so no restart loop
        # `lifecontext init` in the container writes config.yaml before it asks for the key.
        self.key_state.write_text('{"api_key": "missing"}')
        self.write_config()
        self.read_until(process, "waiting for the model key")
        self.assertIsNone(process.poll())
        self.key_state.write_text('{"api_key": "set"}')
        self.assertEqual(process.wait(timeout=30), 0)
        self.assertEqual(self.logged(), ["serve --port 8765", "hindsight stop --json"])

    def test_a_key_secret_is_stored_once_the_wait_ends(self) -> None:
        self.secret.write_text("sk-secret\n")
        process = self.start(LIFECONTEXT_API_KEY_FILE=str(self.secret))
        self.read_until(process, "waiting for ~/.lifeweave/config.yaml")
        self.write_config()
        self.assertEqual(process.wait(timeout=30), 0)
        self.assertEqual(self.logged(), ["init --non-interactive --replace-key --json",
                                         "serve --port 8765", "hindsight stop --json"])

    def test_a_refused_model_shows_why_and_waits_instead_of_exiting(self) -> None:
        self.init_report.write_text(json.dumps(REFUSED))
        process = self.start(LIFECONTEXT_PROVIDER="anthropic", LIFECONTEXT_MODEL="claude-opus-5-5")
        log = self.read_until(process, "waiting for ~/.lifeweave/config.yaml")
        self.assertIn("lifecontext init failed: claude-opus-5-5 ... do not work with LifeContext yet", log)
        time.sleep(1)
        self.assertIsNone(process.poll())
        process.send_signal(signal.SIGTERM)
        self.assertEqual(process.wait(timeout=30), 0)
        self.assertEqual(self.logged(), ["init --non-interactive --json --vault /data/vault"])

    def test_a_missing_model_alone_is_named_and_docker_stop_ends_the_wait(self) -> None:
        for environ, missing in (({"LIFECONTEXT_PROVIDER": "anthropic"}, "LIFECONTEXT_MODEL is not set"),
                                 ({"LIFECONTEXT_MODEL": "claude-sonnet-5"}, "LIFECONTEXT_PROVIDER is not set")):
            with self.subTest(missing=missing):
                process = self.start(**environ)
                self.assertIn(missing, self.read_until(process, "waiting for"))
                process.send_signal(signal.SIGTERM)
                self.assertEqual(process.wait(timeout=30), 0)
                self.assertEqual(self.logged(), [])

    def test_with_provider_model_and_key_the_first_start_runs_init_and_serves(self) -> None:
        self.secret.write_text("sk-secret\n")
        self.key_state.write_text('{"api_key": "set"}')  # what init stored from the secret
        process = self.start(LIFECONTEXT_PROVIDER="anthropic", LIFECONTEXT_MODEL="claude-sonnet-5",
                             LIFECONTEXT_API_KEY_FILE=str(self.secret))
        self.assertEqual(process.wait(timeout=30), 0, process.stderr.read())
        self.assertEqual(self.logged(), ["init --non-interactive --json --vault /data/vault",
                                         "serve --port 8765", "hindsight stop --json"])

    def test_without_a_key_it_waits_instead_of_serving_also_after_a_restart(self) -> None:
        self.key_state.write_text('{"api_key": "missing"}')
        native = {"LIFECONTEXT_PROVIDER": "anthropic", "LIFECONTEXT_MODEL": "claude-sonnet-5"}
        for start, expected_calls in (("first", ["init --non-interactive --json --vault /data/vault"]),
                                      ("restart", [])):  # config.yaml is there now: no init, still no serve
            with self.subTest(start=start):
                self.calls.unlink(missing_ok=True)
                if start == "restart":
                    self.write_config()
                process = self.start(**native)
                log = self.read_until(process, "waiting for the model key")
                self.assertIn("no model key is stored, and there is no key file at", log)
                time.sleep(1)
                self.assertIsNone(process.poll())
                process.send_signal(signal.SIGTERM)
                self.assertEqual(process.wait(timeout=30), 0)
                self.assertEqual(self.logged(), expected_calls)

    def test_an_empty_key_file_is_no_key_and_is_stored_once_it_has_content(self) -> None:
        self.key_state.write_text('{"api_key": "missing"}')
        self.secret.write_text("")
        process = self.start(LIFECONTEXT_PROVIDER="anthropic", LIFECONTEXT_MODEL="claude-sonnet-5",
                             LIFECONTEXT_API_KEY_FILE=str(self.secret))
        log = self.read_until(process, "waiting for the model key")
        self.assertIn(f"the key file {self.secret} is empty", log)
        time.sleep(1)
        self.assertIsNone(process.poll())
        self.secret.write_text("sk-secret\n")
        self.assertEqual(process.wait(timeout=30), 0)
        self.assertEqual(self.logged(), ["init --non-interactive --json --vault /data/vault",
                                         "init --non-interactive --replace-key --json",
                                         "serve --port 8765", "hindsight stop --json"])

    def test_a_loopback_endpoint_that_needs_no_key_serves_without_one(self) -> None:
        self.key_state.write_text('{"api_key": "not_required"}')
        process = self.start(LIFECONTEXT_PROVIDER="openai_compatible", LIFECONTEXT_MODEL="llama3.3",
                             LIFECONTEXT_BASE_URL="http://127.0.0.1:11434/v1")
        self.assertEqual(process.wait(timeout=30), 0, process.stderr.read())
        self.assertEqual(self.logged(), ["init --non-interactive --json --vault /data/vault",
                                         "serve --port 8765", "hindsight stop --json"])


if __name__ == "__main__":
    unittest.main()
