"""Opt-in restart proof for the pinned, disposable Hindsight profile."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import yaml

from lifecontext.application import ApplicationError, JournalApplication
from lifecontext.bundle import decode_bundle
from lifecontext.credentials import load_credentials
from lifecontext.hindsight import (
    PROFILE,
    _load_runtime,
    engine_token,
    preflight,
    stop_profile,
)
from lifecontext.hindsight_adapter import (
    HindsightAdapterError,
    LifeContextHindsightAdapter,
    source_scope_tag,
)
from lifecontext.paths import LifeContextPaths
from lifecontext.vault import ManagedVault
from local_paths import scratch_root

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(_SCRIPTS))
from disposable_migration import convert_migration_export  # noqa: E402


class _RoundTripFailure(RuntimeError):
    def __init__(self, reason_code: str):
        super().__init__(reason_code)
        self.reason_code = reason_code


def _require_roundtrip(condition: bool, reason_code: str) -> None:
    if not condition:
        raise _RoundTripFailure(reason_code)


def _stop_disposable_profile(home: Path) -> bool:
    try:
        with patch.dict(
            os.environ,
            {
                "HOME": str(home),
                "HERMES_HOME": str(home / "hermes-home-must-not-be-used"),
            },
        ):
            paths = LifeContextPaths.current()
            if not paths.hindsight_profile_env_path.exists():
                return True
            # The daemon, then the socket-only Postgres LifeContext runs for it.
            stop_profile(paths)
            return not _load_runtime().is_daemon_running(profile=PROFILE)
    except Exception:
        return False


def _legacy_migration_export(root: Path, records: list) -> Path:
    """Build a historical-format fixture without loading the retired writer."""
    from disposable_migration import MIGRATION_EXPORT_FORMAT

    exported = root / "legacy-export"
    exported.mkdir(mode=0o700)
    items = []
    for record in records:
        items.append({"source_id": record.source_id, "source_version": record.source_version,
                      "lifecycle_state": record.lifecycle_state})
        if record.lifecycle_state == "deleted":
            continue
        version = exported / "sources" / record.source_id / f"v{record.source_version}"
        version.mkdir(mode=0o700, parents=True)
        manifest = record.manifest.to_dict()
        for name, raw in (("manifest.json", json.dumps(manifest).encode()),
                          ("original.bin", record.original_bytes)):
            path = version / name
            path.write_bytes(raw)
            path.chmod(0o600)
    inventory = {
        "format": MIGRATION_EXPORT_FORMAT, "format_version": 1,
        "inventory_complete": True, "owner_namespace": "b" * 32,
        "source_ids": sorted({record.source_id for record in records}),
        "tombstone_ids": sorted({record.source_id for record in records if record.lifecycle_state == "deleted"}),
        "items": sorted(items, key=lambda item: (item["source_id"], item["source_version"])),
    }
    manifest_path = exported / "manifest.json"
    manifest_path.write_text(json.dumps(inventory))
    manifest_path.chmod(0o600)
    return exported


def _run_bundle_import_worker(payload_path: Path) -> dict[str, object]:
    """Import into a fresh child-process profile and emit content-free proof."""
    payload: dict[str, object] = {}
    app = None
    vault = None
    client = None
    profile_stopped = True
    stage = "target_setup"
    model_name: str | None = None
    verified_stages: list[str] = []
    proof_scope = "synthetic_legacy_migration_export_v1_to_fresh_disposable_home_profile_and_vault"
    report: dict[str, object] = {
        "category": "v1_bundle_roundtrip",
        "input_format": "synthetic_legacy_migration_export_v1",
        "proof_scope": proof_scope,
        "reason_code": "target_worker_aborted",
        "status": "FAILED",
        "profile_stopped": False,
    }
    root: Path | None = None
    home: Path | None = None
    evidence_path: Path | None = None
    try:
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
        root = Path(payload["root"]).resolve(strict=True)
        home = Path(payload["target_home"]).resolve(strict=True)
        bundle_path = Path(payload["bundle_path"]).resolve(strict=True)
        vault_root = Path(payload["vault_root"]).resolve(strict=False)
        journal_path = Path(payload["journal_path"]).resolve(strict=False)
        expected_rows = payload["expected"]
        active_source_id = payload["active_source_id"]
        active_checksum = payload["active_checksum"]
        active_query = payload["active_query"]
        expected_bundle_checksum = payload["bundle_sha256"]
        _require_roundtrip(
            all(path.is_relative_to(root) for path in (home, bundle_path, vault_root, journal_path)),
            "target_path_outside_scratch",
        )
        _require_roundtrip(isinstance(expected_rows, list), "invalid_expected_inventory")
        evidence_path = root / "v1_bundle_roundtrip.json"
        with patch.dict(
            os.environ,
            {
                "HOME": str(home),
                "HERMES_HOME": str(root / "target-hermes-home-must-not-be-used"),
            },
        ):
            paths = LifeContextPaths.current()
            paths.ensure_private_home()
            settings = yaml.safe_load(paths.config_path.read_text(encoding="utf-8"))
            _require_roundtrip(isinstance(settings, dict), "target_config_invalid")
            llm_settings = settings.get("llm")
            model_name = llm_settings.get("model") if isinstance(llm_settings, dict) else None
            _require_roundtrip(isinstance(model_name, str) and bool(model_name), "test_model_missing")
            profile_stopped = False
            stage = "target_profile_start"
            startup = preflight(settings)
            runtime = _load_runtime()
            _require_roundtrip(
                all(version == "0.9.2" for version in runtime.versions.values()),
                "hindsight_version_mismatch",
            )
            client = runtime.Hindsight(base_url=startup["base_url"], api_key=engine_token(paths), timeout=60.0)
            adapter = LifeContextHindsightAdapter(client, bank_id=PROFILE)
            vault = ManagedVault(vault_root, journal_path)
            app = JournalApplication(paths=paths, hindsight=adapter, vault=vault)

            bundle = bundle_path.read_bytes()
            stage = "bundle_decode"
            _require_roundtrip(
                hashlib.sha256(bundle).hexdigest() == expected_bundle_checksum,
                "bundle_digest_mismatch",
            )
            records = decode_bundle(bundle, limits=app.limits)
            observed_rows = [
                {
                    "source_id": record.source_id,
                    "source_version": record.source_version,
                    "lifecycle_state": record.lifecycle_state,
                    "sha256": record.manifest.sha256 if record.manifest else None,
                }
                for record in records
            ]
            _require_roundtrip(
                sorted(observed_rows, key=lambda item: (item["source_id"], item["source_version"]))
                == sorted(expected_rows, key=lambda item: (item["source_id"], item["source_version"])),
                "source_inventory_mismatch",
            )

            stage = "bundle_import"

            async def verify_import() -> list:
                nonlocal client, stage
                try:
                    imported = await app.import_bundle(
                        bundle, idempotency_key="roundtrip-import-" + uuid.uuid4().hex
                    )
                    _require_roundtrip(
                        imported["item_count"] == len(expected_rows), "import_count_mismatch"
                    )
                    stage = "active_exact_evidence"
                    active = app.exact_evidence(active_source_id, 1)
                    _require_roundtrip(
                        active.manifest.source_id == active_source_id
                        and active.manifest.source_version == 1
                        and active.manifest.sha256 == active_checksum
                        and hashlib.sha256(active.original_bytes).hexdigest() == active_checksum,
                        "active_exact_evidence_mismatch",
                    )
                    forgotten_rows = [
                        row for row in expected_rows if row["lifecycle_state"] == "forgotten"
                    ]
                    deleted_rows = [
                        row for row in expected_rows if row["lifecycle_state"] == "deleted"
                    ]
                    _require_roundtrip(
                        len(forgotten_rows) == 1 and len(deleted_rows) == 1,
                        "lifecycle_inventory_mismatch",
                    )
                    forgotten_id = forgotten_rows[0]["source_id"]
                    deleted_id = deleted_rows[0]["source_id"]
                    stage = "lifecycle_evidence"
                    forgotten = app.exact_evidence(forgotten_id, 1)
                    _require_roundtrip(
                        hashlib.sha256(forgotten.original_bytes).hexdigest()
                        == forgotten_rows[0]["sha256"],
                        "forgotten_exact_evidence_mismatch",
                    )
                    _require_roundtrip(
                        app.catalog.get_tombstone(forgotten_id, 1)["tombstone_type"]
                        == "forgotten",
                        "forgotten_tombstone_mismatch",
                    )
                    _require_roundtrip(
                        app.catalog.get_tombstone(deleted_id, 1)["tombstone_type"] == "deleted",
                        "deleted_tombstone_mismatch",
                    )
                    try:
                        app.exact_evidence(deleted_id, 1)
                    except ApplicationError:
                        pass
                    else:
                        raise _RoundTripFailure("deleted_content_available")

                    stage = "forgotten_hindsight_lookup"
                    forgotten_document = await adapter.get_document(forgotten_id)
                    stage = "deleted_hindsight_lookup"
                    deleted_document = await adapter.get_document(deleted_id)
                    _require_roundtrip(
                        not forgotten_document and not deleted_document,
                        "forgotten_or_deleted_in_hindsight",
                    )
                    stage = "active_hindsight_lookup"
                    target_document = await adapter.get_document(active_source_id)
                    target_metadata = target_document.get("document_metadata")
                    _require_roundtrip(
                        target_document.get("id") == active_source_id
                        and isinstance(target_metadata, dict)
                        and target_metadata.get("source_version") == "1"
                        and target_metadata.get("sha256") == active_checksum,
                        "active_hindsight_attribution_mismatch",
                    )
                    stage = "recall_attribution"
                    recall = await app.recall(
                        active_query, max_results=10, max_tokens=1024, budget="low"
                    )
                    _require_roundtrip(any(
                        item["evidence"]["source_id"] == active_source_id
                        and item["evidence"]["source_version"] == 1
                        and item["evidence"]["sha256"] == active_checksum
                        for item in recall["results"]
                    ), "recall_attribution_mismatch")
                    stage = "reflect_attribution"
                    reflection = await app.reflect(
                        active_query,
                        max_results=10,
                        max_tokens=1024,
                        budget="low",
                    )
                    _require_roundtrip(any(
                        item["source_id"] == active_source_id
                        and item["source_version"] == 1
                        and item["sha256"] == active_checksum
                        for item in reflection["evidence"]
                    ), "reflect_attribution_mismatch")
                    verified_stages.append("reflect_attribution")

                    stage = "roundtrip_export"
                    roundtrip = decode_bundle(app.export_bundle(), limits=app.limits)
                    roundtrip_rows = [
                        {
                            "source_id": record.source_id,
                            "source_version": record.source_version,
                            "lifecycle_state": record.lifecycle_state,
                            "sha256": record.manifest.sha256 if record.manifest else None,
                        }
                        for record in roundtrip
                    ]
                    _require_roundtrip(
                        sorted(
                            roundtrip_rows,
                            key=lambda item: (item["source_id"], item["source_version"]),
                        )
                        == sorted(
                            expected_rows,
                            key=lambda item: (item["source_id"], item["source_version"]),
                        ),
                        "roundtrip_inventory_mismatch",
                    )
                    verified_stages.append("complete_inventory_comparison")
                    _require_roundtrip(
                        (journal_path / app.catalog.get_projection_state(active_source_id, 1)["projection_path"]).is_file()
                        and not (journal_path / "entries" / f"{forgotten_id}-v1.md").exists()
                        and not (journal_path / "entries" / f"{deleted_id}-v1.md").exists(),
                        "vault_projection_mismatch",
                    )
                    stage = "vault_reconciliation"
                    reconciliation = await app.recover_startup()
                    _require_roundtrip(
                        reconciliation["unresolved"] == 0
                        and reconciliation["projections_unresolved"] == 0
                        and reconciliation["projections_examined"] == 0
                        and app.catalog.list_recoverable_projection_states(limit=10) == []
                        and app.catalog.list_reconciliation_facts() == [],
                        "vault_reconciliation_failed",
                    )
                    verified_stages.append("vault_reconciliation")
                    HindsightRestartRoundTripTests._assert_contained(root, (
                        paths.lifeweave_home,
                        paths.config_path,
                        paths.credentials_path,
                        paths.archive_dir,
                        paths.catalog_path,
                        paths.hindsight_home,
                        paths.hindsight_profiles_dir,
                        paths.hindsight_profile_env_path,
                        vault_root,
                        journal_path,
                        journal_path / "entries",
                    ))
                    return roundtrip
                finally:
                    if client is not None:
                        closing_client = client
                        client = None
                        await closing_client.aclose()

            roundtrip = asyncio.run(verify_import())
            report = {
                "category": "v1_bundle_roundtrip",
                "input_format": "synthetic_legacy_migration_export_v1",
                "proof_scope": proof_scope,
                "model": model_name,
                "verified_stages": list(verified_stages),
                "item_count": len(roundtrip),
                "bundle_sha256": expected_bundle_checksum,
                "hindsight_scope": "fresh_disposable_profile",
                "status": "PASSED",
                "profile_stopped": False,
            }
    except _RoundTripFailure as exc:
        report = {
            "category": "v1_bundle_roundtrip",
            "reason_code": exc.reason_code,
            "status": "FAILED",
            "profile_stopped": False,
        }
    except Exception as exc:
        detail = (
            exc.code
            if isinstance(exc, (ApplicationError, HindsightAdapterError))
            else type(exc).__name__
        )
        report = {
            "category": "v1_bundle_roundtrip",
            "reason_code": f"{stage}_{detail}",
            "status": "FAILED",
            "profile_stopped": False,
        }
    except BaseException as exc:
        report = {
            "category": "v1_bundle_roundtrip",
            "reason_code": f"{stage}_{type(exc).__name__}",
            "status": "FAILED",
            "profile_stopped": False,
        }
        raise
    finally:
        if report.get("reason_code") == "target_worker_aborted":
            report["reason_code"] = f"{stage}_aborted"
        try:
            if app is not None:
                app.close()
                app = None
                vault = None
            elif vault is not None:
                vault.close()
                vault = None
        except Exception:
            report = {
                "category": "v1_bundle_roundtrip",
                "input_format": "synthetic_legacy_migration_export_v1",
                "reason_code": "target_resources_close_failed",
                "status": "FAILED",
                "profile_stopped": False,
            }
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    report = {
                        "category": "v1_bundle_roundtrip",
                        "input_format": "synthetic_legacy_migration_export_v1",
                        "reason_code": "target_client_close_failed",
                        "status": "FAILED",
                        "profile_stopped": False,
                    }
            if home is not None:
                profile_stopped = _stop_disposable_profile(home)
                report["profile_stopped"] = profile_stopped
                if not profile_stopped:
                    report["status"] = "FAILED"
                    report["reason_code"] = "target_profile_stop_failed"
                if evidence_path is not None and root is not None:
                    report["input_format"] = "synthetic_legacy_migration_export_v1"
                    try:
                        descriptor = os.open(
                            evidence_path,
                            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                            0o600,
                        )
                        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                            json.dump(report, stream, sort_keys=True)
                            stream.write("\n")
                    except FileExistsError:
                        pass
                    except OSError:
                        report = {
                            "category": "v1_bundle_roundtrip",
                            "input_format": "synthetic_legacy_migration_export_v1",
                            "proof_scope": proof_scope,
                            "model": model_name,
                            "verified_stages": list(verified_stages),
                            "reason_code": "evidence_report_write_failed",
                            "status": "FAILED",
                            "profile_stopped": profile_stopped,
                        }
                    else:
                        report["model"] = model_name
                        report["proof_scope"] = proof_scope
                        report["verified_stages"] = list(verified_stages)
    return report


def _worker_environment(home: Path, root: Path) -> dict[str, str]:
    v1_root = Path(__file__).resolve().parents[1]
    return {
        "HOME": str(home),
        "HERMES_HOME": str(root / "target-hermes-home-must-not-be-used"),
        "TMPDIR": str(root),
        "TEMP": str(root),
        "TMP": str(root),
        "PATH": os.pathsep.join((str(Path(sys.executable).resolve().parent), os.defpath)),
        "PYTHONPATH": os.pathsep.join((str(v1_root / "src"), str(v1_root / "tests"))),
        "PYTHONNOUSERSITE": "1",
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_CACHE_HOME": str(home / ".cache"),
    }


class HistoricalMigrationFixtureTests(unittest.TestCase):
    def test_historical_format_conversion_needs_no_retired_runtime(self):
        from lifecontext.bundle import BundleRecord
        from lifecontext.custody import SourceManifest
        from lifecontext.config import JournalLimits

        source_id = "jrnl_src_" + "a" * 32
        original = b"synthetic historical export"
        manifest = SourceManifest(source_id, 1, hashlib.sha256(original).hexdigest(),
                                  len(original), {"source": "fixture"})
        records = [BundleRecord(source_id, 1, "active", manifest, original),
                   BundleRecord("jrnl_src_" + "b" * 32, 1, "deleted")]
        with tempfile.TemporaryDirectory(dir=scratch_root()) as temporary:
            exported = _legacy_migration_export(Path(temporary), records)
            bundle, _ = convert_migration_export(exported)
            converted = decode_bundle(bundle, limits=JournalLimits())
            self.assertEqual([(r.source_id, r.lifecycle_state) for r in converted],
                             [(r.source_id, r.lifecycle_state) for r in records])
            self.assertEqual(converted[0].original_bytes, original)
            self.assertEqual(converted[0].manifest, manifest)


@unittest.skipUnless(
    os.environ.get("LIFEWEAVE_HINDSIGHT_ROUNDTRIP") == "1",
    "set LIFEWEAVE_HINDSIGHT_ROUNDTRIP=1 to start a disposable Hindsight profile",
)
class HindsightRestartRoundTripTests(unittest.TestCase):
    def test_v1_bundle_roundtrip_and_restart_recovery_use_exact_evidence(self) -> None:
        root = Path(tempfile.mkdtemp(
            prefix="lifeweave-hindsight-roundtrip.", dir=scratch_root()
        )).resolve(strict=True)
        home = root / "home"
        lifeweave_home = home / ".lifeweave"
        vault_root = root / "vault"
        journal_path = vault_root / "Journal"
        profile_stopped = True
        target_profile_stopped = True
        app = None
        vault = None

        source_home = Path(__file__).resolve().parents[1] / ".dev" / "home"
        try:
            with patch.dict(os.environ, {"HOME": str(source_home)}):
                credentials = load_credentials()
                source_config = yaml.safe_load(
                    (source_home / ".lifeweave" / "config.yaml").read_text(encoding="utf-8")
                )
            source_llm = source_config.get("llm") if isinstance(source_config, dict) else None
            if not isinstance(source_llm, dict):
                self.fail("the V1 disposable LLM settings are unavailable")
            llm = {
                key: source_llm[key]
                for key in ("provider", "model", "base_url", "reasoning_effort")
                if key in source_llm
            }
            self.assertTrue(llm.get("model"), "configure a dev LLM model")

            home.mkdir(mode=0o700)
            lifeweave_home.mkdir(mode=0o700)
            vault_root.mkdir(mode=0o700)
            journal_path.mkdir(mode=0o700)
            (journal_path / "entries").mkdir(mode=0o700)
            config = {
                "llm": llm,
                "vault": {"root": str(vault_root), "journal_path": str(journal_path)},
            }
            config_path = lifeweave_home / "config.yaml"
            config_path.write_text(yaml.safe_dump(config, sort_keys=True), encoding="utf-8")
            config_path.chmod(0o600)
            credentials_path = lifeweave_home / ".env"
            descriptor = os.open(
                credentials_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
            try:
                os.write(
                    descriptor,
                    f"LIFEWEAVE_LLM_API_KEY={credentials['LIFEWEAVE_LLM_API_KEY']}\n".encode(),
                )
            finally:
                os.close(descriptor)

            paths = None
            runtime = None
            daemon_client = None
            with patch.dict(
                os.environ,
                {
                    "HOME": str(home),
                    "HERMES_HOME": str(root / "hermes-home-must-not-be-used"),
                },
            ):
                paths = LifeContextPaths.current()
                checked_paths = (
                    paths.lifeweave_home,
                    paths.config_path,
                    paths.credentials_path,
                    paths.archive_dir,
                    paths.catalog_path,
                    paths.hindsight_home,
                    paths.hindsight_profiles_dir,
                    paths.hindsight_profile_env_path,
                    vault_root,
                    journal_path,
                    journal_path / "entries",
                )
                self._assert_contained(root, checked_paths)
                if not vault_root.is_relative_to(root):
                    self.fail("disposable vault escaped its temporary root")

                settings = {"llm": llm, "vault": config["vault"]}
                profile_stopped = False
                startup = preflight(settings)
                runtime = _load_runtime()
                from hindsight_embed import daemon_client as daemon_client_module

                daemon_client = daemon_client_module
                if any(version != "0.9.2" for version in runtime.versions.values()):
                    self.fail("the installed Hindsight runtime is not pinned")
                self.assertTrue(runtime.is_daemon_running(profile=PROFILE))
                source_id = "jrnl_src_" + uuid.uuid4().hex
                source_version = 1
                source_text = "The synthetic test notebook's cover color is teal."
                source_query = "What color is the synthetic test notebook's cover?"
                source_bytes = source_text.encode("utf-8")
                checksum = hashlib.sha256(source_bytes).hexdigest()

                async def retain_and_lookup():
                    client = runtime.Hindsight(base_url=startup["base_url"], api_key=engine_token(paths),
                                               timeout=60.0)
                    try:
                        adapter = LifeContextHindsightAdapter(client, bank_id=PROFILE)
                        await adapter.retain(
                            source_id,
                            source_text,
                            scope_tag=source_scope_tag(source_id, source_version),
                            metadata={"source_version": str(source_version), "sha256": checksum},
                            retain_async=False,
                        )
                        return await adapter.get_document(source_id)
                    finally:
                        client.close()

                first_document = asyncio.run(retain_and_lookup())
                self.assertEqual(first_document.get("id"), source_id)
                first_metadata = first_document.get("document_metadata")
                self.assertIsInstance(first_metadata, dict)
                self.assertEqual(first_metadata.get("source_version"), str(source_version))
                self.assertEqual(first_metadata.get("sha256"), checksum)

                stop_profile(paths)
                self.assertFalse(runtime.is_daemon_running(profile=PROFILE))
                startup = preflight(settings)
                self.assertTrue(runtime.is_daemon_running(profile=PROFILE))

                retained_again = 0

                async def recover_after_restart():
                    nonlocal app, vault, retained_again, target_profile_stopped
                    client = runtime.Hindsight(base_url=startup["base_url"], api_key=engine_token(paths),
                                               timeout=60.0)
                    try:
                        original_retain = client.aretain

                        async def count_retain(
                            bank_id, content, document_id, metadata=None, retain_async=False,
                            tags=None,
                        ):
                            nonlocal retained_again
                            retained_again += 1
                            return await original_retain(
                                bank_id=bank_id,
                                content=content,
                                document_id=document_id,
                                metadata=metadata,
                                retain_async=retain_async,
                                tags=tags,
                            )

                        client.aretain = count_retain
                        adapter = LifeContextHindsightAdapter(client, bank_id=PROFILE)
                        after_restart = await adapter.get_document(source_id)
                        metadata = after_restart.get("document_metadata")
                        self.assertEqual(after_restart.get("id"), source_id)
                        self.assertIsInstance(metadata, dict)
                        self.assertEqual(metadata.get("source_version"), str(source_version))
                        self.assertEqual(metadata.get("sha256"), checksum)

                        vault = ManagedVault(vault_root, journal_path)
                        app = JournalApplication(paths=paths, hindsight=adapter, vault=vault)
                        manifest = app.custody.write_version(
                            source_id,
                            source_version,
                            source_bytes,
                            provenance={"source": "synthetic-roundtrip"},
                        )
                        app.catalog.register_manifest(manifest)
                        idempotency_key = "roundtrip-" + uuid.uuid4().hex
                        operation = app.catalog.begin_operation(
                            idempotency_key,
                            hashlib.sha256(idempotency_key.encode("ascii")).hexdigest(),
                            "retain",
                        )
                        intent = {
                            "kind": "retain",
                            "source_id": source_id,
                            "source_version": source_version,
                            "sha256": checksum,
                            "size_bytes": len(source_bytes),
                        }
                        operation = app.catalog.set_operation_intent(
                            operation["operation_id"], {"recovery_intent": intent}
                        )
                        app.catalog.transition_operation(
                            operation["operation_id"], "pending", "running",
                            result=operation["result"],
                        )

                        report = await app.recover_startup()
                        recovered = app.catalog.get_operation(operation["operation_id"])
                        status = app.operation_status(operation["operation_id"])
                        self.assertEqual(recovered["state"], "completed")
                        self.assertEqual(report["completed"], 1)
                        self.assertEqual(retained_again, 0)
                        self.assertNotIn(source_text, repr(report))
                        self.assertNotIn(source_text, repr(status))

                        forgotten_id = "jrnl_src_" + uuid.uuid4().hex
                        forgotten_text = (
                            "synthetic forgotten evidence " + uuid.uuid4().hex
                        ).encode()
                        await app.retain(
                            forgotten_id,
                            1,
                            forgotten_text,
                            provenance={"source": "synthetic-roundtrip"},
                            idempotency_key="roundtrip-forget-retain-" + uuid.uuid4().hex,
                        )
                        prepared_forget = await app.curate(
                            {
                                "action": "prepare_forget",
                                "source_id": forgotten_id,
                                "expected_version": 1,
                            },
                            idempotency_key="roundtrip-forget-prepare-" + uuid.uuid4().hex,
                        )
                        await app.curate(
                            {
                                "action": "forget",
                                "source_id": forgotten_id,
                                "expected_version": 1,
                                "prepared_operation_id": prepared_forget["operation_id"],
                            },
                            idempotency_key="roundtrip-forget-apply-" + uuid.uuid4().hex,
                        )

                        deleted_id = "jrnl_src_" + uuid.uuid4().hex
                        deleted_text = (
                            "synthetic deleted evidence " + uuid.uuid4().hex
                        ).encode()
                        await app.retain(
                            deleted_id,
                            1,
                            deleted_text,
                            provenance={"source": "synthetic-roundtrip"},
                            idempotency_key="roundtrip-delete-retain-" + uuid.uuid4().hex,
                        )
                        prepared_delete = await app.curate(
                            {
                                "action": "prepare_delete",
                                "source_id": deleted_id,
                                "expected_version": 1,
                            },
                            idempotency_key="roundtrip-delete-prepare-" + uuid.uuid4().hex,
                        )
                        await app.curate(
                            {
                                "action": "delete",
                                "source_id": deleted_id,
                                "expected_version": 1,
                                "prepared_operation_id": prepared_delete["operation_id"],
                            },
                            idempotency_key="roundtrip-delete-apply-" + uuid.uuid4().hex,
                        )

                        source_bundle = app.export_bundle()
                        records = decode_bundle(source_bundle, limits=app.limits)
                        expected = {
                            (source_id, 1, "active", checksum),
                            (
                                forgotten_id,
                                1,
                                "forgotten",
                                hashlib.sha256(forgotten_text).hexdigest(),
                            ),
                            (deleted_id, 1, "deleted", None),
                        }
                        self.assertEqual(
                            {
                                (
                                    record.source_id,
                                    record.source_version,
                                    record.lifecycle_state,
                                    record.manifest.sha256 if record.manifest else None,
                                )
                                for record in records
                            },
                            expected,
                        )
                        deleted_records = [
                            record for record in records
                            if record.lifecycle_state == "deleted"
                        ]
                        self.assertEqual(len(deleted_records), 1)

                        migration_export = _legacy_migration_export(root, records)
                        bundle, migration_counts = convert_migration_export(migration_export)
                        migrated_records = decode_bundle(bundle, limits=app.limits)
                        self.assertEqual(migration_counts, {
                            "source_count": 3,
                            "version_count": 3,
                            "forgotten_source_count": 1,
                            "deleted_source_count": 1,
                        })
                        self.assertEqual(
                            [
                                (
                                    record.source_id,
                                    record.source_version,
                                    record.lifecycle_state,
                                    record.manifest.sha256 if record.manifest else None,
                                )
                                for record in migrated_records
                            ],
                            [
                                (
                                    record.source_id,
                                    record.source_version,
                                    record.lifecycle_state,
                                    record.manifest.sha256 if record.manifest else None,
                                )
                                for record in records
                            ],
                        )

                        target_home = root / "target-home"
                        target_lifeweave_home = target_home / ".lifeweave"
                        target_vault_root = root / "target-vault"
                        target_journal = target_vault_root / "Journal"
                        target_home.mkdir(mode=0o700)
                        target_lifeweave_home.mkdir(mode=0o700)
                        target_vault_root.mkdir(mode=0o700)
                        target_journal.mkdir(mode=0o700)
                        (target_journal / "entries").mkdir(mode=0o700)
                        target_config = {
                            "llm": llm,
                            "vault": {
                                "root": str(target_vault_root),
                                "journal_path": str(target_journal),
                            },
                        }
                        target_config_path = target_lifeweave_home / "config.yaml"
                        target_config_path.write_text(
                            yaml.safe_dump(target_config, sort_keys=True), encoding="utf-8"
                        )
                        target_config_path.chmod(0o600)
                        target_credentials_path = target_lifeweave_home / ".env"
                        target_descriptor = os.open(
                            target_credentials_path,
                            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                            0o600,
                        )
                        try:
                            os.write(
                                target_descriptor,
                                (
                                    "LIFEWEAVE_LLM_API_KEY="
                                    f"{credentials['LIFEWEAVE_LLM_API_KEY']}\n"
                                ).encode(),
                            )
                        finally:
                            os.close(target_descriptor)
                        bundle_path = root / "source.bundle"
                        bundle_descriptor = os.open(
                            bundle_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
                        )
                        with os.fdopen(bundle_descriptor, "wb") as stream:
                            stream.write(bundle)
                        expected_rows = [
                            {
                                "source_id": item_id,
                                "source_version": version,
                                "lifecycle_state": lifecycle,
                                "sha256": digest,
                            }
                            for item_id, version, lifecycle, digest in sorted(expected)
                        ]
                        payload = {
                            "root": str(root),
                            "target_home": str(target_home),
                            "bundle_path": str(bundle_path),
                            "vault_root": str(target_vault_root),
                            "journal_path": str(target_journal),
                            "expected": expected_rows,
                            "active_source_id": source_id,
                            "active_checksum": checksum,
                            "active_query": source_query,
                            "bundle_sha256": hashlib.sha256(bundle).hexdigest(),
                        }
                        payload_path = root / "v1_bundle_roundtrip_payload.json"
                        payload_descriptor = os.open(
                            payload_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
                        )
                        with os.fdopen(payload_descriptor, "w", encoding="utf-8") as stream:
                            json.dump(payload, stream, sort_keys=True)
                            stream.write("\n")

                        # Stop the source profile so only the fresh target HOME's runs.
                        app.close()
                        app = None
                        vault = None
                        client.close()
                        client = None
                        stop_profile(paths)
                        self.assertFalse(runtime.is_daemon_running(profile=PROFILE))

                        worker_environment = _worker_environment(target_home, root)
                        worker_command = [
                            sys.executable,
                            str(Path(__file__).resolve()),
                            "--bundle-import-worker",
                            str(payload_path),
                        ]
                        evidence_path = root / "v1_bundle_roundtrip.json"
                        worker_error = None
                        try:
                            worker = subprocess.run(
                                worker_command,
                                cwd=Path(__file__).resolve().parents[1],
                                env=worker_environment,
                                capture_output=True,
                                text=True,
                                timeout=900,
                                check=False,
                            )
                            if worker.returncode != 0:
                                worker_error = f"target_worker_exit_{worker.returncode}"
                                for line in reversed(worker.stderr.splitlines()):
                                    candidate, separator, _ = line.strip().partition(":")
                                    candidate = candidate.rsplit(".", 1)[-1]
                                    if (
                                        separator
                                        and candidate.isidentifier()
                                        and candidate.endswith(("Error", "Exception"))
                                    ):
                                        worker_error += f"_{candidate}"
                                        if candidate == "UnboundLocalError":
                                            marker = "cannot access local variable '"
                                            if marker in line:
                                                variable = line.split(marker, 1)[1].split("'", 1)[0]
                                                if variable.isidentifier():
                                                    worker_error += f"_{variable}"
                                        break
                        except subprocess.TimeoutExpired:
                            worker_error = "target_worker_timeout"
                        except OSError:
                            worker_error = "target_worker_unavailable"

                        try:
                            cleanup = subprocess.run(
                                [
                                    sys.executable,
                                    str(Path(__file__).resolve()),
                                    "--stop-disposable-profile",
                                    str(target_home),
                                ],
                                cwd=Path(__file__).resolve().parents[1],
                                env=worker_environment,
                                capture_output=True,
                                text=True,
                                timeout=60,
                                check=False,
                            )
                            cleanup_succeeded = cleanup.returncode == 0
                        except (OSError, subprocess.SubprocessError):
                            cleanup_succeeded = False
                        target_profile_stopped = cleanup_succeeded

                        if not evidence_path.is_file():
                            failed_evidence = {
                                "category": "v1_bundle_roundtrip",
                                "input_format": "synthetic_legacy_migration_export_v1",
                                "proof_scope": "synthetic_legacy_migration_export_v1_to_fresh_disposable_home_profile_and_vault",
                                "model": llm.get("model"),
                                "verified_stages": [],
                                "reason_code": worker_error or "target_report_missing",
                                "status": "FAILED",
                                "profile_stopped": cleanup_succeeded,
                            }
                            failed_descriptor = os.open(
                                evidence_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
                            )
                            with os.fdopen(failed_descriptor, "w", encoding="utf-8") as stream:
                                json.dump(failed_evidence, stream, sort_keys=True)
                                stream.write("\n")
                        bundle_evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
                        self.assertTrue(cleanup_succeeded, "disposable target profile did not stop")
                        self.assertEqual(
                            bundle_evidence.get("status"),
                            "PASSED",
                            bundle_evidence.get("reason_code", "target round-trip failed"),
                        )
                        self.assertTrue(bundle_evidence.get("profile_stopped"))
                        self.assertEqual(bundle_evidence.get("category"), "v1_bundle_roundtrip")
                        self.assertEqual(
                            bundle_evidence.get("input_format"),
                            "synthetic_legacy_migration_export_v1",
                        )
                        self.assertEqual(bundle_evidence.get("model"), llm["model"])
                        self.assertEqual(
                            bundle_evidence.get("proof_scope"),
                            "synthetic_legacy_migration_export_v1_to_fresh_disposable_home_profile_and_vault",
                        )
                        self.assertEqual(
                            bundle_evidence.get("verified_stages"),
                            ["reflect_attribution", "complete_inventory_comparison", "vault_reconciliation"],
                        )
                        return report, recovered, status
                    finally:
                        try:
                            if app is not None:
                                app.close()
                                app = None
                                vault = None
                            elif vault is not None:
                                vault.close()
                                vault = None
                        finally:
                            if client is not None:
                                client.close()

                asyncio.run(recover_after_restart())
                self._assert_contained(root, checked_paths)
        finally:
            close_error = None
            if app is not None:
                try:
                    app.close()
                except Exception as exc:
                    close_error = exc
            elif vault is not None:
                try:
                    vault.close()
                except Exception as exc:
                    close_error = exc
            stop_error = None
            if paths is not None and not profile_stopped:
                try:
                    with patch.dict(
                        os.environ,
                        {
                            "HOME": str(home),
                            "HERMES_HOME": str(root / "hermes-home-must-not-be-used"),
                        },
                    ):
                        if runtime is None or daemon_client is None:
                            from lifecontext.hindsight import _ensure_private_profile_directories

                            _ensure_private_profile_directories(paths)
                            runtime = runtime or _load_runtime()
                            from hindsight_embed import daemon_client as daemon_client_module

                            daemon_client = daemon_client or daemon_client_module
                        self._assert_contained(root, (
                            paths.hindsight_home,
                            paths.hindsight_profiles_dir,
                            paths.hindsight_profile_env_path,
                        ))
                        stop_profile(paths)
                        profile_stopped = not runtime.is_daemon_running(profile=PROFILE)
                except Exception as exc:
                    stop_error = exc
                    profile_stopped = False
            if profile_stopped and target_profile_stopped:
                shutil.rmtree(root)
            else:
                raise AssertionError(
                    f"disposable Hindsight profile was not confirmed stopped; retained {root}"
                ) from stop_error
            if close_error is not None:
                raise AssertionError("disposable V1 resources did not close cleanly") from close_error

    @staticmethod
    def _assert_contained(root: Path, paths: tuple[Path, ...]) -> None:
        for path in paths:
            if not path.resolve(strict=False).is_relative_to(root):
                raise AssertionError("a disposable Hindsight path escaped its temporary root")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--bundle-import-worker":
        worker_report = _run_bundle_import_worker(Path(sys.argv[2]))
        sys.exit(0 if worker_report.get("status") == "PASSED" else 1)
    if len(sys.argv) == 3 and sys.argv[1] == "--stop-disposable-profile":
        sys.exit(0 if _stop_disposable_profile(Path(sys.argv[2])) else 1)
    unittest.main()
