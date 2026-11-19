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
LOCAL = "http://127.0.0.1:11434/v1"
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


def _run(*arguments: str, stdin: str | None = None, restart=NOT_RUNNING, tty: bool = False) -> tuple[int, str, str]:
    """Run `lifecontext llm ...` in this process; ``restart=None`` makes any attempt to restart a failure.

    ``tty`` says whether stdin is a terminal.
    """
    out, err = io.StringIO(), io.StringIO()
    fake_stdin = SimpleNamespace(buffer=io.BytesIO((stdin or "").encode()), isatty=lambda: tty)
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
            self.assertEqual((code, json.loads(out)["error"]), (1, "config_missing"))


class LlmSetNetworkPolicyTests(unittest.TestCase):
    def test_set_needs_the_flag_for_plain_http_on_another_host_and_saves_it(self) -> None:
        url = "http://models.lan:8000/v1"
        with _home():
            _initialize(provider="openai", model="gpt-test-model")
            args = ("set", "--provider", "openai_compatible", "--model", "local", "--base-url", url)
            code, out, _ = _run(*args, "--json", restart=None)
            failure = json.loads(out)
            self.assertEqual((code, failure["error"]), (1, "invalid_llm_settings"))
            self.assertIn("--allow-insecure-http", failure["message"])
            self.assertEqual(load_settings()["llm"]["provider"], "openai")
            code, out, err = _run(*args, "--allow-insecure-http", "--json")
            self.assertEqual(code, 0, err)
            report = json.loads(out)
            self.assertTrue(report["allow_insecure_http"])
            self.assertIn("model_uses_plain_http", report["warnings"])
            self.assertEqual(load_settings()["llm"]["allow_insecure_http"], url)
            code, out, _ = _run("show", "--json", restart=None)
            self.assertTrue(json.loads(out)["allow_insecure_http"])
            # The text forms say what was granted, and that the text and a stored key travel unencrypted.
            code, out, _ = _run("show", restart=None)
            self.assertIn("- Plain http: accepted for this URL (--allow-insecure-http)", out)
            self.assertIn("travel unencrypted over http", out)
            code, out, _ = _run("set", "--model", "local-2")
            self.assertIn("travel unencrypted over http", out)
            self.assertEqual(load_settings()["llm"]["allow_insecure_http"], url)
            # Another http host needs its own grant; the old one does not carry over.
            code, out, _ = _run("set", "--base-url", "http://other.lan/v1", "--json", restart=None)
            self.assertEqual((code, json.loads(out)["error"]), (1, "invalid_llm_settings"))
            code, out, _ = _run("set", "--base-url", "https://api.example.com/v1", "--json")
            self.assertEqual(code, 0)
            self.assertNotIn("allow_insecure_http", load_settings()["llm"])

    def test_the_flag_on_https_or_a_private_address_saves_nothing_and_says_so(self) -> None:
        with _home():
            _initialize(provider="openai_compatible", model="m", base_url="https://api.example.com/v1")
            code, out, _ = _run("set", "--allow-insecure-http", "--json")
            report = json.loads(out)
            self.assertEqual(code, 0)
            self.assertIn("insecure_http_flag_not_needed", report["warnings"])
            self.assertNotIn("allow_insecure_http", report)
            self.assertNotIn("allow_insecure_http", load_settings()["llm"])
            code, out, _ = _run("set", "--allow-insecure-http", "--base-url", "http://192.168.1.4:8000/v1")
            self.assertIn("was not saved: this URL does not need it", out)
            self.assertNotIn("allow_insecure_http", load_settings()["llm"])

    def test_a_keyless_openai_compatible_server_on_the_network_is_accepted(self) -> None:
        with _home():
            run_init(InitOptions(provider="openai_compatible", model="local", base_url="http://192.168.1.20:8000/v1",
                                 timezone="UTC"), environ={})
            report = json.loads(_run("show", "--json")[1])
            self.assertEqual(report["api_key"], "not_required")
            # No key, but the text still travels over the network unencrypted.
            self.assertIn("travel unencrypted", _run("show")[1])
            self.assertIn("not set (optional for this endpoint)", _run("show")[1])

    def test_a_remote_keyless_endpoint_is_asked_for_a_key_and_only_loopback_needs_none(self) -> None:
        with _home() as home:
            run_init(InitOptions(provider="openai_compatible", model="m", base_url="https://api.example.com/v1",
                                 timezone="UTC"), environ={})
            self.assertEqual(json.loads(_run("show", "--json")[1])["api_key"], "not_required")
            # Bare `llm set` at a terminal asks (an empty answer changes nothing), it does not say "needs no key".
            with patch.object(cli, "_ask_secret", return_value="") as asked:
                code, out, err = _run("set", restart=None, tty=True)
            self.assertEqual((code, asked.call_count), (1, 1))
            self.assertNotIn("needs no key", out + err)
            with patch.object(cli, "_ask_secret", return_value=NEW_KEY):
                code, out, err = _run("set", tty=True)
            self.assertEqual(code, 0, err)
            self.assertIn(f"LIFEWEAVE_LLM_API_KEY={NEW_KEY}", LifeContextPaths(home).credentials_path.read_text())
        with _home():
            run_init(InitOptions(provider="openai_compatible", model="m", base_url="http://127.0.0.1:1/v1",
                                 timezone="UTC"), environ={})
            with patch.object(cli, "_ask_secret", side_effect=AssertionError("asked")):
                code, out, _ = _run("set", "--json", restart=None, tty=True)
            self.assertIn("needs no key", json.loads(out)["message"])


