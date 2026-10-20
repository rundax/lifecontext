"""HOME, configuration, and secret-file isolation checks for V1."""

from __future__ import annotations

import os
import stat
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from lifecontext.config import (
    ConfigError,
    JournalLimits,
    LIMIT_CEILINGS,
    _read_config,
    load_settings,
)
from lifecontext.credentials import CredentialError, load_credentials
from lifecontext.paths import LifeContextPaths
from local_paths import scratch_root


@contextmanager
def _temporary_home():
    with tempfile.TemporaryDirectory(
        prefix="lifeweave-credentials-test.", dir=scratch_root()
    ) as temporary:
        home = Path(temporary).resolve(strict=True)
        with patch.dict(
            os.environ,
            {
                "HOME": str(home),
                "HERMES_HOME": str(home / "poisoned-hermes-home"),
                "LIFEWEAVE_LLM_API_KEY": "ambient-secret-must-not-load",
                "OPENROUTER_API_KEY": "ambient-fallback-must-not-load",
            },
        ):
            yield home


class LifeContextPathTests(unittest.TestCase):
    def test_paths_and_private_home_are_below_temporary_process_home(self) -> None:
        with _temporary_home() as home:
            paths = LifeContextPaths.current()
            self.assertEqual(paths.home, home)
            self.assertEqual(paths.lifeweave_home, home / ".lifeweave")
            self.assertEqual(paths.config_path, home / ".lifeweave" / "config.yaml")
            self.assertEqual(paths.credentials_path, home / ".lifeweave" / ".env")
            self.assertEqual(paths.hindsight_profile, "lifeweave")
            self.assertEqual(paths.hindsight_home, home / ".hindsight")
            self.assertEqual(
                paths.hindsight_profile_env_path,
                home / ".hindsight" / "profiles" / "lifeweave.env",
            )

            paths.ensure_private_home()

            self.assertTrue(paths.lifeweave_home.is_dir())
            self.assertFalse(paths.lifeweave_home.is_symlink())
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(paths.lifeweave_home.stat().st_mode), 0o700)
            self.assertTrue(paths.lifeweave_home.is_relative_to(home))

    def test_symlinked_lifeweave_home_is_rejected(self) -> None:
        with _temporary_home() as home:
            paths = LifeContextPaths.current()
            target = home / "other"
            target.mkdir()
            paths.lifeweave_home.symlink_to(target, target_is_directory=True)

            with self.assertRaisesRegex(OSError, "unsafe LifeContext home"):
                paths.ensure_private_home()


