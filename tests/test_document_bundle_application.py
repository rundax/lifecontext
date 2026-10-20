"""Disposable document transfer and replacement application contracts."""
from __future__ import annotations

import asyncio
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from docx import Document

from lifecontext.application import ApplicationError, JournalApplication
from lifecontext.bundle import decode_bundle
from lifecontext.categories import CategoryClassification
from lifecontext.document_bundle import decode_document_bundle
from lifecontext.paths import LifeContextPaths
from lifecontext.vault import ManagedVault, VaultError
from lifecontext.wiki import WikiPatch


class Provider:
    def __init__(self):
        self.documents = {}
        self.calls = []
        self.fail = False
        self.delete_success = True

    async def retain(self, source_id, content, *, scope_tag, metadata, retain_async=False):
        self.calls.append(source_id)
        if self.fail:
            raise RuntimeError("private provider failure")
        self.documents[source_id] = {"id": source_id, "document_metadata": metadata, "memory_unit_count": 1}
        return {"success": True, "async": False}

    async def get_document(self, source_id):
        return self.documents.get(source_id, {})

    async def delete_document(self, source_id):
        if self.delete_success:
            self.documents.pop(source_id, None)
        return {"success": self.delete_success}

    async def reflect(self, query, **kwargs):
        return {"text": "invalid"}


class DocumentBundleApplicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.applications = []
        self.source, self.provider = self.make_app("source")
        self.destination, self.destination_provider = self.make_app("destination")
        self.addCleanup(self.close_apps)

    def close_apps(self):
        for app in self.applications:
            app.close()

    def make_app(self, name):
        home = self.base / name
        home.mkdir(mode=0o700)
        vault = home / "vault"
        journal = vault / "Journal"
        (journal / "entries").mkdir(parents=True, mode=0o700)
        vault.chmod(0o700)
        journal.chmod(0o700)
        provider = Provider()
        with patch.dict(os.environ, {"HOME": str(home)}):
            app = JournalApplication(paths=LifeContextPaths.current(), hindsight=provider,
                                     vault=ManagedVault(vault, journal))
        app.documents.wiki_hook = None
        self.applications.append(app)
        return app, provider

    def import_document(self, app=None, *, key="document", content=None, source_id=None, source_version=None):
        app = app or self.source
        if content is None:
            document = Document()
            document.add_paragraph("The sky is blue.")
            output = io.BytesIO()
            document.save(output)
            content = output.getvalue()
        return asyncio.run(app.import_document(content, "sample.docx", None, {"source": "fixture"}, key,
                                               source_id=source_id, source_version=source_version))

    def seed_history(self, outcome):
        sid = outcome["document"]["source_id"]
        extraction = outcome["document"]["extractions"][-1]
        payload = self.source.derived.read(sid, 1, 1, expected_sha256=extraction["artifact_sha256"])
        quote = payload["segments"][0]["text"]
        citation = {"source_id": sid, "source_version": 1, "extraction_revision": 1,
                    "passage_start": 0, "passage_end": 1,
                    "original_sha256": extraction["original_sha256"], "quote": quote}
        self.source.catalog.create_wiki_page("index", title="Index")
        for revision in range(2):
            value = {"page_id": "index", "title": "Index", "expected_revision": revision,
                     "claims": [{"text": "The sky is blue.", "citations": [citation]}],
                     "contradictions": [], "links": []}
            if revision:
                # A later revision carries a labelled summary; bundles keep both shapes.
                value["summary"] = [{"text": "The page says the sky is blue.", "claims": [1]}]
            patch_value = WikiPatch.parse(json.dumps(value))
            stored = self.source.wiki.store.write(patch_value)
            self.source.catalog.commit_wiki_revision("index", expected_revision=revision,
                artifact_path=str(stored.path.relative_to(self.source.paths.lifeweave_home)),
                artifact_sha256=stored.sha256, dependencies=[patch_value.claims[0].citations[0].dependency()])
        proposal = self.source.categories.propose(CategoryClassification("Health", "Broad topic", "Fixture", .95, True),
                                                  documents=[{"source_id": sid, "source_version": 1}])["proposal"]
        self.source.categories.approve(proposal["proposal_id"], expected_revision=proposal["revision"], category_id="health")

    def test_automatic_classification_proposes_but_never_approves(self):
        outcome = self.import_document()
        value = {"name": "Research", "description": "Broad topic", "rationale": "Fixture evidence",
                 "confidence": .95, "broad": True, "tags": [], "closest_category_id": None}

        async def reflect(query, **kwargs):
            self.assertIn("untrusted DATA", query)
            return {"text": json.dumps(value)}

        self.provider.reflect = reflect
        asyncio.run(self.source._classify_document(outcome["document"]))
        asyncio.run(self.source._classify_document(outcome["document"]))
        self.assertEqual(len(self.source.list_category_proposals()), 1)
        self.assertEqual(len(self.source.list_categories()), 1)
        self.assertEqual(self.source.show_document(outcome["document"]["source_id"])["category_id"], "inbox")

    def test_automatic_classification_reuses_approved_category_but_not_uncertainty(self):
        first = self.import_document()
        self.seed_history(first)
        self.source.categories.alias("health", "Wellbeing", expected_revision=1)
        second = self.import_document(key="second")
        calls = []
        value = {"name": "Wellbeing", "description": "Broad topic", "rationale": "Fixture evidence",
                 "confidence": .95, "broad": True, "tags": [], "closest_category_id": "health"}

        async def reflect(query, **kwargs):
            calls.append(query)
            return {"text": json.dumps(value)}

        self.provider.reflect = reflect
        asyncio.run(self.source._classify_document(second["document"]))
        asyncio.run(self.source._classify_document(second["document"]))
        self.assertEqual(self.source.show_document(second["document"]["source_id"])["category_id"], "health")
        self.assertEqual(len(calls), 1)
        third = self.import_document(key="third")
        value["confidence"] = .2
        asyncio.run(self.source._classify_document(third["document"]))
        asyncio.run(self.source._classify_document(third["document"]))
        self.assertEqual(self.source.show_document(third["document"]["source_id"])["category_id"], "inbox")
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.source.list_category_proposals(), [])

    def test_classification_failure_retries_completed_import_after_restart(self):
        calls = []
        failed = True
        value = {"name": "Research", "description": "Broad topic", "rationale": "Fixture evidence",
                 "confidence": .95, "broad": True, "tags": [], "closest_category_id": None}

        async def reflect(query, **kwargs):
            calls.append(query)
            if failed:
                raise RuntimeError("private provider failure")
            return {"text": json.dumps(value)}

        self.provider.reflect = reflect
        async def wiki(document):
            await self.source._classify_document(document)
            return {"state": "completed"}

        self.source.documents.wiki_hook = wiki
        outcome = self.import_document()
        self.assertEqual(outcome["state"], "completed")
        pending = [row for row in self.source.catalog.list_recoverable_operations()
                   if row["operation_kind"] == "category_decision"]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["state"], "retryable_failed")
        self.assertNotIn("private provider", json.dumps(pending))
        self.source.close()
        home = self.base / "source"
        with patch.dict(os.environ, {"HOME": str(home)}):
            resumed = JournalApplication(paths=LifeContextPaths.current(), hindsight=self.provider,
                vault=ManagedVault(home / "vault", home / "vault" / "Journal"))
        self.applications.append(resumed)
        failed = False
        replay = asyncio.run(resumed.retry_document(outcome["operation_id"]))
        self.assertEqual(replay["state"], "completed")
        asyncio.run(resumed.retry_document(outcome["operation_id"]))
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(resumed.list_category_proposals()), 1)
        self.assertEqual(resumed.catalog.list_recoverable_operations(), [])

    def test_binary_reconciliation_uses_document_projection(self):
        outcome = self.import_document()
        sid = outcome["document"]["source_id"]
        self.seed_history(outcome)
        report = self.source.reconcile_projections()
        self.assertEqual(report["failed"], 0)
        self.assertEqual(report["projected"], 2)
        attachment = self.source.vault.journal_path / "documents" / "health" / "attachments" / f"{sid}-v1--sample.docx"
        self.assertEqual(attachment.read_bytes(), self.source.download_document(sid).original_bytes)
        self.assertFalse((self.source.vault.journal_path / "entries" / f"{sid}-v1.md").exists())
        again = self.source.reconcile_projections()
        self.assertEqual(again["projected"], 0)
        self.assertEqual(again["already_projected"], 2)

    def test_reconciliation_completes_projection_stage_after_category_merge(self):
        first = self.import_document()
        sid = first["document"]["source_id"]
        self.seed_history(first)
        self.assertEqual(self.source.show_document(sid)["stages"]["projection"]["state"], "pending")
        self.assertEqual(self.source.reconcile_projections()["failed"], 0)
        self.assertEqual(self.source.show_document(sid)["stages"]["projection"]["state"], "completed")
        second = self.import_document(key="second")
        proposal = self.source.categories.propose(
            CategoryClassification("Research", "Broad topic", "Fixture", .95, True),
            documents=[{"source_id": second["document"]["source_id"], "source_version": 1}],
        )["proposal"]
        self.source.categories.approve(proposal["proposal_id"], expected_revision=proposal["revision"], category_id="research")
        self.source.categories.merge("health", "research", expected_source_revision=1, expected_target_revision=1)
        self.assertEqual(self.source.show_document(sid)["stages"]["projection"]["state"], "pending")
        self.assertEqual(self.source.reconcile_projections()["failed"], 0)
        self.assertEqual(self.source.show_document(sid)["stages"]["projection"]["state"], "completed")
        attachment = self.source.vault.journal_path / "documents" / "research" / "attachments" / f"{sid}-v1--sample.docx"
        self.assertEqual(attachment.read_bytes(), self.source.download_document(sid).original_bytes)
        self.assertFalse((self.source.vault.journal_path / "documents" / "health" / "attachments" / attachment.name).exists())

    def test_reconciliation_records_edited_attachment_conflict_without_false_completion(self):
        outcome = self.import_document()
        sid = outcome["document"]["source_id"]
        attachment = self.source.vault.journal_path / "documents" / "inbox" / "attachments" / f"{sid}-v1--sample.docx"
        original = attachment.read_bytes()
        attachment.write_bytes(b"Manual binary edit")
        self.assertEqual(self.source.reconcile_projections()["failed"], 1)
        stage = self.source.show_document(sid)["stages"]["projection"]
        self.assertEqual(stage["state"], "conflict")
        self.assertEqual(stage["error_code"], "source_conflict")
        self.assertEqual(attachment.read_bytes(), b"Manual binary edit")
        self.assertEqual(self.source.download_document(sid).original_bytes, original)
        attachment.write_bytes(original)
        self.assertEqual(self.source.reconcile_projections()["failed"], 0)
        self.assertEqual(self.source.show_document(sid)["stages"]["projection"]["state"], "completed")

    def test_classifier_rejects_foreign_child_proposal_receipt(self):
        first = self.import_document()
        self.seed_history(first)
        target = self.import_document(key="target")
        foreign = self.import_document(key="foreign")
        sid = target["document"]["source_id"]
        key = f"document-category:{sid}:1:1:proposal"
        self.source.propose_category(CategoryClassification("Health", "Broad topic", "Fixture", .95, True),
            documents=[{"source_id": foreign["document"]["source_id"], "source_version": 1}], idempotency_key=key)
        original = self.source.catalog.find_category_operation(key)
        self.assertFalse(asyncio.run(self.source._classify_document(target["document"])))
        self.assertEqual(self.source.show_document(sid)["category_id"], "inbox")
        self.assertEqual(self.source.catalog.find_category_operation(key), original)
        pending = self.source.catalog.list_recoverable_operations()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["state"], "retryable_failed")
        self.assertEqual(pending[0]["result"]["recovery_intent"]["source_id"], sid)

    def test_classification_failure_recovers_on_startup(self):
        outcome = self.import_document()
        asyncio.run(self.source._classify_document(outcome["document"]))
        value = {"name": "Research", "description": "Broad topic", "rationale": "Fixture evidence",
                 "confidence": .95, "broad": True, "tags": [], "closest_category_id": None}

        async def reflect(query, **kwargs):
            return {"text": json.dumps(value)}

        self.provider.reflect = reflect
        report = asyncio.run(self.source.recover_startup())
        self.assertEqual(report["completed"], 1)
        self.assertEqual(len(self.source.list_category_proposals()), 1)
        self.assertEqual(self.source.catalog.list_recoverable_operations(), [])

    def test_mixed_binary_restore_preserves_category_and_history(self):
        outcome = self.import_document()
        self.assertEqual(outcome["state"], "completed")
        sid = outcome["document"]["source_id"]
        self.seed_history(outcome)
        asyncio.run(self.source.retain("journal_fixture", 1, b"Journal evidence", provenance={"source": "fixture"}, idempotency_key="journal"))
        raw = self.source.export_bundle()
        snapshot = decode_document_bundle(raw)
        self.assertEqual(snapshot.bundle_version, 2)
        self.assertNotIn("artifact_publications", snapshot.catalog_data())
        source_proof = self.source.catalog.get_artifact_publication(sid + "_attachment", 1)
        self.assertIsNotNone(source_proof)
        restored = asyncio.run(self.destination.import_bundle(raw, idempotency_key="restore"))
        destination_proof = self.destination.catalog.get_artifact_publication(sid + "_attachment", 1)
        self.assertIsNotNone(destination_proof)
        self.assertNotEqual((source_proof["device"], source_proof["inode"]),
                            (destination_proof["device"], destination_proof["inode"]))
        self.assertEqual(restored["item_count"], 2)
        self.assertEqual(self.destination.download_document(sid).original_bytes, self.source.download_document(sid).original_bytes)
        self.assertEqual(self.destination.show_document(sid)["category_id"], "health")
        self.assertEqual(len(self.destination.wiki_history("index")), 2)
        self.assertEqual(self.destination.catalog.get_wiki_page("index")["state"], "stale")
        self.assertEqual(self.destination.show_document(sid)["stages"]["indexing"]["state"], "completed")
        self.assertNotIn(sid, self.destination_provider.calls)
        calls = list(self.destination_provider.calls)
        replay = asyncio.run(self.destination.import_bundle(raw, idempotency_key="restore"))
        self.assertEqual(replay.operation_id, restored.operation_id)
        self.assertEqual(self.destination_provider.calls, calls)

    def test_journal_only_export_remains_version_one(self):
        asyncio.run(self.source.retain("journal_fixture", 1, b"Journal evidence", provenance={"source": "fixture"}, idempotency_key="journal"))
        raw = self.source.export_bundle()
        self.assertEqual(len(decode_bundle(raw)), 1)
        self.assertEqual(decode_document_bundle(raw).bundle_version, 1)
        result = asyncio.run(self.destination.import_bundle(raw, idempotency_key="legacy"))
        self.assertEqual(result["item_count"], 1)

    def test_tampered_snapshot_has_no_restore_effects(self):
        self.import_document()
        raw = self.source.export_bundle()
        output = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(raw)) as source, zipfile.ZipFile(output, "w") as target:
            for name in source.namelist():
                value = source.read(name)
                if name == "snapshot.json":
                    value += b" "
                target.writestr(name, value)
        with self.assertRaises(ApplicationError):
            asyncio.run(self.destination.import_bundle(output.getvalue(), idempotency_key="tamper"))
        self.assertEqual(self.destination.catalog.list_inventory(), [])
        self.assertEqual(self.destination.custody.list_inventory(), [])
        self.assertEqual(self.destination_provider.calls, [])

    def test_outage_restore_remains_incomplete_and_replay_resumes(self):
        outcome = self.import_document()
        raw = self.source.export_bundle()
        self.destination_provider.fail = True
        with self.assertRaises(ApplicationError):
            asyncio.run(self.destination.import_bundle(raw, idempotency_key="outage"))
        self.assertEqual(len(self.destination.catalog.list_incomplete_transfers()), 1)
        sid = outcome["document"]["source_id"]
        self.assertEqual(self.destination.show_document(sid)["stages"]["indexing"]["state"], "failed")
        self.destination_provider.fail = False
        result = asyncio.run(self.destination.import_bundle(raw, idempotency_key="outage"))
        self.assertEqual(result["item_count"], 1)
        self.assertEqual(self.destination.catalog.list_incomplete_transfers(), [])

    def test_outage_restore_resumes_after_restart(self):
        outcome = self.import_document()
        raw = self.source.export_bundle()
        self.destination_provider.fail = True
        with self.assertRaises(ApplicationError):
            asyncio.run(self.destination.import_bundle(raw, idempotency_key="restart-restore"))
        self.destination.close()
        home = self.base / "destination"
        with patch.dict(os.environ, {"HOME": str(home)}):
            resumed = JournalApplication(paths=LifeContextPaths.current(), hindsight=self.destination_provider,
                vault=ManagedVault(home / "vault", home / "vault" / "Journal"))
        self.applications.append(resumed)
        resumed.documents.wiki_hook = None
        self.destination_provider.fail = False
        report = asyncio.run(resumed.recover_startup())
        self.assertGreaterEqual(report["completed"], 1)
        calls = list(self.destination_provider.calls)
        restored = asyncio.run(resumed.import_bundle(raw, idempotency_key="restart-restore"))
        self.assertEqual(restored["item_count"], 1)
        self.assertEqual(self.destination_provider.calls, calls)
        self.assertEqual(resumed.download_document(outcome["document"]["source_id"]).original_bytes,
                         self.source.download_document(outcome["document"]["source_id"]).original_bytes)

    def test_confirmed_document_delete_removes_all_versions_and_category_paths(self):
        first = self.import_document()
        sid = first["document"]["source_id"]
        old_attachment = self.source.vault.journal_path / "documents" / "inbox" / "attachments" / f"{sid}-v1--sample.docx"
        self.seed_history(first)
        self.assertEqual(self.source.reconcile_projections()["failed"], 0)
        self.assertFalse(old_attachment.exists())
        self.import_document(key="replacement", source_id=sid, source_version=2)
        unrelated = self.import_document(key="unrelated")
        other_id = unrelated["document"]["source_id"]
        self.assertEqual(self.source.document_projections.project_wiki("index")["state"], "completed")
        wiki_path = self.source.vault.journal_path / "wiki" / "Index.md"
        wiki_before = wiki_path.read_bytes()
        receipts = [row for row in self.source.catalog.list_artifact_projections(layer="document")
                    if row["artifact_id"] in {sid + "_attachment", sid + "_note"}]
        self.assertEqual(len(receipts), 4)
        prepared = asyncio.run(self.source.curate(
            {"action": "prepare_delete", "source_id": sid, "expected_version": 2},
            idempotency_key="prepare-delete"))
        request = {"action": "delete", "source_id": sid, "expected_version": 2,
                   "prepared_operation_id": prepared["operation_id"]}
        deleted = asyncio.run(self.source.curate(request, idempotency_key="delete"))
        self.assertEqual(deleted["state"], "completed")
        self.assertEqual(asyncio.run(self.source.curate(request, idempotency_key="delete"))["operation_id"], deleted["operation_id"])
        for receipt in receipts:
            self.assertFalse((self.source.vault.journal_path / receipt["projection_path"]).exists())
            current = self.source.catalog.get_artifact_projection("document", receipt["artifact_id"], receipt["revision"])
            self.assertEqual(current["state"], "not_projected")
        self.assertEqual(self.source.custody.list_inventory(), [(other_id, 1)])
        # The delete rewrote the page that cited the document at once, without a new revision.
        self.assertIn(b"The sky is blue.", wiki_before)
        self.assertNotIn(b"The sky is blue.", wiki_path.read_bytes())
        self.assertIn(b"Withdrawn: this page's sources were forgotten.", wiki_path.read_bytes())
        self.assertEqual(self.source.catalog.get_wiki_page("index")["current_revision"], 2)
        self.assertEqual(self.source.catalog.get_wiki_page("index")["state"], "stale")
        self.assertEqual(self.source.reconcile_projections()["failed"], 0)
        self.assertTrue((self.source.vault.journal_path / "documents" / "inbox" / "attachments" / f"{other_id}-v1--sample.docx").exists())
        for tombstone in self.source.catalog.list_tombstones(sid):
            self.assertEqual(set(tombstone["progress"].values()), {"completed"})

    def move_outage(self, path, **kwargs):
        if kwargs.get("staged_name") is not None:
            return ManagedVault.safe_remove_artifact(self.source.vault, path, **kwargs)
        raise VaultError("vault_unavailable")

    def test_confirmed_delete_after_failed_move_removes_old_and_new_copies(self):
        imported = self.import_document()
        sid = imported["document"]["source_id"]
        self.seed_history(imported)
        with patch.object(self.source.vault, "safe_remove_artifact", side_effect=self.move_outage):
            self.assertGreater(self.source.reconcile_projections()["failed"], 0)
        copies = [p for p in self.source.vault.journal_path.glob("documents/**/*") if p.is_file()]
        self.assertEqual(len(copies), 4)
        prepared = asyncio.run(self.source.curate(
            {"action": "prepare_delete", "source_id": sid, "expected_version": 1},
            idempotency_key="prepare-moved-delete"))
        result = asyncio.run(self.source.curate(
            {"action": "delete", "source_id": sid, "expected_version": 1,
             "prepared_operation_id": prepared["operation_id"]}, idempotency_key="moved-delete"))
        self.assertEqual(result["state"], "completed")
        self.assertTrue(all(not p.exists() for p in copies))
        self.assertEqual(self.source.custody.list_inventory(), [])
        self.assertEqual(set(self.source.catalog.get_tombstone(sid, 1)["progress"].values()), {"completed"})

    def test_failed_move_edited_destination_blocks_delete_until_restart_repair(self):
        imported = self.import_document()
        sid = imported["document"]["source_id"]
        self.seed_history(imported)
        with patch.object(self.source.vault, "safe_remove_artifact", side_effect=self.move_outage):
            self.source.reconcile_projections()
        path = self.source.vault.journal_path / "documents/health/attachments" / f"{sid}-v1--sample.docx"
        original = path.read_bytes()
        path.write_bytes(b"manual destination edit")
        prepared = asyncio.run(self.source.curate(
            {"action": "prepare_delete", "source_id": sid, "expected_version": 1},
            idempotency_key="prepare-edited-move-delete"))
        with self.assertRaises(ApplicationError):
            asyncio.run(self.source.curate(
                {"action": "delete", "source_id": sid, "expected_version": 1,
                 "prepared_operation_id": prepared["operation_id"]}, idempotency_key="edited-move-delete"))
        operation = self.source.catalog.list_recoverable_operations()[0]
        self.assertEqual(operation["state"], "retryable_failed")
        self.assertTrue(self.source.custody.original_path(sid, 1).exists())
        self.source.close()
        home = self.base / "source"
        with patch.dict(os.environ, {"HOME": str(home)}):
            resumed = JournalApplication(paths=LifeContextPaths.current(), hindsight=self.provider,
                vault=ManagedVault(home / "vault", home / "vault" / "Journal"))
        self.applications.append(resumed)
        self.assertEqual(asyncio.run(resumed.recover_startup())["completed"], 0)
        self.assertEqual(path.read_bytes(), b"manual destination edit")
        self.assertTrue(resumed.custody.original_path(sid, 1).exists())
        path.write_bytes(original)
        self.assertGreaterEqual(asyncio.run(resumed.recover_startup())["completed"], 1)
        self.assertEqual(resumed.catalog.get_operation(operation["operation_id"])["state"], "completed")
        self.assertEqual(resumed.custody.list_inventory(), [])
        self.assertEqual([p for p in resumed.vault.journal_path.glob("documents/**/*") if p.is_file()], [])

    def test_foreign_identical_publish_crash_blocks_confirmed_delete_after_restart(self):
        imported = self.import_document()
        sid = imported["document"]["source_id"]
        original = self.source.download_document(sid).original_bytes
        self.seed_history(imported)
        original_link = os.link

        def crash(*args, **kwargs):
            if args[1].endswith(".hold"):
                return original_link(*args, **kwargs)
            descriptor = os.open(args[1], os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600, dir_fd=kwargs["dst_dir_fd"])
            os.write(descriptor, original)
            os.fsync(descriptor)
            os.close(descriptor)
            raise SystemExit("foreign creation before rollback")

        with patch("lifecontext.vault.os.link", side_effect=crash), self.assertRaises(SystemExit):
            self.source.reconcile_projections()
        path = self.source.vault.journal_path / "documents/health/attachments" / f"{sid}-v1--sample.docx"
        foreign = (path.stat().st_dev, path.stat().st_ino)
        self.source.close()
        home = self.base / "source"
        with patch.dict(os.environ, {"HOME": str(home)}):
            resumed = JournalApplication(paths=LifeContextPaths.current(), hindsight=self.provider,
                vault=ManagedVault(home / "vault", home / "vault" / "Journal"))
        self.applications.append(resumed)
        self.assertGreater(resumed.reconcile_projections()["failed"], 0)
        prepared = asyncio.run(resumed.curate(
            {"action": "prepare_delete", "source_id": sid, "expected_version": 1},
            idempotency_key="prepare-foreign-delete"))
        with self.assertRaises(ApplicationError) as caught:
            asyncio.run(resumed.curate(
                {"action": "delete", "source_id": sid, "expected_version": 1,
                 "prepared_operation_id": prepared["operation_id"]}, idempotency_key="foreign-delete"))
        self.assertEqual(caught.exception.code, "conflict")
        operation = resumed.catalog.list_recoverable_operations()[0]
        self.assertEqual(operation["state"], "retryable_failed")
        self.assertEqual(asyncio.run(resumed.recover_startup())["completed"], 0)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual((path.stat().st_dev, path.stat().st_ino), foreign)
        self.assertEqual(resumed.custody.resolve_exact(sid, 1).original_bytes, original)
        self.assertEqual(resumed.catalog.get_tombstone(sid, 1)["progress"]["custody_delete"], "pending")
        path.unlink()
        self.assertGreaterEqual(asyncio.run(resumed.recover_startup())["completed"], 1)
        self.assertEqual(resumed.catalog.get_operation(operation["operation_id"])["state"], "completed")
        self.assertEqual(resumed.custody.list_inventory(), [])

    @unittest.skipUnless(sys.platform == "linux", "deterministic SIGKILL fixture uses procfs")
    def test_preproof_sigkill_staged_copy_blocks_confirmed_delete_until_manual_repair(self):
        imported = self.import_document()
        sid = imported["document"]["source_id"]
        original = self.source.download_document(sid).original_bytes
        self.seed_history(imported)
        script = """
import os, signal, stat, sys
from pathlib import Path
from lifecontext.catalog import LifeContextCatalog
from lifecontext.custody import LifeContextCustody
from lifecontext.derived import DerivedStore
from lifecontext.document_projection import DocumentProjectionService
from lifecontext.paths import LifeContextPaths
from lifecontext.vault import ManagedVault
os.environ['HOME'] = sys.argv[1]
paths = LifeContextPaths.current()
catalog = LifeContextCatalog(paths)
vault = ManagedVault(Path(sys.argv[1]) / 'vault', Path(sys.argv[1]) / 'vault/Journal')
service = DocumentProjectionService(catalog, LifeContextCustody(paths), DerivedStore(paths), vault)
original_fsync = os.fsync
def fsync(descriptor):
    original_fsync(descriptor)
    if stat.S_ISREG(os.fstat(descriptor).st_mode):
        name = Path(os.readlink('/proc/self/fd/' + str(descriptor))).name
        if name.startswith('.publication.') and name.endswith('.tmp'):
            os.kill(os.getpid(), signal.SIGKILL)
os.fsync = fsync
service.project_document(sys.argv[2], 1)
raise AssertionError('kill point was not reached')
"""
        result = subprocess.run([sys.executable, "-c", script, str(self.base / "source"), sid],
            capture_output=True, timeout=30, check=False)
        self.assertEqual(result.returncode, -signal.SIGKILL, result.stderr.decode())
        proof = self.source.catalog.get_artifact_publication(sid + "_attachment", 1)
        self.assertEqual(proof["state"], "planned")
        staged = (self.source.vault.journal_path / proof["projection_path"]).parent / proof["staged_name"][:-5]
        self.assertEqual(staged.read_bytes(), original)
        prepared = asyncio.run(self.source.curate(
            {"action": "prepare_delete", "source_id": sid, "expected_version": 1},
            idempotency_key="prepare-staged-delete"))
        with self.assertRaises(ApplicationError) as caught:
            asyncio.run(self.source.curate(
                {"action": "delete", "source_id": sid, "expected_version": 1,
                 "prepared_operation_id": prepared["operation_id"]}, idempotency_key="staged-delete"))
        self.assertEqual(caught.exception.code, "conflict")
        operation = self.source.catalog.list_recoverable_operations()[0]
        self.assertEqual(operation["state"], "retryable_failed")
        self.assertEqual(self.source.custody.resolve_exact(sid, 1).original_bytes, original)
        self.assertEqual(self.source.catalog.get_tombstone(sid, 1)["progress"]["custody_delete"], "pending")
        self.source.close()
        home = self.base / "source"
        with patch.dict(os.environ, {"HOME": str(home)}):
            resumed = JournalApplication(paths=LifeContextPaths.current(), hindsight=self.provider,
                vault=ManagedVault(home / "vault", home / "vault" / "Journal"))
        self.applications.append(resumed)
        self.assertEqual(asyncio.run(resumed.recover_startup())["completed"], 0)
        self.assertEqual(staged.read_bytes(), original)
        self.assertEqual(resumed.custody.resolve_exact(sid, 1).original_bytes, original)
        staged.unlink()
        self.assertGreaterEqual(asyncio.run(resumed.recover_startup())["completed"], 1)
        self.assertEqual(resumed.catalog.get_operation(operation["operation_id"])["state"], "completed")
        self.assertEqual(resumed.custody.list_inventory(), [])
        self.assertEqual([p for p in resumed.vault.journal_path.glob("documents/**/*") if p.is_file()], [])

    def test_document_delete_preserves_edited_artifact_and_recovers_after_repair(self):
        for suffix in ("attachment", "note"):
            with self.subTest(suffix=suffix):
                imported = self.import_document(key="edited-" + suffix)
                sid = imported["document"]["source_id"]
                receipt = self.source.catalog.get_artifact_projection("document", sid + "_" + suffix, 1)
                path = self.source.vault.journal_path / receipt["projection_path"]
                original = path.read_bytes()
                custody_original = self.source.custody.resolve_exact(sid, 1).original_bytes
                path.write_bytes(b"manual edit must remain")
                prepared = asyncio.run(self.source.curate(
                    {"action": "prepare_delete", "source_id": sid, "expected_version": 1},
                    idempotency_key="prepare-edited-" + suffix))
                with self.assertRaises(ApplicationError) as caught:
                    asyncio.run(self.source.curate(
                        {"action": "delete", "source_id": sid, "expected_version": 1,
                         "prepared_operation_id": prepared["operation_id"]},
                        idempotency_key="delete-edited-" + suffix))
                self.assertEqual(caught.exception.code, "conflict")
                operation = self.source.catalog.list_recoverable_operations()[0]
                self.assertEqual(operation["state"], "retryable_failed")
                self.assertEqual(path.read_bytes(), b"manual edit must remain")
                self.assertEqual(self.source.custody.resolve_exact(sid, 1).original_bytes, custody_original)
                progress = self.source.catalog.get_tombstone(sid, 1)["progress"]
                self.assertEqual(progress["projection_delete"], "running")
                self.assertEqual(progress["custody_delete"], "pending")
                self.assertEqual(asyncio.run(self.source.recover_startup())["completed"], 0)
                self.assertEqual(path.read_bytes(), b"manual edit must remain")
                path.write_bytes(original)
                self.assertGreaterEqual(asyncio.run(self.source.recover_startup())["completed"], 1)
                self.assertEqual(self.source.catalog.get_operation(operation["operation_id"])["state"], "completed")
                self.assertFalse(path.exists())
                self.assertNotIn((sid, 1), self.source.custody.list_inventory())

    def test_document_delete_recovers_after_unlink_before_receipt_acknowledgement(self):
        imported = self.import_document()
        sid = imported["document"]["source_id"]
        receipts = [self.source.catalog.get_artifact_projection("document", sid + "_" + suffix, 1)
                    for suffix in ("attachment", "note")]
        prepared = asyncio.run(self.source.curate(
            {"action": "prepare_delete", "source_id": sid, "expected_version": 1},
            idempotency_key="prepare-outage-delete"))
        remove = self.source.vault.safe_remove_artifact
        calls = []

        def fail_after_unlink(path, **kwargs):
            calls.append(path)
            remove(path, **kwargs)
            raise VaultError("vault_unavailable", retryable=True)

        with patch.object(self.source.vault, "safe_remove_artifact", side_effect=fail_after_unlink), self.assertRaises(ApplicationError) as caught:
            asyncio.run(self.source.curate(
                {"action": "delete", "source_id": sid, "expected_version": 1,
                 "prepared_operation_id": prepared["operation_id"]},
                idempotency_key="outage-delete"))
        self.assertEqual(caught.exception.code, "vault_unavailable")
        operation = self.source.catalog.list_recoverable_operations()[0]
        self.assertEqual(operation["state"], "retryable_failed")
        self.assertEqual(len(calls), 1)
        self.assertFalse((self.source.vault.journal_path / receipts[0]["projection_path"]).exists())
        self.assertTrue(self.source.custody.original_path(sid, 1).exists())
        self.source.close()
        home = self.base / "source"
        with patch.dict(os.environ, {"HOME": str(home)}):
            resumed = JournalApplication(paths=LifeContextPaths.current(), hindsight=self.provider,
                vault=ManagedVault(home / "vault", home / "vault" / "Journal"))
        self.applications.append(resumed)
        report = asyncio.run(resumed.recover_startup())
        self.assertGreaterEqual(report["completed"], 1)
        self.assertEqual(resumed.catalog.get_operation(operation["operation_id"])["state"], "completed")
        self.assertEqual(resumed.custody.list_inventory(), [])
        for receipt in receipts:
            self.assertFalse((resumed.vault.journal_path / receipt["projection_path"]).exists())
        self.assertEqual(asyncio.run(resumed.recover_startup())["completed"], 0)

    def test_document_forget_retains_original_and_projection_copies(self):
        imported = self.import_document()
        sid = imported["document"]["source_id"]
        receipts = [self.source.catalog.get_artifact_projection("document", sid + "_" + suffix, 1)
                    for suffix in ("attachment", "note")]
        original = self.source.custody.resolve_exact(sid, 1).original_bytes
        prepared = asyncio.run(self.source.curate(
            {"action": "prepare_forget", "source_id": sid, "expected_version": 1},
            idempotency_key="prepare-forget"))
        forgotten = asyncio.run(self.source.curate(
            {"action": "forget", "source_id": sid, "expected_version": 1,
             "prepared_operation_id": prepared["operation_id"]},
            idempotency_key="forget"))
        self.assertEqual(forgotten["state"], "completed")
        self.assertEqual(self.source.custody.resolve_exact(sid, 1).original_bytes, original)
        for receipt in receipts:
            self.assertTrue((self.source.vault.journal_path / receipt["projection_path"]).exists())

    def test_replacement_does_not_delete_changed_provider_identity(self):
        first = self.import_document()
        sid = first["document"]["source_id"]
        pid = first["document"]["chunks"][0]["provider_id"]
        foreign = {"id": pid, "document_metadata": {"source_id": "foreign"}, "memory_unit_count": 1}
        self.provider.documents[pid] = foreign
        replacement = self.import_document(key="replace-foreign", source_id=sid, source_version=2)
        self.assertEqual(replacement["state"], "retryable_failed")
        self.assertEqual(self.provider.documents[pid], foreign)

    def test_replacement_retirement_outage_is_retryable(self):
        first = self.import_document()
        sid = first["document"]["source_id"]
        self.provider.delete_success = False
        replacement = self.import_document(key="replace", source_id=sid, source_version=2)
        self.assertEqual(replacement["state"], "retryable_failed")
        old = self.source.catalog.list_provider_chunks(sid, 1)
        self.assertTrue(all(row["submission_state"] == "retired" and row["completion_state"] == "pending" for row in old))
        self.provider.delete_success = True
        recovered = asyncio.run(self.source.retry_document(replacement["operation_id"]))
        self.assertEqual(recovered["state"], "completed")
        self.assertTrue(all(row["completion_state"] == "completed" for row in self.source.catalog.list_provider_chunks(sid, 1)))

    def test_replacement_retirement_recovers_after_restart(self):
        first = self.import_document()
        sid = first["document"]["source_id"]
        self.provider.delete_success = False
        replacement = self.import_document(key="replace", source_id=sid, source_version=2)
        self.assertEqual(replacement["state"], "retryable_failed")
        self.source.close()
        home = self.base / "source"
        with patch.dict(os.environ, {"HOME": str(home)}):
            resumed = JournalApplication(paths=LifeContextPaths.current(), hindsight=self.provider,
                vault=ManagedVault(home / "vault", home / "vault" / "Journal"))
        self.applications.append(resumed)
        resumed.documents.wiki_hook = None
        self.provider.delete_success = True
        report = asyncio.run(resumed.recover_startup())
        self.assertGreaterEqual(report["completed"], 1)
        self.assertEqual(resumed.catalog.get_operation(replacement["operation_id"])["state"], "completed")