class LlmSetTests(unittest.TestCase):
    def test_set_changes_only_the_given_fields_and_keeps_the_rest(self) -> None:
        with _home() as home:
            _initialize(provider="openai_compatible", model="first-model", base_url="http://127.0.0.1:8000/v1")
            paths = LifeContextPaths(home)
            paths.config_path.write_text(paths.config_path.read_text() + "vision:\n  max_pages_per_document: 5\n")
            before = load_settings()
            key_before = paths.credentials_path.read_bytes()

            code, out, err = _run("set", "--model", "second-model", "--json")
            self.assertEqual(code, 0, err)
            report = json.loads(out)
            self.assertEqual((report["provider"], report["model"], report["base_url"]),
                             ("openai_compatible", "second-model", "http://127.0.0.1:8000/v1"))
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
                    self.assertEqual((code, json.loads(out)["error"]), (1, error))
                    self.assertEqual((paths.config_path.read_bytes(), paths.credentials_path.read_bytes()), before)

    def test_each_refused_setting_says_why(self) -> None:
        with _home():
            _initialize(provider="openai", model="gpt-test-model")
            compatible = ("--provider", "openai_compatible", "--model", "m", "--base-url")
            for arguments, words in (
                ((*compatible, "http://models.example.invalid:11434/v1"), "or add --allow-insecure-http"),
                ((*compatible, "https://example.invalid/v1/chat/completions"), "give the API root instead"),
                (("--provider", "nonsense", "--model", "m"), "use one of: anthropic, "),
                (("--base-url", "ftp://example.invalid/v1"), "check the provider name, the exact model ID"),
            ):
                with self.subTest(arguments=arguments):
                    code, out, _ = _run("set", *arguments, "--json", restart=None)
                    failure = json.loads(out)
                    self.assertEqual((code, failure["error"]), (1, "invalid_llm_settings"))
                    self.assertIn(words, failure["message"])
                    self.assertNotIn("models.example.invalid", failure["message"])

    def test_a_new_provider_without_a_model_says_to_add_one(self) -> None:
        with _home():
            _initialize(provider="openai", model="gpt-test-model")
            code, out, _ = _run("set", "--provider", "anthropic", "--json", restart=None)
            failure = json.loads(out)
            self.assertEqual((code, failure["error"]), (1, "model_required"))
            self.assertIn("add --model", failure["message"])
            self.assertNotIn("LIFECONTEXT_MODEL", failure["message"])

    def test_set_needs_something_to_change_and_an_existing_setup(self) -> None:
        with _home():
            code, out, _ = _run("set", "--model", "m", "--json", restart=None)
            self.assertEqual((code, json.loads(out)["error"]), (1, "config_missing"))
            _initialize(provider="openai", model="gpt-test-model")
            code, out, _ = _run("set", "--json", restart=None)
            self.assertEqual((code, json.loads(out)["error"]), (1, "nothing_to_change"))
            # At a terminal, an empty answer to the key question changes nothing either.
            with patch.object(cli, "_ask_secret", return_value="") as asked:
                code, _, err = _run("set", restart=None, tty=True)
            self.assertEqual((code, asked.call_count), (1, 1))
            self.assertIn("nothing to change", err.lower())
            # --json never prompts, even at a terminal.
            with patch.object(cli, "_ask_secret", side_effect=AssertionError("asked")):
                code, out, _ = _run("set", "--json", restart=None, tty=True)
            self.assertEqual((code, json.loads(out)["error"]), (1, "nothing_to_change"))

    def test_set_alone_checks_the_setup_before_asking_for_a_key(self) -> None:
        with _home():
            with patch.object(cli, "_ask_secret", side_effect=AssertionError("asked")):
                code, out, _ = _run("set", "--json", restart=None, tty=True)
            self.assertEqual((code, json.loads(out)["error"]), (1, "config_missing"))
        with _home():
            run_init(InitOptions(provider="openai_compatible", model="local", base_url=LOCAL, timezone="UTC"),
                     environ={})
            with patch.object(cli, "_ask_secret", side_effect=AssertionError("asked")):
                code, out, _ = _run("set", "--json", restart=None, tty=True)
            failure = json.loads(out)
            self.assertEqual((code, failure["error"]), (1, "nothing_to_change"))
            self.assertIn("needs no key", failure["message"])

    def test_ctrl_d_or_ctrl_c_at_the_key_prompt_is_nothing_to_change_not_a_traceback(self) -> None:
        with _home():
            _initialize(provider="openai", model="gpt-test-model")
            for interruption in (EOFError, KeyboardInterrupt):
                with self.subTest(interruption=interruption.__name__):
                    with patch.object(cli, "_ask_secret", side_effect=interruption):
                        code, out, err = _run("set", restart=None, tty=True)
                    self.assertEqual(code, 1)
                    self.assertIn("nothing to change", err.lower())
                    self.assertNotIn("Traceback", out + err)

    def test_api_key_stdin_at_a_terminal_asks_hidden_instead_of_reading_echoed_input(self) -> None:
        with _home() as home:
            _initialize(provider="openai", model="gpt-test-model")
            with patch.object(cli, "_ask_secret", return_value=NEW_KEY) as asked:
                code, out, err = _run("set", "--api-key-stdin", tty=True, stdin="ignored-echoed-key\n")
            self.assertEqual((code, asked.call_count), (0, 1), err)
            stored = LifeContextPaths(home).credentials_path.read_text()
            self.assertIn(f"LIFEWEAVE_LLM_API_KEY={NEW_KEY}", stored)
            self.assertNotIn("ignored-echoed-key", stored)
            # Not at a terminal it still reads stdin, and nothing is asked.
            with patch.object(cli, "_ask_secret", side_effect=AssertionError("asked")):
                code, _, err = _run("set", "--api-key-stdin", stdin="sk-from-a-pipe-1234\n")
            self.assertEqual(code, 0, err)
            # With --json and a terminal there is no prompt and no echoed read.
            with patch.object(cli, "_ask_secret", side_effect=AssertionError("asked")):
                code, out, _ = _run("set", "--api-key-stdin", "--json", restart=None, tty=True)
            self.assertEqual((code, json.loads(out)["error"]), (1, "api_key_required"))

    def test_set_alone_at_a_terminal_asks_for_the_key_hidden_and_stores_it(self) -> None:
        with _home() as home:
            _initialize(provider="openai", model="gpt-test-model")
            paths = LifeContextPaths(home)
            config = paths.config_path.read_bytes()
            with patch.object(cli, "_ask_secret", return_value=f"  {NEW_KEY}  ") as asked:
                code, out, err = _run("set", tty=True)
            self.assertEqual(code, 0, err)
            self.assertIn("hidden", asked.call_args.args[0])
            self.assertNotIn(NEW_KEY, out + err)
            self.assertIn(f"LIFEWEAVE_LLM_API_KEY={NEW_KEY}", paths.credentials_path.read_text())
            self.assertEqual(paths.config_path.read_bytes(), config)
            self.assertIn("Model settings updated.", out)
            # Not at a terminal nothing is asked.
            with patch.object(cli, "_ask_secret", side_effect=AssertionError("asked")):
                code, out, _ = _run("set", "--json", restart=None)
            self.assertEqual((code, json.loads(out)["error"]), (1, "nothing_to_change"))

    def test_changing_the_provider_warns_when_the_old_key_stays(self) -> None:
        with _home():
            _initialize(provider="openai", model="gpt-test-model")
            code, out, _ = _run("set", "--provider", "anthropic", "--model", "claude-sonnet-5")
            self.assertEqual(code, 0)
            self.assertIn("the provider changed but the stored key was kept", out)
            code, out, _ = _run("set", "--provider", "openai", "--model", "gpt-test-model", "--api-key-stdin",
                                stdin=NEW_KEY + "\n")
            self.assertNotIn("provider changed", out)

    def test_set_refuses_claude_models_that_refuse_forced_tool_choice(self) -> None:
        with _home() as home:
            _initialize(provider="openai", model="gpt-test-model")
            paths = LifeContextPaths(home)
            before = paths.config_path.read_bytes()
            code, out, _ = _run("set", "--provider", "anthropic", "--model", "claude-opus-5-5", "--json", restart=None)
            failure = json.loads(out)
            self.assertEqual((code, failure["error"]), (1, "model_unsupported"))
            self.assertIn("forced tool call", failure["message"])
            code, _, err = _run("set", "--provider", "anthropic", "--model", "claude-opus-5-5", restart=None)
            self.assertIn("Use claude-sonnet-5 or claude-haiku-5-5", err)
            self.assertEqual(paths.config_path.read_bytes(), before)
            # Behind an OpenAI-compatible endpoint the same ID is only a warning.
            code, out, _ = _run("set", "--provider", "openai_compatible", "--base-url", LOCAL,
                                "--model", "claude-opus-5-5")
            self.assertEqual(code, 0)
            self.assertIn("Warning: claude-opus-5-5 refuses the forced tool choice", out)

    def test_set_keeps_an_existing_refused_model_when_only_the_key_changes(self) -> None:
        with _home() as home:
            _initialize(provider="anthropic", model="claude-sonnet-5")
            paths = LifeContextPaths(home)
            paths.config_path.write_text(paths.config_path.read_text().replace('"claude-sonnet-5"',
                                                                               '"claude-sonnet-5-5"'))
            code, out, err = _run("set", "--api-key-stdin", "--json", stdin=NEW_KEY + "\n")
            self.assertEqual(code, 0, err)
            report = json.loads(out)
            self.assertEqual(report["model"], "claude-sonnet-5-5")
            self.assertIn("model_rejects_forced_tool_use", report["warnings"])

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
