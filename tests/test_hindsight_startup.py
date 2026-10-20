"""Contract and failure tests for the isolated Hindsight startup adapter."""

from __future__ import annotations

import os
import stat
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from lifecontext.hindsight import (
    EMBEDDING_MODEL,
    RERANKER_MODEL,
    TIKTOKEN_URL,
    HindsightStartupError,
    _client_capabilities,
    _load_runtime,
    _prepare_profile_file,
    _probe_until_ready,
    _validate_server_response,
    _validated_profile_config,
    _validated_profile_settings,
    engine_token,
    model_cache_status,
    preflight,
    read_daemon_start_record,
    retain_smoke,
)
from lifecontext.paths import LifeContextPaths
from local_paths import scratch_root


@contextmanager
def _temporary_home():
    with tempfile.TemporaryDirectory(
        prefix="lifeweave-startup-test.", dir=scratch_root()
    ) as temporary:
        home = Path(temporary).resolve(strict=True)
        with patch.dict(
            os.environ,
            {
                "HOME": str(home),
                "HERMES_HOME": str(home / "poisoned-hermes-home"),
                "LIFEWEAVE_LLM_API_KEY": "ambient-secret-must-not-load",
            },
        ):
            yield home


FAKE_DATABASE_URL = "postgresql://hindsight:pw@/hindsight?host=/sock&port=5432"


@contextmanager
def _secured_engine(owner="none"):
    """No real listener, Postgres or HTTP: who holds a port, the socket-only Postgres and the auth probe."""
    owners = owner if callable(owner) else (lambda port: owner)
    with (
        patch("lifecontext.hindsight._daemon_owner", side_effect=lambda port, paths: owners(port)),
        patch("lifecontext.hindsight._listener_pids", return_value=set()),
        patch("lifecontext.hindsight.hindsight_postgres.ensure_socket_only", return_value=FAKE_DATABASE_URL),
        patch("lifecontext.hindsight._require_engine_auth") as auth,
    ):
        yield auth


def _started_here():
    """Nothing listens before the start; afterwards this HOME's daemon does."""
    seen = []

    def owner(port):
        seen.append(port)
        return "none" if len(seen) == 1 else "here"

    return owner


class _FakeClient:
    def __init__(self, base_url=None, api_key=None, timeout=None):
        self.base_url, self.api_key, self.timeout = base_url, api_key, timeout

    async def aretain(
        self, bank_id, content, document_id, metadata=None, retain_async=False,
        tags=None,
    ):
        pass

    async def arecall(self, bank_id, query, max_tokens=4096, budget="mid"):
        pass

    async def areflect(
        self, bank_id, query, budget="low", max_tokens=None, include_facts=False
    ):
        pass

    @property
    def documents(self):
        return self

    async def get_document(self, bank_id, document_id):
        pass

    async def delete_document(self, bank_id, document_id):
        pass


def _modules(*, client=_FakeClient):
    class FakeProfileManager:
        def create_profile(self, name, port_or_config, config=None):
            pass

    def ensure_daemon_running(config, profile=None):
        return True

    def is_daemon_running(profile=None):
        return False

    def get_daemon_url(profile=None):
        return "http://127.0.0.1:9000"

    def stop_daemon(profile=None):
        return True

    return {
        "hindsight_embed.profile_manager": SimpleNamespace(
            ProfileManager=FakeProfileManager
        ),
        "hindsight_embed.daemon_client": SimpleNamespace(
            ensure_daemon_running=ensure_daemon_running,
            is_daemon_running=is_daemon_running,
            get_daemon_url=get_daemon_url,
            stop_daemon=stop_daemon,
        ),
        "hindsight_client": SimpleNamespace(Hindsight=client),
    }


PROBE = {
    "health": "healthy",
    "server_version": "0.9.2",
    "capabilities": {"retain": True, "recall": True, "reflect": True, "features": {}},
    "base_url": "http://127.0.0.1:9000",
}


class _FakeRuntime:
    """hindsight-embed's daemon client; as in the real one, the port is the last profile's."""

    def __init__(self, *, running: bool = False, port: int = 9855):
        self.versions = {"hindsight-all": "0.9.2", "hindsight-embed": "0.9.2", "hindsight-client": "0.9.2"}
        self.calls: list = []
        self.running, self.port = running, port
        runtime = self

        class ProfileManager:
            def create_profile(self, name, config, optional=None):
                runtime.calls.append(("create", dict(config)))
                runtime.port = int(config["HINDSIGHT_API_PORT"])

        self.ProfileManager = ProfileManager
        self.Hindsight = _FakeClient

    def ensure_daemon_running(self, config, profile=None):
        self.calls.append(("ensure", profile))
        self.running = True
        return True

    def is_daemon_running(self, profile=None):
        return self.running

    def get_daemon_url(self, profile=None):
        return f"http://127.0.0.1:{self.port}"

    def stop_daemon(self, profile=None):
        self.calls.append(("stop", self.port))
        self.running = False
        return True


