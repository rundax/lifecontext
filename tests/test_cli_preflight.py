"""Subprocess checks for config-first preflight error ordering."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from local_paths import scratch_root


V1_ROOT = Path(__file__).resolve().parents[1]


class CliPreflightTests(unittest.TestCase):
    def _run(self, yaml_text: str, *arguments: str) -> tuple[subprocess.CompletedProcess[str], Path]:
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
                [sys.executable, "-m", "lifecontext.cli", "preflight", *arguments],
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

    def test_model_probe_runs_only_the_probe_and_exits_3_on_a_failure(self) -> None:
        result, _ = self._run("llm:\n  provider: openai\n  model: test\n", "--model-probe")
        self.assertEqual(result.returncode, 3, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(list(report), ["model_probe"])
        self.assertEqual(report["model_probe"]["cause"], "key_missing")
        self.assertEqual(report["model_probe"]["message"], "no API key is stored for this provider")
        self.assertIn("lifecontext llm set", report["model_probe"]["fix"])
        self.assertNotIn("ambient-secret", result.stdout + result.stderr)
        broken, _ = self._run("hindsight: [secret-marker\n", "--model-probe")
        self.assertEqual((broken.returncode, json.loads(broken.stdout)["model_probe"]["cause"]),
                         (3, "invalid_config"))
        self.assertNotIn("secret-marker", broken.stdout + broken.stderr)

    def test_model_probe_never_starts_the_memory_engine(self) -> None:
        from lifecontext import cli

        passed = {"status": "pass", "cause": None, "reflect": {"status": "pass"}}
        out = io.StringIO()
        with tempfile.TemporaryDirectory(prefix="lifeweave-cli-test.", dir=scratch_root()) as temporary, \
                patch.dict(os.environ, {"HOME": temporary}), \
                patch("lifecontext.config.load_settings", return_value={"llm": {}}), \
                patch("lifecontext.hindsight.preflight", side_effect=AssertionError("engine started")), \
                patch("lifecontext.hindsight.retain_smoke", side_effect=AssertionError("engine started")), \
                patch("lifecontext.hindsight.model_probe", return_value=passed), redirect_stdout(out):
            code = cli.main(["preflight", "--model-probe"])
        self.assertEqual((code, json.loads(out.getvalue())), (0, {"model_probe": passed}))

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