class LifeContextConfigTests(unittest.TestCase):
    def _write_config(self, paths: LifeContextPaths, content: str, mode: int = 0o600) -> None:
        paths.lifeweave_home.mkdir(mode=0o700, exist_ok=True)
        paths.config_path.write_text(content, encoding="utf-8")
        if os.name == "posix":
            os.chmod(paths.config_path, mode)

    def test_valid_nested_yaml_loads_from_only_lifeweave_config(self) -> None:
        with _temporary_home() as home:
            paths = LifeContextPaths.current()
            self._write_config(
                paths,
                "llm:\n  provider: openai_compatible\n"
                "  model: local-model\n"
                "  base_url: http://127.0.0.1:5012/v1\n"
                "  reasoning_effort: medium\n",
            )
            (home / "poisoned-hermes-home").mkdir()
            (home / "poisoned-hermes-home" / "config.yaml").write_text(
                "wrong: true\n", encoding="utf-8"
            )

            self.assertEqual(
                load_settings(),
                {
                    "llm": {
                        "provider": "openai_compatible",
                        "model": "local-model",
                        "base_url": "http://127.0.0.1:5012/v1",
                        "reasoning_effort": "medium",
                    },
                },
            )

    def test_absent_configuration_returns_empty_defaults(self) -> None:
        with _temporary_home():
            self.assertEqual(load_settings(), {})

    def test_exact_pyyaml_version_is_required(self) -> None:
        with _temporary_home():
            import importlib.metadata

            self.assertEqual(importlib.metadata.version("PyYAML"), "6.0.3")

    def test_invalid_or_secret_shaped_config_is_redacted(self) -> None:
        cases = (
            ("broken: [secret-marker", "yaml"),
            ("- not-a-mapping", "mapping"),
            ("llm: []", "llm"),
            ("llm:\n  api_key: secret-marker", "credential-shaped"),
            ("nested:\n  access_token: secret-marker", "credential-shaped"),
            ("llm:\n  7: bad-key", "string keys"),
        )
        with _temporary_home():
            paths = LifeContextPaths.current()
            paths.lifeweave_home.mkdir(mode=0o700)
            for content, category in cases:
                with self.subTest(category=category, content=content):
                    self._write_config(paths, content)
                    with self.assertRaises(ConfigError) as caught:
                        load_settings()
                    self.assertIn(category, str(caught.exception))
                    self.assertNotIn("secret-marker", str(caught.exception))

    def test_symlinked_config_is_rejected(self) -> None:
        with _temporary_home() as home:
            paths = LifeContextPaths.current()
            paths.lifeweave_home.mkdir(mode=0o700)
            target = home / "other-config.yaml"
            target.write_text("ok: true\n", encoding="utf-8")
            paths.config_path.symlink_to(target)

            with self.assertRaisesRegex(ConfigError, "unsafe config file"):
                load_settings()

    def test_oversized_and_insecure_config_files_fail_redacted(self) -> None:
        with _temporary_home():
            paths = LifeContextPaths.current()
            self._write_config(paths, "# secret-marker\n" + "x" * (64 * 1024))
            with self.assertRaisesRegex(ConfigError, "config too large") as caught:
                load_settings()
            self.assertNotIn("secret-marker", str(caught.exception))

            self._write_config(paths, "llm:\n  provider: openai\n", mode=0o644)
            with self.assertRaisesRegex(ConfigError, "unsafe config file"):
                load_settings()

    @unittest.skipUnless(os.name == "posix", "ownership is a POSIX property")
    def test_config_owner_and_opened_identity_are_checked(self) -> None:
        with _temporary_home():
            paths = LifeContextPaths.current()
            self._write_config(paths, "llm:\n  provider: openai\n")
            info = paths.config_path.lstat()
            wrong_owner = SimpleNamespace(
                st_mode=info.st_mode,
                st_dev=info.st_dev,
                st_ino=info.st_ino,
                st_uid=info.st_uid + 1,
            )
            with (
                patch("lifecontext.config.Path.lstat", return_value=wrong_owner),
                patch("lifecontext.config.os.open") as open_path,
            ):
                with self.assertRaisesRegex(ConfigError, "unsafe config file"):
                    _read_config(paths.config_path)
            open_path.assert_not_called()

            swapped_identity = SimpleNamespace(
                st_mode=info.st_mode,
                st_dev=info.st_dev,
                st_ino=info.st_ino + 1,
                st_uid=info.st_uid,
            )
            with (
                patch("lifecontext.config.Path.lstat", return_value=swapped_identity),
                patch("lifecontext.config.os.fdopen") as read_file,
            ):
                with self.assertRaisesRegex(ConfigError, "unsafe config file"):
                    _read_config(paths.config_path)
            read_file.assert_not_called()

    def test_limits_are_typed_bounded_and_have_fixed_defaults(self) -> None:
        with _temporary_home():
            paths = LifeContextPaths.current()
            self._write_config(
                paths,
                "llm:\n  provider: openai\n  model: local-model\n"
                "limits:\n  max_text_bytes: 4096\n  max_result_count: 5\n",
            )
            self.assertEqual(
                JournalLimits.from_config(load_settings()),
                JournalLimits(
                    max_text_bytes=4096,
                    max_file_bytes=16_777_216,
                    max_query_bytes=8_192,
                    max_result_count=5,
                    max_response_bytes=1_048_576,
                ),
            )

    def test_invalid_limit_types_names_and_ceilings_fail_redacted(self) -> None:
        cases = (
            ("max_text_bytes: true", "integer"),
            ('max_text_bytes: "8"', "integer"),
            ("max_other_bytes: 8", "unknown limit"),
            (
                f"max_file_bytes: {LIMIT_CEILINGS['max_file_bytes'] + 1}",
                "out of range",
            ),
            ("max_result_count: 0", "out of range"),
        )
        with _temporary_home():
            paths = LifeContextPaths.current()
            for value, category in cases:
                with self.subTest(category=category, value=value):
                    self._write_config(paths, f"limits:\n  {value}\n")
                    with self.assertRaises(ConfigError) as caught:
                        load_settings()
                    self.assertIn(category, str(caught.exception))
                    self.assertNotIn(value, str(caught.exception))

    def test_vault_paths_must_be_absolute_unambiguous_and_contained(self) -> None:
        cases = (
            ("root: relative\n  journal_path: /tmp/journal", "absolute path"),
            (
                "root: /tmp/lifeweave-vault\n  journal_path: /tmp/lifeweave-vault/../other",
                "absolute path",
            ),
            (
                "root: /tmp/lifeweave-vault\n  journal_path: /tmp/lifeweave-vault-elsewhere/Journal",
                "absolute path",
            ),
        )
        with _temporary_home():
            paths = LifeContextPaths.current()
            for vault, category in cases:
                with self.subTest(vault=vault):
                    self._write_config(paths, "vault:\n  " + vault + "\n")
                    with self.assertRaises(ConfigError) as caught:
                        load_settings()
                    self.assertIn(category, str(caught.exception))
                    self.assertNotIn("lifeweave-vault", str(caught.exception))

    def test_valid_vault_paths_are_preserved_for_the_managed_writer(self) -> None:
        with _temporary_home():
            paths = LifeContextPaths.current()
            self._write_config(
                paths,
                "vault:\n  root: /tmp/lifeweave-scratch\n"
                "  journal_path: /tmp/lifeweave-scratch/Journal\n",
            )
            self.assertEqual(
                load_settings()["vault"],
                {
                    "root": "/tmp/lifeweave-scratch",
                    "journal_path": "/tmp/lifeweave-scratch/Journal",
                },
            )

    def test_invalid_config_is_rejected_before_any_catalog_access(self) -> None:
        with _temporary_home():
            paths = LifeContextPaths.current()
            self._write_config(paths, "limits:\n  unknown_limit: secret-marker\n")

            def unexpected_catalog_access(_paths: LifeContextPaths) -> Path:
                raise AssertionError("catalog must not be opened while validating config")

            with patch.object(
                LifeContextPaths,
                "catalog_path",
                property(unexpected_catalog_access),
            ), patch.object(
                LifeContextPaths,
                "archive_dir",
                property(unexpected_catalog_access),
            ):
                with self.assertRaises(ConfigError) as caught:
                    load_settings()
            self.assertIn("unknown limit", str(caught.exception))
            self.assertNotIn("secret-marker", str(caught.exception))


