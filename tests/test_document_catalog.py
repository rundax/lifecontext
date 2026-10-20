"""V6 document, category, wiki and artifact ledger contracts."""
from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from local_paths import scratch_root

from lifecontext import catalog as catalog_module
from lifecontext.catalog import CatalogConflict, CatalogError, LifeContextCatalog
from lifecontext.custody import SourceManifest
from lifecontext.paths import LifeContextPaths


class DocumentCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=scratch_root())
        self.paths = LifeContextPaths(Path(self.temporary.name))
        self.catalog = LifeContextCatalog(self.paths)
        self.manifest = SourceManifest("doc_1", 1, hashlib.sha256(b"original").hexdigest(), 8, {"source": "test"})

    def tearDown(self):
        self.catalog.close()
        self.temporary.cleanup()

    def document(self):
        self.catalog.register_manifest(self.manifest, layer="document")
        return self.catalog.register_document_metadata("doc_1", 1, filename="original.pdf", title="Original", mime_type="application/pdf")

    def extraction(self, revision=1, status="completed"):
        return self.catalog.record_extraction("doc_1", 1, revision, original_sha256=self.manifest.sha256,
            extractor="pdf", extractor_version="1.0", config_sha256="c" * 64,
            artifact_path="extractions/doc_1/1/1.json" if status == "completed" else None,
            artifact_sha256="a" * 64 if status == "completed" else None,
            status=status, passage_count=3 if status == "completed" else 0, warnings=["ocr_used"])

    def dependency(self):
        return {"source_id": "doc_1", "source_version": 1, "extraction_revision": 1,
            "passage_start": 0, "passage_end": 3, "original_sha256": self.manifest.sha256}

    def proposal(self, proposal_id="proposal_1", name="Work"):
        return self.catalog.record_category_proposal(proposal_id, name=name, description="Broad", rationale="Fits",
            documents=[{"source_id": "doc_1", "source_version": 1}], closest_category_id="inbox")

    def downgrade_v5(self):
        self.catalog.close()
        connection = sqlite3.connect(self.paths.catalog_path)
        connection.execute("PRAGMA foreign_keys=OFF")
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall():
            if row[0] not in catalog_module._SCHEMA_V5_COLUMNS:
                connection.execute(f'DROP TABLE "{row[0]}"')
        connection.execute("ALTER TABLE sources DROP COLUMN layer")
        operation_sql = connection.execute("SELECT sql FROM sqlite_master WHERE name='operations'").fetchone()[0]
        operation_sql = operation_sql.replace('CREATE TABLE "operations"', 'CREATE TABLE operations_v5')
        operation_sql = operation_sql.replace(", 'document_import', 'document_retry', 'wiki_rebuild', 'category_decision', 'wiki_review'", "")
        connection.execute(operation_sql)
        connection.execute("INSERT INTO operations_v5 SELECT * FROM operations")
        connection.execute("DROP TABLE operations")
        connection.execute("ALTER TABLE operations_v5 RENAME TO operations")
        connection.execute("CREATE INDEX operations_state_created_idx ON operations(state,created_at,operation_id)")
        connection.execute("PRAGMA user_version=5")
        connection.commit()
        connection.close()

    def test_fresh_reopen_and_derived_outside_archive(self):
        self.assertEqual([row["category_id"] for row in self.catalog.list_categories()], ["inbox"])
        self.assertEqual(self.paths.derived_dir, self.paths.lifeweave_home / "derived")
        self.assertNotIn(self.paths.archive_dir, self.paths.derived_dir.parents)
        for _ in range(2):
            self.catalog.close()
            self.catalog = LifeContextCatalog(self.paths)
            self.assertEqual(self.catalog._connection.execute("PRAGMA user_version").fetchone()[0], 9)
            self.assertEqual(len(self.catalog.list_categories()), 1)

    def test_populated_v5_migration_preserves_originals_operations_and_foreign_keys(self):
        self.catalog.register_manifest(self.manifest)
        operation = self.catalog.begin_operation("legacy", "b" * 64, "retain")
        self.catalog.transition_operation(operation["operation_id"], "pending", "running")
        self.catalog.transition_operation(operation["operation_id"], "running", "completed", result={"sources": [{"source_id": "doc_1", "source_version": 1, "sha256": self.manifest.sha256}]})
        before = self.catalog.get_manifest("doc_1", 1)
        with self.catalog.transaction() as connection:
            connection.execute("INSERT INTO curation_history(operation_id,source_id,source_version,action) VALUES(?,?,1,'correct')", (operation["operation_id"], "doc_1"))
        self.downgrade_v5()
        self.catalog = LifeContextCatalog(self.paths)
        self.assertEqual(self.catalog.get_manifest("doc_1", 1), before)
        self.assertEqual(self.catalog.get_source("doc_1")["layer"], "journal")
        self.assertEqual(self.catalog.get_operation(operation["operation_id"])["state"], "completed")
        self.assertEqual(self.catalog.get_curation_history(operation["operation_id"])["action"], "correct")
        self.assertEqual(self.catalog._connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_v5_failure_rolls_back_all_ddl_and_operation_rebuild(self):
        self.catalog.register_manifest(self.manifest)
        operation = self.catalog.begin_operation("legacy", "b" * 64, "retain")
        self.downgrade_v5()
        with sqlite3.connect(self.paths.catalog_path) as connection:
            before = connection.execute("SELECT name,sql FROM sqlite_master ORDER BY name").fetchall()
        with patch.object(catalog_module, "_SCHEMA_V6", (*catalog_module._SCHEMA_V6, "INVALID SQL")), self.assertRaises(CatalogError):
            LifeContextCatalog(self.paths)
        with sqlite3.connect(self.paths.catalog_path) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 5)
            self.assertEqual(connection.execute("SELECT name,sql FROM sqlite_master ORDER BY name").fetchall(), before)
            self.assertEqual(connection.execute("SELECT operation_id FROM operations").fetchone()[0], operation["operation_id"])
        self.catalog = LifeContextCatalog(self.paths)

    def test_extra_column_or_weakened_v6_check_rejected(self):
        self.catalog.close()
        with sqlite3.connect(self.paths.catalog_path) as connection:
            connection.execute("ALTER TABLE document_metadata ADD COLUMN unexpected TEXT")
        with self.assertRaises(CatalogError):
            LifeContextCatalog(self.paths)

    def test_strict_foreign_keys_and_range_constraints(self):
        self.document()
        with self.assertRaises(CatalogError), self.catalog.transaction() as connection:
            connection.execute("INSERT INTO document_categories VALUES('missing','inbox',1)")
        with self.assertRaises(CatalogError), self.catalog.transaction() as connection:
            connection.execute("UPDATE sources SET layer='wiki'")
        with self.assertRaises(CatalogError), self.catalog.transaction() as connection:
            connection.execute("UPDATE document_stages SET state='accepted' WHERE stage='indexing'")
        with self.assertRaises(CatalogError), self.catalog.transaction() as connection:
            connection.execute("INSERT INTO wiki_revisions(page_id,revision,artifact_path,artifact_sha256) VALUES('missing',1,'wiki/a','" + "a" * 64 + "')")

    def test_document_metadata_immutable_version_bound_and_layer_is_immutable(self):
        metadata = self.document()
        self.assertEqual(metadata["category_id"], "inbox")
        self.assertEqual(self.document(), metadata)
        with self.assertRaises(CatalogConflict):
            self.catalog.register_manifest(self.manifest)
        with self.assertRaises(CatalogConflict):
            self.catalog.register_document_metadata("doc_1", 1, filename="other.pdf", title="Original", mime_type="application/pdf")
        with self.assertRaises(CatalogError):
            self.catalog.register_document_metadata("doc_1", 1, filename="../other", title="Original", mime_type="text/plain")
        journal = SourceManifest("journal_1", 1, "b" * 64, 0, {})
        self.catalog.register_manifest(journal)
        with self.assertRaises(CatalogError):
            self.catalog.register_document_metadata("journal_1", 1, filename="j.txt", title="Journal", mime_type="text/plain")
        self.assertEqual(self.catalog._connection.execute("SELECT manifest_json FROM source_versions WHERE source_id='doc_1' AND source_version=1").fetchone()[0], self.manifest.to_bytes().decode())

    def test_extraction_revisions_and_failure_statuses(self):
        self.document()
        artifact = self.extraction()
        self.assertEqual(artifact["warnings"], ["ocr_used"])
        self.assertEqual(self.extraction(), artifact)
        for revision, status in enumerate(("blocked", "failed", "unsupported", "locked", "limit_exceeded"), 2):
            self.extraction(revision, status)
        self.assertEqual(len(self.catalog.list_extractions("doc_1", 1)), 6)
        with self.assertRaises(CatalogConflict):
            self.extraction(1, "failed")
        with self.assertRaises(CatalogError):
            self.catalog.record_extraction("doc_1", 1, 8, original_sha256=self.manifest.sha256, extractor="pdf", extractor_version="1", config_sha256="c" * 64, artifact_path="../escape", artifact_sha256="a" * 64, status="completed", passage_count=1)

    def test_stage_cas_retry_attempts_and_redacted_errors(self):
        self.document()
        current = self.catalog.get_document_stage("doc_1", 1, "extraction")
        running = self.catalog.transition_document_stage("doc_1", 1, "extraction", expected_state=current["state"], expected_revision=current["revision"], state="running")
        self.assertEqual(running["attempt"], 1)
        with self.assertRaises(CatalogConflict):
            self.catalog.transition_document_stage("doc_1", 1, "extraction", expected_state="pending", expected_revision=1, state="running")
        with self.assertRaises(CatalogError):
            self.catalog.transition_document_stage("doc_1", 1, "extraction", expected_state="running", expected_revision=2, state="failed", error_code="secret path /tmp/input")
        self.catalog.transition_document_stage("doc_1", 1, "extraction", expected_state="running", expected_revision=2, state="blocked", error_code="internal_error")
        retry = self.catalog.transition_document_stage("doc_1", 1, "extraction", expected_state="blocked", expected_revision=3, state="running")
        self.assertEqual(retry["attempt"], 2)
        self.assertIsNone(retry["error_code"])

    def test_provider_chunks_cas_acknowledgement_and_complete_coverage(self):
        self.document()
        self.extraction()
        self.catalog.transition_document_stage("doc_1", 1, "indexing", expected_state="pending", expected_revision=1, state="running")
        def finish():
            return self.catalog.transition_document_stage("doc_1", 1, "indexing", expected_state="running", expected_revision=2, state="completed")
        with self.assertRaises(CatalogConflict):
            finish()
        self.catalog.record_provider_chunk("chunk_1", "doc_1", 1, 1, passage_start=0, passage_end=1, content_sha256="d" * 64)
        with self.assertRaises(CatalogError):
            self.catalog.update_provider_chunk("chunk_1", expected_revision=1, submission_state="pending", completion_state="completed")
        self.catalog.update_provider_chunk("chunk_1", expected_revision=1, submission_state="submitted", completion_state="completed")
        with self.assertRaises(CatalogConflict):
            finish()
        self.catalog.record_provider_chunk("chunk_2", "doc_1", 1, 1, passage_start=1, passage_end=3, content_sha256="e" * 64)
        with self.assertRaises(CatalogConflict):
            finish()
        self.catalog.update_provider_chunk("chunk_2", expected_revision=1, submission_state="submitted", completion_state="completed")
        self.assertEqual(finish()["state"], "completed")
        with self.assertRaises(CatalogConflict):
            self.catalog.update_provider_chunk("chunk_2", expected_revision=1, submission_state="submitted", completion_state="completed")
        self.assertEqual(len(self.catalog.list_provider_chunks("doc_1", 1)), 2)
        with self.assertRaises(CatalogConflict):
            self.catalog.record_provider_chunk("overflow", "doc_1", 1, 1, passage_start=3, passage_end=4, content_sha256="e" * 64)

    def test_pending_proposal_dedup_decision_and_merge_are_transactional(self):
        self.document()
        self.proposal()
        duplicate = self.proposal("proposal_2", "WORK")
        self.assertEqual(duplicate["proposal_id"], "proposal_1")
        self.catalog.decide_category_proposal("proposal_1", expected_revision=1, decision="approve", category_id="work")
        self.assertEqual(self.catalog.get_document_metadata("doc_1", 1)["category_id"], "work")
        self.proposal("proposal_3", "Home")
        with self.assertRaises(CatalogConflict):
            self.catalog.decide_category_proposal("proposal_3", expected_revision=1, decision="approve", category_id="home", category_budget=1)
        self.catalog.decide_category_proposal("proposal_3", expected_revision=1, decision="approve", category_id="home", category_budget=1, budget_override=True)
        merged = self.catalog.merge_categories("home", "work", expected_source_revision=1, expected_target_revision=1)
        self.assertIn("Home", merged["aliases"])
        self.assertEqual(self.catalog.get_category_proposal("proposal_3")["category_id"], "work")
        self.assertEqual(self.catalog.get_document_metadata("doc_1", 1)["category_id"], "work")
        with self.assertRaises(CatalogError):
            self.catalog.merge_categories("inbox", "work", expected_source_revision=1, expected_target_revision=2)
        self.assertEqual(self.catalog.get_document_stage("doc_1", 1, "indexing")["revision"], 1)

    def test_wiki_ancestry_dependencies_review_and_category_invalidation(self):
        self.document()
        self.extraction()
        self.catalog.create_wiki_page("page_1", title="Topic")
        with self.assertRaises(CatalogConflict):
            self.catalog.commit_wiki_revision("page_1", expected_revision=0, artifact_path="wiki/page_1/1.md", artifact_sha256="f" * 64, dependencies=[self.dependency()])
        self.catalog.record_provider_chunk("wiki_chunk", "doc_1", 1, 1, passage_start=0, passage_end=3, content_sha256="e" * 64)
        self.catalog.update_provider_chunk("wiki_chunk", expected_revision=1, submission_state="submitted", completion_state="completed")
        revision = self.catalog.commit_wiki_revision("page_1", expected_revision=0, artifact_path="wiki/page_1/1.md", artifact_sha256="f" * 64, dependencies=[self.dependency()])
        self.assertIsNone(revision["parent_revision"])
        self.assertEqual(revision["dependencies"], [self.dependency()])
        with self.assertRaises(CatalogConflict):
            self.catalog.commit_wiki_revision("page_1", expected_revision=0, artifact_path="wiki/page_1/2.md", artifact_sha256="f" * 64, dependencies=[self.dependency()])
        with self.assertRaises(CatalogConflict):
            self.catalog.commit_wiki_revision("page_1", expected_revision=1, artifact_path="wiki/page_1/2.md", artifact_sha256="f" * 64, dependencies=[self.dependency() | {"original_sha256": "b" * 64}])
        self.proposal()
        self.catalog.decide_category_proposal("proposal_1", expected_revision=1, decision="approve", category_id="work")
        self.assertEqual(self.catalog.get_wiki_page("page_1")["state"], "stale")
        self.catalog.record_wiki_review("review_1", "page_1", 1, code="manual_edit", details={"expected_hash": "f" * 64, "actual_hash": "b" * 64})
        with self.assertRaises(CatalogConflict):
            self.catalog.commit_wiki_revision("page_1", expected_revision=1, artifact_path="wiki/page_1/2.md", artifact_sha256="f" * 64, dependencies=[self.dependency()])
        self.assertEqual(len(self.catalog.list_wiki_revisions("page_1")), 1)
        self.catalog.resolve_wiki_review("review_1", note="Restored the managed text.")
        self.assertEqual(self.catalog.list_wiki_reviews("page_1")[0]["state"], "resolved")

    def test_projection_ledger_separates_layers_and_checks_paths_hashes(self):
        projection = self.catalog.set_artifact_projection("document", "doc_1", 1, projection_path="documents/inbox/a.pdf", desired_hash="a" * 64, state="pending")
        self.assertEqual(projection["state"], "pending")
        self.catalog.set_artifact_projection("wiki", "doc_1", 1, projection_path="wiki/a.md", desired_hash="b" * 64, state="projected", last_projected_hash="b" * 64)
        self.assertEqual(self.catalog.get_artifact_projection("document", "doc_1", 1), projection)
        for path in ("../escape", "/absolute", "a//b", "a\\b"):
            with self.assertRaises(CatalogError):
                self.catalog.set_artifact_projection("document", "doc_1", 1, projection_path=path, desired_hash="a" * 64, state="pending")
        with self.assertRaises(CatalogError):
            self.catalog.set_artifact_projection("document", "doc_1", 1, projection_path="a.pdf", desired_hash="a" * 64, state="projected", last_projected_hash="b" * 64)

    def test_nested_savepoint_rolls_back_caught_error_without_losing_outer_changes(self):
        with self.catalog.transaction() as connection:
            connection.execute("INSERT INTO sources(source_id) VALUES('outer')")
            try:
                with self.catalog.transaction() as nested:
                    nested.execute("INSERT INTO sources(source_id) VALUES('inner')")
                    raise RuntimeError("inner failure")
            except RuntimeError:
                pass
            self.assertIsNone(connection.execute("SELECT 1 FROM sources WHERE source_id='inner'").fetchone())
        self.assertIsNotNone(self.catalog.get_source("outer"))
        self.assertIsNone(self.catalog.get_source("inner"))

    def test_category_operation_atomic_completion_and_failure_rollback(self):
        self.document()
        self.proposal()
        operation = self.catalog.begin_operation("approve", "a" * 64, "category_decision")
        intent = {"kind": "category_decision", "decision": "approve", "proposal_id": "proposal_1", "expected_revision": 1, "category_id": "work"}
        self.catalog.transition_operation(operation["operation_id"], "pending", "running", result={"recovery_intent": intent})
        with self.assertRaises(RuntimeError), self.catalog.category_operation(operation["operation_id"]):
            self.catalog.decide_category_proposal("proposal_1", expected_revision=1, decision="approve", category_id="work")
            raise RuntimeError("crash before checkpoint")
        self.assertEqual(self.catalog.get_category_proposal("proposal_1")["state"], "pending")
        self.assertEqual(self.catalog.get_operation(operation["operation_id"])["state"], "running")
        self.assertEqual(len(self.catalog.list_categories()), 1)
        with self.catalog.category_operation(operation["operation_id"]):
            self.catalog.decide_category_proposal("proposal_1", expected_revision=1, decision="approve", category_id="work")
        self.assertEqual(self.catalog.get_operation(operation["operation_id"])["state"], "completed")
        self.assertEqual(self.catalog.get_category_proposal("proposal_1")["state"], "approved")
        with self.assertRaises(CatalogConflict), self.catalog.category_operation(operation["operation_id"]):
            pass

    def test_proposal_new_documents_invalidate_approval_revision_but_retry_does_not(self):
        self.document()
        self.proposal()
        other = SourceManifest("doc_2", 1, "b" * 64, 0, {})
        self.catalog.register_manifest(other, layer="document")
        self.catalog.register_document_metadata("doc_2", 1, filename="two.txt", title="Two", mime_type="text/plain")
        proposal = self.catalog.record_category_proposal("another_id", name=" work ", description="", rationale="", documents=[{"source_id": "doc_2", "source_version": 1}])
        self.assertEqual(proposal["revision"], 2)
        identical = self.catalog.record_category_proposal("another_id", name=" work ", description="", rationale="", documents=[{"source_id": "doc_2", "source_version": 1}])
        self.assertEqual(identical["revision"], 2)
        with self.assertRaises(CatalogConflict):
            self.catalog.decide_category_proposal("proposal_1", expected_revision=1, decision="approve", category_id="work")
        self.catalog.decide_category_proposal("proposal_1", expected_revision=2, decision="approve", category_id="work")

    def test_category_mutation_recovery_variants_validate_exact_metadata(self):
        variants = [
            {"decision": "propose", "classification": {"name": "Work", "description": "", "rationale": "", "confidence": .9, "broad": True, "tags": [], "closest_category_id": None}, "documents": [{"source_id": "doc_1", "source_version": 1}]},
            {"decision": "rename", "category_id": "work", "name": "Work", "description": "", "expected_revision": 1},
            {"decision": "alias", "category_id": "work", "alias": "Career", "expected_revision": 1},
            {"decision": "merge", "source_category_id": "work", "target_category_id": "inbox", "expected_source_revision": 1, "expected_target_revision": 1},
            {"decision": "reassign", "source_id": "doc_1", "category_id": "inbox", "expected_revision": 1},
        ]
        for variant in variants:
            intent = {"kind": "category_decision", **variant}
            encoded = catalog_module._validate_operation_result({"recovery_intent": intent})
            self.assertIn('"category_decision"', encoded)
            with self.assertRaises(CatalogError):
                catalog_module._validate_operation_result({"recovery_intent": intent | {"untrusted_sql": "DELETE"}})
        with self.assertRaises(CatalogError):
            catalog_module._validate_operation_result({"recovery_intent": {"kind": "wiki_rebuild", "source_id": "doc_1", "source_version": None}})

    def test_gap_overlap_indexing_and_tombstoned_wiki_are_rejected(self):
        self.document()
        self.extraction()
        self.catalog.transition_document_stage("doc_1", 1, "indexing", expected_state="pending", expected_revision=1, state="running")
        self.catalog.record_provider_chunk("gap", "doc_1", 1, 1, passage_start=1, passage_end=3, content_sha256="e" * 64)
        self.catalog.update_provider_chunk("gap", expected_revision=1, submission_state="submitted", completion_state="completed")
        with self.assertRaises(CatalogConflict):
            self.catalog.transition_document_stage("doc_1", 1, "indexing", expected_state="running", expected_revision=2, state="completed")
        self.catalog.record_provider_chunk("overlap", "doc_1", 1, 1, passage_start=0, passage_end=2, content_sha256="e" * 64)
        self.catalog.update_provider_chunk("overlap", expected_revision=1, submission_state="submitted", completion_state="completed")
        with self.assertRaises(CatalogConflict):
            self.catalog.transition_document_stage("doc_1", 1, "indexing", expected_state="running", expected_revision=2, state="completed")
        self.catalog.create_wiki_page("page_1", title="Page")
        operation = self.catalog.begin_operation("forget", "a" * 64, "curation")
        with self.catalog.transaction() as connection:
            connection.execute("INSERT INTO tombstones(source_id,source_version,tombstone_type,operation_id) VALUES('doc_1',1,'forgotten',?)", (operation["operation_id"],))
        with self.assertRaises(CatalogConflict):
            self.catalog.commit_wiki_revision("page_1", expected_revision=0, artifact_path="wiki/p.md", artifact_sha256="f" * 64, dependencies=[self.dependency()])
        self.assertEqual(self.catalog.get_wiki_page("page_1")["current_revision"], 0)

    def test_new_operation_kinds_and_bounded_recovery_envelopes(self):
        intents = {
            "document_import": {"source_id": "doc_1", "source_version": 1, "sha256": self.manifest.sha256, "size_bytes": 8, "filename": "a.pdf", "title": "A", "mime_type": "application/pdf", "provenance": {"source": "test"}},
            "document_retry": {"source_id": "doc_1", "source_version": 1, "sha256": self.manifest.sha256, "size_bytes": 8},
            "wiki_rebuild": {"page_id": "page_1"},
            "category_decision": {"proposal_id": "proposal_1", "decision": "approve", "category_id": "work", "expected_revision": 1, "category_budget": 10, "budget_override": False},
        }
        for kind, fields in intents.items():
            operation = self.catalog.begin_operation(kind, "a" * 64, kind)
            intent = {"kind": kind, **fields}
            self.catalog.transition_operation(operation["operation_id"], "pending", "running", result={"recovery_intent": intent})
            self.assertEqual(self.catalog.get_operation(operation["operation_id"])["result"]["recovery_intent"], intent)
            with self.assertRaises(CatalogError):
                self.catalog.transition_operation(operation["operation_id"], "running", "completed", result={"extracted_text": "content"})


if __name__ == "__main__":
    unittest.main()
