"""`lifecontext init`: private files, idempotent re-runs, key handling and defaults."""

from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from local_paths import scratch_root

from lifecontext.config import load_settings
from lifecontext.initialize import (
    LOCAL_MODEL_SERVER,
    PROVIDER_QUESTION,
    SUGGESTED_ANTHROPIC_MODEL,
    InitError,
    InitOptions,
    detect_timezone,
    provider_menu,
    render,
    run_init,
)
from lifecontext.paths import LifeContextPaths

V1_ROOT = Path(__file__).resolve().parents[1]
KEY = "sk-test-private-key-0123456789"


@contextmanager
def _temporary_home():
    with tempfile.TemporaryDirectory(prefix="lifecontext-init-test.", dir=scratch_root()) as temporary:
        home = Path(temporary).resolve(strict=True)
        with patch.dict(os.environ, {"HOME": str(home), "LIFEWEAVE_LLM_API_KEY": "ambient-must-not-load"}):
            yield home


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def _options(**values) -> InitOptions:
    return InitOptions(**{"timezone": "Europe/Vienna", **values})


def _anthropic(**values) -> InitOptions:
    """An unattended first setup names its provider and model; there is no default."""
    return _options(**{"provider": "anthropic", "model": SUGGESTED_ANTHROPIC_MODEL, **values})


