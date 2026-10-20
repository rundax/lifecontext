"""Portable catalog records do not assert destination provider completion."""
from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from local_paths import scratch_root

from lifecontext.catalog import CatalogConflict, CatalogError, LifeContextCatalog
from lifecontext.custody import SourceManifest
from lifecontext.paths import LifeContextPaths


class DocumentSnapshotCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=scratch_root())
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.source = LifeContextCatalog(LifeContextPaths(root / "source"))
        self.target = LifeContextCatalog(LifeContextPaths(root / "target"))
        self.addCleanup(self.source.close)
        self.addCleanup(self.target.close)
        self.manifests = []

    def document(self, source_id="doc_1", version=1):
        original = f"Original {source_id} version {version}".encode()
        manifest = SourceManifest(source_id, version, hashlib.sha256(original).hexdigest(), len(original), {"source": "fixture"})
        self.manifests.append(manifest)
        self.source.register_manifest(manifest, layer="document")
        self.source.register_document_metadata(source_id, version, filename="original.txt", title="Original", mime_type="text/plain")
        self.source.record_extraction(source_id, version, 1, original_sha256=manifest.sha256, extractor="text", extractor_version="1", config_sha256="c" * 64,
            artifact_path=f"derived/{source_id}/v{version}/r1.json", artifact_sha256="a" * 64, status="completed", passage_count=1)
        self.source.record_provider_chunk(f"chunk_{source_id}_{version}", source_id, version, 1, passage_start=0, passage_end=1, content_sha256="d" * 64)
        self.source.update_provider_chunk(f"chunk_{source_id}_{version}", expected_revision=1, submission_state="submitted", completion_state="completed")
        for stage in ("extraction", "indexing"):
            self.source.transition_document_stage(source_id, version, stage, expected_state="pending", expected_revision=1, state="running")
            self.source.transition_document_stage(source_id, version, stage, expected_state="running", expected_revision=2, state="completed")
        return manifest

    def page(self, manifest, page_id="topic_1", expected_revision=0):
        self.source.create_wiki_page(page_id, title="Topic")
        revision = expected_revision + 1
        checksum = str(revision) * 64
        return self.source.commit_wiki_revision(page_id, expected_revision=expected_revision,
            artifact_path=f"wiki_revisions/{page_id}/r{revision}-{checksum}.json", artifact_sha256=checksum,
            dependencies=[{"source_id": manifest.source_id, "source_version": manifest.source_version, "extraction_revision": 1,
                "passage_start": 0, "passage_end": 1, "original_sha256": manifest.sha256}])

    def register_target(self):
        for manifest in self.manifests:
            self.target.register_manifest(manifest, layer="document")

    def reserve(self, catalog, manifest, state):
        operation = catalog.begin_operation(f"reserve-{manifest.source_id}-{manifest.source_version}", "b" * 64, "import")
        catalog.initialize_transfer(operation["operation_id"], "b" * 64, 1,
            [{"source_id": manifest.source_id, "source_version": manifest.source_version, "lifecycle_state": state,
              "sha256": None if state == "deleted" else manifest.sha256}])
        catalog.transition_operation(operation["operation_id"], "pending", "running")
        catalog.record_transfer_tombstone(operation["operation_id"], manifest.source_id, manifest.source_version, state)

    def test_export_preflight_restore_history_category_and_destination_pending(self):
        first = self.document()
        self.page(first)
        second = self.document(version=2)
        self.page(second, expected_revision=1)
        proposal = self.source.record_category_proposal("proposal_1", name="Work", description="Broad", rationale="Recurring",
            documents=[{"source_id": "doc_1", "source_version": 2}], closest_category_id="inbox")
        self.source.decide_category_proposal("proposal_1", expected_revision=proposal["revision"], decision="approve", category_id="work")
        self.source.record_wiki_review("review_1", "topic_1", 2, code="content_conflict", details={"expected_hash": "2" * 64, "actual_hash": "e" * 64})
        self.source.resolve_wiki_review("review_1", note="Restored the managed text.")
        snapshot = self.source.export_document_snapshot()
        self.assertEqual(snapshot["metadata"][0]["category_id"], "work")
        self.assertEqual(snapshot["wiki_revisions"][0]["dependencies"][0]["source_version"], 1)
        before = self.target._connection.total_changes
        detached = self.target.preflight_document_snapshot(snapshot)
        detached["metadata"][0]["title"] = "Not shared"
        self.assertEqual(snapshot["metadata"][0]["title"], "Original")
        self.assertEqual(self.target._connection.total_changes, before)
        self.register_target()
        self.target.restore_document_snapshot(snapshot)
        exported = self.target.export_document_snapshot()
        self.target.restore_document_snapshot(snapshot)
        self.assertEqual(self.target.export_document_snapshot(), exported)
        self.assertEqual(self.target.get_document_metadata("doc_1", 2)["category_id"], "work")
        self.assertEqual(self.target.list_wiki_revisions("topic_1"), self.source.list_wiki_revisions("topic_1"))
        self.assertEqual(self.target.list_wiki_reviews("topic_1"), self.source.list_wiki_reviews("topic_1"))
        self.assertEqual(self.target.get_wiki_page("topic_1")["state"], "stale")
        for version in (1, 2):
            for stage in ("indexing", "wiki", "projection"):
                self.assertEqual(self.target.get_document_stage("doc_1", version, stage)["state"], "pending")
            self.assertEqual(self.target.list_provider_chunks("doc_1", version), [])
        with self.assertRaises(CatalogConflict):
            self.target.commit_wiki_revision("topic_1", expected_revision=2, artifact_path="wiki_revisions/new.json", artifact_sha256="f" * 64,
                dependencies=self.target.get_wiki_revision("topic_1", 2)["dependencies"])

    def review_snapshots(self):
        manifest = self.document()
        self.page(manifest)
        self.source.record_wiki_review("review_1", "topic_1", 1, code="manual_edit", details={"expected_hash": "2" * 64, "actual_hash": "e" * 64})
        pending = self.source.export_document_snapshot()
        self.source.resolve_wiki_review("review_1", note="Undid the edit; keep the managed text.")
        resolved = self.source.export_document_snapshot()
        return pending, resolved

    def target_review(self, catalog=None):
        found, _ = (catalog or self.target).find_wiki_reviews(review_id="review_1")
        return found[0]["state"], found[0]["resolution"]

    def test_review_resolutions_round_trip_and_merge_one_way(self):
        pending, resolved = self.review_snapshots()
        self.assertNotIn("resolution", pending["wiki_reviews"][0])
        note = {"note": "Undid the edit; keep the managed text.", "resolved_at": resolved["wiki_reviews"][0]["resolution"]["resolved_at"]}
        self.assertEqual(resolved["wiki_reviews"][0]["resolution"], note)
        self.register_target()
        self.target.restore_document_snapshot(pending)
        self.assertEqual(self.target_review(), ("pending", None))
        self.assertEqual(self.target.get_wiki_page("topic_1")["state"], "conflict")
        self.target.restore_document_snapshot(resolved)
        self.assertEqual(self.target_review(), ("resolved", note))
        self.assertEqual(self.target.get_wiki_page("topic_1")["state"], "stale")
        self.assertEqual(self.target.export_document_snapshot()["wiki_reviews"], resolved["wiki_reviews"])
        self.target.restore_document_snapshot(pending)
        self.assertEqual(self.target_review(), ("resolved", note))
        self.assertEqual(self.target.get_wiki_page("topic_1")["state"], "stale")

    def test_a_locally_resolved_review_keeps_its_own_note_and_old_bundles_still_import(self):
        pending, resolved = self.review_snapshots()
        self.register_target()
        self.target.restore_document_snapshot(pending)
        self.target.resolve_wiki_review("review_1", note="Resolved on this device.")
        local = self.target_review()
        self.target.restore_document_snapshot(resolved)
        self.assertEqual(self.target_review(), local)
        legacy = copy.deepcopy(resolved)
        del legacy["wiki_reviews"][0]["resolution"]
        fresh = LifeContextCatalog(LifeContextPaths(Path(self.temp.name) / "fresh"))
        self.addCleanup(fresh.close)
        for manifest in self.manifests:
            fresh.register_manifest(manifest, layer="document")
        fresh.restore_document_snapshot(legacy)
        self.assertEqual(self.target_review(fresh), ("resolved", None))

    def test_invalid_review_resolutions_are_rejected(self):
        _, resolved = self.review_snapshots()
        for change in ({"state": "pending"}, {"resolution": {"note": "", "resolved_at": "2026-10-17T08:00:00Z"}},
                       {"resolution": {"note": "ok", "resolved_at": "yesterday"}},
                       {"resolution": {"note": "ok", "resolved_at": "2026-10-17T08:00:00Z", "by": "agent"}}):
            invalid = copy.deepcopy(resolved)
            invalid["wiki_reviews"][0].update(change)
            with self.subTest(change=change), self.assertRaises(CatalogError):
                self.target.preflight_document_snapshot(invalid)

    def test_source_layers_include_deleted_document_without_auxiliary_records(self):
        manifest = self.document()
        self.reserve(self.source, manifest, "deleted")
        snapshot = self.source.export_document_snapshot()
        self.assertEqual(snapshot["sources"], [{"source_id": "doc_1", "source_version": 1, "layer": "document", "lifecycle_state": "deleted", "sha256": None}])
        self.assertEqual(snapshot["metadata"], [])
        self.assertEqual(snapshot["extractions"], [])
        self.reserve(self.target, manifest, "deleted")
        self.target.restore_document_snapshot(snapshot)
        self.assertEqual(self.target.get_source("doc_1")["layer"], "document")
        self.assertIsNone(self.target.get_manifest("doc_1", 1))
        self.assertEqual(self.target.get_tombstone("doc_1", 1)["tombstone_type"], "deleted")

    def test_forgotten_historical_dependencies_restored_without_publication(self):
        manifest = self.document()
        self.page(manifest)
        self.reserve(self.source, manifest, "forgotten")
        snapshot = self.source.export_document_snapshot()
        self.register_target()
        self.reserve(self.target, manifest, "forgotten")
        self.target.restore_document_snapshot(snapshot)
        self.assertEqual(self.target.get_wiki_revision("topic_1", 1)["dependencies"], snapshot["wiki_revisions"][0]["dependencies"])
        self.assertEqual(self.target.get_wiki_page("topic_1")["state"], "stale")
        self.assertEqual(self.target.list_provider_chunks("doc_1", 1), [])

    def test_deleted_dependent_history_export_fails_instead_of_dropping_history(self):
        manifest = self.document()
        self.page(manifest)
        self.reserve(self.source, manifest, "deleted")
        with self.assertRaises(CatalogError):
            self.source.export_document_snapshot()
        self.assertEqual(len(self.source.list_wiki_revisions("topic_1")), 1)

    def test_selection_expands_dependency_and_proposal_document_closure(self):
        first = self.document()
        second = self.document("doc_2")
        self.page(first)
        self.source.record_category_proposal("proposal_1", name="Work", description="", rationale="Recurring",
            documents=[{"source_id": first.source_id, "source_version": 1}, {"source_id": second.source_id, "source_version": 1}])
        snapshot = self.source.export_document_snapshot(source_ids=["doc_1"])
        self.assertEqual({row["source_id"] for row in snapshot["sources"]}, {"doc_1", "doc_2"})
        self.assertEqual(len(snapshot["category_proposals"][0]["documents"]), 2)

    def test_preflight_category_alias_and_identity_conflicts_have_no_effects(self):
        self.document()
        snapshot = self.source.export_document_snapshot()
        collision = copy.deepcopy(snapshot)
        collision["categories"].append({"category_id": "other", "name": "Other", "name_key": "other", "description": "", "aliases": ["INBOX"], "revision": 1})
        with self.assertRaises(CatalogConflict):
            self.target.preflight_document_snapshot(collision)
        self.assertEqual(len(self.target.list_categories()), 1)
        self.register_target()
        self.target.restore_document_snapshot(snapshot)
        changed = copy.deepcopy(snapshot)
        changed["extractions"][0]["artifact_sha256"] = "e" * 64
        with self.assertRaises(CatalogConflict):
            self.target.preflight_document_snapshot(changed)
        self.assertEqual(self.target.get_extraction("doc_1", 1, 1)["artifact_sha256"], "a" * 64)

    def test_restore_rolls_back_category_and_metadata_on_late_artifact_failure(self):
        self.document()
        snapshot = self.source.export_document_snapshot()
        snapshot["categories"].append({"category_id": "work", "name": "Work", "name_key": "work", "description": "", "aliases": [], "revision": 1})
        self.register_target()
        with self.target.transaction() as connection:
            connection.execute("CREATE TRIGGER fail_snapshot BEFORE INSERT ON extractions BEGIN SELECT RAISE(ABORT,'test failure'); END")
        with self.assertRaises(CatalogError):
            self.target.restore_document_snapshot(snapshot)
        self.assertEqual([row["category_id"] for row in self.target.list_categories()], ["inbox"])
        self.assertIsNone(self.target.get_document_metadata("doc_1", 1))
        self.assertEqual(self.target.list_extractions("doc_1", 1), [])

    def test_invalid_nested_fields_and_missing_source_fail_closed(self):
        self.document()
        snapshot = self.source.export_document_snapshot()
        for mutate in (
            lambda data: data["sources"][0].update(layer=[]),
            lambda data: data["metadata"][0].update(category_revision=True),
            lambda data: data["stage_intents"][0].update(state=[]),
            lambda data: data["extractions"][0].update(original_sha256="b" * 64),
        ):
            changed = copy.deepcopy(snapshot)
            mutate(changed)
            with self.assertRaises(CatalogError):
                self.target.preflight_document_snapshot(changed)
        with self.assertRaises(CatalogConflict):
            self.target.restore_document_snapshot(snapshot)
        self.assertEqual(self.target.list_documents(), [])

    def test_category_operation_uses_content_free_receipt_and_atomic_completion(self):
        self.document()
        intent = {"kind": "category_decision", "decision": "propose", "classification": {"name": "Work", "description": "", "rationale": "Recurring", "confidence": 0.95, "broad": True, "tags": [], "closest_category_id": None}, "documents": [{"source_id": "doc_1", "source_version": 1}]}
        operation = self.source.begin_operation("category", "b" * 64, "category_decision")
        self.source.set_operation_intent(operation["operation_id"], {"recovery_intent": intent})
        with self.source.category_operation(operation["operation_id"]) as checkpoint:
            proposal = self.source.record_category_proposal("proposal_1", name="Work", description="", rationale="Recurring", documents=intent["documents"])
            body = {"status": "pending", "category_id": "inbox", "proposal": proposal, "tags": [], "budget_exceeded": False, "reuse_candidates": self.source.list_categories()}
            checkpoint["result"]["category_result"] = self.source.category_result_receipt(body)
        completed = self.source.get_operation(operation["operation_id"])
        self.assertEqual(completed["state"], "completed")
        receipt = completed["result"]["category_result"]
        self.assertNotIn("Recurring", json.dumps(receipt))
        self.assertNotIn("Work", json.dumps(receipt))
        self.assertEqual(self.source.category_result_response(receipt, intent)["proposal"]["proposal_id"], "proposal_1")
        other = self.source.begin_operation("unsafe-category", "c" * 64, "category_decision")
        self.source.set_operation_intent(other["operation_id"], {"recovery_intent": intent})
        with self.assertRaises(CatalogError), self.source.category_operation(other["operation_id"]) as checkpoint:
            checkpoint["result"]["category_result"] = {"kind": "category", "category_id": "inbox", "revision": 1, "text": "unbounded secret"}
        self.assertEqual(self.source.get_operation(other["operation_id"])["state"], "pending")

    def test_wiki_publication_rolls_back_nested_page_creation(self):
        with self.assertRaises(CatalogConflict), self.source.wiki_publication():
            self.source.create_wiki_page("topic_1", title="Topic")
            self.source.create_wiki_page("topic_1", title="Different")
        self.assertIsNone(self.source.get_wiki_page("topic_1"))
