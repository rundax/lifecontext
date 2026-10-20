"""Opt-in E2E proof through the installed CLI and real loopback service."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
import uuid
from unittest.mock import patch

import yaml

from lifecontext.config import load_settings
from lifecontext.credentials import load_credentials
from local_paths import V1_ROOT, scratch_root


@unittest.skipUnless(
    os.environ.get("LIFEWEAVE_E2E") == "1",
    "set LIFEWEAVE_E2E=1 to call the configured provider with synthetic test data",
)
class LifeContextServiceE2ETests(unittest.TestCase):
    def test_installed_cli_retains_and_reads_exact_evidence_through_service(self) -> None:
        dev_root = (V1_ROOT / ".dev").resolve(strict=True)
        home = dev_root / "home"
        with patch.dict(
            os.environ,
            {
                "HOME": str(home),
                "HERMES_HOME": str(home / "hermes-home-must-not-be-used"),
            },
        ):
            settings = load_settings()
            load_credentials()
        vault = settings.get("vault")
        self.assertIsInstance(vault, dict, "configure disposable vault paths before E2E")
        for key in ("root", "journal_path"):
            path = Path(vault[key]).resolve(strict=False)
            self.assertTrue(
                path.is_relative_to(dev_root),
                f"E2E refuses a {key} outside .dev",
            )

        environment = self._environment(home, scratch_root())
        cli = Path(sys.executable).with_name("lifecontext")
        self.assertTrue(cli.is_file(), "install V1 into the active virtual environment first")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]

        process: subprocess.Popen[bytes] | None = None
        try:
            process = subprocess.Popen(
                [str(cli), "serve", "--port", str(port)],
                cwd=V1_ROOT,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self._wait_for_health(process, port)

            status = self._run_json(cli, environment, [
                "status", "--port", str(port), "--json",
            ])
            self.assertTrue(status.get("ready"))
            self.assertTrue(status["dependencies"]["hindsight"]["ready"])
            self.assertTrue(status["dependencies"]["vault"]["ready"])

            token = uuid.uuid4().hex
            source_id = f"src_e2e_{token}"
            idempotency_key = f"e2e-retain-{token}"
            source = f"Synthetic LifeContext service E2E record {token}. No personal data.\n".encode()
            expected_sha256 = hashlib.sha256(source).hexdigest()
            with tempfile.TemporaryDirectory(
                prefix="lifeweave-e2e-source.", dir=scratch_root()
            ) as scratch:
                source_path = Path(scratch) / "synthetic-source.md"
                source_path.write_bytes(source)
                if os.name == "posix":
                    source_path.chmod(0o600)
                retain_args = [
                    "retain", source_id, "1", "--input", str(source_path),
                    "--idempotency-key", idempotency_key,
                    "--port", str(port), "--json",
                ]
                retained = self._run_json(cli, environment, retain_args, timeout=100)
                self.assertEqual(retained["source_id"], source_id)
                self.assertEqual(retained["source_version"], 1)
                self.assertEqual(retained["sha256"], expected_sha256)
                operation_id = retained["operation_id"]

                replayed = self._run_json(cli, environment, retain_args, timeout=100)
                self.assertEqual(replayed["operation_id"], operation_id)

            operation = self._run_json(cli, environment, [
                "operations", "get", operation_id, "--port", str(port), "--json",
            ])
            self.assertEqual(operation["state"], "completed")

            evidence = self._run_json(cli, environment, [
                "evidence", source_id, "--version", "1",
                "--port", str(port), "--json",
            ])
            self.assertEqual(evidence["source_id"], source_id)
            self.assertEqual(evidence["source_version"], 1)
            self.assertEqual(evidence["sha256"], expected_sha256)
            self.assertNotIn("content", evidence)
        finally:
            if process is not None and process.poll() is None:
                process.send_signal(signal.SIGINT)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
            if process is not None and process.returncode not in (0, None):
                self.fail(f"LifeContext service exited with code {process.returncode}")

    @staticmethod
    def _environment(home: Path, temporary: Path) -> dict[str, str]:
        environment = {
            "HOME": str(home),
            "HERMES_HOME": str(home / "hermes-home-must-not-be-used"),
            "PATH": os.pathsep.join((str(Path(sys.executable).parent), os.defpath)),
            "PYTHONPATH": str(V1_ROOT / "src"),
            "PYTHONNOUSERSITE": "1",
            "TMPDIR": str(temporary),
            "TEMP": str(temporary),
            "TMP": str(temporary),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_CACHE_HOME": str(home / ".cache"),
        }
        for name in (
            "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY",
            "http_proxy", "https_proxy", "no_proxy", "all_proxy",
        ):
            if name in os.environ:
                environment[name] = os.environ[name]
        return environment

    def _wait_for_health(self, process: subprocess.Popen[bytes], port: int) -> None:
        deadline = time.monotonic() + 120
        url = f"http://127.0.0.1:{port}/healthz"
        while time.monotonic() < deadline:
            if process.poll() is not None:
                self.fail(f"LifeContext service exited before readiness: {process.returncode}")
            try:
                with urllib.request.urlopen(url, timeout=1) as response:
                    if response.status == 200:
                        return
            except (OSError, urllib.error.URLError):
                time.sleep(0.2)
        self.fail("LifeContext service did not become healthy within 120 seconds")

    def _run_json(
        self,
        cli: Path,
        environment: dict[str, str],
        arguments: list[str],
        *,
        timeout: int = 20,
    ) -> dict[str, object]:
        result = subprocess.run(
            [str(cli), *arguments],
            cwd=V1_ROOT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        stdout = result.stdout.decode("utf-8", errors="replace")[:2000]
        stderr = result.stderr.decode("utf-8", errors="replace")[:1000]
        self.assertEqual(
            result.returncode,
            0,
            f"CLI command failed ({result.returncode}); stdout={stdout!r}, stderr={stderr!r}",
        )
        try:
            value = json.loads(stdout)
        except ValueError:
            self.fail("CLI did not return valid JSON")
        self.assertIsInstance(value, dict)
        return value


if __name__ == "__main__":
    unittest.main()
