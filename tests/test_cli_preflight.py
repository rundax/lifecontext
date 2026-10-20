"""Subprocess checks for config-first preflight error ordering."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from local_paths import scratch_root


V1_ROOT = Path(__file__).resolve().parents[1]


class CliPreflightTests(unittest.TestCase):
    def _run(self, yaml_text: str) -> tuple[subprocess.CompletedProcess[str], Path]:
        with tempfile.TemporaryDirectory(
            prefix="lifeweave-cli-test.", dir=scratch_root()
        ) as temporary:
            home = Path(temporary).resolve(strict=True)
            app_home = home / ".lifeweave"
            app_home.mkdir(mode=0o700)
            config_path = app_home / "config.yaml"
            config_path.write_text(yaml_text, encoding="utf-8")
            config_path.chmod(0o600)
            hermes_home = home / "poisoned-hermes-home"
            hermes_home.mkdir()
            (hermes_home / ".env").write_text(
                "LIFEWEAVE_LLM_API_KEY=poisoned-hermes-secret\n", encoding="utf-8"
            )
            env = {
                "HOME": str(home),
                "HERMES_HOME": str(hermes_home),
                "LIFEWEAVE_LLM_API_KEY": "ambient-secret-must-not-load",
                "PATH": os.defpath,
                "PYTHONPATH": str(V1_ROOT / "src"),
                "TMPDIR": str(scratch_root()),
                "TEMP": str(scratch_root()),
                "TMP": str(scratch_root()),
            }
            result = subprocess.run(
                [sys.executable, "-m", "lifecontext.cli", "preflight"],
                cwd=home,
                env=env,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            # Copy only result data out before TemporaryDirectory cleanup.
            return result, home

    def test_valid_yaml_reaches_missing_credentials_without_fallback(self) -> None:
        result, _ = self._run("llm:\n  provider: openai\n  model: test\n")
        self.assertNotEqual(result.returncode, 0)
        report = json.loads(result.stdout)
        self.assertEqual(report["error"], "missing_credentials")
        self.assertNotIn("ambient-secret", result.stdout + result.stderr)
        self.assertNotIn("poisoned-hermes-secret", result.stdout + result.stderr)

    def test_malformed_yaml_fails_before_credentials_or_hindsight(self) -> None:
        result, _ = self._run("hindsight: [secret-marker\n")
        self.assertNotEqual(result.returncode, 0)
        report = json.loads(result.stdout)
        self.assertEqual(report["error"], "invalid_config")
        self.assertNotIn("secret-marker", result.stdout + result.stderr)
        self.assertNotIn("poisoned-hermes-secret", result.stdout + result.stderr)

    def test_legacy_hindsight_settings_are_rejected_before_credentials(self) -> None:
        result, _ = self._run(
            "hindsight:\n  llm_provider: openai\n  llm_model: model\n"
        )
        self.assertNotEqual(result.returncode, 0)
        report = json.loads(result.stdout)
        self.assertEqual(report["error"], "invalid_config")
        self.assertNotIn("poisoned-hermes-secret", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
