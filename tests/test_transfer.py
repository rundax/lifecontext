"""Application bundle transfer behavior over disposable local state."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from lifecontext.application import ApplicationError, JournalApplication
from lifecontext.bundle import BundleRecord, decode_bundle, encode_bundle
from lifecontext.custody import CustodyError, SourceManifest
from lifecontext.hindsight_adapter import LifeContextHindsightAdapter
from lifecontext.paths import LifeContextPaths
from lifecontext.vault import ManagedVault
from local_paths import scratch_root

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(_SCRIPTS))
from disposable_migration import convert_migration_export  # noqa: E402


class FakeHindsight:
    def __init__(self) -> None:
        self.documents = self
        self.metadata: dict[str, dict[str, str]] = {}
        self.retain_calls: list[tuple[str, str, dict[str, str] | None]] = []
        self.get_document_calls: list[str] = []
        self.delete_document_calls: list[str] = []

    async def aretain(
        self,
        bank_id: str,
        content: str,
        document_id: str,
        metadata: dict[str, str] | None = None,
        retain_async: bool = False,
        tags: list[str] | None = None,
    ):
        self.retain_calls.append((document_id, content, metadata))
        if metadata is not None:
            self.metadata[document_id] = dict(metadata)
        return SimpleNamespace(success=True, var_async=False, operation_id=None)

    async def get_document(self, bank_id: str, document_id: str):
        self.get_document_calls.append(document_id)
        return {"id": document_id, "document_metadata": self.metadata.get(document_id)}

    async def delete_document(self, bank_id: str, document_id: str):
        self.delete_document_calls.append(document_id)
        self.metadata.pop(document_id, None)
        return {"success": True, "document_id": document_id}


class TransferApplicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="lifeweave-transfer-test.", dir=scratch_root()
        )
        self.base = Path(self.temporary.name).resolve(strict=True)
        self._home_patches: list[object] = []
        self.apps: list[JournalApplication] = []

    def tearDown(self) -> None:
        for app in reversed(self.apps):
            app.close()
        for patcher in reversed(self._home_patches):
            patcher.stop()
        self.temporary.cleanup()

    def _new_app(self, name: str) -> tuple[JournalApplication, FakeHindsight]:
        home = self.base / name
        home.mkdir(mode=0o700, exist_ok=True)
        home_patch = patch.dict(os.environ, {"HOME": str(home)})
        home_patch.start()
        self._home_patches.append(home_patch)
        paths = LifeContextPaths.current()
        paths.ensure_private_home()
        vault_root = home / "vault"
        journal = vault_root / "Journal"
        (journal / "entries").mkdir(mode=0o700, parents=True, exist_ok=True)
        vault_root.chmod(0o700)
        journal.chmod(0o700)
        (journal / "entries").chmod(0o700)
        client = FakeHindsight()
        app = JournalApplication(
            paths=paths,
            hindsight=LifeContextHindsightAdapter(client, bank_id="lifeweave"),
            vault=ManagedVault(vault_root, journal),
        )
        self.apps.append(app)
        return app, client

    def _retain(self, app: JournalApplication, source_id: str, version: int, content: bytes) -> None:
        asyncio.run(app.retain(
            source_id,
            version,
            content,
            provenance={"source": "test", "source_ref": f"ref-{version}"},
            idempotency_key=f"retain:{source_id}:{version}",
        ))

    def _curate(
        self, app: JournalApplication, action: str, source_id: str, version: int = 1
    ) -> None:
        prepared = asyncio.run(app.curate(
            {
                "action": f"prepare_{action}",
                "source_id": source_id,
                "expected_version": version,
            },
            idempotency_key=f"prepare:{action}:{source_id}",
        ))
        asyncio.run(app.curate(
            {
                "action": action,
                "source_id": source_id,
                "expected_version": version,
                "prepared_operation_id": prepared["operation_id"],
            },
            idempotency_key=f"confirm:{action}:{source_id}",
        ))

    def test_export_reconciles_active_forgotten_and_deleted_inventory(self) -> None:
        app, _client = self._new_app("source")
        self._retain(app, "src_active", 1, b"active version one")
        self._retain(app, "src_active", 2, b"active version two")
        self._retain(app, "src_forgotten", 1, b"forgotten local bytes one")
        self._retain(app, "src_forgotten", 2, b"forgotten local bytes two")
        self._curate(app, "forget", "src_forgotten", version=2)
        self._retain(app, "src_deleted", 1, b"must not be exported")
        self._curate(app, "delete", "src_deleted")

        raw = app.export_bundle()
        records = decode_bundle(raw, limits=app.limits)
        by_identity = {(item.source_id, item.source_version): item for item in records}
        self.assertEqual(by_identity[("src_active", 1)].lifecycle_state, "active")
        self.assertEqual(by_identity[("src_active", 1)].original_bytes, b"active version one")
        self.assertEqual(by_identity[("src_active", 2)].original_bytes, b"active version two")
        self.assertEqual(by_identity[("src_forgotten", 1)].lifecycle_state, "forgotten")
        self.assertEqual(by_identity[("src_forgotten", 1)].original_bytes, b"forgotten local bytes one")
        self.assertEqual(by_identity[("src_forgotten", 2)].lifecycle_state, "forgotten")
        self.assertEqual(by_identity[("src_forgotten", 2)].original_bytes, b"forgotten local bytes two")
        self.assertEqual(by_identity[("src_deleted", 1)].lifecycle_state, "deleted")
        self.assertIsNone(by_identity[("src_deleted", 1)].original_bytes)

    def test_import_restores_lifecycle_and_replays_without_duplicate_provider_write(self) -> None:
        source, _source_client = self._new_app("source")
        self._retain(source, "src_active", 1, b"active one")
        self._retain(source, "src_active", 2, b"active two")
        self._retain(source, "src_forgotten", 1, b"local only one")
        self._retain(source, "src_forgotten", 2, b"local only two")
        self._curate(source, "forget", "src_forgotten", version=2)
        self._retain(source, "src_deleted", 1, b"deleted bytes")
        self._curate(source, "delete", "src_deleted")
        raw = source.export_bundle()

        target, client = self._new_app("target")
        source_v1 = source.custody.resolve_exact("src_active", 1).manifest
        client.metadata["src_active"] = {
            "source_version": "1", "sha256": source_v1.sha256,
        }
        result = asyncio.run(target.import_bundle(raw, idempotency_key="bundle-import-1"))
        self.assertEqual(result["item_count"], 5)
        self.assertEqual(len(client.retain_calls), 1)
        self.assertEqual(client.retain_calls[0][0], "src_active")
        self.assertEqual(client.retain_calls[0][1], "active two")
        self.assertEqual(client.metadata["src_active"]["source_version"], "2")
        self.assertEqual(
            target.custody.resolve_exact("src_forgotten", 1).original_bytes,
            b"local only one",
        )
        self.assertEqual(
            target.custody.resolve_exact("src_forgotten", 2).original_bytes,
            b"local only two",
        )
        self.assertEqual(
            target.catalog.get_tombstone("src_forgotten", 2)["tombstone_type"],
            "forgotten",
        )
        self.assertEqual(
            target.catalog.get_tombstone("src_deleted", 1)["tombstone_type"],
            "deleted",
        )
        with self.assertRaises(CustodyError):
            target.custody.resolve_exact("src_deleted", 1)
        for version in (1, 2):
            projection = target.catalog.get_projection_state("src_active", version)
            self.assertTrue((target.vault.journal_path / projection["projection_path"]).exists())
        self.assertFalse((target.vault.journal_path / "entries/src_forgotten-v1.md").exists())
        self.assertFalse((target.vault.journal_path / "entries/src_forgotten-v2.md").exists())
        for version in (1, 2):
            self.assertEqual(
                target.catalog.get_projection_state("src_forgotten", version)["state"],
                "not_projected",
            )
        self.assertEqual(target.catalog.list_recoverable_projection_states(), [])
        startup = asyncio.run(target.recover_startup())
        self.assertEqual(startup["projections_examined"], 0)
        self.assertFalse((target.vault.journal_path / "entries/src_forgotten-v1.md").exists())
        self.assertFalse((target.vault.journal_path / "entries/src_forgotten-v2.md").exists())

        replay = asyncio.run(target.import_bundle(raw, idempotency_key="bundle-import-1"))
        self.assertEqual(replay, result)
        self.assertEqual(len(client.retain_calls), 1)
        asyncio.run(target.import_bundle(raw, idempotency_key="bundle-import-2"))
        self.assertEqual(len(client.retain_calls), 1)

        forgotten = target.custody.resolve_exact("src_forgotten", 2)
        stale_active = encode_bundle((BundleRecord(
            "src_forgotten", 2, "active", forgotten.manifest, forgotten.original_bytes
        ),), limits=target.limits)
        with self.assertRaises(ApplicationError) as caught:
            asyncio.run(target.import_bundle(stale_active, idempotency_key="stale-active"))
        self.assertEqual(caught.exception.code, "conflict")
        self.assertEqual(len(client.retain_calls), 1)

    def test_import_preserves_nested_json_provenance_when_reusing_custody(self) -> None:
        source_id = "src_nested_provenance"
        original = b"nested provenance source"
        provenance = {
            "source_ref": "journal:test/nested",
            "journal_source_metadata": {
                "tags": ["one", "two"],
                "provenance": {"processors": ["capture", "normalize"]},
            },
        }
        manifest = SourceManifest(
            source_id=source_id,
            source_version=1,
            sha256=hashlib.sha256(original).hexdigest(),
            size_bytes=len(original),
            provenance=provenance,
        )
        raw = encode_bundle((BundleRecord(source_id, 1, "active", manifest, original),))
        target, client = self._new_app("nested-provenance-target")

        result = asyncio.run(
            target.import_bundle(raw, idempotency_key="nested-provenance-import")
        )

        self.assertEqual(result["item_count"], 1)
        self.assertEqual(target.custody.resolve_exact(source_id, 1).original_bytes, original)
        self.assertEqual(len(client.retain_calls), 1)

    def test_stale_active_replay_cannot_reverse_imported_deleted_tombstone(self) -> None:
        source, _ = self._new_app("deleted-replay-source")
        source_id = "src_deleted_replay"
        self._retain(source, source_id, 1, b"must stay deleted")
        stale_active = source.export_bundle()
        self._curate(source, "delete", source_id)
        deleted_tombstone = source.export_bundle()

        target, client = self._new_app("deleted-replay-target")
        asyncio.run(target.import_bundle(
            deleted_tombstone, idempotency_key="deleted-tombstone-import"
        ))
        before = {
            "catalog": target.catalog.list_inventory(),
            "custody": target.custody.list_inventory(),
            "tombstone": target.catalog.get_tombstone(source_id, 1),
            "operations": target.catalog._connection.execute(
                "SELECT COUNT(*) FROM operations"
            ).fetchone()[0],
            "projections": sorted(
                path.relative_to(target.vault.journal_path).as_posix()
                for path in target.vault.journal_path.rglob("*")
                if path.is_file()
            ),
            "provider": (list(client.get_document_calls), list(client.retain_calls), list(client.delete_document_calls)),
        }
        self.assertEqual(before["tombstone"]["tombstone_type"], "deleted")
        self.assertEqual(before["custody"], [])

        with self.assertRaises(ApplicationError) as caught:
            asyncio.run(target.import_bundle(
                stale_active, idempotency_key="stale-active-after-deleted"
            ))
        self.assertEqual(caught.exception.code, "conflict")
        self.assertEqual(target.catalog.list_inventory(), before["catalog"])
        self.assertEqual(target.custody.list_inventory(), before["custody"])
        self.assertEqual(target.catalog.get_tombstone(source_id, 1), before["tombstone"])
        self.assertEqual(
            target.catalog._connection.execute(
                "SELECT COUNT(*) FROM operations"
            ).fetchone()[0],
            before["operations"],
        )
        self.assertEqual(
            sorted(
                path.relative_to(target.vault.journal_path).as_posix()
                for path in target.vault.journal_path.rglob("*")
                if path.is_file()
            ),
            before["projections"],
        )
        self.assertEqual(
            (client.get_document_calls, client.retain_calls, client.delete_document_calls),
            tuple(before["provider"]),
        )

    def test_legacy_migration_export_converts_and_imports_through_v1_application(self) -> None:
        export_root = self.base / "legacy-migration-export"
        sources_root = export_root / "sources"
        sources_root.mkdir(mode=0o700, parents=True)
        active = "jrnl_src_" + "a" * 32
        forgotten = "jrnl_src_" + "b" * 32
        deleted = "jrnl_src_" + "c" * 32
        fixtures = [
            (active, 1, "active", b"legacy active one", {"source_ref": "legacy:active:1"}),
            (active, 2, "active", b"legacy active two", {"source_ref": "legacy:active:2"}),
            (deleted, 1, "deleted", None, None),
            (forgotten, 1, "forgotten", b"legacy forgotten", {"source_ref": "legacy:forgotten:1"}),
        ]
        items = []
        for source_id, version, state, content, provenance in fixtures:
            items.append({
                "source_id": source_id,
                "source_version": version,
                "lifecycle_state": state,
            })
            if state == "deleted":
                continue
            version_dir = sources_root / source_id / f"v{version}"
            version_dir.mkdir(mode=0o700, parents=True)
            version_manifest = {
                "schema_version": 1,
                "source_id": source_id,
                "source_version": version,
                "sha256": hashlib.sha256(content).hexdigest(),
                "size_bytes": len(content),
                "provenance": provenance,
            }
            manifest_path = version_dir / "manifest.json"
            original_path = version_dir / "original.bin"
            manifest_path.write_text(json.dumps(version_manifest), encoding="utf-8")
            original_path.write_bytes(content)
            manifest_path.chmod(0o600)
            original_path.chmod(0o600)
        items.sort(key=lambda item: (item["source_id"], item["source_version"]))
        root_manifest = {
            "format": "hermes.journal.migration",
            "format_version": 1,
            "inventory_complete": True,
            "owner_namespace": "d" * 32,
            "source_ids": sorted({item["source_id"] for item in items}),
            "tombstone_ids": [deleted],
            "items": items,
        }
        (export_root / "manifest.json").write_text(json.dumps(root_manifest), encoding="utf-8")

        bundle, counts = convert_migration_export(export_root)
        target, client = self._new_app("legacy-migration-target")
        result = asyncio.run(target.import_bundle(bundle, idempotency_key="legacy-migration-import"))

        self.assertEqual(counts, {
            "source_count": 3,
            "version_count": 4,
            "forgotten_source_count": 1,
            "deleted_source_count": 1,
        })
        self.assertEqual(result["item_count"], 4)
        self.assertEqual(set(target.custody.list_inventory()), {
            (active, 1), (active, 2), (forgotten, 1),
        })
        for identity, content, source_ref in (
            ((active, 1), b"legacy active one", "legacy:active:1"),
            ((active, 2), b"legacy active two", "legacy:active:2"),
            ((forgotten, 1), b"legacy forgotten", "legacy:forgotten:1"),
        ):
            evidence = target.exact_evidence(*identity)
            self.assertEqual(evidence.original_bytes, content)
            self.assertEqual(evidence.manifest.sha256, hashlib.sha256(content).hexdigest())
            self.assertEqual(evidence.manifest.provenance["source_ref"], source_ref)
        self.assertEqual(len(client.retain_calls), 1)
        self.assertEqual(client.retain_calls[0][0], active)
        self.assertEqual(client.retain_calls[0][1], "legacy active two")
        self.assertEqual(client.metadata[active]["source_version"], "2")
        self.assertEqual(target.catalog.get_tombstone(forgotten, 1)["tombstone_type"], "forgotten")
        self.assertEqual(target.catalog.get_tombstone(deleted, 1)["tombstone_type"], "deleted")
        with self.assertRaises(CustodyError):
            target.custody.resolve_exact(deleted, 1)
        projection = target.catalog.get_projection_state(active, 2)
        self.assertTrue((target.vault.journal_path / projection["projection_path"]).exists())
        self.assertFalse((target.vault.journal_path / f"entries/{forgotten}-v1.md").exists())
        self.assertEqual(target.catalog.list_recoverable_projection_states(), [])

    def test_active_binary_import_fails_before_any_local_or_provider_write(self) -> None:
        target, client = self._new_app("binary-target")
        original = b"\x00\xffsynthetic binary source"
        manifest = SourceManifest(
            source_id="src_binary_source",
            source_version=1,
            sha256=hashlib.sha256(original).hexdigest(),
            size_bytes=len(original),
            provenance={"source": "synthetic test"},
        )
        raw = encode_bundle((
            BundleRecord("src_binary_source", 1, "active", manifest, original),
        ), limits=target.limits)

        with self.assertRaises(ApplicationError) as caught:
            asyncio.run(target.import_bundle(raw, idempotency_key="binary-import"))
        self.assertEqual(caught.exception.code, "invalid_source_text")
        self.assertEqual(target.catalog.list_inventory(), [])
        self.assertEqual(target.custody.list_inventory(), [])
        self.assertEqual(target.catalog.list_incomplete_transfers(), [])
        self.assertEqual(
            target.catalog._connection.execute("SELECT COUNT(*) FROM operations").fetchone()[0],
            0,
        )
        self.assertEqual(client.retain_calls, [])
        self.assertEqual(list((target.vault.journal_path / "entries").iterdir()), [])

    def test_conflicting_import_fails_before_creating_transfer_operation(self) -> None:
        source, _ = self._new_app("source")
        self._retain(source, "src_conflict", 1, b"source bytes")
        raw = source.export_bundle()
        record = decode_bundle(raw, limits=source.limits)[0]

        target, _ = self._new_app("target")
        self._retain(target, "src_conflict", 1, b"different destination bytes")
        before = target.catalog._connection.execute(
            "SELECT COUNT(*) FROM operations"
        ).fetchone()[0]
        with self.assertRaises(ApplicationError) as caught:
            asyncio.run(target.import_bundle(raw, idempotency_key="conflicting-import"))
        self.assertEqual(caught.exception.code, "conflict")
        after = target.catalog._connection.execute(
            "SELECT COUNT(*) FROM operations"
        ).fetchone()[0]
        self.assertEqual(after, before)
        self.assertEqual(
            target.custody.resolve_exact("src_conflict", 1).original_bytes,
            b"different destination bytes",
        )
        self.assertIsNotNone(record.manifest)

        provider_target, provider_client = self._new_app("provider-conflict")
        provider_client.metadata["src_conflict"] = {
            "source_version": "9", "sha256": "f" * 64,
        }
        before = provider_target.catalog._connection.execute(
            "SELECT COUNT(*) FROM operations"
        ).fetchone()[0]
        with self.assertRaises(ApplicationError) as provider_caught:
            asyncio.run(provider_target.import_bundle(raw, idempotency_key="provider-conflict"))
        self.assertEqual(provider_caught.exception.code, "conflict")
        after = provider_target.catalog._connection.execute(
            "SELECT COUNT(*) FROM operations"
        ).fetchone()[0]
        self.assertEqual(after, before)
        self.assertEqual(provider_target.custody.list_inventory(), [])
        self.assertEqual(provider_client.retain_calls, [])
        self.assertEqual(list((provider_target.vault.journal_path / "entries").iterdir()), [])

    def test_export_fails_when_catalog_and_custody_inventory_disagree(self) -> None:
        app, _ = self._new_app("source")
        app.custody.write_version(
            "src_unregistered", 1, b"unregistered", provenance={"source": "test"}
        )
        with self.assertRaises(ApplicationError) as caught:
            app.export_bundle()
        self.assertEqual(caught.exception.code, "evidence_unavailable")

    def test_reopened_import_resumes_after_projection_boundary(self) -> None:
        source, _ = self._new_app("source")
        self._retain(source, "src_resume", 1, b"resume exactly once")
        raw = source.export_bundle()

        target, _client = self._new_app("target")
        real_project = target.project

        def project_then_interrupt(source_id: str, source_version: int):
            result = real_project(source_id, source_version)
            raise RuntimeError("injected stop after managed projection")

        with patch.object(target, "project", side_effect=project_then_interrupt):
            with self.assertRaisesRegex(RuntimeError, "injected stop"):
                asyncio.run(target.import_bundle(raw, idempotency_key="resume-import"))
        with self.assertRaises(ApplicationError) as blocked:
            target.export_bundle()
        self.assertEqual(blocked.exception.code, "transfer_unavailable")
        self.assertEqual(
            target.catalog.list_transfer_items(
                target.catalog._connection.execute(
                    "SELECT operation_id FROM operations WHERE operation_kind='import'"
                ).fetchone()[0]
            )[0]["state"],
            "catalog_registered",
        )
        target.close()
        self.apps.remove(target)

        resumed, client = self._new_app("target")
        result = asyncio.run(resumed.import_bundle(raw, idempotency_key="resume-import"))
        self.assertEqual(result["item_count"], 1)
        self.assertEqual(len(client.retain_calls), 1)
        self.assertEqual(
            resumed.catalog.list_transfer_items(result.operation_id)[0]["state"],
            "completed",
        )


if __name__ == "__main__":
    unittest.main()
