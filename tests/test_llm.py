"""`lifecontext llm show` and `llm set`: partial updates, validation, and a key that is never printed."""

from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from local_paths import scratch_root

from lifecontext import cli
from lifecontext.config import load_settings
from lifecontext.initialize import InitOptions, run_init
from lifecontext.paths import LifeContextPaths

KEY = "sk-old-private-key-0123456789"
NEW_KEY = "sk-new-private-key-9876543210"
LOCAL = "http://127.0.0.1:8317/v1"
NOT_RUNNING = {"state": "not_running", "command": ["systemctl", "--user", "restart", "lifecontext.service"]}


@contextmanager
def _home():
    with tempfile.TemporaryDirectory(prefix="lifecontext-llm-test.", dir=scratch_root()) as temporary:
        home = Path(temporary).resolve(strict=True)
        with patch.dict(os.environ, {"HOME": str(home)}):
            os.environ.pop("LIFECONTEXT_API_KEY_FILE", None)
            yield home


def _initialize(**values) -> None:
    run_init(InitOptions(timezone="Europe/Vienna", api_key_stdin=True, **values),
             key_stream=io.BytesIO((KEY + "\n").encode()), environ={})


def _run(*arguments: str, stdin: str | None = None, restart=NOT_RUNNING) -> tuple[int, str, str]:
    """Run `lifecontext llm ...` in this process; ``restart=None`` makes any attempt to restart a failure."""
    out, err = io.StringIO(), io.StringIO()
    fake_stdin = SimpleNamespace(buffer=io.BytesIO(stdin.encode())) if stdin is not None else sys.stdin
    outcome = {"return_value": restart} if restart is not None else {"side_effect": AssertionError("restarted")}
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), patch.object(sys, "stdin", fake_stdin), \
            patch("lifecontext.service.restart_if_running", **outcome):
        code = cli.main(["llm", *arguments])
    return code, out.getvalue(), err.getvalue()


class LlmShowTests(unittest.TestCase):
    def test_show_prints_the_settings_and_never_the_key(self) -> None:
        with _home():
            _initialize(provider="openai", model="gpt-test-model")
            code, out, err = _run("show", restart=None)
            self.assertEqual(code, 0)
            self.assertIn("- Provider: openai", out)
            self.assertIn("- Model: gpt-test-model", out)
            self.assertIn("- API key: set", out)
            code, out, err = _run("show", "--json", restart=None)
            self.assertEqual(json.loads(out), {"provider": "openai", "model": "gpt-test-model", "base_url": None,
                                               "api_key": "set"})
            self.assertNotIn(KEY, out + err)

    def test_show_says_when_a_key_is_missing_or_not_needed(self) -> None:
        with _home() as home:
            run_init(InitOptions(provider="openai_compatible", model="local", base_url=LOCAL, timezone="UTC"),
                     environ={})
            self.assertEqual(json.loads(_run("show", "--json")[1])["api_key"], "not_required")
            self.assertIn(LOCAL, _run("show")[1])
            LifeContextPaths(home).credentials_path.write_text(f"LIFEWEAVE_LLM_API_KEY={KEY}\n")
            LifeContextPaths(home).credentials_path.chmod(0o600)
            self.assertEqual(json.loads(_run("show", "--json")[1])["api_key"], "set")
        with _home():
            run_init(InitOptions(provider="openai", model="gpt-test-model", timezone="UTC"), environ={})
            self.assertEqual(json.loads(_run("show", "--json")[1])["api_key"], "missing")
            self.assertIn("lifecontext llm set --api-key-stdin", _run("show")[1])

    def test_show_without_setup_fails_cleanly(self) -> None:
        with _home():
            code, out, _ = _run("show", "--json", restart=None)
            self.assertEqual((code, json.loads(out)), (1, {"error": "config_missing"}))