class InitTests(unittest.TestCase):
    def test_unattended_setup_writes_private_files_and_never_reports_the_key(self) -> None:
        with _temporary_home() as home:
            report = run_init(_anthropic(api_key_stdin=True), key_stream=io.BytesIO((KEY + "\n").encode()))
            paths = LifeContextPaths.current()
            vault = home / "LifeContext" / "vault"
            self.assertEqual(report["config_state"], "written")
            self.assertEqual(report["credentials_state"], "stored")
            self.assertEqual((report["provider"], report["model"]), ("anthropic", SUGGESTED_ANTHROPIC_MODEL))
            self.assertEqual(report["vault_root"], str(vault))
            self.assertEqual(_mode(paths.lifeweave_home), 0o700)
            self.assertEqual(_mode(paths.config_path), 0o600)
            self.assertEqual(_mode(paths.credentials_path), 0o600)
            self.assertEqual(_mode(vault), 0o700)
            self.assertEqual(_mode(vault / "Journal"), 0o700)
            self.assertEqual(_mode(vault / "Journal" / "entries"), 0o700)
            self.assertEqual(_mode(vault / "Journal" / "Inbox"), 0o700)
            self.assertEqual(paths.credentials_path.read_text(), f"LIFEWEAVE_LLM_API_KEY={KEY}\n")
            settings = load_settings()
            self.assertEqual(settings["llm"], {"provider": "anthropic", "model": SUGGESTED_ANTHROPIC_MODEL})
            self.assertEqual(settings["vault"], {"root": str(vault), "journal_path": str(vault / "Journal")})
            self.assertEqual(settings["timezone"], "Europe/Vienna")
            self.assertNotIn(KEY, json.dumps(report) + render(report))
            self.assertNotIn(KEY, paths.config_path.read_text())

    def test_rerun_keeps_everything_and_names_the_ignored_flags(self) -> None:
        with _temporary_home():
            paths = LifeContextPaths.current()
            run_init(_anthropic(api_key_stdin=True), key_stream=io.BytesIO(KEY.encode()))
            paths.credentials_path.write_text(f"LIFEWEAVE_LLM_API_KEY={KEY}\nLIFEWEAVE_VISION_API_KEY=vision\n")
            before = (paths.config_path.read_bytes(), paths.credentials_path.read_bytes())

            same = run_init(_options(model=SUGGESTED_ANTHROPIC_MODEL))
            self.assertEqual(same["warnings"], [])
            again = run_init(_options(model="claude-haiku-5-5", vault="~/Elsewhere", api_key_stdin=True),
                             key_stream=io.BytesIO(b"other-key\n"))
            self.assertEqual((again["config_state"], again["credentials_state"]), ("kept", "kept"))
            self.assertEqual(again["warnings"], ["flags_ignored", "existing_key_kept"])
            self.assertEqual(again["ignored"], ["model", "vault"])
            text = render(again)
            self.assertIn("--model, --vault did not change it; add --force", text)
            self.assertIn("add --replace-key to replace it", text)
            self.assertEqual((paths.config_path.read_bytes(), paths.credentials_path.read_bytes()), before)

    def test_force_applies_flags_to_the_existing_config_and_keeps_the_rest(self) -> None:
        with _temporary_home() as home:
            paths = LifeContextPaths.current()
            vault = home / "Notes"
            run_init(_anthropic(vault=str(vault), api_key_stdin=True), key_stream=io.BytesIO(KEY.encode()))
            with paths.config_path.open("a") as config:
                config.write("vision:\n  provider: \"anthropic\"\n  model: \"claude-haiku-5-5\"\n"
                             "limits:\n  max_result_count: 7\n")
            text = paths.config_path.read_text().replace(
                "  journal_path:", "  relationship_roots: [\"People\"]\n  journal_path:")
            paths.config_path.write_text(text)
            key_before = paths.credentials_path.read_bytes()

            forced = run_init(_options(model="claude-haiku-5-5", force=True, api_key_stdin=True),
                              key_stream=io.BytesIO(b"other-key\n"))
            self.assertEqual(forced["config_state"], "updated")
            self.assertEqual(forced["credentials_state"], "kept")
            self.assertIn("existing_key_kept", forced["warnings"])
            settings = load_settings()
            self.assertEqual(settings["llm"], {"provider": "anthropic", "model": "claude-haiku-5-5"})
            self.assertEqual(settings["vault"], {"root": str(vault), "journal_path": str(vault / "Journal"),
                                                 "relationship_roots": ["People"]})
            self.assertEqual(settings["vision"], {"provider": "anthropic", "model": "claude-haiku-5-5"})
            self.assertEqual(settings["limits"], {"max_result_count": 7})
            self.assertEqual(settings["timezone"], "Europe/Vienna")
            self.assertEqual(paths.credentials_path.read_bytes(), key_before)
            self.assertEqual(_mode(paths.config_path), 0o600)

            switched = run_init(InitOptions(provider="openai_compatible", model="llama3.3",
                                            base_url="http://127.0.0.1:11434/v1", force=True))
            self.assertEqual(switched["timezone"], "Europe/Vienna")
            self.assertEqual(load_settings()["llm"], {"provider": "openai_compatible", "model": "llama3.3",
                                                      "base_url": "http://127.0.0.1:11434/v1"})
            self.assertEqual(load_settings()["limits"], {"max_result_count": 7})

    def test_replace_key_changes_only_the_key(self) -> None:
        with _temporary_home():
            paths = LifeContextPaths.current()
            run_init(_anthropic(api_key_stdin=True), key_stream=io.BytesIO(KEY.encode()))
            paths.credentials_path.write_text(f"LIFEWEAVE_LLM_API_KEY={KEY}\nLIFEWEAVE_VISION_API_KEY=vision\n")
            config = paths.config_path.read_bytes()
            replaced = run_init(_options(model="claude-haiku-5-5", api_key_stdin=True, replace_key=True),
                                key_stream=io.BytesIO(b"other-key\n"))
            self.assertEqual((replaced["config_state"], replaced["credentials_state"]), ("kept", "replaced"))
            self.assertEqual(paths.config_path.read_bytes(), config)
            self.assertEqual(paths.credentials_path.read_text(),
                             "LIFEWEAVE_LLM_API_KEY=other-key\nLIFEWEAVE_VISION_API_KEY=vision\n")
            self.assertEqual(_mode(paths.credentials_path), 0o600)
            with self.assertRaises(InitError) as caught:
                run_init(_options(replace_key=True))
            self.assertEqual(caught.exception.code, "api_key_required")

    def test_environment_supplies_unattended_values_and_the_key_file(self) -> None:
        with _temporary_home() as home:
            key_file = home / "secret"
            key_file.write_text(KEY + "\n")
            environ = {"LIFECONTEXT_PROVIDER": "openai", "LIFECONTEXT_MODEL": "gpt-5.5",
                       "LIFECONTEXT_BASE_URL": "https://api.example.test/v1",
                       "LIFECONTEXT_VAULT": str(home / "FromEnv"), "LIFECONTEXT_API_KEY_FILE": str(key_file),
                       "LIFEWEAVE_LLM_API_KEY": "ambient-must-not-load"}
            report = run_init(_options(model="gpt-5.5-mini"), environ=environ)
            self.assertEqual((report["provider"], report["model"]), ("openai", "gpt-5.5-mini"))
            self.assertEqual(report["base_url"], "https://api.example.test/v1")
            self.assertEqual(report["vault_root"], str(home / "FromEnv"))
            self.assertEqual(report["credentials_state"], "stored")
            self.assertEqual(LifeContextPaths.current().credentials_path.read_text(),
                             f"LIFEWEAVE_LLM_API_KEY={KEY}\n")
            self.assertNotIn(KEY, json.dumps(report))
            key_file.chmod(0o644)
            shared = run_init(_options(replace_key=True), environ={"LIFECONTEXT_API_KEY_FILE": str(key_file)})
            self.assertIn("api_key_file_readable_by_others", shared["warnings"])
            self.assertIn("other users can read", render(shared))
            key_file.chmod(0o600)
            private = run_init(_options(replace_key=True), environ={"LIFECONTEXT_API_KEY_FILE": str(key_file)})
            self.assertNotIn("api_key_file_readable_by_others", private["warnings"])
        for target, code in (("missing", "api_key_file_unreadable"), ("directory", "api_key_file_unreadable"),
                             ("two-lines", "invalid_api_key")):
            with self.subTest(target=target), _temporary_home() as home:
                (home / "directory").mkdir()
                (home / "two-lines").write_text("one\ntwo\n")
                with self.assertRaises(InitError) as caught:
                    run_init(_options(), environ={"LIFECONTEXT_API_KEY_FILE": str(home / target)})
                self.assertEqual(caught.exception.code, code)
                self.assertFalse(LifeContextPaths.current().config_path.exists())

    def test_a_failing_vault_leaves_no_config_behind(self) -> None:
        with _temporary_home() as home:
            (home / "file").write_text("not a folder")
            with self.assertRaises(InitError) as caught:
                run_init(_anthropic(vault=str(home / "file")))
            self.assertEqual(caught.exception.code, "unsafe_vault")
            self.assertFalse(LifeContextPaths.current().config_path.exists())
            locked = home / "locked"
            locked.mkdir(mode=0o500)
            try:
                with self.assertRaises(OSError):
                    run_init(_anthropic(vault=str(locked / "vault")))
            finally:
                locked.chmod(0o700)
            self.assertFalse(LifeContextPaths.current().config_path.exists())

    def test_symlinked_vault_is_resolved_or_named(self) -> None:
        with _temporary_home() as home:
            real = home / "Real"
            real.mkdir()
            (home / "Link").symlink_to(real)
            report = run_init(_anthropic(vault=str(home / "Link")))
            self.assertEqual(report["vault_root"], str(real))
            self.assertEqual(load_settings()["vault"]["root"], str(real))
            paths = LifeContextPaths.current()
            paths.config_path.write_text(paths.config_path.read_text().replace(str(real), str(home / "Link")))
            before = paths.config_path.read_bytes()
            with self.assertRaises(InitError) as caught:
                run_init(_options())
            self.assertEqual(caught.exception.code, "vault_path_has_symlink")
            self.assertEqual(paths.config_path.read_bytes(), before)
            fixed = run_init(_options(force=True))
            self.assertEqual(fixed["vault_root"], str(real))
            # A link higher up the path counts too: the service opens every folder without following links.
            (real / "Inner").mkdir(mode=0o700)
            paths.config_path.write_text(paths.config_path.read_text().replace(
                f'"{real}', f'"{home / "Link" / "Inner"}'))
            with self.assertRaises(InitError) as caught:
                run_init(_options())
            self.assertEqual(caught.exception.code, "vault_path_has_symlink")

    def test_a_key_failure_leaves_config_as_it_was(self) -> None:
        with _temporary_home():
            paths = LifeContextPaths.current()
            run_init(_anthropic(api_key_stdin=True), key_stream=io.BytesIO(KEY.encode()))
            before = paths.config_path.read_bytes()

            def ask(question, default):
                return "claude-haiku-5-5" if question.startswith("Model ID") else ""

            with self.assertRaises(InitError) as caught:
                run_init(InitOptions(interactive=True, force=True, replace_key=True), ask=ask,
                         ask_secret=lambda question: "")
            self.assertEqual(caught.exception.code, "api_key_required")
            self.assertEqual(paths.config_path.read_bytes(), before)
            self.assertEqual(paths.credentials_path.read_text(), f"LIFEWEAVE_LLM_API_KEY={KEY}\n")

    def test_missing_key_is_reported_with_a_next_step(self) -> None:
        with _temporary_home():
            report = run_init(_anthropic())
            self.assertEqual(report["credentials_state"], "missing")
            self.assertIn("--api-key-stdin", report["setup_steps"][0])
            self.assertFalse(LifeContextPaths.current().credentials_path.exists())
            stored = run_init(_options(api_key_stdin=True), key_stream=io.BytesIO(KEY.encode()))
            self.assertEqual((stored["config_state"], stored["credentials_state"]), ("kept", "stored"))

    def test_loopback_openai_compatible_endpoint_needs_no_key(self) -> None:
        with _temporary_home():
            report = run_init(_options(provider="openai_compatible", model="llama3.3",
                                       base_url="http://127.0.0.1:11434/v1"))
            self.assertEqual(report["credentials_state"], "not_required")
            self.assertEqual(load_settings()["llm"]["base_url"], "http://127.0.0.1:11434/v1")
            remote = run_init(_options(provider="openai_compatible", model="m",
                                       base_url="https://api.example.test/v1", force=True))
            self.assertEqual(remote["credentials_state"], "missing")

    def test_invalid_input_fails_before_writing(self) -> None:
        cases = (
            (_options(provider="unknown"), None, "invalid_llm_settings"),
            (_options(provider="openai_compatible", model="m"), None, "invalid_llm_settings"),
            (_options(provider="openai"), None, "model_required"),
            (_options(), None, "provider_required"),
            (_anthropic(timezone="Mars/Olympus"), None, "invalid_timezone"),
            (_options(api_key_stdin=True), b"two\nlines\n", "invalid_api_key"),
            (_options(api_key_stdin=True), b"has space\n", "invalid_api_key"),
            (_options(api_key_stdin=True), b"\n", "invalid_api_key"),
        )
        for options, stdin, code in cases:
            with self.subTest(code=code), _temporary_home():
                with self.assertRaises(InitError) as caught:
                    run_init(options, key_stream=io.BytesIO(stdin) if stdin is not None else None)
                self.assertEqual(caught.exception.code, code)
                self.assertFalse(LifeContextPaths.current().credentials_path.exists())

    def test_existing_vault_is_tightened_and_a_custom_vault_is_used(self) -> None:
        with _temporary_home() as home:
            custom = home / "Notes"
            (custom / "Journal").mkdir(parents=True)
            os.chmod(custom, 0o755)
            report = run_init(_anthropic(vault="~/Notes"))
            self.assertEqual(report["vault_root"], str(custom))
            self.assertEqual(report["vault_state"], "tightened")
            self.assertEqual(_mode(custom), 0o700)
            for bad in (str(home / ".lifeweave" / "vault"), "/"):
                with self.subTest(vault=bad), self.assertRaises(InitError) as caught:
                    run_init(_options(vault=bad, force=True))
                self.assertEqual(caught.exception.code, "invalid_vault")

    def test_interactive_answers_and_hidden_key(self) -> None:
        questions = []

        def ask(question, default):
            questions.append((question, default))
            return {provider_menu(): "1", "Vault folder": "~/Vault"}.get(question, "")

        with _temporary_home() as home:
            report = run_init(InitOptions(interactive=True), ask=ask, ask_secret=lambda question: KEY,
                              environ={"TZ": "America/New_York"})
            self.assertEqual((report["provider"], report["model"]), ("anthropic", SUGGESTED_ANTHROPIC_MODEL))
            self.assertEqual(report["timezone"], "America/New_York")
            self.assertEqual(report["vault_root"], str(home / "Vault"))
            self.assertEqual(report["credentials_state"], "stored")
            # The menu preselects nothing; Anthropic suggests a model; no base URL is asked for.
            self.assertEqual(questions[0], (provider_menu(), ""))
            self.assertEqual(questions[1], ("Model ID (claude-haiku-5-5 costs less)", SUGGESTED_ANTHROPIC_MODEL))
            self.assertNotIn("URL", " ".join(question for question, _ in questions))

    def test_the_menu_has_no_preselection_and_asks_again(self) -> None:
        self.assertEqual(provider_menu(), f"""{PROVIDER_QUESTION}
  1) Anthropic (API key)
  2) OpenAI (API key)
  3) Google Gemini (API key)
  4) OpenRouter (API key)
  5) A local model server (Ollama, vLLM, LM Studio)
  6) Another OpenAI-compatible endpoint
Choose 1-6""")
        self.assertEqual(PROVIDER_QUESTION,
                         "Which model should LifeContext use? The text of your documents and notes is sent to it.")
        for answers, code in ((["", "", ""], "provider_required"), (["7", "x", ""], "provider_required"),
                              (["", "2", ""], "model_required")):
            with self.subTest(answers=answers), _temporary_home():
                replies = iter(answers + [""] * 10)
                with self.assertRaises(InitError) as caught:
                    run_init(InitOptions(interactive=True), ask=lambda question, default, replies=replies: next(replies),
                             ask_secret=lambda question: "")
                self.assertEqual(caught.exception.code, code)
                self.assertFalse(LifeContextPaths.current().config_path.exists())

    def test_a_local_server_suggests_its_url_and_another_endpoint_asks_for_one(self) -> None:
        for choice, url, expected in (("5", "", LOCAL_MODEL_SERVER),
                                      ("6", "https://llm.example.test/v1", "https://llm.example.test/v1")):
            questions = []

            def ask(question, default, choice=choice, url=url, questions=questions):
                questions.append((question, default))
                if question == provider_menu():
                    return choice
                return url if "URL" in question else ("llama3.3" if question.startswith("Model ID") else "")

            with self.subTest(choice=choice), _temporary_home():
                report = run_init(InitOptions(interactive=True, timezone="UTC"), ask=ask,
                                  ask_secret=lambda question: "")
                self.assertEqual((report["provider"], report["model"], report["base_url"]),
                                 ("openai_compatible", "llama3.3", expected))
                url_question = next(item for item in questions if "URL" in item[0])
                self.assertEqual(url_question[1], LOCAL_MODEL_SERVER if choice == "5" else "")
                model_question = next(question for question, _ in questions if question.startswith("Model ID"))
                self.assertIn("ollama list" if choice == "5" else "exactly as your endpoint", model_question)

    def test_a_refused_model_is_asked_for_again_with_the_reason(self) -> None:
        questions = []
        replies = iter(["1", "claude-opus-5-5", "claude-haiku-5-5"])

        def ask(question, default):
            questions.append(question)
            return next(replies, "")

        with _temporary_home():
            report = run_init(InitOptions(interactive=True, timezone="UTC"), ask=ask, ask_secret=lambda question: "")
            self.assertEqual(report["model"], "claude-haiku-5-5")
            self.assertTrue(questions[2].startswith("claude-opus-5-5 refuses the forced tool choice"))

    def test_menu_choices_decide_the_base_url_whatever_the_environment_says(self) -> None:
        environ = {"LIFECONTEXT_BASE_URL": "http://127.0.0.1:8000/v1"}
        for choice, expected in (("1", None), ("5", "http://127.0.0.1:8000/v1")):
            defaults = {}

            def ask(question, default, choice=choice, defaults=defaults):
                defaults[question] = default
                if question == provider_menu():
                    return choice
                return "llama3.3" if question.startswith("Model ID") else ""

            with self.subTest(choice=choice), _temporary_home():
                report = run_init(InitOptions(interactive=True, timezone="UTC"), ask=ask,
                                  ask_secret=lambda question: "", environ=environ)
                self.assertEqual(report["base_url"], expected)
                if choice == "5":  # the variable is only the suggestion; the question is still asked
                    self.assertEqual(defaults["Base URL of the server"], "http://127.0.0.1:8000/v1")

    def test_unattended_setup_needs_an_explicit_provider_and_model(self) -> None:
        for options, environ, code in (
            (_options(), {}, "provider_required"),
            (_options(model="claude-sonnet-5"), {}, "provider_required"),
            (_options(provider="anthropic"), {}, "model_required"),
            (_options(), {"LIFECONTEXT_PROVIDER": "anthropic"}, "model_required"),
        ):
            with self.subTest(options=options, environ=environ), _temporary_home():
                with self.assertRaises(InitError) as caught:
                    run_init(options, environ=environ)
                self.assertEqual(caught.exception.code, code)
                for variable in ("LIFECONTEXT_PROVIDER", "LIFECONTEXT_MODEL"):
                    self.assertIn(variable, caught.exception.message)
                self.assertFalse(LifeContextPaths.current().config_path.exists())
        with _temporary_home():
            report = run_init(_options(), environ={"LIFECONTEXT_PROVIDER": "anthropic",
                                                   "LIFECONTEXT_MODEL": "claude-haiku-5-5"})
            self.assertEqual((report["provider"], report["model"]), ("anthropic", "claude-haiku-5-5"))

    def test_models_that_refuse_forced_tool_choice_are_refused_with_anthropic(self) -> None:
        for model in ("claude-sonnet-5-5", "claude-opus-5-5", "claude-fable-5-1", "claude-mythos-5-1"):
            with self.subTest(model=model), _temporary_home():
                with self.assertRaises(InitError) as caught:
                    run_init(_anthropic(model=model))
                self.assertEqual(caught.exception.code, "model_unsupported")
                for words in ("strict structured output", "forced tool call", "claude-sonnet-5 or claude-haiku"):
                    self.assertIn(words, caught.exception.message)
                self.assertFalse(LifeContextPaths.current().config_path.exists())

    def test_the_same_ids_behind_an_openai_compatible_endpoint_only_warn(self) -> None:
        with _temporary_home():
            report = run_init(_options(provider="openai_compatible", model="claude-sonnet-5-5",
                                       base_url="http://127.0.0.1:11434/v1"))
            self.assertEqual(report["config_state"], "written")
            self.assertIn("model_rejects_forced_tool_use", report["warnings"])
            self.assertIn("Warning: claude-sonnet-5-5 refuses the forced tool choice", render(report))

    def test_an_existing_config_with_such_a_model_is_left_alone(self) -> None:
        with _temporary_home():
            run_init(_anthropic())
            paths = LifeContextPaths.current()
            paths.config_path.write_text(paths.config_path.read_text().replace(
                f'"{SUGGESTED_ANTHROPIC_MODEL}"', '"claude-opus-5-5"'))
            before = paths.config_path.read_bytes()
            kept = run_init(_options())
            self.assertEqual((kept["config_state"], kept["model"]), ("kept", "claude-opus-5-5"))
            self.assertIn("model_rejects_forced_tool_use", kept["warnings"])
            # Updating something else (here the time zone) keeps the model too.
            updated = run_init(_options(timezone="UTC", force=True))
            self.assertEqual((updated["config_state"], updated["model"]), ("updated", "claude-opus-5-5"))
            self.assertNotEqual(paths.config_path.read_bytes(), before)

    def test_two_lists_of_steps_one_for_people_and_one_for_a_source_install(self) -> None:
        with _temporary_home() as home:
            report = run_init(_anthropic(api_key_stdin=True), key_stream=io.BytesIO(KEY.encode()))
            vault = home / "LifeContext" / "vault"
            self.assertEqual(report["next_steps"], [
                f'Open {vault} in Obsidian as a vault ("Open folder as vault").',
                "Import a document: lifecontext documents import ~/path/to/lease.pdf",
                'Ask your agent: "What does my lease say about the deposit?"'])
            self.assertEqual([step.split("  ")[0] for step in report["setup_steps"]], [
                "lifecontext preflight", "lifecontext service install --maintenance",
                "lifecontext skills install", "lifecontext doctor"])
            text = render(report)
            self.assertLess(text.index("To finish the setup:"), text.index("Then:"))
            self.assertIn("  3. Ask your agent:", text)

    def test_timezone_detection(self) -> None:
        with tempfile.TemporaryDirectory(dir=scratch_root()) as temporary:
            etc = Path(temporary)
            self.assertEqual(detect_timezone({}, etc), "UTC")
            (etc / "localtime").symlink_to("/usr/share/zoneinfo/Europe/Kyiv")
            self.assertEqual(detect_timezone({}, etc), "Europe/Kyiv")
            (etc / "timezone").write_text("Europe/Vienna\n")
            self.assertEqual(detect_timezone({}, etc), "Europe/Vienna")
            self.assertEqual(detect_timezone({"TZ": ":Asia/Tokyo"}, etc), "Asia/Tokyo")
            self.assertEqual(detect_timezone({"TZ": "EST5EDT,M3.2.0"}, etc), "Europe/Vienna")


