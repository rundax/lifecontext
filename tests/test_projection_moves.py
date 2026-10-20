"""Durable journal projection move contracts."""

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from catalog_downgrade import downgrade_to_version_eight
from local_paths import scratch_root

from lifecontext.catalog import CatalogConflict, CatalogError, LifeContextCatalog
from lifecontext.custody import SourceManifest
from lifecontext.paths import LifeContextPaths


class ProjectionMoveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=scratch_root())
        self.addCleanup(self.temporary.cleanup)
        self.home_patch = patch.dict(os.environ, {"HOME": str(Path(self.temporary.name))})
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)
        self.paths = LifeContextPaths.current()
        self.catalog = LifeContextCatalog(self.paths)
        self.addCleanup(lambda: self.catalog.close())
        self.catalog.register_manifest(SourceManifest(
            source_id="src_move", source_version=1, sha256="a" * 64,
            size_bytes=1, provenance={"source": "test"},
        ))
        self.move = {
            "old_path": "entries/old.md", "new_path": "entries/new.md",
            "old_hash": "a" * 64, "new_hash": "b" * 64,
        }
        self.catalog.set_projection_state(
            "src_move", 1, state="projected", projection_path=self.move["old_path"],
            desired_hash=self.move["old_hash"], last_projected_hash=self.move["old_hash"],
        )

    def test_durable_idempotent_move_and_finish(self):
        self.assertIsNone(self.catalog.get_projection_move("src_move", 1))
        self.catalog.begin_projection_move("src_move", 1, **self.move)
        self.catalog.begin_projection_move("src_move", 1, **self.move)
        self.catalog.close()
        self.catalog = LifeContextCatalog(self.paths)
        expected = {"source_id": "src_move", "source_version": 1, **self.move}
        self.assertEqual(self.catalog.get_projection_move("src_move", 1), expected)
        self.assertEqual(self.catalog.list_projection_moves(), [expected])
        self.assertEqual(self.catalog.list_projection_paths(), ["entries/new.md", "entries/old.md"])
        state = self.catalog.get_projection_state("src_move", 1)
        self.assertEqual(state["state"], "pending")
        self.assertEqual(state["projection_path"], self.move["old_path"])
        self.assertEqual(state["desired_hash"], self.move["old_hash"])
        self.assertEqual(state["last_projected_hash"], self.move["old_hash"])
        self.assertEqual(len(self.catalog.list_recoverable_projection_states()), 1)
        self.catalog.set_projection_state(
            "src_move", 1, state="projected", projection_path=self.move["new_path"],
            desired_hash=self.move["new_hash"], last_projected_hash=self.move["new_hash"],
        )
        state = self.catalog.get_projection_state("src_move", 1)
        self.catalog.finish_projection_move("src_move", 1)
        self.catalog.finish_projection_move("src_move", 1)
        self.assertEqual(self.catalog.get_projection_state("src_move", 1), state)
        self.assertEqual(self.catalog.list_projection_moves(), [])
        self.assertEqual(self.catalog.list_projection_paths(), ["entries/new.md"])

    def test_outstanding_moves_recover_regardless_of_projection_state(self):
        self.catalog.register_manifest(SourceManifest(
            source_id="src_other", source_version=1, sha256="a" * 64,
            size_bytes=1, provenance={"source": "test"},
        ))
        self.catalog.register_manifest(SourceManifest(
            source_id="src_move", source_version=2, sha256="a" * 64,
            size_bytes=1, provenance={"source": "test"},
        ))
        self.catalog.begin_projection_move("src_move", 1, **self.move)
        for state in ("conflict", "failed", "projected", "not_projected", "pending"):
            with self.subTest(state=state):
                for source_id, version in (("src_move", 1), ("src_move", 2), ("src_other", 1)):
                    self.catalog.set_projection_state(source_id, version, state=state)
                self.catalog.close()
                self.catalog = LifeContextCatalog(self.paths)
                rows = self.catalog.list_recoverable_projection_states()
                identities = {(row["source_id"], row["source_version"]) for row in rows}
                expected = {("src_move", 1)}
                if state == "pending":
                    expected |= {("src_move", 2), ("src_other", 1)}
                self.assertEqual(identities, expected)
                self.assertEqual(len(rows), len(expected))
                self.assertEqual(len(self.catalog.list_recoverable_projection_states(limit=1)), 1)
        self.catalog.set_projection_state("src_move", 1, state="conflict")
        self.catalog.set_projection_state("src_move", 2, state="failed")
        self.catalog.set_projection_state("src_other", 1, state="conflict")
        self.catalog.finish_projection_move("src_move", 1)
        self.assertEqual(self.catalog.list_recoverable_projection_states(), [])

    def test_new_move_requires_matching_old_ownership(self):
        before = self.catalog.get_projection_state("src_move", 1)
        for field, replacement in (
            ("old_path", "entries/stale.md"), ("old_hash", "c" * 64),
        ):
            with self.subTest(field=field), self.assertRaises(CatalogConflict):
                self.catalog.begin_projection_move("src_move", 1, **{**self.move, field: replacement})
            self.assertIsNone(self.catalog.get_projection_move("src_move", 1))
            self.assertEqual(self.catalog.get_projection_state("src_move", 1), before)
        self.catalog.set_projection_state("src_move", 1, state="not_projected")
        with self.assertRaises(CatalogConflict):
            self.catalog.begin_projection_move("src_move", 1, **self.move)
        self.assertEqual(self.catalog.list_projection_moves(), [])

    def test_matching_retry_after_new_path_published(self):
        self.catalog.begin_projection_move("src_move", 1, **self.move)
        self.catalog.set_projection_state(
            "src_move", 1, state="projected", projection_path=self.move["new_path"],
            desired_hash=self.move["new_hash"], last_projected_hash=self.move["new_hash"],
        )
        self.catalog.close()
        self.catalog = LifeContextCatalog(self.paths)
        self.catalog.begin_projection_move("src_move", 1, **self.move)
        projection = self.catalog.get_projection_state("src_move", 1)
        self.assertEqual(projection["state"], "pending")
        self.assertEqual(projection["projection_path"], self.move["new_path"])
        self.assertEqual(projection["last_projected_hash"], self.move["new_hash"])
        self.assertEqual(self.catalog.get_projection_move("src_move", 1),
                         {"source_id": "src_move", "source_version": 1, **self.move})

    def test_conflicting_retries_preserve_original(self):
        self.catalog.begin_projection_move("src_move", 1, **self.move)
        for field, replacement in (
            ("old_path", "entries/other.md"), ("new_path", "entries/other.md"),
            ("old_hash", "c" * 64), ("new_hash", "c" * 64),
        ):
            with self.subTest(field=field), self.assertRaises(CatalogConflict):
                self.catalog.begin_projection_move("src_move", 1, **{**self.move, field: replacement})
        self.assertEqual(len(self.catalog.list_projection_moves()), 1)

    def test_destination_reservation_and_missing_source_rollback(self):
        self.catalog.register_manifest(SourceManifest(
            source_id="src_other", source_version=1, sha256="a" * 64,
            size_bytes=1, provenance={"source": "test"},
        ))
        self.catalog.set_projection_state(
            "src_other", 1, state="projected", projection_path=self.move["old_path"],
            last_projected_hash=self.move["old_hash"],
        )
        self.catalog.begin_projection_move("src_move", 1, **self.move)
        with self.assertRaises(CatalogConflict):
            self.catalog.begin_projection_move("src_other", 1, **self.move)
        self.assertEqual(self.catalog.get_projection_state("src_other", 1)["state"], "projected")
        with self.assertRaises(CatalogError):
            self.catalog.begin_projection_move("missing", 1, **{**self.move, "new_path": "entries/free.md"})
        self.assertEqual(len(self.catalog.list_projection_moves()), 1)

    def test_invalid_move_inputs(self):
        for field in ("old_path", "new_path"):
            for value in (None, 3, "", "entries/.md", "entries/nested/a.md", "other/a.md",
                          "/entries/a.md", "entries/../a.md", "entries/a.txt",
                          "entries//a.md", "entries/a\\b.md", "entries/a\n.md"):
                with self.subTest(field=field, value=value), self.assertRaises(CatalogError):
                    self.catalog.begin_projection_move("src_move", 1, **{**self.move, field: value})
        for field in ("old_hash", "new_hash"):
            for value in (None, "A" * 64, "a" * 63, 3):
                with self.subTest(field=field, value=value), self.assertRaises(CatalogError):
                    self.catalog.begin_projection_move("src_move", 1, **{**self.move, field: value})
        with self.assertRaises(CatalogError):
            self.catalog.begin_projection_move("src_move", 1, **{**self.move, "new_path": self.move["old_path"]})
        for source_id, version in (("../bad", 1), ("src_move", True), ("src_move", 0)):
            for method in (self.catalog.get_projection_move, self.catalog.finish_projection_move):
                with self.assertRaises(CatalogError):
                    method(source_id, version)
            with self.assertRaises(CatalogError):
                self.catalog.begin_projection_move(source_id, version, **self.move)
        for limit in (0, True, 1001, "1"):
            with self.assertRaises(CatalogError):
                self.catalog.list_projection_moves(limit=limit)
        self.assertEqual(self.catalog.list_projection_moves(), [])

    def test_migrate_seven_preserves_projection(self):
        before = self.catalog.get_projection_state("src_move", 1)
        self.catalog.close()
        with sqlite3.connect(self.paths.catalog_path) as connection:
            downgrade_to_version_eight(connection)
            connection.execute("DROP TABLE journal_projection_moves")
            connection.execute("PRAGMA user_version=7")
        self.catalog = LifeContextCatalog(self.paths)
        self.assertEqual(self.catalog._connection.execute("PRAGMA user_version").fetchone()[0], 9)
        self.assertEqual(self.catalog.get_projection_state("src_move", 1), before)
        self.catalog.begin_projection_move("src_move", 1, **self.move)

    def test_strict_schema_validation(self):
        self.catalog.close()
        with sqlite3.connect(self.paths.catalog_path) as connection:
            connection.execute("ALTER TABLE journal_projection_moves ADD COLUMN unexpected TEXT")
        with self.assertRaises(CatalogError):
            LifeContextCatalog(self.paths)

    def test_database_constraints(self):
        connection = self.catalog._connection
        for values in (
            ("missing", 1, *self.move.values()),
            ("src_move", 1, None, self.move["new_path"], "a" * 64, "b" * 64),
            ("src_move", 1, self.move["old_path"], self.move["new_path"], "bad", "b" * 64),
        ):
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("INSERT INTO journal_projection_moves VALUES (?,?,?,?,?,?)", values)

    def test_missing_projection_state_rolls_back_move(self):
        self.catalog._connection.execute(
            "DELETE FROM projection_states WHERE source_id='src_move'"
        )
        with self.assertRaises(CatalogError):
            self.catalog.begin_projection_move("src_move", 1, **self.move)
        self.assertIsNone(self.catalog.get_projection_move("src_move", 1))

    def test_current_schema_rejects_missing_or_weakened_move_table(self):
        self.catalog.close()
        with sqlite3.connect(self.paths.catalog_path) as connection:
            connection.execute("DROP TABLE journal_projection_moves")
        with self.assertRaises(CatalogError):
            LifeContextCatalog(self.paths)
        with sqlite3.connect(self.paths.catalog_path) as connection:
            connection.execute("""CREATE TABLE journal_projection_moves (
                source_id TEXT, source_version INTEGER, old_path TEXT,
                new_path TEXT, old_hash TEXT, new_hash TEXT
            )""")
        with self.assertRaises(CatalogError):
            LifeContextCatalog(self.paths)


if __name__ == "__main__":
    unittest.main()
