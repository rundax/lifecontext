"""`lifecontext doctor`: read-only diagnostics, exit codes and no private content."""

from __future__ import annotations

import io
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from local_paths import scratch_root

from lifecontext.doctor import render, run_doctor
from lifecontext.hindsight import EMBEDDING_MODEL, RERANKER_MODEL
from lifecontext.initialize import InitOptions, run_init
from lifecontext.paths import LifeContextPaths

V1_ROOT = Path(__file__).resolve().parents[1]
KEY = "sk-doctor-private-key"


@contextmanager
def _temporary_home():
    with tempfile.TemporaryDirectory(prefix="lifecontext-doctor-test.", dir=scratch_root()) as temporary:
        home = Path(temporary).resolve(strict=True)
        with patch.dict(os.environ, {"HOME": str(home), "HF_HOME": str(home / "hf")}):
            for name in ("HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE"):
                os.environ.pop(name, None)
            yield home


class _Unavailable(RuntimeError):
    code = "api_unavailable"


def _service_down(target: str) -> dict:
    raise _Unavailable()


def _service_up(target: str) -> dict:
    return {
        "/healthz": {"status": "ok"},
        "/v1/status": {"ready": True, "max_file_bytes": 1,
                       "dependencies": {"hindsight": {"ready": True}, "vault": {"ready": True},
                                        "catalog": {"ready": True}},
                       "reconciliation": {"fact_count": 0, "facts": {}}},
        "/v1/wiki/lint": {"findings": [{"page_id": "source_secret", "hint": "private"}],
                          "counts": {"error": 1, "warning": 2, "info": 3}, "omitted": 0},
    }[target]


def _tesseract_run(command, **kwargs):
    if command[-1] == "--version":
        return SimpleNamespace(stdout="tesseract 5.3.4\n leptonica-1.82.0\n", stderr="")
    return SimpleNamespace(stdout='List of available languages in "/usr/share" (3):\neng\nosd\nrus\n', stderr="")


def _which(name):
    return {"tesseract": "/usr/bin/tesseract", "lifecontext": "/home/u/.local/bin/lifecontext"}.get(name)


def _set_up(home: Path) -> LifeContextPaths:
    run_init(InitOptions(timezone="Europe/Vienna", api_key_stdin=True), key_stream=io.BytesIO(KEY.encode()))
    return LifeContextPaths.current()


