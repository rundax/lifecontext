"""Transactional SQLite catalog contracts for the LifeContext core."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from lifecontext.catalog import CatalogBusy, CatalogConflict, CatalogError, LifeContextCatalog
from lifecontext.custody import SourceManifest
from lifecontext.paths import LifeContextPaths
from catalog_downgrade import downgrade_to_version_eight
from local_paths import scratch_root


class LifeContextCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="lifeweave-catalog-test.", dir=scratch_root()
        )
        self.home = Path(self.temporary.name).resolve(strict=True)
        self.home_patch = patch.dict(os.environ, {"HOME": str(self.home)})
        self.home_patch.start()
        self.paths = LifeContextPaths.current()
        self.original = b"catalog source bytes"
        self.manifest = SourceManifest(
            source_id="src_catalog_1",
            source_version=1,
            sha256=hashlib.sha256(self.original).hexdigest(),
            size_bytes=len(self.original),
            provenance={"source": "test", "source_ref": "item-1"},
        )
        self.catalog = LifeContextCatalog(self.paths)

    def tearDown(self) -> None:
        if hasattr(self, "catalog"):
            self.catalog.close()
        self.home_patch.stop()
        self.temporary.cleanup()

    @staticmethod
    def _remove_version_six_additions(connection: sqlite3.Connection) -> None:
        for table in (
            "wiki_review_resolutions", "journal_projection_moves",
            "artifact_publications", "artifact_projections", "wiki_reviews", "wiki_dependencies", "wiki_revisions",
            "wiki_pages", "category_proposal_documents", "category_proposals", "provider_chunks",
            "document_stages", "extractions", "document_categories", "document_metadata", "categories",
        ):
            connection.execute(f"DROP TABLE IF EXISTS {table}")
        connection.execute("ALTER TABLE sources DROP COLUMN layer")

    def _downgrade_catalog_to_version_one(self) -> None:
        connection = sqlite3.connect(self.paths.catalog_path)
        self._remove_version_six_additions(connection)
        for table in (
            "transfer_items", "transfer_tombstones", "transfers",
            "tombstones", "curation_history", "operations",
        ):
            connection.execute(f"DROP TABLE IF EXISTS {table}")
        connection.execute("PRAGMA user_version=1")
        connection.commit()
        connection.close()

    def _downgrade_catalog_to_version_two(self) -> None:
        connection = sqlite3.connect(self.paths.catalog_path)
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("BEGIN IMMEDIATE")
        self._remove_version_six_additions(connection)
        for table in ("transfer_items", "transfer_tombstones", "transfers"):
            connection.execute(f"DROP TABLE IF EXISTS {table}")
        connection.execute(
            """CREATE TABLE operations_v2 (
                operation_id TEXT PRIMARY KEY NOT NULL CHECK (
                    length(operation_id) = 35
                    AND substr(operation_id, 1, 3) = 'op_'
                    AND substr(operation_id, 4) NOT GLOB '*[^0-9a-f]*'
                ),
                idempotency_key_digest TEXT NOT NULL UNIQUE
                    CHECK (length(idempotency_key_digest) = 64
                        AND idempotency_key_digest NOT GLOB '*[^0-9a-f]*'),
                request_digest TEXT NOT NULL
                    CHECK (length(request_digest) = 64
                        AND request_digest NOT GLOB '*[^0-9a-f]*'),
                operation_kind TEXT NOT NULL CHECK (operation_kind IN (
                    'retain', 'curation', 'relationship_scan', 'relationship_decision'
                )),
                state TEXT NOT NULL CHECK (state IN (
                    'pending', 'running', 'retryable_failed', 'permanent_failed',
                    'completed', 'cancelled'
                )),
                result_json TEXT NOT NULL DEFAULT '{}' CHECK (length(result_json) <= 65536),
                error_code TEXT CHECK (error_code IS NULL OR (
                    length(error_code) BETWEEN 1 AND 64
                    AND error_code NOT GLOB '*[^a-z0-9_]*'
                    AND substr(error_code, 1, 1) GLOB '[a-z]'
                )),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        connection.execute(
            """INSERT INTO operations_v2 SELECT operation_id, idempotency_key_digest,
                       request_digest, operation_kind, state, result_json, error_code,
                       created_at, updated_at FROM operations"""
        )
        connection.execute("DROP TABLE operations")
        connection.execute("ALTER TABLE operations_v2 RENAME TO operations")
        connection.execute(
            "CREATE INDEX operations_state_created_idx ON operations(state, created_at, operation_id)"
        )
        connection.execute("PRAGMA user_version=2")
        connection.commit()
        connection.execute("PRAGMA foreign_keys=ON")
        connection.close()

    def _downgrade_catalog_to_version_four(self) -> None:
        connection = sqlite3.connect(self.paths.catalog_path)
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("BEGIN IMMEDIATE")
        self._remove_version_six_additions(connection)
        for table in ("transfer_items", "transfer_tombstones", "transfers"):
            connection.execute(f"DROP TABLE IF EXISTS {table}")
        connection.execute(
            """CREATE TABLE operations_v4 (
                operation_id TEXT PRIMARY KEY NOT NULL,
                idempotency_key_digest TEXT NOT NULL UNIQUE,
                request_digest TEXT NOT NULL,
                operation_kind TEXT NOT NULL CHECK (operation_kind IN (
                    'retain', 'curation', 'relationship_scan', 'relationship_decision'
                )),
                state TEXT NOT NULL CHECK (state IN (
                    'pending', 'running', 'retryable_failed', 'permanent_failed',
                    'completed', 'cancelled'
                )),
                result_json TEXT NOT NULL DEFAULT '{}'
                    CHECK (length(CAST(result_json AS BLOB)) <= 4194304),
                error_code TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        connection.execute(
            "INSERT INTO operations_v4 SELECT * FROM operations"
        )
        connection.execute("DROP TABLE operations")
        connection.execute("ALTER TABLE operations_v4 RENAME TO operations")
        connection.execute(
            "CREATE INDEX operations_state_created_idx ON operations(state, created_at, operation_id)"
        )
        connection.execute("PRAGMA user_version=4")
        connection.commit()
        connection.execute("PRAGMA foreign_keys=ON")
        connection.close()

    def test_database_is_a_private_current_owner_regular_file(self) -> None:
        info = self.paths.catalog_path.lstat()
        self.assertTrue(stat.S_ISREG(info.st_mode))
        if os.name == "posix":
            self.assertEqual(info.st_uid, os.geteuid())
            self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)

    def test_manifest_and_independent_states_survive_reopen(self) -> None:
        self.catalog.register_manifest(self.manifest)
        self.catalog.set_provider_state(
            self.manifest.source_id,
            self.manifest.source_version,
            submission_state="submitted",
            completion_state="completed",
            recall_visibility="unknown",
        )
        self.catalog.set_projection_state(
            self.manifest.source_id,
            self.manifest.source_version,
            state="pending",
            projection_path="Journal/Inbox/item.md",
            desired_hash="a" * 64,
            last_projected_hash=None,
        )

        self.catalog.close()
        self.catalog = LifeContextCatalog(self.paths)

        recorded = self.catalog.get_manifest(
            self.manifest.source_id, self.manifest.source_version
        )
        provider = self.catalog.get_provider_state(
            self.manifest.source_id, self.manifest.source_version
        )
        projection = self.catalog.get_projection_state(
            self.manifest.source_id, self.manifest.source_version
        )
        self.assertEqual(recorded["sha256"], self.manifest.sha256)
        self.assertEqual(recorded["size_bytes"], len(self.original))
        self.assertEqual(recorded["manifest_path"], "sources/src_catalog_1/v1/manifest.json")
        self.assertEqual(provider["submission_state"], "submitted")
        self.assertEqual(provider["completion_state"], "completed")
        self.assertEqual(provider["recall_visibility"], "unknown")
        self.assertEqual(projection["state"], "pending")
        self.assertEqual(projection["desired_hash"], "a" * 64)
        self.assertIsNone(projection["last_projected_hash"])

    def test_identical_manifest_replay_converges_but_changed_identity_conflicts(self) -> None:
        self.catalog.register_manifest(self.manifest)
        self.catalog.register_manifest(self.manifest)
        changed = SourceManifest(
            source_id=self.manifest.source_id,
            source_version=self.manifest.source_version,
            sha256=hashlib.sha256(b"changed").hexdigest(),
            size_bytes=len(b"changed"),
            provenance={"source": "different"},
        )

        with self.assertRaises(CatalogConflict):
            self.catalog.register_manifest(changed)

        self.assertEqual(
            self.catalog.get_manifest(self.manifest.source_id, 1)["sha256"],
            self.manifest.sha256,
        )

    def test_transaction_exception_rolls_back_all_changes(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "abort transaction"):
            with self.catalog.transaction() as connection:
                connection.execute(
                    "INSERT INTO sources(source_id, lifecycle_state) VALUES (?, ?)",
                    ("src_rolled_back", "active"),
                )
                raise RuntimeError("abort transaction")

        self.assertIsNone(self.catalog.get_source("src_rolled_back"))

    def test_reconciliation_records_local_facts_without_changing_provider_state(self) -> None:
        self.catalog.register_manifest(self.manifest)
        self.catalog.set_provider_state(
            self.manifest.source_id,
            1,
            submission_state="submitted",
            completion_state="completed",
            recall_visibility="visible",
        )
        self.catalog.record_reconciliation_fact(
            self.manifest.source_id, 1, "missing_original", expected_hash=self.manifest.sha256
        )
        self.catalog.record_reconciliation_fact(
            self.manifest.source_id,
            1,
            "stale_projection",
            expected_hash="a" * 64,
            actual_hash="b" * 64,
        )

        facts = self.catalog.list_reconciliation_facts(self.manifest.source_id, 1)
        provider = self.catalog.get_provider_state(self.manifest.source_id, 1)
        self.assertEqual({fact["fact"] for fact in facts}, {"missing_original", "stale_projection"})
        stale = next(fact for fact in facts if fact["fact"] == "stale_projection")
        self.assertEqual(stale["expected_hash"], "a" * 64)
        self.assertEqual(stale["actual_hash"], "b" * 64)
        self.assertEqual(provider["submission_state"], "submitted")
        self.assertEqual(provider["completion_state"], "completed")
        self.assertEqual(provider["recall_visibility"], "visible")

    def test_symlinked_catalog_is_rejected_before_sqlite_connect(self) -> None:
        self.catalog.close()
        (self.paths.catalog_path.parent / "catalog.sqlite3").unlink()
        external = self.home / "external.sqlite3"
        external.write_bytes(b"not opened")
        self.paths.catalog_path.symlink_to(external)

        with patch("lifecontext.catalog.sqlite3.connect") as connect:
            with self.assertRaises(CatalogError):
                LifeContextCatalog(self.paths)
        connect.assert_not_called()
        self.assertEqual(external.read_bytes(), b"not opened")

    def test_unsafe_mode_is_rejected_before_sqlite_connect(self) -> None:
        self.catalog.close()
        if os.name != "posix":
            self.skipTest("file permission modes are POSIX-specific")
        self.paths.catalog_path.chmod(0o644)

        with patch("lifecontext.catalog.sqlite3.connect") as connect:
            with self.assertRaises(CatalogError):
                LifeContextCatalog(self.paths)
        connect.assert_not_called()

    def test_unknown_schema_version_is_rejected(self) -> None:
        self.catalog.close()
        connection = sqlite3.connect(self.paths.catalog_path)
        connection.execute("PRAGMA user_version=999")
        connection.close()
        if os.name == "posix":
            self.paths.catalog_path.chmod(0o600)

        with self.assertRaises(CatalogError):
            LifeContextCatalog(self.paths)

    def test_malformed_known_schema_version_is_rejected(self) -> None:
        self.catalog.close()
        connection = sqlite3.connect(self.paths.catalog_path)
        connection.execute("DROP TABLE sources")
        connection.execute("CREATE TABLE sources (unexpected TEXT)")
        connection.execute("PRAGMA user_version=1")
        connection.commit()
        connection.close()
        if os.name == "posix":
            self.paths.catalog_path.chmod(0o600)

        with self.assertRaises(CatalogError):
            LifeContextCatalog(self.paths)

    def test_version_one_migration_preserves_existing_catalog_rows(self) -> None:
        self.catalog.register_manifest(self.manifest)
        self.catalog.set_provider_state(
            self.manifest.source_id,
            1,
            submission_state="submitted",
            completion_state="completed",
            recall_visibility="visible",
        )
        self.catalog.set_projection_state(
            self.manifest.source_id,
            1,
            state="projected",
            projection_path="Journal/Inbox/catalog.md",
            desired_hash="a" * 64,
            last_projected_hash="a" * 64,
        )
        self.catalog.record_reconciliation_fact(
            self.manifest.source_id, 1, "stale_projection", expected_hash="a" * 64,
            actual_hash="b" * 64,
        )
        self.catalog.close()
        self._downgrade_catalog_to_version_one()

        connection = sqlite3.connect(self.paths.catalog_path)
        before = {
            table: connection.execute(f"SELECT * FROM {table}").fetchall()
            for table in (
                "sources", "source_versions", "provider_states",
                "projection_states", "reconciliation_facts",
            )
        }
        connection.execute("PRAGMA user_version=1")
        connection.commit()
        connection.close()

        self.catalog = LifeContextCatalog(self.paths)
        connection = sqlite3.connect(self.paths.catalog_path)
        self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 9)
        after = {
            table: connection.execute(
                "SELECT source_id,lifecycle_state,created_at FROM sources"
                if table == "sources" else f"SELECT * FROM {table}"
            ).fetchall()
            for table in before
        }
        connection.close()
        self.assertEqual(after, before)
        self.assertEqual(
            self.catalog.get_manifest(self.manifest.source_id, 1)["sha256"],
            self.manifest.sha256,
        )

    def test_version_two_migration_preserves_operation_rows_and_expands_result_limit(self) -> None:
        result = {
            "sources": [
                {"source_id": self.manifest.source_id, "source_version": 1, "sha256": "c" * 64}
            ],
            "item_count": len(self.original),
        }
        operation = self.catalog.begin_operation("legacy-operation", "d" * 64, "retain")
        history_operation = self.catalog.begin_operation("legacy-history", "f" * 64, "curation")
        tombstone_operation = self.catalog.begin_operation("legacy-tombstone", "a" * 64, "curation")
        self.catalog.transition_operation(operation["operation_id"], "pending", "running")
        self.catalog.transition_operation(
            operation["operation_id"], "running", "completed", result=result
        )
        self.catalog.transition_operation(history_operation["operation_id"], "pending", "running")
        self.catalog.transition_operation(history_operation["operation_id"], "running", "completed")
        self.catalog.transition_operation(tombstone_operation["operation_id"], "pending", "running")
        self.catalog.transition_operation(tombstone_operation["operation_id"], "running", "completed")
        self.catalog.register_manifest(self.manifest)
        with self.catalog.transaction() as connection:
            connection.execute(
                "INSERT INTO curation_history(operation_id, source_id, source_version, action) "
                "VALUES (?, ?, ?, ?)",
                (history_operation["operation_id"], self.manifest.source_id, 1, "correct"),
            )
            connection.execute(
                "INSERT INTO tombstones(source_id, source_version, tombstone_type, operation_id) "
                "VALUES (?, ?, ?, ?)",
                (self.manifest.source_id, 1, "forgotten", tombstone_operation["operation_id"]),
            )
        self.catalog.close()
        self._downgrade_catalog_to_version_two()
        if os.name == "posix":
            self.paths.catalog_path.chmod(0o600)

        self.catalog = LifeContextCatalog(self.paths)

        connection = sqlite3.connect(self.paths.catalog_path)
        self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 9)
        sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='operations'"
        ).fetchone()[0]
        self.assertIn("4194304", sql)
        connection.close()
        reopened = self.catalog.get_operation(operation["operation_id"])
        self.assertEqual(reopened["state"], "completed")
        self.assertEqual(reopened["result"], result)
        with sqlite3.connect(self.paths.catalog_path) as connection:
            history = connection.execute("SELECT * FROM curation_history").fetchall()
            tombstones = connection.execute("SELECT * FROM tombstones").fetchall()
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(
            history[0][1:5],
            (history_operation["operation_id"], self.manifest.source_id, 1, "correct"),
        )
        self.assertEqual(
            tombstones[0][:4],
            (self.manifest.source_id, 1, "forgotten", tombstone_operation["operation_id"]),
        )
        self.assertEqual(
            self.catalog.get_manifest(self.manifest.source_id, 1)["sha256"],
            self.manifest.sha256,
        )

    def test_version_three_migration_adds_prepared_binding_and_tombstone_progress(self) -> None:
        operation = self.catalog.begin_operation("v3-migration-key", "9" * 64, "curation")
        self.catalog.transition_operation(operation["operation_id"], "pending", "running")
        self.catalog.transition_operation(operation["operation_id"], "running", "completed")
        self.catalog.register_manifest(self.manifest)
        with self.catalog.transaction() as connection:
            connection.execute(
                "INSERT INTO curation_history(operation_id, source_id, source_version, action) "
                "VALUES (?, ?, ?, ?)",
                (operation["operation_id"], self.manifest.source_id, 1, "prepare_delete"),
            )
            connection.execute(
                "INSERT INTO tombstones(source_id, source_version, tombstone_type, operation_id) "
                "VALUES (?, ?, ?, ?)",
                (self.manifest.source_id, 1, "deleted", operation["operation_id"]),
            )
        self.catalog.close()

        self.catalog = LifeContextCatalog(self.paths)

        with sqlite3.connect(self.paths.catalog_path) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 9)
            history_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(curation_history)")
            }
            tombstone_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(tombstones)")
            }
            self.assertIn("prepared_operation_id", history_columns)
            self.assertIn("progress_json", tombstone_columns)
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(self.catalog.get_curation_history(operation["operation_id"])["action"], "prepare_delete")
        self.assertEqual(self.catalog.get_tombstone(self.manifest.source_id, 1)["progress"], {
            "provider_delete": "pending",
            "projection_delete": "pending",
            "custody_delete": "pending",
        })

    def test_version_four_migration_adds_transfer_tables_and_import_kind(self) -> None:
        prior = self.catalog.begin_operation("v4-migration", "8" * 64, "retain")
        self.catalog.transition_operation(prior["operation_id"], "pending", "running")
        self.catalog.transition_operation(prior["operation_id"], "running", "completed")
        self.catalog.close()
        self._downgrade_catalog_to_version_four()
        if os.name == "posix":
            self.paths.catalog_path.chmod(0o600)

        self.catalog = LifeContextCatalog(self.paths)
        self.assertEqual(self.catalog.get_operation(prior["operation_id"])["state"], "completed")
        imported = self.catalog.begin_operation("v5-import-kind", "7" * 64, "import")
        self.assertEqual(imported["operation_kind"], "import")
        with sqlite3.connect(self.paths.catalog_path) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 9)
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_version_six_migration_preserves_projection_receipts_without_importing_proofs(self):
        self.catalog.register_manifest(self.manifest)
        receipt = self.catalog.set_artifact_projection("document", "doc_attachment", 1,
            projection_path="documents/inbox/attachments/doc.bin", desired_hash=self.manifest.sha256,
            state="projected", last_projected_hash=self.manifest.sha256)
        self.catalog.close()
        with sqlite3.connect(self.paths.catalog_path) as connection:
            downgrade_to_version_eight(connection)
            connection.execute("DROP TABLE journal_projection_moves")
            connection.execute("DROP TABLE artifact_publications")
            connection.execute("PRAGMA user_version=6")
        self.catalog = LifeContextCatalog(self.paths)
        self.assertEqual(self.catalog.get_artifact_projection("document", "doc_attachment", 1), receipt)
        self.assertIsNone(self.catalog.get_artifact_publication("doc_attachment", 1))
        self.assertEqual(self.catalog.get_manifest(self.manifest.source_id, 1)["sha256"], self.manifest.sha256)
        with sqlite3.connect(self.paths.catalog_path) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 9)
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_version_eight_migration_admits_review_operations_and_keeps_rows(self):
        self.catalog.register_manifest(self.manifest)
        prior = self.catalog.begin_operation("v8-operation", "6" * 64, "wiki_rebuild")
        self.catalog.close()
        with sqlite3.connect(self.paths.catalog_path) as connection:
            downgrade_to_version_eight(connection)
            self.assertEqual(connection.execute("SELECT operation_kind FROM operations").fetchall(), [("wiki_rebuild",)])
        self.catalog = LifeContextCatalog(self.paths)
        self.assertEqual(self.catalog.get_operation(prior["operation_id"])["operation_kind"], "wiki_rebuild")
        self.assertEqual(self.catalog.begin_operation("v9-review", "5" * 64, "wiki_review")["operation_kind"], "wiki_review")
        self.assertEqual(self.catalog.get_manifest(self.manifest.source_id, 1)["sha256"], self.manifest.sha256)
        with sqlite3.connect(self.paths.catalog_path) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 9)
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertIn("'wiki_review'", connection.execute("SELECT sql FROM sqlite_master WHERE name='operations'").fetchone()[0])
            self.assertIsNotNone(connection.execute("SELECT name FROM sqlite_master WHERE name='wiki_review_resolutions'").fetchone())

    def test_a_held_write_lock_is_reported_as_busy(self):
        blocker = sqlite3.connect(self.paths.catalog_path, isolation_level=None)
        self.addCleanup(blocker.close)
        blocker.execute("BEGIN IMMEDIATE")
        busy = LifeContextCatalog(self.paths, busy_timeout_ms=1)
        self.addCleanup(busy.close)
        with self.assertRaises(CatalogBusy):
            busy.create_wiki_page("topic_busy", title="Busy")
        blocker.execute("ROLLBACK")
        self.assertEqual(busy.create_wiki_page("topic_busy", title="Busy")["page_id"], "topic_busy")

    def test_checked_in_version_eight_catalog_migrates_with_its_rows(self):
        self.catalog.close()
        self.paths.catalog_path.unlink()
        fixture = Path(__file__).resolve().parent / "fixtures" / "catalog_v8.sql"
        with sqlite3.connect(self.paths.catalog_path) as connection:
            connection.executescript(fixture.read_text(encoding="utf-8"))
        self.paths.catalog_path.chmod(0o600)
        self.catalog = LifeContextCatalog(self.paths)
        with sqlite3.connect(self.paths.catalog_path) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 9)
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            kinds = sorted(row[0] for row in connection.execute("SELECT operation_kind FROM operations"))
        self.assertEqual(kinds, ["retain", "wiki_rebuild"])
        self.assertEqual(self.catalog.get_document_metadata("doc_v8", 1)["title"], "Version eight")
        self.assertEqual(self.catalog.get_wiki_page("source_doc_v8")["state"], "conflict")
        self.catalog.resolve_wiki_review("review_v8", note="Undid the hand edit.")
        self.assertEqual(self.catalog.get_wiki_page("source_doc_v8")["state"], "stale")
        self.assertEqual(self.catalog.begin_operation("v9-review", "3" * 64, "wiki_review")["operation_kind"], "wiki_review")

    def test_failed_version_eight_migration_rolls_back(self):
        prior = self.catalog.begin_operation("v8-rollback", "4" * 64, "retain")
        self.catalog.close()
        with sqlite3.connect(self.paths.catalog_path) as connection:
            downgrade_to_version_eight(connection)
        with patch("lifecontext.catalog._SCHEMA_V9", ("CREATE TABLE migration_probe(value TEXT)", "INVALID SQL")), self.assertRaises(CatalogError):
            LifeContextCatalog(self.paths)
        with sqlite3.connect(self.paths.catalog_path) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 8)
            self.assertIsNone(connection.execute("SELECT name FROM sqlite_master WHERE name='migration_probe'").fetchone())
            self.assertNotIn("wiki_review", connection.execute("SELECT sql FROM sqlite_master WHERE name='operations'").fetchone()[0])
            self.assertEqual(connection.execute("SELECT state FROM operations WHERE operation_id=?", (prior["operation_id"],)).fetchone()[0], "pending")
        self.catalog = LifeContextCatalog(self.paths)
        self.assertEqual(self.catalog.get_operation(prior["operation_id"])["state"], "pending")

    def test_version_nine_rejects_a_weakened_review_resolution_table(self):
        self.catalog.close()
        with sqlite3.connect(self.paths.catalog_path) as connection:
            connection.execute("DROP TABLE wiki_review_resolutions")
            connection.execute("CREATE TABLE wiki_review_resolutions (review_id TEXT PRIMARY KEY, note TEXT, resolved_at TEXT)")
        with self.assertRaises(CatalogError):
            LifeContextCatalog(self.paths)

    def test_failed_version_six_migration_rolls_back(self):
        self.catalog.register_manifest(self.manifest)
        self.catalog.close()
        with sqlite3.connect(self.paths.catalog_path) as connection:
            downgrade_to_version_eight(connection)
            connection.execute("DROP TABLE journal_projection_moves")
            connection.execute("DROP TABLE artifact_publications")
            connection.execute("PRAGMA user_version=6")
        with patch("lifecontext.catalog._SCHEMA_V7", ("CREATE TABLE migration_probe(value TEXT)", "INVALID SQL")), self.assertRaises(CatalogError):
            LifeContextCatalog(self.paths)
        with sqlite3.connect(self.paths.catalog_path) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 6)
            self.assertIsNone(connection.execute("SELECT name FROM sqlite_master WHERE name='migration_probe'").fetchone())
            self.assertEqual(connection.execute("SELECT sha256 FROM source_versions").fetchone()[0], self.manifest.sha256)

    def test_publication_constraints_reject_weakened_schema_and_invalid_identity(self):
        from lifecontext.catalog import _SCHEMA_V7
        arguments = {"projection_path": "documents/inbox/doc.md", "desired_hash": "a" * 64,
                     "device": 1, "inode": 2, "staged_name": ".publication." + "b" * 20 + ".tmp.hold"}
        for invalid in ({"device": -1}, {"inode": True}, {"staged_name": "../escape"}, {"state": "trusted"},
                        {"device": None, "inode": None}, {"device": None}, {"state": "planned"}):
            with self.subTest(invalid=invalid), self.assertRaises(CatalogError):
                self.catalog.set_artifact_publication("doc_note", 1, **{**arguments, **invalid})
        self.catalog.set_artifact_publication("doc_note", 1, **arguments)
        self.catalog.close()
        with sqlite3.connect(self.paths.catalog_path) as connection:
            connection.execute("DROP TABLE artifact_publications")
            connection.execute(_SCHEMA_V7[0].replace("device IS NULL OR device >= 0", "device IS NULL OR device >= -1"))
        with self.assertRaises(CatalogError):
            LifeContextCatalog(self.paths)

    def test_failed_version_two_migration_rolls_back_schema_version_and_operation_rows(self) -> None:
        operation = self.catalog.begin_operation("rollback-v2-key", "e" * 64, "retain")
        self.catalog.close()
        self._downgrade_catalog_to_version_two()
        if os.name == "posix":
            self.paths.catalog_path.chmod(0o600)

        with patch(
            "lifecontext.catalog._SCHEMA_V3",
            ("CREATE TABLE migration_probe(value TEXT)", "INVALID MIGRATION SQL"),
        ), self.assertRaises(CatalogError):
            LifeContextCatalog(self.paths)

        connection = sqlite3.connect(self.paths.catalog_path)
        self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        self.assertNotIn("migration_probe", tables)
        row = connection.execute(
            "SELECT state FROM operations WHERE operation_id=?", (operation["operation_id"],)
        ).fetchone()
        connection.close()
        self.assertEqual(row[0], "pending")

    def test_failed_version_one_migration_rolls_back_schema_and_version(self) -> None:
        self.catalog.register_manifest(self.manifest)
        self.catalog.close()
        self._downgrade_catalog_to_version_one()

        with patch(
            "lifecontext.catalog._SCHEMA_V2",
            ("CREATE TABLE migration_probe(value TEXT)", "INVALID MIGRATION SQL"),
        ), self.assertRaises(CatalogError):
            LifeContextCatalog(self.paths)

        connection = sqlite3.connect(self.paths.catalog_path)
        self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        self.assertNotIn("migration_probe", tables)
        self.assertNotIn("operations", tables)
        self.assertEqual(
            connection.execute("SELECT sha256 FROM source_versions").fetchone()[0],
            self.manifest.sha256,
        )
        connection.close()

    def test_version_two_reopen_rejects_missing_operation_state_check(self) -> None:
        self.catalog.close()
        self.catalog = LifeContextCatalog(self.paths)
        self.catalog.close()
        connection = sqlite3.connect(self.paths.catalog_path)
        connection.execute("DROP TABLE operations")
        connection.execute(
            """CREATE TABLE operations (
                operation_id TEXT PRIMARY KEY,
                idempotency_key_digest TEXT,
                request_digest TEXT,
                operation_kind TEXT,
                state TEXT,
                result_json TEXT,
                error_code TEXT,
                created_at TEXT,
                updated_at TEXT
            )"""
        )
        connection.commit()
        connection.close()
        if os.name == "posix":
            self.paths.catalog_path.chmod(0o600)

        with self.assertRaises(CatalogError):
            LifeContextCatalog(self.paths)

    def test_invalid_provider_state_types_are_rejected(self) -> None:
        self.catalog.register_manifest(self.manifest)
        for field, value in (
            ("submission_state", []),
            ("completion_state", {}),
            ("recall_visibility", []),
        ):
            with self.subTest(field=field):
                with self.assertRaises(CatalogError):
                    self.catalog.set_provider_state(
                        self.manifest.source_id,
                        1,
                        **{field: value},
                    )

    def test_operation_admission_replays_same_request_and_conflicts_on_changed_request(self) -> None:
        first = self.catalog.begin_operation("client-key-1", "a" * 64, "retain")
        replay = self.catalog.begin_operation("client-key-1", "a" * 64, "retain")
        self.assertEqual(replay["operation_id"], first["operation_id"])
        self.assertEqual(replay["state"], "pending")

        with self.assertRaises(CatalogConflict):
            self.catalog.begin_operation("client-key-1", "b" * 64, "retain")
        with self.assertRaises(CatalogConflict):
            self.catalog.begin_operation("client-key-1", "a" * 64, "curation")

    def test_operation_key_digest_and_content_free_result_survive_reopen(self) -> None:
        key = "raw-client-key"
        result = {
            "sources": [
                {"source_id": "src_catalog_1", "source_version": 1, "sha256": "c" * 64}
            ],
            "item_count": 1,
        }
        operation = self.catalog.begin_operation(key, "d" * 64, "retain")
        self.catalog.transition_operation(
            operation["operation_id"], "pending", "running"
        )
        self.catalog.transition_operation(
            operation["operation_id"], "running", "completed", result=result
        )

        self.catalog.close()
        self.catalog = LifeContextCatalog(self.paths)
        reopened = self.catalog.get_operation(operation["operation_id"])
        replay = self.catalog.begin_operation(key, "d" * 64, "retain")
        self.assertEqual(reopened["state"], "completed")
        self.assertEqual(reopened["result"], result)
        self.assertEqual(replay["operation_id"], operation["operation_id"])
        connection = sqlite3.connect(self.paths.catalog_path)
        row = connection.execute(
            "SELECT idempotency_key_digest, request_digest, result_json FROM operations"
        ).fetchone()
        connection.close()
        self.assertEqual(row[0], hashlib.sha256(key.encode("ascii")).hexdigest())
        self.assertEqual(row[1], "d" * 64)
        self.assertNotIn(key, repr(row))
        self.assertNotIn("private source content", repr(row))

    def test_operation_transitions_are_checked_and_recoverable_list_is_bounded(self) -> None:
        pending = self.catalog.begin_operation("key-pending", "1" * 64, "retain")
        running = self.catalog.begin_operation("key-running", "2" * 64, "retain")
        retryable = self.catalog.begin_operation("key-retry", "3" * 64, "retain")
        complete = self.catalog.begin_operation("key-complete", "4" * 64, "retain")
        self.catalog.transition_operation(running["operation_id"], "pending", "running")
        self.catalog.transition_operation(
            retryable["operation_id"], "pending", "running"
        )
        self.catalog.transition_operation(
            retryable["operation_id"], "running", "retryable_failed",
            error_code="provider_unavailable",
        )
        self.catalog.transition_operation(complete["operation_id"], "pending", "running")
        self.catalog.transition_operation(complete["operation_id"], "running", "completed")

        with self.assertRaises(CatalogError):
            self.catalog.transition_operation(pending["operation_id"], "pending", "completed")
        with self.assertRaises(CatalogConflict):
            self.catalog.transition_operation(running["operation_id"], "pending", "running")
        with self.assertRaises(CatalogError):
            self.catalog.transition_operation(running["operation_id"], "running", "unknown")

        recoverable = self.catalog.list_recoverable_operations(limit=2)
        self.assertEqual(len(recoverable), 2)
        self.assertNotIn(complete["operation_id"], {item["operation_id"] for item in recoverable})
        all_recoverable = self.catalog.list_recoverable_operations(limit=10)
        self.assertEqual(
            {item["state"] for item in all_recoverable},
            {"pending", "running", "retryable_failed"},
        )
        with self.assertRaises(CatalogError):
            self.catalog.list_recoverable_operations(limit=0)

    def test_operation_result_rejects_paths_text_and_unrecognized_shapes(self) -> None:
        operation = self.catalog.begin_operation("safe-result-key", "e" * 64, "retain")
        self.catalog.transition_operation(operation["operation_id"], "pending", "running")
        for result in (
            {"content": "source text"},
            {"path": "/private/source"},
            {"sources": [{"source_id": "bad id", "source_version": 1, "sha256": "f" * 64}]},
            {"item_count": True},
            {"provider_payload": {"text": "source text"}},
        ):
            with self.subTest(result=result), self.assertRaises(CatalogError):
                self.catalog.transition_operation(
                    operation["operation_id"], "running", "completed", result=result
                )

    def _relationship_edge(self, **changes):
        edge = {
            "edge_id": "edge_" + "a" * 64,
            "revision": 1,
            "from": "journal:src_catalog_1",
            "to": "note:plan-note",
            "relation": "references",
            "status": "proposed",
            "source_revision": "1:" + "b" * 64,
            "target_revision": "c" * 64,
            "provenance": "wikilink",
            "evidence_id": "sha256:" + "d" * 64,
            "decision_principal": None,
        }
        edge.update(changes)
        return edge

    def _relationship_scan_result(self, **changes):
        scan_edge = self._relationship_edge()
        scan_edge.pop("decision_principal")
        scan = {
            "edges": [scan_edge],
            "review": [
                {
                    "code": "missing_target",
                    "source": "path:Notes/資料/plan.md",
                    "hint_sha256": "e" * 64,
                }
            ],
            "scanned_files": 3,
            "truncated": False,
        }
        scan.update(changes)
        return {"relationship_scan": scan}

    def _start_result_operation(self, key: str, kind: str = "relationship_scan") -> str:
        operation = self.catalog.begin_operation(key, hashlib.sha256(key.encode()).hexdigest(), kind)
        self.catalog.transition_operation(operation["operation_id"], "pending", "running")
        return operation["operation_id"]

    def test_relationship_scan_snapshot_is_bounded_strict_and_survives_reopen(self) -> None:
        key = "relationship-scan-snapshot"
        operation_id = self._start_result_operation(key)
        result = self._relationship_scan_result()

        self.catalog.transition_operation(
            operation_id, "running", "completed", result=result
        )
        self.catalog.close()
        self.catalog = LifeContextCatalog(self.paths)
        reopened = self.catalog.get_operation(operation_id)
        replay = self.catalog.begin_operation(
            key, hashlib.sha256(key.encode()).hexdigest(), "relationship_scan"
        )

        self.assertEqual(reopened["result"], result)
        self.assertEqual(replay["result"], result)
        self.assertEqual(reopened["state"], "completed")

    def test_relationship_decision_snapshot_persists_one_exact_edge(self) -> None:
        operation_id = self._start_result_operation(
            "relationship-decision-snapshot", "relationship_decision"
        )
        edge = self._relationship_edge(status="approved", revision=2, decision_principal="operator")
        result = {"relationship_decision": edge}

        completed = self.catalog.transition_operation(
            operation_id, "running", "completed", result=result
        )

        self.assertEqual(completed["result"], result)

    def test_relationship_snapshot_validator_rejects_invalid_shapes_and_bounds(self) -> None:
        valid = self._relationship_scan_result()
        cases = []

        def scan_case(mutator):
            value = self._relationship_scan_result()
            mutator(value["relationship_scan"])
            cases.append(value)

        scan_case(lambda scan: scan.update(extra="forbidden"))
        scan_case(lambda scan: scan.update(scanned_files=True))
        scan_case(lambda scan: scan.update(scanned_files=20_001))
        scan_case(lambda scan: scan.update(truncated=1))
        scan_case(lambda scan: scan.update(edges=[self._relationship_edge()] * 101))
        scan_case(lambda scan: scan.update(review=[{"code": "missing_target"}] * 101))
        scan_case(lambda scan: scan["edges"][0].update(extra="forbidden"))
        scan_case(lambda scan: scan["edges"][0].update(edge_id="edge_bad"))
        scan_case(lambda scan: scan["edges"][0].update(revision=True))
        scan_case(lambda scan: scan["edges"][0].update(relation="unknown"))
        scan_case(lambda scan: scan["edges"][0].update(status="unknown"))
        scan_case(lambda scan: scan["edges"][0].update(provenance="source_text"))
        scan_case(lambda scan: scan["edges"][0].update(evidence_id="sha256:bad"))
        scan_case(lambda scan: scan["edges"][0].update(source_revision="latest"))
        scan_case(lambda scan: scan["edges"][0].update(**{"from": "/absolute/note"}))
        scan_case(lambda scan: scan["edges"][0].update(to="path:Notes//plan.md"))
        scan_case(lambda scan: scan["edges"][0].update(to="path:Notes/../plan.md"))
        scan_case(lambda scan: scan["edges"][0].update(to="journal:"))
        scan_case(lambda scan: scan["review"][0].update(extra="forbidden"))
        scan_case(lambda scan: scan["review"][0].update(code="free_form_diagnostic"))
        scan_case(lambda scan: scan["review"][0].update(source="journal:bad suffix"))
        scan_case(lambda scan: scan["review"][0].update(source="note:bad/suffix"))
        scan_case(lambda scan: scan["review"][0].update(source="path:/absolute/note.md"))
        scan_case(lambda scan: scan["review"][0].update(source="path:Notes/./plan.md"))
        scan_case(lambda scan: scan["review"][0].update(hint_sha256="not-a-hash"))
        scan_case(lambda scan: scan["review"][0].update(edge_id="edge_bad"))
        scan_case(lambda scan: scan["review"][0].update(source="path:" + "a" * 1024))
        scan_case(lambda scan: scan["review"][0].update(source="path:" + "界" * 1400))
        scan_case(lambda scan: scan["edges"][0].update(to="path:" + "界" * 400))
        cases.extend(
            [
                {"relationship_scan": valid["relationship_scan"], "relationship_decision": self._relationship_edge()},
                {"relationship_scan": {**valid["relationship_scan"], "review": [{"code": "missing_target", "text": "source sentinel"}]}},
                {"relationship_scan": {**valid["relationship_scan"], "content": "source sentinel"}},
            ]
        )

        for index, result in enumerate(cases):
            operation_id = self._start_result_operation(f"invalid-snapshot-{index}")
            with self.subTest(index=index, result=result), self.assertRaises(CatalogError):
                self.catalog.transition_operation(
                    operation_id, "running", "completed", result=result
                )

    def test_relationship_snapshot_serialized_result_limit_is_enforced(self) -> None:
        operation_id = self._start_result_operation("serialized-snapshot-limit")
        with patch("lifecontext.catalog._MAX_OPERATION_RESULT_BYTES", 256):
            with self.assertRaises(CatalogError):
                self.catalog.transition_operation(
                    operation_id,
                    "running",
                    "completed",
                    result=self._relationship_scan_result(),
                )

    def test_relationship_snapshot_size_limit_counts_utf8_bytes(self) -> None:
        result = self._relationship_scan_result()
        serialized = json.dumps(
            result, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        self.assertGreater(len(serialized.encode("utf-8")), len(serialized))
        operation_id = self._start_result_operation("utf8-snapshot-boundary")
        with patch("lifecontext.catalog._MAX_OPERATION_RESULT_BYTES", len(serialized)):
            with self.assertRaises(CatalogError):
                self.catalog.transition_operation(
                    operation_id, "running", "completed", result=result
                )

    def test_concurrent_operation_admission_converges_to_one_row(self) -> None:
        key = "concurrent-key"

        def admit() -> str:
            catalog = LifeContextCatalog(self.paths)
            try:
                return catalog.begin_operation(key, "9" * 64, "relationship_scan")["operation_id"]
            finally:
                catalog.close()

        with ThreadPoolExecutor(max_workers=8) as pool:
            operation_ids = list(pool.map(lambda _index: admit(), range(8)))

        self.assertEqual(len(set(operation_ids)), 1)
        connection = sqlite3.connect(self.paths.catalog_path)
        count = connection.execute("SELECT COUNT(*) FROM operations").fetchone()[0]
        connection.close()
        self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