class LifeContextCredentialTests(unittest.TestCase):
    def _write_env(self, paths: LifeContextPaths, content: str, mode: int = 0o600) -> None:
        paths.lifeweave_home.mkdir(mode=0o700, exist_ok=True)
        paths.credentials_path.write_text(content, encoding="utf-8")
        if os.name == "posix":
            os.chmod(paths.credentials_path, mode)

    def test_only_private_lifeweave_env_supplies_credentials(self) -> None:
        with _temporary_home() as home:
            paths = LifeContextPaths.current()
            self._write_env(
                paths,
                "LIFEWEAVE_LLM_API_KEY=lifeweave-key\n",
            )
            hermes_home = Path(os.environ["HERMES_HOME"])
            hermes_home.mkdir()
            (hermes_home / ".env").write_text(
                "LIFEWEAVE_LLM_API_KEY=poisoned-hermes-key\n", encoding="utf-8"
            )

            self.assertEqual(
                load_credentials(),
                {"LIFEWEAVE_LLM_API_KEY": "lifeweave-key"},
            )
            self.assertEqual(os.environ["LIFEWEAVE_LLM_API_KEY"], "ambient-secret-must-not-load")
            self.assertTrue((hermes_home / ".env").exists())
            self.assertTrue(paths.credentials_path.is_relative_to(home))

    def test_missing_credentials_do_not_fall_back_to_ambient_or_hermes(self) -> None:
        with _temporary_home():
            with self.assertRaisesRegex(CredentialError, "missing credentials") as caught:
                load_credentials()
            self.assertNotIn("ambient-secret", str(caught.exception))

    def test_llm_key_can_be_optional_for_a_keyless_endpoint(self) -> None:
        with _temporary_home():
            paths = LifeContextPaths.current()
            self.assertEqual(load_credentials(require_llm_key=False), {})
            self._write_env(paths, "LIFEWEAVE_VISION_API_KEY=vision-key\n")
            self.assertEqual(load_credentials(require_llm_key=False),
                             {"LIFEWEAVE_VISION_API_KEY": "vision-key"})
            with self.assertRaisesRegex(CredentialError, "missing credentials"):
                load_credentials()
            self._write_env(paths, "LIFEWEAVE_VISION_API_KEY=vision-key\n", mode=0o644)
            if os.name == "posix":
                with self.assertRaisesRegex(CredentialError, "unsafe credential file"):
                    load_credentials(require_llm_key=False)

    def test_symlink_and_unsafe_modes_are_rejected(self) -> None:
        with _temporary_home() as home:
            paths = LifeContextPaths.current()
            paths.lifeweave_home.mkdir(mode=0o700)
            target = home / "elsewhere.env"
            target.write_text("LIFEWEAVE_LLM_API_KEY=secret-marker\n", encoding="utf-8")
            paths.credentials_path.symlink_to(target)
            with self.assertRaisesRegex(CredentialError, "unsafe credential file") as caught:
                load_credentials()
            self.assertNotIn("secret-marker", str(caught.exception))

            paths.credentials_path.unlink()
            self._write_env(paths, "LIFEWEAVE_LLM_API_KEY=secret-marker\n", mode=0o644)
            if os.name == "posix":
                with self.assertRaisesRegex(CredentialError, "unsafe credential file") as caught:
                    load_credentials()
                self.assertNotIn("secret-marker", str(caught.exception))

    def test_duplicate_malformed_unknown_and_empty_credentials_fail_redacted(self) -> None:
        cases = (
            (
                "LIFEWEAVE_LLM_API_KEY=secret-marker\n"
                "LIFEWEAVE_LLM_API_KEY=other-marker\n",
                "duplicate credentials",
            ),
            ("not an env entry secret-marker\n", "malformed credentials"),
            (
                "LIFEWEAVE_LLM_API_KEY=first\u2028OPENROUTER_API_KEY=second\n",
                "malformed credentials",
            ),
            ("OTHER_API_KEY=secret-marker\n", "unsupported credential name"),
            ("LIFEWEAVE_LLM_API_KEY=\n", "missing credentials"),
            ("OPENROUTER_API_KEY=secret-marker\n", "unsupported credential name"),
        )
        with _temporary_home():
            paths = LifeContextPaths.current()
            for content, category in cases:
                with self.subTest(category=category):
                    self._write_env(paths, content)
                    with self.assertRaises(CredentialError) as caught:
                        load_credentials()
                    self.assertIn(category, str(caught.exception))
                    self.assertNotIn("secret-marker", str(caught.exception))
                    self.assertNotIn("other-marker", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