class DoctorTests(unittest.TestCase):
    def test_empty_home_reports_broken_prerequisites_and_creates_nothing(self) -> None:
        with _temporary_home() as home:
            report, code = run_doctor(port=8765, fetch=_service_down, run=_tesseract_run)
            self.assertEqual(code, 1)
            self.assertFalse(report["ok"])
            for problem in ("home_missing", "config_missing", "vault_missing"):
                self.assertIn(problem, report["problems"])
            self.assertIn("service_not_running", report["warnings"])
            self.assertEqual(sorted(path.name for path in home.iterdir()), [])
            render(report)

    def test_set_up_home_is_healthy_and_the_report_holds_no_secrets_or_content(self) -> None:
        with _temporary_home() as home:
            paths = _set_up(home)
            entry = home / "LifeContext" / "vault" / "Journal" / "2026-10-17 - Private.md"
            entry.write_text("SECRET-JOURNAL-CONTENT")
            with patch("lifecontext.doctor.shutil.which", side_effect=_which):
                report, code = run_doctor(port=8765, fetch=_service_up, run=_tesseract_run)
            text = json.dumps(report) + render(report)
            self.assertEqual(code, 0, report["problems"])
            self.assertEqual(report["credentials"]["llm_key"], "present")
            self.assertEqual(report["config"]["llm"]["provider"], "anthropic")
            self.assertTrue(report["paths"]["home"]["private"])
            self.assertEqual(report["paths"]["config"]["mode"], "0600")
            self.assertEqual(report["service"]["state"], "running")
            self.assertTrue(report["service"]["ready"])
            self.assertEqual(report["service"]["lint"], {"error": 1, "warning": 2, "info": 3, "omitted": 0})
            self.assertEqual(report["command"]["lifecontext"], "/home/u/.local/bin/lifecontext")
            self.assertEqual(report["tesseract"]["missing_languages"], ["ukr"])
            self.assertIn("tesseract_languages_missing", report["warnings"])
            self.assertEqual(report["vision"], {"configured": False, "status": "off"})
            self.assertIn("models_not_cached", report["warnings"])
            for private in (KEY, "SECRET-JOURNAL-CONTENT", "source_secret", "Private.md"):
                self.assertNotIn(private, text)
            self.assertIsNone(report["operations"])
            self.assertTrue(paths.credentials_path.exists())

    def test_unsafe_modes_and_missing_key_are_problems(self) -> None:
        with _temporary_home() as home:
            paths = _set_up(home)
            os.chmod(home / "LifeContext" / "vault", 0o755)
            paths.credentials_path.unlink()
            report, code = run_doctor(port=8765, fetch=_service_down, run=_tesseract_run)
            self.assertEqual(code, 1)
            self.assertIn("vault_unsafe", report["problems"])
            self.assertIn("credentials_missing", report["problems"])
            os.chmod(paths.config_path, 0o644)
            report, _ = run_doctor(port=8765, fetch=_service_down, run=_tesseract_run)
            self.assertIn("config_invalid", report["problems"])

    def test_operations_models_and_last_daemon_start(self) -> None:
        with _temporary_home() as home:
            paths = _set_up(home)
            with sqlite3.connect(paths.catalog_path) as connection:
                connection.execute("CREATE TABLE operations (operation_id TEXT, state TEXT)")
                connection.executemany("INSERT INTO operations VALUES (?, ?)", [
                    ("a", "completed"), ("b", "pending"), ("c", "running"), ("d", "retryable_failed"),
                    ("e", "permanent_failed"), ("f", "permanent_failed")])
            for model in (EMBEDDING_MODEL, RERANKER_MODEL):
                directory = home / "hf" / "hub" / ("models--" + model.replace("/", "--"))
                snapshot = directory / "snapshots" / ("a" * 40)
                snapshot.mkdir(parents=True)
                (directory / "refs").mkdir()
                (directory / "refs" / "main").write_text("a" * 40)
                (snapshot / "config.json").write_text("{}")
                (snapshot / "model.safetensors").write_text("x")
            paths.hindsight_start_record_path.write_text(json.dumps({"hf_hub_offline": True}))
            report, _ = run_doctor(port=8765, fetch=_service_down, run=_tesseract_run)
            self.assertEqual(report["operations"], {"pending": 2, "retryable_failed": 1, "permanent_failed": 2})
            self.assertTrue(report["models"]["offline_next_start"])
            self.assertTrue(report["models"]["last_daemon_start"]["hf_hub_offline"])
            self.assertNotIn("models_not_cached", report["warnings"])

    def test_engine_auth_port_owner_and_postgres_listen_are_reported_without_secrets(self) -> None:
        from lifecontext import hindsight
        from lifecontext.hindsight import engine_token

        with _temporary_home() as home:
            paths = _set_up(home)
            token = engine_token(paths, create=True)
            paths.hindsight_profiles_dir.mkdir(mode=0o700, parents=True)
            os.chmod(paths.hindsight_home, 0o700)

            def profile(text: str) -> None:
                paths.hindsight_profile_env_path.write_text(text)
                os.chmod(paths.hindsight_profile_env_path, 0o600)

            profile("HINDSIGHT_API_PORT=9855\nHINDSIGHT_API_LLM_API_KEY=provider-secret\n")
            with patch.object(hindsight, "_daemon_owner", return_value="here"), \
                    patch.object(hindsight, "_http_status", return_value=200):
                report, code = run_doctor(port=8765, fetch=_service_down, run=_tesseract_run)
            self.assertEqual(code, 1)
            self.assertIn("engine_auth_off", report["problems"])
            self.assertIn("postgres_tcp", report["problems"])
            self.assertFalse(report["engine"]["auth_enforced"])

            profile("HINDSIGHT_API_PORT=24001\n"
                    "HINDSIGHT_API_TENANT_EXTENSION=hindsight_api.extensions.builtin.tenant:ApiKeyTenantExtension\n"
                    f"HINDSIGHT_API_TENANT_API_KEY={token}\nHINDSIGHT_API_LLM_API_KEY=provider-secret\n"
                    "HINDSIGHT_EMBED_API_DATABASE_URL=postgresql://hindsight:pg-secret@/hindsight?"
                    f"host={paths.postgres_socket_dir}&port=5432\n")
            with patch.object(hindsight, "_daemon_owner", return_value="here"), \
                    patch.object(hindsight, "_http_status", return_value=401):
                report, code = run_doctor(port=8765, fetch=_service_down, run=_tesseract_run)
            text = json.dumps(report) + render(report)
            self.assertEqual([problem for problem in report["problems"] if "engine" in problem or "postgres" in problem],
                             [])
            self.assertTrue(report["engine"]["auth_enforced"])
            self.assertIn("Hindsight engine: auth on, enforced; port 24001 (this HOME's daemon)", text)
            for secret in (token, "pg-secret", "provider-secret", KEY):
                self.assertNotIn(secret, text)

            with patch.object(hindsight, "_daemon_owner", return_value="elsewhere"):
                report, _ = run_doctor(port=8765, fetch=_service_down, run=_tesseract_run)
            self.assertIn("engine_port_taken", report["problems"])

    def test_command_prints_json_and_exits_one_for_an_empty_home(self) -> None:
        with tempfile.TemporaryDirectory(prefix="lifecontext-doctor-cli.", dir=scratch_root()) as temporary:
            home = Path(temporary).resolve(strict=True)
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            env = {"HOME": str(home), "PATH": os.defpath, "PYTHONPATH": str(V1_ROOT / "src")}
            result = subprocess.run(
                [sys.executable, "-m", "lifecontext.cli", "doctor", "--json", "--port", str(port)],
                env=env, cwd=home, capture_output=True, text=True, timeout=120, check=False)
            self.assertEqual(result.returncode, 1, result.stderr)
            report = json.loads(result.stdout)
            self.assertIn("home_missing", report["problems"])
            self.assertEqual(report["service"]["state"], "not_running")
            self.assertFalse((home / ".lifeweave").exists())


if __name__ == "__main__":
    unittest.main()