class LlmSetTests(unittest.TestCase):
    def test_set_changes_only_the_given_fields_and_keeps_the_rest(self) -> None:
        with _home() as home:
            _initialize(provider="openai_compatible", model="first-model", base_url="http://127.0.0.1:11434/v1")
            paths = LifeContextPaths(home)
            paths.config_path.write_text(paths.config_path.read_text() + "vision:\n  max_pages_per_document: 5\n")
            before = load_settings()
            key_before = paths.credentials_path.read_bytes()

            code, out, err = _run("set", "--model", "second-model", "--json")
            self.assertEqual(code, 0, err)
            report = json.loads(out)
            self.assertEqual((report["provider"], report["model"], report["base_url"]),
                             ("openai_compatible", "second-model", "http://127.0.0.1:11434/v1"))
            self.assertEqual(report["service"], NOT_RUNNING)
            after = load_settings()
            self.assertEqual(after["llm"], {**before["llm"], "model": "second-model"})
            self.assertEqual({name: after[name] for name in after if name != "llm"},
                             {name: before[name] for name in before if name != "llm"})
            self.assertEqual(after["vision"], {"max_pages_per_document": 5})
            self.assertEqual(paths.credentials_path.read_bytes(), key_before)

            code, out, err = _run("set", "--base-url", LOCAL, "--provider", "openai_compatible")
            self.assertEqual(code, 0, err)
            self.assertEqual(load_settings()["llm"]["base_url"], LOCAL)
            self.assertEqual(load_settings()["llm"]["model"], "second-model")
            self.assertIn("Model settings updated.", out)
            self.assertIn("not running", out)
            self.assertEqual(stat.S_IMODE(paths.config_path.stat().st_mode), 0o600)

    def test_set_replaces_the_key_from_stdin_and_never_prints_it(self) -> None:
        with _home() as home:
            _initialize(provider="openai", model="gpt-test-model")
            paths = LifeContextPaths(home)
            code, out, err = _run("set", "--api-key-stdin", "--json", stdin=NEW_KEY + "\n")
            self.assertEqual(code, 0, err)
            self.assertTrue(json.loads(out)["api_key_updated"])
            self.assertEqual(paths.credentials_path.read_text(), f"LIFEWEAVE_LLM_API_KEY={NEW_KEY}\n")
            self.assertEqual(stat.S_IMODE(paths.credentials_path.stat().st_mode), 0o600)
            self.assertEqual(load_settings()["llm"]["model"], "gpt-test-model")
            code, text, text_err = _run("set", "--api-key-stdin", stdin=NEW_KEY + "\n")
            for printed in (out, err, text, text_err, paths.config_path.read_text()):
                self.assertNotIn(NEW_KEY, printed)
                self.assertNotIn(KEY, printed)

    def test_set_reads_the_key_from_the_key_file_variable(self) -> None:
        with _home() as home:
            _initialize(provider="openai", model="gpt-test-model")
            key_file = home / "key.txt"
            key_file.write_text(NEW_KEY + "\n")
            key_file.chmod(0o600)
            with patch.dict(os.environ, {"LIFECONTEXT_API_KEY_FILE": str(key_file),
                                         "LIFECONTEXT_MODEL": "ignored-here", "LIFECONTEXT_VAULT": "/ignored"}):
                code, out, err = _run("set", "--json")
            self.assertEqual(code, 0, err)
            self.assertNotIn(NEW_KEY, out + err)
            self.assertEqual(LifeContextPaths(home).credentials_path.read_text(),
                             f"LIFEWEAVE_LLM_API_KEY={NEW_KEY}\n")
            settings = load_settings()
            self.assertEqual(settings["llm"]["model"], "gpt-test-model")
            self.assertEqual(settings["vault"]["root"], str(home / "LifeContext" / "vault"))

    def test_set_validates_like_init_and_leaves_everything_unchanged_on_failure(self) -> None:
        with _home() as home:
            _initialize(provider="openai", model="gpt-test-model")
            paths = LifeContextPaths(home)
            before = (paths.config_path.read_bytes(), paths.credentials_path.read_bytes())
            for arguments, stdin, error in (
                (("--provider", "openai_compatible"), None, "model_required"),  # another provider: no old model
                (("--provider", "openai_compatible", "--model", "m"), None, "invalid_llm_settings"),  # no base URL
                (("--provider", "nonsense", "--model", "m"), None, "invalid_llm_settings"),
                (("--base-url", "ftp://example.invalid/v1"), None, "invalid_llm_settings"),
                (("--api-key-stdin",), "key with spaces\n", "invalid_api_key"),
                (("--api-key-stdin",), "", "invalid_api_key"),
            ):
                with self.subTest(arguments=arguments):
                    code, out, _ = _run("set", *arguments, "--json", stdin=stdin, restart=None)
                    self.assertEqual((code, json.loads(out)), (1, {"error": error}))
                    self.assertEqual((paths.config_path.read_bytes(), paths.credentials_path.read_bytes()), before)

    def test_set_needs_something_to_change_and_an_existing_setup(self) -> None:
        with _home():
            code, out, _ = _run("set", "--model", "m", "--json", restart=None)
            self.assertEqual((code, json.loads(out)), (1, {"error": "config_missing"}))
            _initialize(provider="openai", model="gpt-test-model")
            code, out, _ = _run("set", "--json", restart=None)
            self.assertEqual((code, json.loads(out)), (1, {"error": "nothing_to_change"}))

    def test_changing_the_provider_warns_when_the_old_key_stays(self) -> None:
        with _home():
            _initialize(provider="openai", model="gpt-test-model")
            code, out, _ = _run("set", "--provider", "anthropic", "--model", "claude-sonnet-5")
            self.assertEqual(code, 0)
            self.assertIn("the provider changed but the stored key was kept", out)
            code, out, _ = _run("set", "--provider", "openai", "--model", "gpt-test-model", "--api-key-stdin",
                                stdin=NEW_KEY + "\n")
            self.assertNotIn("provider changed", out)

    def test_set_reports_what_the_service_did(self) -> None:
        with _home():
            _initialize(provider="openai", model="gpt-test-model")
            restarted = {"state": "restarted", "command": ["systemctl", "--user", "restart", "lifecontext.service"]}
            code, out, _ = _run("set", "--model", "other-model", restart=restarted)
            self.assertIn("Restarted the service", out)
            absent = {"state": "no_manager", "command": ["systemctl", "--user", "restart", "lifecontext.service"]}
            code, out, _ = _run("set", "--model", "third-model", restart=absent)
            self.assertEqual(code, 0)
            self.assertIn("with: systemctl --user restart lifecontext.service", out)
            self.assertEqual(json.loads(_run("set", "--model", "gpt-test-model", "--json", restart=absent)[1])
                             ["service"], absent)


if __name__ == "__main__":
    unittest.main()