class FirstRetainTests(unittest.TestCase):
    """The first Journal entry after a fresh init, which once failed on the missing entries/ folder."""

    def _application(self, report):
        from test_application import ControlledClient

        from lifecontext.application import JournalApplication
        from lifecontext.hindsight_adapter import LifeContextHindsightAdapter
        from lifecontext.vault import ManagedVault

        vault = ManagedVault(report["vault_root"], report["journal_path"])
        return JournalApplication(paths=LifeContextPaths.current(), vault=vault,
                                  hindsight=LifeContextHindsightAdapter(ControlledClient(), bank_id="lifeweave"))

    def _retain(self, app, source_id: str, key: str):
        import asyncio

        return asyncio.run(app.retain(source_id, 1, b"# Lake walk\n\nDate: 2026-10-17\n\nWalked to the lake.\n",
                                      provenance={"source": "test"}, idempotency_key=key))

    def test_first_retain_after_init_writes_the_entry(self) -> None:
        with _temporary_home():
            report = run_init(_anthropic())
            entries = Path(report["journal_path"]) / "entries"
            self.assertEqual(_mode(entries), 0o700)
            app = self._application(report)
            try:
                retained = self._retain(app, "src_first", "first-retain")
                self.assertEqual(app.catalog.get_operation(retained.operation_id)["state"], "completed")
            finally:
                app.close()
            self.assertEqual([path.name for path in entries.iterdir()], ["2026-10-17 - Lake walk.md"])

    def test_entries_folder_is_private_whatever_the_umask(self) -> None:
        from lifecontext.vault import ManagedVault

        with _temporary_home():
            report = run_init(_anthropic())
            entries = Path(report["journal_path"]) / "entries"
            entries.rmdir()
            vault = ManagedVault(report["vault_root"], report["journal_path"])
            previous = os.umask(0o177)
            try:
                vault.safe_write_text("entries/2026-10-17 - Note.md", "A note.\n")
            finally:
                os.umask(previous)
                vault.close()
            self.assertEqual(_mode(entries), 0o700)
            self.assertEqual([path.name for path in entries.iterdir()], ["2026-10-17 - Note.md"])

    def test_missing_entries_folder_is_created_and_reconcile_recovers_a_failed_projection(self) -> None:
        from lifecontext.application import ApplicationError, JournalApplication
        from lifecontext.vault import VaultError

        with _temporary_home():
            report = run_init(_anthropic())
            entries = Path(report["journal_path"]) / "entries"
            entries.rmdir()
            app = self._application(report)
            try:
                with patch.object(JournalApplication, "_allocate_journal_path",
                                  side_effect=VaultError("not_found")), self.assertRaises(ApplicationError):
                    self._retain(app, "src_lost", "lost-projection")
                self.assertFalse(entries.exists())
                self.assertEqual(app.reconcile_projections()["projected"], 1)
            finally:
                app.close()
            self.assertEqual(_mode(entries), 0o700)
            self.assertEqual([path.name for path in entries.iterdir()], ["2026-10-17 - Lake walk.md"])