class RuntimeContractTests(unittest.TestCase):
    def test_exact_package_versions_and_supported_symbols_are_required(self) -> None:
        modules = _modules()
        with (
            patch("lifecontext.hindsight.importlib.metadata.version", return_value="0.9.2"),
            patch(
                "lifecontext.hindsight.importlib.import_module",
                side_effect=lambda name: modules[name],
            ),
        ):
            runtime = _load_runtime()

        self.assertEqual(set(runtime.versions), {"hindsight-all", "hindsight-embed", "hindsight-client"})
        self.assertEqual(set(runtime.versions.values()), {"0.9.2"})

    def test_version_drift_has_a_stable_code(self) -> None:
        with patch("lifecontext.hindsight.importlib.metadata.version", return_value="0.10.1"):
            with self.assertRaises(HindsightStartupError) as caught:
                _load_runtime()
        self.assertEqual(caught.exception.code, "unsupported_version")
        self.assertEqual(str(caught.exception), "unsupported_version")

    def test_missing_package_has_a_stable_code(self) -> None:
        with patch("lifecontext.hindsight.importlib.metadata.version", side_effect=KeyError):
            with self.assertRaises(HindsightStartupError) as caught:
                _load_runtime()
        self.assertEqual(caught.exception.code, "missing_package")

    def test_client_capabilities_are_reported_independently(self) -> None:
        class NoRetainAndDocumentsClient:
            def __init__(self, base_url=None, api_key=None, timeout=None):
                pass

            async def arecall(self, bank_id, query, max_tokens=4096, budget="mid"):
                pass

            async def areflect(self, bank_id, query, budget="low", max_tokens=None):
                pass

        missing = _client_capabilities(NoRetainAndDocumentsClient())
        self.assertFalse(missing["aretain"])
        self.assertTrue(missing["arecall"])
        self.assertTrue(missing["areflect"])
        self.assertFalse(missing["documents.get_document"])
        self.assertFalse(missing["documents.delete_document"])
        self.assertFalse(missing["attributed_reflect"])

        class NoAttributedReflect(_FakeClient):
            async def areflect(self, bank_id, query, budget="low", max_tokens=None):
                pass

        degraded = _client_capabilities(NoAttributedReflect())
        self.assertTrue(degraded["areflect"])
        self.assertFalse(degraded["attributed_reflect"])

        complete = _client_capabilities(_FakeClient())
        self.assertEqual(
            complete,
            {
                "aretain": True,
                "arecall": True,
                "areflect": True,
                "documents.get_document": True,
                "documents.delete_document": True,
                "attributed_reflect": True,
            },
        )

        class MissingCallOptions(_FakeClient):
            async def aretain(self, bank_id, content, document_id, retain_async=False):
                pass

            async def arecall(self, bank_id, query):
                pass

            async def areflect(self, bank_id, query, include_facts=False):
                pass

        incomplete = _client_capabilities(MissingCallOptions())
        self.assertFalse(incomplete["aretain"])
        self.assertFalse(incomplete["arecall"])
        self.assertFalse(incomplete["areflect"])

        modules = _modules(client=NoRetainAndDocumentsClient)
        with (
            patch("lifecontext.hindsight.importlib.metadata.version", return_value="0.9.2"),
            patch(
                "lifecontext.hindsight.importlib.import_module",
                side_effect=lambda name: modules[name],
            ),
        ):
            runtime = _load_runtime()
        self.assertIs(runtime.Hindsight, NoRetainAndDocumentsClient)

    def test_server_health_version_and_features_are_checked(self) -> None:
        health = {"status": "healthy"}
        version = {"api_version": "0.9.2", "features": {"worker": True}}
        result = _validate_server_response(health, version)
        self.assertEqual(result["health"], "healthy")
        self.assertEqual(result["server_version"], "0.9.2")
        self.assertEqual(result["version"]["api_version"], "0.9.2")
        self.assertEqual(result["capabilities"]["features"], {"worker": True})

        with self.assertRaises(HindsightStartupError) as caught:
            _validate_server_response(health, {"api_version": "0.10.1", "features": {}})
        self.assertEqual(caught.exception.code, "unsupported_version")

        with self.assertRaises(HindsightStartupError) as caught:
            _validate_server_response(health, {"api_version": "0.9.2"})
        self.assertEqual(caught.exception.code, "missing_capability")

    def test_readiness_probes_only_a_loopback_server_with_a_bounded_contract(self) -> None:
        calls = []

        def get_json(url):
            calls.append(url)
            if url.endswith("/health"):
                return {"status": "healthy"}
            return {"api_version": "0.9.2", "features": {"worker": True}}

        with patch("lifecontext.hindsight._get_json", side_effect=get_json):
            report = _probe_until_ready("http://127.0.0.1:9000")

        self.assertEqual(calls, [
            "http://127.0.0.1:9000/health",
            "http://127.0.0.1:9000/version",
        ])
        self.assertEqual(report["base_url"], "http://127.0.0.1:9000")

        with self.assertRaises(HindsightStartupError) as caught:
            _probe_until_ready("http://example.com:9000")
        self.assertEqual(caught.exception.code, "daemon_unavailable")

    def test_profile_settings_reject_values_that_can_break_env_lines(self) -> None:
        credentials = {"LIFEWEAVE_LLM_API_KEY": "test-secret"}
        for value in (
            "model\nHINDSIGHT_API_LLM_PROVIDER=evil",
            "model=bad",
            "model\u2028HINDSIGHT_API_LLM_PROVIDER=evil",
            "model #comment",
        ):
            with self.subTest(value=value):
                with self.assertRaises(HindsightStartupError) as caught:
                    _validated_profile_config(
                        {"llm": {"provider": "openai", "model": value}},
                        credentials,
                    )
                self.assertEqual(caught.exception.code, "invalid_config")

    def test_invalid_nonsecret_settings_fail_before_missing_credentials(self) -> None:
        with _temporary_home():
            with self.assertRaises(HindsightStartupError) as caught:
                preflight({"llm": {"provider": "openai", "model": "bad\nmodel"}})
        self.assertEqual(caught.exception.code, "invalid_config")

        self.assertEqual(
            _validated_profile_settings(
                {"llm": {"provider": "openai", "model": "model"}}
            ),
            {"provider": "openai", "model": "model"},
        )
        self.assertEqual(
            _validated_profile_settings(
                {"llm": {"provider": "openai", "model": "model"}, "timezone": "Europe/Vienna"}
            ),
            {"provider": "openai", "model": "model"},
        )
        with self.assertRaises(HindsightStartupError):
            _validated_profile_settings({"llm": {"provider": "openai", "model": "model"}, "unknown": 1})
        self.assertEqual(
            _validated_profile_settings(
                {
                    "llm": {"provider": "openai", "model": "model"},
                    "limits": {"max_query_bytes": 256},
                    "vault": {
                        "root": "/tmp/lifeweave-vault",
                        "journal_path": "/tmp/lifeweave-vault/Journal",
                    },
                }
            ),
            {"provider": "openai", "model": "model"},
        )

    def test_provider_contract_is_explicit_and_provider_neutral(self) -> None:
        native_providers = {
            "openai",
            "openai-responses",
            "anthropic",
            "gemini",
            "groq",
            "openrouter",
            "minimax",
            "ollama-cloud",
            "deepseek",
            "volcano",
            "requesty",
            "zai",
            "opencode-go",
            "atlas",
            "fireworks",
        }
        for provider in native_providers:
            with self.subTest(provider=provider):
                llm = {"provider": provider, "model": "provider-model"}
                if provider == "volcano":
                    llm["base_url"] = "https://api.example.test/v1"
                self.assertEqual(
                    _validated_profile_settings({"llm": llm}),
                    {key: value for key, value in llm.items()},
                )

        compatible = _validated_profile_settings(
            {
                "llm": {
                    "provider": "openai_compatible",
                    "model": "provider-model",
                    "base_url": "https://api.example.test/v1",
                    "reasoning_effort": "high",
                }
            }
        )
        self.assertEqual(compatible["provider"], "openai_compatible")
        self.assertEqual(compatible["base_url"], "https://api.example.test/v1")
        self.assertEqual(compatible["reasoning_effort"], "high")
        for effort in ("low", "medium", "high", "xhigh"):
            with self.subTest(reasoning_effort=effort):
                self.assertEqual(
                    _validated_profile_settings(
                        {
                            "llm": {
                                "provider": "openai",
                                "model": "reasoning-model",
                                "reasoning_effort": effort,
                            }
                        }
                    )["reasoning_effort"],
                    effort,
                )

        openrouter = _validated_profile_config(
            {
                "llm": {
                    "provider": "openrouter",
                    "model": "vendor/model",
                    "reasoning_effort": "medium",
                }
            },
            {"LIFEWEAVE_LLM_API_KEY": "test-secret"},
        )
        self.assertEqual(openrouter["HINDSIGHT_API_LLM_PROVIDER"], "openrouter")
        self.assertEqual(openrouter["HINDSIGHT_API_LLM_MODEL"], "vendor/model")
        self.assertEqual(openrouter["HINDSIGHT_API_LLM_REASONING_EFFORT"], "medium")
        self.assertNotIn("HINDSIGHT_API_LLM_BASE_URL", openrouter)
        default_effort = _validated_profile_config(
            {"llm": {"provider": "openai", "model": "model"}},
            {"LIFEWEAVE_LLM_API_KEY": "test-secret"},
        )
        self.assertNotIn("HINDSIGHT_API_LLM_REASONING_EFFORT", default_effort)
        self.assertEqual(
            default_effort["HINDSIGHT_API_RETAIN_MAX_COMPLETION_TOKENS"], "8192"
        )

        for invalid in (
            {"hindsight": {"llm_provider": "openai", "llm_model": "model"}},
            {"llm": {"provider": "OpenRouter", "model": "model"}},
            {"llm": {"provider": "new-provider", "model": "model"}},
            {"llm": {"provider": "openai", "llm_model": "model"}},
            {"llm": {"provider": "openai", "model": " model "}},
            {"llm": {"provider": "openai_compatible", "model": "model"}},
            {"llm": {"provider": "volcano", "model": "model"}},
            {"llm": {"provider": "openai", "model": "model", "reasoning_effort": "extreme"}},
            {"llm": {"provider": "openai", "model": "model", "reasoning_effort": "High"}},
            {"llm": {"provider": "openai", "model": "model", "reasoning_effort": True}},
            {"llm": {"provider": "anthropic", "model": "model", "reasoning_effort": "high"}},
            {"llm": {"provider": "gemini", "model": "model", "reasoning_effort": "high"}},
            {
                "llm": {
                    "provider": "gemini",
                    "model": "model",
                    "base_url": "https://api.example.test",
                }
            },
            {"llm": {"provider": "openai", "model": "model"}, "other": True},
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(HindsightStartupError) as caught:
                    _validated_profile_settings(invalid)
                self.assertEqual(caught.exception.code, "invalid_config")

    def test_provider_base_url_validation_is_strict(self) -> None:
        accepted = (
            "https://api.example.test/v1",
            "http://127.0.0.1:5012/v1",
            "http://[::1]:5012/v1",
        )
        for base_url in accepted:
            with self.subTest(base_url=base_url):
                self.assertEqual(
                    _validated_profile_settings(
                        {
                            "llm": {
                                "provider": "openai_compatible",
                                "model": "model",
                                "base_url": base_url,
                            }
                        }
                    )["base_url"],
                    base_url,
                )

        rejected = (
            "http://api.example.test/v1",
            "https://user:password@api.example.test/v1",
            "https://api.example.test/v1?token=x",
            "https://api.example.test/v1?",
            "https://api.example.test/v1#fragment",
            "https://api.example.test/v1#",
            "https://api.example.test/v1/chat/completions",
            "https://api.example.test/v1/responses",
            "https://api.example.test:bad/v1",
            "https://bad host/v1",
            "https://api.example.test/v1 ",
        )
        for base_url in rejected:
            with self.subTest(base_url=base_url):
                with self.assertRaises(HindsightStartupError) as caught:
                    _validated_profile_settings(
                        {
                            "llm": {
                                "provider": "openai_compatible",
                                "model": "model",
                                "base_url": base_url,
                            }
                        }
                    )
                self.assertEqual(caught.exception.code, "invalid_config")

    def test_profile_url_with_invalid_port_is_rejected(self) -> None:
        with self.assertRaises(HindsightStartupError) as caught:
            _validated_profile_config(
                {
                    "llm": {
                        "provider": "openai_compatible",
                        "model": "model",
                        "base_url": "http://127.0.0.1:bad/v1",
                    }
                },
                {"LIFEWEAVE_LLM_API_KEY": "test-secret"},
            )
        self.assertEqual(caught.exception.code, "invalid_config")

    def test_shared_profile_directory_is_not_repermissioned(self) -> None:
        with _temporary_home():
            paths = LifeContextPaths.current()
            paths.hindsight_profiles_dir.mkdir(parents=True, mode=0o700)
            if os.name == "posix":
                os.chmod(paths.hindsight_profiles_dir, 0o755)
                with self.assertRaises(HindsightStartupError) as caught:
                    _prepare_profile_file(paths)
                self.assertEqual(caught.exception.code, "unsafe_profile_paths")
                self.assertEqual(
                    stat.S_IMODE(paths.hindsight_profiles_dir.stat().st_mode), 0o755
                )

    def test_profile_env_symlink_is_rejected(self) -> None:
        with _temporary_home() as home:
            paths = LifeContextPaths.current()
            _prepare_profile_file(paths)
            paths.hindsight_profile_env_path.unlink()
            target = home / "other.env"
            target.write_text("safe-looking placeholder\n", encoding="utf-8")
            paths.hindsight_profile_env_path.symlink_to(target)
            with self.assertRaises(HindsightStartupError) as caught:
                _prepare_profile_file(paths)
            self.assertEqual(caught.exception.code, "unsafe_profile_file")


class StartupLifecycleTests(unittest.TestCase):
    def _write_credentials(self, paths: LifeContextPaths) -> None:
        paths.ensure_private_home()
        paths.credentials_path.write_text(
            "LIFEWEAVE_LLM_API_KEY=test-secret\n", encoding="utf-8"
        )
        if os.name == "posix":
            os.chmod(paths.credentials_path, 0o600)

    def test_builder_receives_separate_settings_and_credentials_after_private_setup(self) -> None:
        with _temporary_home() as home:
            paths = LifeContextPaths.current()
            self._write_credentials(paths)
            calls = {"create": [], "ensure": []}

            class ProfileManager:
                def create_profile(self, name, config, optional=None):
                    calls["create"].append((name, dict(config)))
                    self.assert_private_paths(paths)
                    paths.hindsight_profile_env_path.write_text(
                        "generated by supported builder\n", encoding="utf-8"
                    )

                @staticmethod
                def assert_private_paths(paths):
                    if os.name == "posix":
                        assert stat.S_IMODE(paths.hindsight_profiles_dir.stat().st_mode) == 0o700
                        assert stat.S_IMODE(paths.hindsight_profile_env_path.stat().st_mode) == 0o600

            def ensure(config, profile=None):
                calls["ensure"].append((dict(config), profile))
                return True

            class Client:
                def __init__(self, base_url, api_key, timeout):
                    calls["client"] = (base_url, api_key)
                    self.base_url = base_url
                    self.timeout = timeout

                async def aretain(
                    self, bank_id, content, document_id, metadata=None, retain_async=False,
                    tags=None,
                ):
                    return SimpleNamespace(success=True, var_async=retain_async)

                async def arecall(self, bank_id, query, max_tokens=4096, budget="mid"):
                    return SimpleNamespace(results=[])

                async def areflect(self, bank_id, query, budget="low", max_tokens=None):
                    return SimpleNamespace(text="ok")

                def close(self):
                    pass

            runtime = SimpleNamespace(
                ProfileManager=ProfileManager,
                ensure_daemon_running=ensure,
                is_daemon_running=lambda profile=None: False,
                get_daemon_url=lambda profile=None: "http://127.0.0.1:9000",
                Hindsight=Client,
                versions={"hindsight-all": "0.9.2", "hindsight-embed": "0.9.2", "hindsight-client": "0.9.2"},
            )
            probe = {
                "health": "healthy",
                "server_version": "0.9.2",
                "capabilities": {"retain": True, "recall": True, "reflect": True, "features": {"worker": True}},
                "base_url": "http://127.0.0.1:9000",
            }
            settings = {
                "llm": {
                    "provider": "openai_compatible",
                    "model": "test-model",
                    "base_url": "http://127.0.0.1:5012/v1",
                    "reasoning_effort": "high",
                },
                "limits": {"max_query_bytes": 256},
                "vault": {
                    "root": "/tmp/lifeweave-vault",
                    "journal_path": "/tmp/lifeweave-vault/Journal",
                },
            }

            def load_runtime_after_private_dirs():
                if os.name == "posix":
                    self.assertEqual(
                        stat.S_IMODE(paths.hindsight_home.stat().st_mode), 0o700
                    )
                    self.assertEqual(
                        stat.S_IMODE(paths.hindsight_profiles_dir.stat().st_mode), 0o700
                    )
                return runtime

            with (
                patch(
                    "lifecontext.hindsight._load_runtime",
                    side_effect=load_runtime_after_private_dirs,
                ),
                patch("lifecontext.hindsight._probe_until_ready", return_value=probe),
                _secured_engine(lambda port: "here" if calls["ensure"] else "none") as auth,
            ):
                result = preflight(settings)

            profile, env_config = calls["create"][0]
            token = paths.hindsight_api_token_path.read_text(encoding="ascii")
            self.assertRegex(token, r"\A[0-9a-f]{64}\Z")
            self.assertEqual(stat.S_IMODE(paths.hindsight_api_token_path.stat().st_mode), 0o600)
            self.assertEqual(env_config["HINDSIGHT_API_TENANT_EXTENSION"],
                             "hindsight_api.extensions.builtin.tenant:ApiKeyTenantExtension")
            self.assertEqual(env_config["HINDSIGHT_API_TENANT_API_KEY"], token)
            self.assertEqual(env_config["HINDSIGHT_EMBED_API_DATABASE_URL"], FAKE_DATABASE_URL)
            self.assertTrue(20000 <= int(env_config["HINDSIGHT_API_PORT"]) < 30000)
            auth.assert_called_once_with("http://127.0.0.1:9000", token)
            self.assertEqual(calls["client"], ("http://127.0.0.1:9000", token))
            self.assertEqual((result["engine_auth"], result["postgres"]), ("enforced", "socket_only"))
            self.assertNotIn(token, str(result))
            self.assertEqual(profile, "lifeweave")
            self.assertEqual(env_config["HINDSIGHT_API_HOST"], "127.0.0.1")
            self.assertEqual(env_config["HINDSIGHT_API_LLM_PROVIDER"], "openai")
            self.assertEqual(env_config["HINDSIGHT_API_LLM_MODEL"], "test-model")
            self.assertEqual(env_config["HINDSIGHT_API_LLM_BASE_URL"], "http://127.0.0.1:5012/v1")
            self.assertEqual(env_config["HINDSIGHT_API_LLM_REASONING_EFFORT"], "high")
            self.assertEqual(
                env_config["HINDSIGHT_API_RETAIN_MAX_COMPLETION_TOKENS"], "8192"
            )
            self.assertEqual(env_config["HINDSIGHT_API_LLM_API_KEY"], "test-secret")
            self.assertEqual([key for key in env_config if "database" in key.lower() or "path" in key.lower()],
                             ["HINDSIGHT_EMBED_API_DATABASE_URL"])
            self.assertEqual(calls["ensure"], [(env_config, "lifeweave")])
            self.assertTrue(result["ready"])
            self.assertEqual(result["daemon_status"], "running")
            self.assertEqual(result["health"], "healthy")
            self.assertTrue(result["capabilities"]["aretain"])
            self.assertTrue(result["capabilities"]["arecall"])
            self.assertTrue(result["capabilities"]["areflect"])
            self.assertFalse(result["capabilities"]["attributed_reflect"])
            self.assertFalse(result["capabilities"]["documents.get_document"])
            self.assertFalse(result["capabilities"]["documents.delete_document"])
            self.assertTrue(paths.hindsight_profile_env_path.is_relative_to(home))

    def test_retain_smoke_uses_fixed_document_and_leaves_profile_running(self) -> None:
        with _temporary_home():
            paths = LifeContextPaths.current()
            self._write_credentials(paths)
            calls = {"retain": []}

            class ProfileManager:
                def create_profile(self, name, config):
                    paths.hindsight_profile_env_path.write_text("generated\n", encoding="utf-8")

            class Client:
                def __init__(self, base_url, api_key, timeout):
                    self.base_url = base_url
                    self.timeout = timeout

                async def aretain(
                    self, bank_id, content, document_id, metadata=None, retain_async=False,
                    tags=None,
                ):
                    kwargs = {
                        "bank_id": bank_id,
                        "content": content,
                        "document_id": document_id,
                        "metadata": metadata,
                        "retain_async": retain_async,
                    }
                    calls["retain"].append(kwargs)
                    return SimpleNamespace(
                        success=True, var_async=True, operation_id="provider-op"
                    )

                def close(self):
                    pass

            runtime = SimpleNamespace(
                ProfileManager=ProfileManager,
                ensure_daemon_running=lambda config, profile=None: True,
                is_daemon_running=lambda profile=None: False,
                get_daemon_url=lambda profile=None: "http://127.0.0.1:9000",
                Hindsight=Client,
                versions={"hindsight-all": "0.9.2", "hindsight-embed": "0.9.2", "hindsight-client": "0.9.2"},
            )
            probe = {
                "health": "healthy",
                "server_version": "0.9.2",
                "capabilities": {"retain": True, "recall": True, "reflect": True, "features": {}},
                "base_url": "http://127.0.0.1:9000",
            }
            with (
                patch("lifecontext.hindsight._load_runtime", return_value=runtime),
                patch("lifecontext.hindsight._probe_until_ready", return_value=probe),
                _secured_engine(_started_here()),
            ):
                result = retain_smoke({"llm": {"provider": "openai", "model": "test"}})

            retain_call = calls["retain"][0]
            self.assertEqual(retain_call["bank_id"], "lifeweave-v1-disposable")
            self.assertEqual(retain_call["document_id"], "lifeweave-v1-disposable-smoke")
            self.assertNotIn(retain_call["content"], str(result))
            self.assertEqual(result["retain_status"], "accepted")
            self.assertEqual(result["operation_id"], "provider-op")
            self.assertEqual(result["source_identifier"], "lifeweave-v1-disposable-smoke")
            self.assertEqual(result["daemon_status"], "running")

    @staticmethod
    def _write_profile(paths: LifeContextPaths, text: str) -> None:
        for directory in (paths.hindsight_home, paths.hindsight_profiles_dir):
            directory.mkdir(mode=0o700, exist_ok=True)
            os.chmod(directory, 0o700)
        paths.hindsight_profile_env_path.write_text(text, encoding="utf-8")
        os.chmod(paths.hindsight_profile_env_path, 0o600)

    def _secured_profile(self, paths: LifeContextPaths, port: int) -> str:
        paths.ensure_private_home()
        token = engine_token(paths, create=True)
        self._write_profile(paths, (
            f"HINDSIGHT_API_PORT={port}\n"
            "HINDSIGHT_API_TENANT_EXTENSION=hindsight_api.extensions.builtin.tenant:ApiKeyTenantExtension\n"
            f"HINDSIGHT_API_TENANT_API_KEY={token}\n"
            "HINDSIGHT_API_MCP_ENABLED=false\n"
            f"HINDSIGHT_EMBED_API_DATABASE_URL={FAKE_DATABASE_URL}\n"))
        return token

    def _preflight(self, runtime, owner, **patches):
        with (
            patch("lifecontext.hindsight._load_runtime", return_value=runtime),
            patch("lifecontext.hindsight._probe_until_ready",
                  side_effect=lambda url: {**PROBE, "base_url": url}),
            _secured_engine(owner) as auth,
        ):
            if "auth" in patches:
                auth.side_effect = patches["auth"]
            return preflight({"llm": {"provider": "openai", "model": "test"}}), auth

    def test_preexisting_profile_is_neither_reconfigured_nor_stopped(self) -> None:
        with _temporary_home():
            paths = LifeContextPaths.current()
            self._write_credentials(paths)
            token = self._secured_profile(paths, 24001)
            runtime = _FakeRuntime(running=True, port=24001)
            result, auth = self._preflight(runtime, "here")

            self.assertEqual(runtime.calls, [])
            auth.assert_called_once_with("http://127.0.0.1:24001", token)
            self.assertEqual(result["daemon_status"], "running")
            self.assertEqual(result["base_url"], "http://127.0.0.1:24001")

    def test_a_daemon_from_before_engine_auth_is_stopped_and_moved_to_its_own_port(self) -> None:
        with _temporary_home():
            paths = LifeContextPaths.current()
            self._write_credentials(paths)
            self._write_profile(paths, "HINDSIGHT_API_PORT=9855\nHINDSIGHT_API_LLM_MODEL=old\n")
            runtime = _FakeRuntime(running=True, port=9855)
            order = runtime.calls

            def postgres(paths):
                order.append(("postgres",))
                return FAKE_DATABASE_URL

            with patch("lifecontext.hindsight.hindsight_postgres.ensure_socket_only", side_effect=postgres), \
                    patch("lifecontext.hindsight._listener_pids", return_value=set()), \
                    patch("lifecontext.hindsight._daemon_owner", side_effect=lambda port, paths: (
                        "here" if runtime.running else "none")), \
                    patch("lifecontext.hindsight._load_runtime", return_value=runtime), \
                    patch("lifecontext.hindsight._probe_until_ready",
                          side_effect=lambda url: {**PROBE, "base_url": url}), \
                    patch("lifecontext.hindsight._require_engine_auth"):
                result = preflight({"llm": {"provider": "openai", "model": "test"}})

            self.assertEqual([call[0] for call in order], ["stop", "postgres", "create", "ensure"])
            self.assertEqual(order[0], ("stop", 9855))
            port = int(order[2][1]["HINDSIGHT_API_PORT"])
            self.assertTrue(20000 <= port < 30000)
            self.assertEqual(result["base_url"], f"http://127.0.0.1:{port}")
            self.assertEqual(order[2][1]["HINDSIGHT_API_TENANT_API_KEY"], engine_token(paths))

    def test_another_owners_daemon_on_the_old_port_is_never_stopped_or_reused(self) -> None:
        with _temporary_home():
            paths = LifeContextPaths.current()
            self._write_credentials(paths)
            runtime = _FakeRuntime(running=True, port=9855)
            _result, auth = self._preflight(runtime, lambda port: "elsewhere" if port == 9855 else "here")

            self.assertEqual([call[0] for call in runtime.calls], ["create", "ensure"])
            port = int(runtime.calls[0][1]["HINDSIGHT_API_PORT"])
            self.assertNotEqual(port, 9855)
            self.assertEqual(auth.call_args.args[0], f"http://127.0.0.1:{port}")

    def test_a_listener_that_is_not_this_homes_daemon_gets_no_request(self) -> None:
        with _temporary_home():
            paths = LifeContextPaths.current()
            self._write_credentials(paths)
            runtime = _FakeRuntime()
            clients = []
            runtime.Hindsight = lambda **kwargs: clients.append(kwargs)
            with self.assertRaises(HindsightStartupError) as caught:
                self._preflight(runtime, lambda port: "none" if not runtime.running else "elsewhere")
            self.assertEqual(caught.exception.code, "daemon_port_foreign")
            self.assertEqual(clients, [])

    def test_a_reused_daemon_that_rejects_the_token_is_started_again_once(self) -> None:
        with _temporary_home():
            paths = LifeContextPaths.current()
            self._write_credentials(paths)
            self._secured_profile(paths, 24001)
            runtime = _FakeRuntime(running=True, port=24001)
            failures = [HindsightStartupError("engine_auth_rejected"), None]

            def auth(base_url, token):
                failure = failures.pop(0)
                if failure is not None:
                    raise failure

            result, check = self._preflight(runtime, "here", auth=auth)
            self.assertEqual([call[0] for call in runtime.calls], ["stop", "create", "ensure"])
            self.assertEqual(check.call_count, 2)
            # The new start got a new token, and the profile and the token file hold it.
            first, second = (call.args[1] for call in check.call_args_list)
            self.assertNotEqual(first, second)
            self.assertEqual(engine_token(paths), second)
            self.assertEqual(runtime.calls[1][1]["HINDSIGHT_API_TENANT_API_KEY"], second)
            self.assertEqual(runtime.calls[1][1]["HINDSIGHT_API_MCP_ENABLED"], "false")
            self.assertEqual(result["engine_auth"], "enforced")

    def test_a_new_daemon_that_does_not_enforce_the_token_fails_closed(self) -> None:
        with _temporary_home():
            paths = LifeContextPaths.current()
            self._write_credentials(paths)
            runtime = _FakeRuntime()

            def auth(base_url, token):
                raise HindsightStartupError("engine_auth_disabled")

            with self.assertRaises(HindsightStartupError) as caught:
                self._preflight(runtime, _started_here(), auth=auth)
            self.assertEqual(caught.exception.code, "engine_auth_disabled")
            # The open daemon it just started is stopped again.
            self.assertEqual([call[0] for call in runtime.calls], ["create", "ensure", "stop"])
            self.assertFalse(runtime.running)

    def test_a_new_daemon_that_fails_its_probe_is_stopped_too(self) -> None:
        with _temporary_home():
            paths = LifeContextPaths.current()
            self._write_credentials(paths)
            runtime = _FakeRuntime()
            with (
                patch("lifecontext.hindsight._load_runtime", return_value=runtime),
                patch("lifecontext.hindsight._probe_until_ready",
                      side_effect=HindsightStartupError("unsupported_version")),
                _secured_engine(_started_here()) as auth,
                self.assertRaises(HindsightStartupError) as caught,
            ):
                preflight({"llm": {"provider": "openai", "model": "test"}})
            self.assertEqual(caught.exception.code, "unsupported_version")
            self.assertEqual([call[0] for call in runtime.calls], ["create", "ensure", "stop"])
            auth.assert_not_called()

    def test_an_unfinished_move_from_an_old_install_leaves_nothing_running(self) -> None:
        with _temporary_home():
            paths = LifeContextPaths.current()
            self._write_credentials(paths)
            self._write_profile(paths, "HINDSIGHT_API_PORT=9855\n")
            runtime = _FakeRuntime(running=True, port=9855)

            def ensure(config, profile=None):
                runtime.calls.append(("ensure", profile))
                raise RuntimeError("model download failed")

            runtime.ensure_daemon_running = ensure
            with patch("lifecontext.hindsight.hindsight_postgres.stop") as stop_postgres, \
                    self.assertRaises(HindsightStartupError) as caught:
                self._preflight(runtime, lambda port: "here" if runtime.running else "none")
            self.assertEqual(caught.exception.code, "daemon_unavailable")
            self.assertEqual([call[0] for call in runtime.calls], ["stop", "create", "ensure"])
            stop_postgres.assert_called_once_with(paths)

    def test_provider_failure_is_redacted_without_stopping_the_profile(self) -> None:
        with _temporary_home():
            paths = LifeContextPaths.current()
            self._write_credentials(paths)
            class ProfileManager:
                def create_profile(self, name, config):
                    paths.hindsight_profile_env_path.write_text("generated\n", encoding="utf-8")

            def ensure(config, profile=None):
                print("raw-provider-secret-and-detail")
                raise RuntimeError("raw-provider-secret-and-detail")

            runtime = SimpleNamespace(
                ProfileManager=ProfileManager,
                ensure_daemon_running=ensure,
                is_daemon_running=lambda profile=None: False,
                get_daemon_url=lambda profile=None: "http://127.0.0.1:9000",
                Hindsight=object,
                versions={"hindsight-all": "0.9.2", "hindsight-embed": "0.9.2", "hindsight-client": "0.9.2"},
            )
            import io
            from contextlib import redirect_stdout

            captured = io.StringIO()
            with (
                patch("lifecontext.hindsight._load_runtime", return_value=runtime),
                redirect_stdout(captured),
                _secured_engine(),
            ):
                with self.assertRaises(HindsightStartupError) as caught:
                    preflight({"llm": {"provider": "openai", "model": "test"}})

            self.assertEqual(caught.exception.code, "daemon_unavailable")
            self.assertNotIn("raw-provider-secret", str(caught.exception))
            self.assertNotIn("raw-provider-secret", captured.getvalue())

    def test_missing_credentials_prevents_profile_startup(self) -> None:
        with _temporary_home():
            with self.assertRaises(HindsightStartupError) as caught:
                preflight({"llm": {"provider": "openai", "model": "test"}})
        self.assertEqual(caught.exception.code, "missing_credentials")

    @staticmethod
    def _cache_model(hub: Path, model: str, *, weights: str = "model.safetensors") -> None:
        revision = "0123456789abcdef0123456789abcdef01234567"
        directory = hub / ("models--" + model.replace("/", "--"))
        (directory / "refs").mkdir(parents=True, exist_ok=True)
        (directory / "refs" / "main").write_text(revision)
        snapshot = directory / "snapshots" / revision
        snapshot.mkdir(parents=True, exist_ok=True)
        (directory / "blobs").mkdir(exist_ok=True)
        for name in ("config.json", weights):
            (directory / "blobs" / name).write_text("{}")
            (snapshot / name).symlink_to(directory / "blobs" / name)

    def _offline_runtime(self, seen: list):
        def ensure(config, profile=None):
            seen.append({name: os.environ.get(name) for name in
                         ("HF_HUB_OFFLINE", "LITELLM_LOCAL_MODEL_COST_MAP", "TIKTOKEN_CACHE_DIR")})
            return True

        class ProfileManager:
            def create_profile(self, name, config, optional=None):
                pass

        return SimpleNamespace(
            ProfileManager=ProfileManager, ensure_daemon_running=ensure,
            is_daemon_running=lambda profile=None: False,
            get_daemon_url=lambda profile=None: "http://127.0.0.1:9000", Hindsight=_FakeClient,
            versions={"hindsight-all": "0.9.2", "hindsight-embed": "0.9.2", "hindsight-client": "0.9.2"},
        )

    def test_model_cache_status_reads_a_fake_hub_cache(self) -> None:
        with _temporary_home() as home:
            paths = LifeContextPaths.current()
            hub = home / "hf" / "hub"
            environ = {"HF_HOME": str(home / "hf")}
            status = model_cache_status(paths, environ)
            self.assertEqual(status["hub_cache"], str(hub))
            self.assertFalse(status["complete"])
            self.assertFalse(status["tiktoken"]["cached"])
            self._cache_model(hub, EMBEDDING_MODEL)
            self._cache_model(hub, RERANKER_MODEL, weights="pytorch_model.bin")
            self.assertTrue(model_cache_status(paths, environ)["complete"])
            self.assertEqual(model_cache_status(paths, {"HF_HUB_CACHE": str(hub)})["hub_cache"], str(hub))
            self.assertFalse(model_cache_status(paths, {"XDG_CACHE_HOME": str(home / "x")})["complete"])
            partial = hub / ("models--" + RERANKER_MODEL.replace("/", "--")) / "blobs" / "abc.incomplete"
            partial.write_text("")
            status = model_cache_status(paths, environ)
            self.assertEqual((status["models"]["embedding"]["cached"], status["models"]["reranker"]["cached"]),
                             (True, False))
            paths.tiktoken_cache_dir.mkdir(parents=True)
            import hashlib
            (paths.tiktoken_cache_dir / hashlib.sha1(TIKTOKEN_URL.encode()).hexdigest()).write_bytes(b"x")
            self.assertTrue(model_cache_status(paths, environ)["tiktoken"]["cached"])

    def test_daemon_starts_offline_only_once_both_models_are_cached(self) -> None:
        probe = {"health": "healthy", "server_version": "0.9.2", "base_url": "http://127.0.0.1:9000",
                 "capabilities": {"retain": True, "recall": True, "reflect": True, "features": {}}}
        with _temporary_home() as home:
            self._write_credentials(LifeContextPaths.current())
            paths = LifeContextPaths.current()
            with patch.dict(os.environ, {"HF_HOME": str(home / "hf")}):
                for name in ("HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE", "HF_HUB_OFFLINE",
                             "LITELLM_LOCAL_MODEL_COST_MAP", "TIKTOKEN_CACHE_DIR"):
                    os.environ.pop(name, None)
                seen: list = []
                for cached in (False, True):
                    if cached:
                        self._cache_model(home / "hf" / "hub", EMBEDDING_MODEL)
                        self._cache_model(home / "hf" / "hub", RERANKER_MODEL)
                    with (
                        patch("lifecontext.hindsight._load_runtime", return_value=self._offline_runtime(seen)),
                        patch("lifecontext.hindsight._probe_until_ready", return_value=dict(probe)),
                        patch("lifecontext.hindsight._profile_file_is_private", return_value=True),
                        _secured_engine(_started_here()),
                    ):
                        preflight({"llm": {"provider": "openai", "model": "test"}})
                    self.assertEqual(read_daemon_start_record(paths)["hf_hub_offline"], cached)
                    self.assertNotIn("HF_HUB_OFFLINE", os.environ)
                    self.assertNotIn("LITELLM_LOCAL_MODEL_COST_MAP", os.environ)
                self.assertEqual([item["HF_HUB_OFFLINE"] for item in seen], [None, "1"])
                self.assertEqual({item["LITELLM_LOCAL_MODEL_COST_MAP"] for item in seen}, {"True"})
                self.assertEqual({item["TIKTOKEN_CACHE_DIR"] for item in seen}, {str(paths.tiktoken_cache_dir)})
                self.assertEqual(stat.S_IMODE(paths.tiktoken_cache_dir.stat().st_mode), 0o700)
                record = paths.hindsight_start_record_path.read_text()
                self.assertNotIn("test-secret", record)

    def test_loopback_openai_compatible_endpoint_needs_no_key(self) -> None:
        loopback = {"provider": "openai_compatible", "model": "local-model",
                    "base_url": "http://127.0.0.1:11434/v1"}
        config = _validated_profile_config({"llm": loopback}, {})
        self.assertEqual(config["HINDSIGHT_API_LLM_API_KEY"], "local")
        self.assertEqual(config["HINDSIGHT_API_LLM_PROVIDER"], "openai")
        keyed = _validated_profile_config({"llm": loopback}, {"LIFEWEAVE_LLM_API_KEY": "own-key"})
        self.assertEqual(keyed["HINDSIGHT_API_LLM_API_KEY"], "own-key")
        for llm in (
            {**loopback, "base_url": "https://api.example.test/v1"},
            {"provider": "openai", "model": "m", "base_url": "http://127.0.0.1:11434/v1"},
            {"provider": "anthropic", "model": "m"},
        ):
            with self.subTest(llm=llm), self.assertRaises(HindsightStartupError) as caught:
                _validated_profile_config({"llm": llm}, {})
            self.assertEqual(caught.exception.code, "missing_credentials")

        with _temporary_home():
            calls = []

            class ProfileManager:
                def create_profile(self, name, config, optional=None):
                    calls.append(dict(config))

            runtime = SimpleNamespace(
                ProfileManager=ProfileManager,
                ensure_daemon_running=lambda config, profile=None: True,
                is_daemon_running=lambda profile=None: False,
                get_daemon_url=lambda profile=None: "http://127.0.0.1:9000",
                Hindsight=_FakeClient,
                versions={"hindsight-all": "0.9.2", "hindsight-embed": "0.9.2", "hindsight-client": "0.9.2"},
            )
            probe = {"health": "healthy", "server_version": "0.9.2", "base_url": "http://127.0.0.1:9000",
                     "capabilities": {"retain": True, "recall": True, "reflect": True, "features": {}}}
            with (
                patch("lifecontext.hindsight._load_runtime", return_value=runtime),
                patch("lifecontext.hindsight._probe_until_ready", return_value=probe),
                patch("lifecontext.hindsight._profile_file_is_private", return_value=True),
                _secured_engine(_started_here()),
            ):
                result = preflight({"llm": loopback})
            self.assertTrue(result["ready"])
            self.assertEqual(calls[0]["HINDSIGHT_API_LLM_API_KEY"], "local")
            self.assertFalse(LifeContextPaths.current().credentials_path.exists())


if __name__ == "__main__":
    unittest.main()