class InitCommandTests(unittest.TestCase):
    def test_at_a_terminal_init_checks_the_model_and_shows_the_result(self) -> None:
        from lifecontext import cli

        failed = {"status": "fail", "cause": "key_rejected", "message": "the provider rejected the key",
                  "fix": "lifecontext llm set --api-key-stdin < FILE", "reflect": {"status": "skipped"}}
        answers = {provider_menu(): "1"}
        for as_json in (False, True):
            out, err = io.StringIO(), io.StringIO()
            with self.subTest(as_json=as_json), _temporary_home(), \
                    patch.object(sys.stdin, "isatty", return_value=True), \
                    patch.object(cli, "_ask", side_effect=lambda question, default: answers.get(question, "")), \
                    patch.object(cli, "_ask_secret", return_value=KEY), \
                    patch("lifecontext.hindsight.model_probe", return_value=failed) as probe, \
                    redirect_stdout(out), redirect_stderr(err):
                code = cli.main(["init", "--timezone", "UTC", *(["--json"] if as_json else [])])
                self.assertEqual(code, 0, err.getvalue())
                probe.assert_called_once()
                if as_json:
                    report = json.loads(out.getvalue())
                    self.assertEqual(report["model_probe"], failed)
                    self.assertNotIn("error", report)
                else:
                    self.assertIn("Model check failed: the provider rejected the key.\n"
                                  "Fix it with: lifecontext llm set --api-key-stdin < FILE", out.getvalue())
                self.assertNotIn(KEY, out.getvalue() + err.getvalue())

    def test_command_reads_the_key_from_stdin_and_prints_json_without_it(self) -> None:
        with tempfile.TemporaryDirectory(prefix="lifecontext-init-cli.", dir=scratch_root()) as temporary:
            home = Path(temporary).resolve(strict=True)
            env = {"HOME": str(home), "PATH": os.defpath, "PYTHONPATH": str(V1_ROOT / "src"),
                   "LIFEWEAVE_LLM_API_KEY": "ambient-must-not-load"}
            result = subprocess.run(
                [sys.executable, "-m", "lifecontext.cli", "init", "--non-interactive", "--api-key-stdin",
                 "--provider", "anthropic", "--model", "claude-sonnet-5", "--timezone", "Europe/Vienna", "--json"],
                input=KEY + "\n", env=env, cwd=home, capture_output=True, text=True, timeout=60, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["credentials_state"], "stored")
            self.assertNotIn(KEY, result.stdout + result.stderr)
            self.assertNotIn("ambient-must-not-load", (home / ".lifeweave" / ".env").read_text())
            rejected = subprocess.run(
                [sys.executable, "-m", "lifecontext.cli", "init", "--api-key", KEY],
                env=env, cwd=home, capture_output=True, text=True, timeout=60, check=False)
            self.assertEqual(rejected.returncode, 2)
            replaced = subprocess.run(
                [sys.executable, "-m", "lifecontext.cli", "init", "--non-interactive", "--replace-key",
                 "--api-key-stdin", "--json"],
                input="other-key\n", env=env, cwd=home, capture_output=True, text=True, timeout=60, check=False)
            self.assertEqual(replaced.returncode, 0, replaced.stderr)
            self.assertEqual(json.loads(replaced.stdout)["credentials_state"], "replaced")
            missing = subprocess.run(
                [sys.executable, "-m", "lifecontext.cli", "init", "--non-interactive", "--replace-key", "--json"],
                env=env, cwd=home, capture_output=True, text=True, timeout=60, check=False)
            self.assertEqual((missing.returncode, json.loads(missing.stdout)), (1, {"error": "api_key_required"}))

    def test_command_without_a_provider_says_how_to_name_one(self) -> None:
        with tempfile.TemporaryDirectory(prefix="lifecontext-init-cli.", dir=scratch_root()) as temporary:
            home = Path(temporary).resolve(strict=True)
            env = {"HOME": str(home), "PATH": os.defpath, "PYTHONPATH": str(V1_ROOT / "src")}
            for arguments, code, words in ((["--json"], "provider_required", "LIFECONTEXT_PROVIDER"),
                                           (["--provider", "openai"], "model_required", "--model")):
                with self.subTest(arguments=arguments):
                    result = subprocess.run(
                        [sys.executable, "-m", "lifecontext.cli", "init", "--non-interactive", *arguments],
                        env=env, cwd=home, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60,
                        check=False)
                    self.assertEqual(result.returncode, 1)
                    if "--json" in arguments:
                        self.assertEqual(json.loads(result.stdout)["error"], code)
                        self.assertIn(words, json.loads(result.stdout)["message"])
                    else:
                        self.assertIn(words, result.stderr)
            self.assertFalse((home / ".lifeweave" / "config.yaml").exists())


if __name__ == "__main__":
    unittest.main()
