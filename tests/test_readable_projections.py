import asyncio
import hashlib
import unittest
from contextlib import contextmanager
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import test_application as fixtures
import yaml

from lifecontext.application import JournalApplication
from lifecontext.catalog import CatalogError
from lifecontext.config import ConfigError, load_settings
from lifecontext.hindsight import _validated_profile_settings
from lifecontext.vault import ManagedVault, VaultError


class ReadableProjectionTests(unittest.TestCase):
    setUp = fixtures.JournalApplicationTests.setUp
    tearDown = fixtures.JournalApplicationTests.tearDown

    def register(self, source_id="src_readable", version=1, original=b"# A readable day\n\nBody\n", provenance=None):
        manifest = self.app.custody.write_version(
            source_id, version, original, provenance=provenance or {"date": "2026-10-08"}
        )
        self.app.catalog.register_manifest(manifest)
        return manifest, original

    def legacy(self, source_id="src_legacy"):
        manifest, original = self.register(source_id)
        path = f"entries/{source_id}-v1.md"
        content = self.app._render_projection(manifest, original, legacy=True)
        digest = hashlib.sha256(content.encode()).hexdigest()
        self.vault.safe_write_text(path, content)
        self.app.catalog.set_projection_state(
            source_id, 1, state="projected", projection_path=path,
            desired_hash=digest, last_projected_hash=digest,
        )
        return manifest, path, digest

    def earlier(self, source_id="src_earlier", original=b"Entry date: 2026-10-12 (Europe/Vienna).\n\nA quiet day at home. More.\n",
                path="entries/Entry date 2026-10-12 (EuropeVienna).md", provenance=None):
        """Project an entry the way the release before dated opening-sentence names did."""
        manifest, original = self.register(source_id, original=original, provenance=provenance or {"source": "upload"})
        content = self.app._render_projection(manifest, original, first_line_title=True)
        digest = hashlib.sha256(content.encode()).hexdigest()
        self.vault.safe_write_text(path, content)
        self.app.catalog.set_projection_state(
            source_id, 1, state="projected", projection_path=path,
            desired_hash=digest, last_projected_hash=digest,
        )
        return manifest, original, path, digest

    def test_undated_entry_is_named_by_the_day_it_was_retained(self):
        manifest, _ = self.register(provenance={"source": "upload"})
        self.app.catalog._connection.execute(
            "UPDATE sources SET created_at='2026-10-13 22:30:00' WHERE source_id=?", (manifest.source_id,)
        )
        self.app._journal_timezone = ZoneInfo("UTC")
        self.assertEqual(self.app._retained_date(manifest.source_id), "2026-10-13")
        self.app._journal_timezone = ZoneInfo("Europe/Vienna")
        self.assertEqual(self.app.project(manifest.source_id, 1)["projection_path"], "entries/2026-10-14 - A readable day.md")
        self.register("src_dated", original=b"# Dated in text\nEntry date: 2026-09-30\n", provenance={"source": "upload"})
        self.assertEqual(self.app.project("src_dated", 1)["projection_path"], "entries/2026-09-30 - Dated in text.md")
        self.assertEqual(self.app.reconcile_projections()["already_projected"], 2)

    def test_configured_timezone_is_validated_and_used(self):
        def configure(zone):
            self.paths.config_path.write_text(yaml.safe_dump({"timezone": zone}), encoding="utf-8")
            self.paths.config_path.chmod(0o600)

        for zone in ("Mars/Olympus", "../etc/passwd", "", 3, ["Europe/Vienna"]):
            with self.subTest(zone=zone):
                configure(zone)
                with self.assertRaises(ConfigError):
                    load_settings()
        self.paths.config_path.write_text(yaml.safe_dump({
            "timezone": "Europe/Vienna", "llm": {"provider": "openai", "model": "model"},
        }), encoding="utf-8")
        self.assertEqual(_validated_profile_settings(load_settings()), {"provider": "openai", "model": "model"})
        configure("Europe/Vienna")
        self.assertIsNone(self.app._journal_timezone)
        with ManagedVault(self.vault_root, self.journal) as vault:
            application = JournalApplication(paths=self.paths, hindsight=self.adapter, vault=vault)
            try:
                self.assertEqual(application._journal_timezone, ZoneInfo("Europe/Vienna"))
            finally:
                application.close()

    def test_earlier_rendering_is_the_previous_release_output(self):
        # Captured from the release before dated opening-sentence names.
        golden = (
            "---\naliases:\n- src_earlier-v1\nownership_marker: lifeweave-journal\nprovenance:\n  source: upload\n"
            "source_checksum: a4264664914188316ae7358f69bec532bcc3d6667a5f22156f250f4a71f9767f\n"
            "source_id: src_earlier\nsource_version: 1\ntitle: 'Entry date: 2026-10-12 (Europe/Vienna).'\n\n---\n\n"
            "Entry date: 2026-10-12 (Europe/Vienna).\n\nA quiet day at home. More.\n"
        )
        manifest, original, path, digest = self.earlier()
        self.assertEqual((self.journal / path).read_text(), golden)
        self.assertEqual(digest, "3ec40a57868834698eb3f39ffe605a3e4703d2a8a622a6631f718cd25074c65d")
        self.assertEqual(self.app._earlier_projection(manifest, original, path, digest), golden)
        self.assertIsNone(self.app._earlier_projection(manifest, original, path, "0" * 64))

    def test_catalog_failure_is_not_reported_as_invalid_source_text(self):
        manifest, original = self.register()
        failure = patch.object(self.app.catalog, "get_source", side_effect=CatalogError("catalog operation failed"))
        with failure, self.assertRaises(CatalogError):
            self.app._journal_basename(manifest, original)

    def test_move_begun_by_the_previous_release_finishes_then_renames(self):
        manifest, original = self.register("src_legacy", original=b"Entry date: 2026-10-12 (Europe/Vienna).\n\nA quiet day at home. More.\n",
                                           provenance={"source": "upload"})
        old_path = "entries/src_legacy-v1.md"
        legacy = self.app._render_projection(manifest, original, legacy=True)
        old_hash = hashlib.sha256(legacy.encode()).hexdigest()
        self.vault.safe_write_text(old_path, legacy)
        self.app.catalog.set_projection_state(
            "src_legacy", 1, state="projected", projection_path=old_path, desired_hash=old_hash, last_projected_hash=old_hash,
        )
        planned = self.app._render_projection(manifest, original, first_line_title=True)
        planned_path = "entries/Entry date 2026-10-12 (EuropeVienna).md"
        self.app.catalog.begin_projection_move(
            "src_legacy", 1, old_path=old_path, new_path=planned_path,
            old_hash=old_hash, new_hash=hashlib.sha256(planned.encode()).hexdigest(),
        )
        self.assertEqual(self.app.reconcile_projections()["failed"], 0)
        self.assertEqual(self.app.catalog.list_projection_moves(), [])
        self.assertEqual(self.app.catalog.get_projection_state("src_legacy", 1)["projection_path"], planned_path)
        self.assertEqual(self.app.reconcile_projections()["failed"], 0)
        self.assertEqual([path.name for path in self.entries.glob("*.md")], ["2026-10-12 - A quiet day at home.md"])
        self.assertEqual(
            (self.entries / "2026-10-12 - A quiet day at home.md").read_text(), self.app._render_projection(manifest, original)
        )

    def test_earlier_readable_name_is_renamed_and_retitled(self):
        manifest, original, old_path, _ = self.earlier()
        report = self.app.reconcile_projections()
        self.assertEqual((report["failed"], report["already_projected"]), (0, 1))
        state = self.app.catalog.get_projection_state(manifest.source_id, 1)
        self.assertEqual(state["projection_path"], "entries/2026-10-12 - A quiet day at home.md")
        self.assertEqual(state["state"], "projected")
        self.assertEqual([path.name for path in self.entries.glob("*.md")], ["2026-10-12 - A quiet day at home.md"])
        content = (self.journal / state["projection_path"]).read_text()
        self.assertEqual(content, self.app._render_projection(manifest, original))
        self.assertEqual(yaml.safe_load(content.split("---", 2)[1])["title"], "A quiet day at home")
        self.assertTrue(content.endswith(original.decode()))
        self.assertIsNone(self.app.catalog.get_projection_move(manifest.source_id, 1))
        self.assertFalse((self.journal / old_path).exists())
        self.assertEqual(self.app.reconcile_projections()["already_projected"], 1)
        again = self.app.catalog.get_projection_state(manifest.source_id, 1)
        for field in ("state", "projection_path", "desired_hash", "last_projected_hash"):
            self.assertEqual(again[field], state[field])

    def test_edited_earlier_readable_name_is_preserved(self):
        manifest, _, path, _ = self.earlier()
        target = self.journal / path
        target.write_bytes(target.read_bytes() + b"Human edit\n")
        edited = target.read_bytes()
        self.assertEqual(self.app.reconcile_projections()["failed"], 1)
        self.assertEqual(target.read_bytes(), edited)
        self.assertEqual(len(list(self.entries.glob("*.md"))), 1)
        state = self.app.catalog.get_projection_state(manifest.source_id, 1)
        self.assertEqual((state["projection_path"], state["state"]), (path, "conflict"))
        self.assertIsNone(self.app.catalog.get_projection_move(manifest.source_id, 1))

    def test_unrecognised_stored_rendering_stays_a_conflict(self):
        manifest, original, path, _ = self.earlier()
        content = self.app._render_projection(manifest, original).replace("title: A quiet day at home", "title: Something else")
        digest = hashlib.sha256(content.encode()).hexdigest()
        (self.journal / path).write_text(content)
        self.app.catalog.set_projection_state(
            manifest.source_id, 1, state="projected", projection_path=path,
            desired_hash=digest, last_projected_hash=digest,
        )
        self.assertEqual(self.app.reconcile_projections()["failed"], 1)
        self.assertEqual((self.journal / path).read_text(), content)
        self.assertEqual(self.app.catalog.get_projection_state(manifest.source_id, 1)["state"], "conflict")

    def test_earlier_rendering_with_an_unchanged_name_is_left_as_written(self):
        path = "entries/2026-10-08 - Walk.md"
        manifest, original, _, old_hash = self.earlier(original=b"Walk.\n", path=path, provenance={"date": "2026-10-08"})
        written = (self.journal / path).read_bytes()
        self.assertNotEqual(written.decode(), self.app._render_projection(manifest, original))
        for _ in range(2):
            report = self.app.reconcile_projections()
            self.assertEqual((report["failed"], report["already_projected"]), (0, 1))
        state = self.app.catalog.get_projection_state(manifest.source_id, 1)
        self.assertEqual((state["projection_path"], state["state"], state["desired_hash"]), (path, "projected", old_hash))
        self.assertEqual((self.journal / path).read_bytes(), written)
        self.assertEqual([entry.name for entry in self.entries.glob("*.md")], ["2026-10-08 - Walk.md"])

    def test_numbered_names_stay_put(self):
        for source_id in ("src_one", "src_two", "src_three"):
            self.register(source_id)
        paths = [self.app.project(source_id, 1)["projection_path"] for source_id in ("src_one", "src_two", "src_three")]
        self.assertEqual(paths, [
            "entries/2026-10-08 - A readable day.md",
            "entries/2026-10-08 - A readable day (2).md",
            "entries/2026-10-08 - A readable day (3).md",
        ])
        self.assertEqual(self.app.reconcile_projections()["already_projected"], 3)
        self.assertEqual(
            [self.app.catalog.get_projection_state(source_id, 1)["projection_path"] for source_id in ("src_one", "src_two", "src_three")],
            paths,
        )
        current = self.app._journal_name_is_current
        self.assertTrue(current("entries/Walk.md", "Walk"))
        self.assertTrue(current("entries/Walk (12).md", "Walk"))
        for path in ("entries/Walk (0).md", "entries/Walk (2) (3).md", "entries/Walk - v2.md", "entries/Walks.md", "entries/walk.md"):
            self.assertFalse(current(path, "Walk"), path)
        self.assertTrue(current("entries/Walk (2) (3).md", "Walk (2)"))
        self.assertFalse(current("entries/Wal+.md", "Wal."))

    def test_earlier_names_of_same_titled_entries_get_numbered_dated_names(self):
        for number, source_id in enumerate(("src_a", "src_b", "src_c"), 1):
            suffix = "" if number == 1 else f" ({number})"
            self.earlier(source_id, original=b"Synthetic acceptance.\n", path=f"entries/Synthetic acceptance{suffix}.md")
        self.app.catalog._connection.execute("UPDATE sources SET created_at='2026-09-30 09:11:45'")
        self.assertEqual(self.app.reconcile_projections()["failed"], 0)
        self.assertEqual(sorted(path.name for path in self.entries.glob("*.md")), [
            "2026-09-30 - Synthetic acceptance (2).md",
            "2026-09-30 - Synthetic acceptance (3).md",
            "2026-09-30 - Synthetic acceptance.md",
        ])
        self.assertEqual(self.app.catalog.list_projection_moves(), [])

    def test_forgotten_earlier_readable_name_is_renamed_without_reindexing(self):
        manifest, _, old_path, _ = self.earlier()
        self.app.catalog.set_provider_state(manifest.source_id, 1, recall_visibility="not_visible")
        self.assertEqual(self.app.reconcile_projections()["failed"], 0)
        projection = self.app.catalog.get_projection_state(manifest.source_id, 1)
        self.assertEqual(projection["projection_path"], "entries/2026-10-12 - A quiet day at home.md")
        self.assertFalse((self.journal / old_path).exists())
        self.assertEqual(self.client.retain_calls, [])
        self.assertEqual(self.app.reconcile_projections()["skipped"], 1)

    def test_readable_name_identity_and_idempotency(self):
        manifest, original = self.register()
        result = self.app.project(manifest.source_id, 1)
        self.assertEqual(result["projection_path"], "entries/2026-10-08 - A readable day.md")
        content = (self.journal / result["projection_path"]).read_text()
        fields = yaml.safe_load(content.split("---", 2)[1])
        self.assertEqual(fields["source_id"], manifest.source_id)
        self.assertEqual(fields["source_checksum"], manifest.sha256)
        self.assertEqual(fields["title"], "A readable day")
        self.assertTrue(content.endswith(original.decode()))
        self.assertEqual(self.app.project(manifest.source_id, 1), result)
        self.assertEqual(self.app.reconcile_projections()["already_projected"], 1)

    def test_collisions_versions_and_missing_repair(self):
        self.register("src_first")
        self.register("src_second")
        first = self.app.project("src_first", 1)
        second = self.app.project("src_second", 1)
        self.assertEqual(second["projection_path"], "entries/2026-10-08 - A readable day (2).md")
        self.register("src_first", version=2)
        later = self.app.project("src_first", 2)
        self.assertTrue(later["projection_path"].endswith(" - v2.md"))
        (self.journal / first["projection_path"]).unlink()
        self.assertEqual(self.app.reconcile_projections()["projected"], 1)
        self.assertTrue((self.journal / first["projection_path"]).is_file())

    def test_long_first_line_remains_recoverable(self):
        original = ("Long readable text " * 1000).encode()
        manifest, _ = self.register(original=original)
        projected = self.app.project(manifest.source_id, 1)
        self.assertEqual(self.app.reconcile_projections()["failed"], 0)
        self.assertTrue((self.journal / projected["projection_path"]).read_bytes().endswith(original))

    def test_unicode_equivalent_human_filename_is_reserved(self):
        target = self.entries / "2026-10-08 - Cafe\u0301.md"
        target.write_bytes(b"Human note\n")
        target.chmod(0o600)
        manifest, _ = self.register(original="# Café\n".encode())
        result = self.app.project(manifest.source_id, 1)
        self.assertEqual(result["projection_path"], "entries/2026-10-08 - Café (2).md")
        self.assertEqual(target.read_bytes(), b"Human note\n")

    def test_migration_and_legacy_alias(self):
        manifest, old_path, _ = self.legacy()
        self.assertEqual(self.app.reconcile_projections()["failed"], 0)
        state = self.app.catalog.get_projection_state(manifest.source_id, 1)
        self.assertEqual(state["projection_path"], "entries/2026-10-08 - A readable day.md")
        self.assertFalse((self.journal / old_path).exists())
        fields = yaml.safe_load((self.journal / state["projection_path"]).read_text().split("---", 2)[1])
        self.assertEqual(fields["aliases"], ["src_legacy-v1"])
        self.assertIsNone(self.app.catalog.get_projection_move(manifest.source_id, 1))

    def test_edited_legacy_is_preserved(self):
        manifest, path, _ = self.legacy()
        target = self.journal / path
        target.write_bytes(target.read_bytes() + b"Human edit\n")
        edited = target.read_bytes()
        self.assertEqual(self.app.reconcile_projections()["failed"], 1)
        self.assertEqual(target.read_bytes(), edited)
        self.assertEqual(len(list(self.entries.glob("*.md"))), 1)
        self.assertIsNone(self.app.catalog.get_projection_move(manifest.source_id, 1))

    def test_existing_forgotten_projection_is_renamed_without_reindexing(self):
        manifest, old_path, _ = self.legacy()
        self.app.catalog.set_provider_state(manifest.source_id, 1, recall_visibility="not_visible")
        self.assertEqual(self.app.reconcile_projections()["failed"], 0)
        projection = self.app.catalog.get_projection_state(manifest.source_id, 1)
        self.assertNotEqual(projection["projection_path"], old_path)
        self.assertFalse((self.journal / old_path).exists())
        self.assertEqual(self.app.catalog.get_provider_state(manifest.source_id, 1)["recall_visibility"], "not_visible")
        self.assertEqual(self.client.retain_calls, [])

    def test_missing_forgotten_projection_is_not_restored(self):
        manifest, old_path, _ = self.legacy()
        self.app.catalog.set_provider_state(manifest.source_id, 1, recall_visibility="not_visible")
        (self.journal / old_path).unlink()
        report = self.app.reconcile_projections()
        self.assertEqual(report["skipped"], 1)
        self.assertEqual(list(self.entries.glob("*.md")), [])

    def test_startup_does_not_restore_missing_forgotten_pending_projection(self):
        manifest, path, digest = self.legacy()
        self.app.catalog.set_provider_state(manifest.source_id, 1, recall_visibility="not_visible")
        self.app.catalog.set_projection_state(
            manifest.source_id, 1, state="pending", projection_path=path,
            desired_hash=digest, last_projected_hash=digest,
        )
        (self.journal / path).unlink()
        report = asyncio.run(self.app.recover_startup())
        self.assertEqual(report["projections_reconciled"], 0)
        self.assertEqual(report["projections_unresolved"], 1)
        self.assertEqual(list(self.entries.glob("*.md")), [])

    def test_interrupted_move_resumes_on_startup(self):
        manifest, old_path, _ = self.legacy()
        with patch.object(self.app.relationships, "relocate_journal_projection", side_effect=VaultError("conflict")):
            self.assertEqual(self.app.reconcile_projections()["failed"], 1)
        move = self.app.catalog.get_projection_move(manifest.source_id, 1)
        self.assertIsNotNone(move)
        self.assertTrue((self.journal / old_path).is_file())
        self.assertTrue((self.journal / move["new_path"]).is_file())
        report = asyncio.run(self.app.recover_startup())
        self.assertEqual(report["projections_reconciled"], 1)
        self.assertIsNone(self.app.catalog.get_projection_move(manifest.source_id, 1))
        self.assertFalse((self.journal / old_path).exists())

    def test_crash_after_old_removal_resumes_on_startup(self):
        manifest, old_path, _ = self.legacy()
        with patch.object(self.app.catalog, "finish_projection_move", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                self.app.reconcile_projections()
        self.assertFalse((self.journal / old_path).exists())
        report = asyncio.run(self.app.recover_startup())
        self.assertEqual(report["projections_reconciled"], 1)
        self.assertIsNone(self.app.catalog.get_projection_move(manifest.source_id, 1))

    def test_destination_conflict_keeps_old_and_foreign_file(self):
        manifest, old_path, _ = self.legacy()
        with patch.object(self.app.relationships, "relocate_journal_projection", side_effect=VaultError("conflict")):
            self.app.reconcile_projections()
        move = self.app.catalog.get_projection_move(manifest.source_id, 1)
        target = self.journal / move["new_path"]
        target.write_bytes(b"Unmanaged replacement\n")
        self.assertEqual(self.app.reconcile_projections()["failed"], 1)
        self.assertEqual(target.read_bytes(), b"Unmanaged replacement\n")
        self.assertTrue((self.journal / old_path).is_file())

    def test_readable_removal_requires_matching_identity(self):
        manifest, _ = self.register()
        result = self.app.project(manifest.source_id, 1)
        with self.assertRaises(VaultError):
            self.vault.safe_remove_projection(
                result["projection_path"], expected_hash=result["sha256"],
                expected_source_hash=manifest.sha256, expected_source_id="src_other", expected_source_version=1,
            )
        self.vault.safe_remove_projection(
            result["projection_path"], expected_hash=result["sha256"],
            expected_source_hash=manifest.sha256, expected_source_id=manifest.source_id, expected_source_version=1,
        )
        self.assertFalse((self.journal / result["projection_path"]).exists())

    def test_writer_transaction_is_reentrant_but_blocks_other_instance(self):
        from lifecontext.vault import ManagedVault

        with ManagedVault(self.vault_root, self.journal) as contender:
            with self.vault.writer_transaction():
                with self.vault.writer_transaction():
                    self.register()
                    self.app.project("src_readable", 1)
                with self.assertRaises(VaultError) as raised:
                    with contender.writer_lock(timeout=0.01):
                        self.fail("contender acquired writer lock")
                self.assertEqual(raised.exception.code, "lock_timeout")

    def test_recovery_lock_failure_does_not_mutate_catalog(self):
        manifest, _, _ = self.legacy()
        before = self.app.catalog.get_projection_state(manifest.source_id, 1)
        projection = {"source_id": manifest.source_id, "source_version": 1, **before}
        with patch.object(self.vault, "writer_transaction", side_effect=VaultError("lock_timeout")):
            self.assertFalse(self.app._recover_projection_state(projection))
        self.assertEqual(self.app.catalog.get_projection_state(manifest.source_id, 1), before)

    def test_recovery_conflict_keeps_current_destination_not_stale_snapshot(self):
        manifest, _, _ = self.legacy()
        before = self.app.catalog.get_projection_state(manifest.source_id, 1)
        projection = {"source_id": manifest.source_id, "source_version": 1, **before}
        with patch.object(self.app.catalog, "finish_projection_move", side_effect=VaultError("vault_unavailable")):
            self.assertFalse(self.app._recover_projection_state(projection))
        current = self.app.catalog.get_projection_state(manifest.source_id, 1)
        self.assertEqual(current["projection_path"], "entries/2026-10-08 - A readable day.md")
        self.assertEqual(current["state"], "conflict")
        self.assertEqual(asyncio.run(self.app.recover_startup())["projections_reconciled"], 1)

    def test_deletion_reloads_destination_after_waiting_for_migration(self):
        manifest, old_path, _ = self.legacy()
        projection = {
            "source_id": manifest.source_id, "source_version": 1,
            **self.app.catalog.get_projection_state(manifest.source_id, 1),
        }
        prepared = asyncio.run(self.app.curate(
            {"action": "prepare_delete", "source_id": manifest.source_id, "expected_version": 1},
            idempotency_key="prepare-interleaved-delete",
        ))
        transaction = self.vault.writer_transaction
        migrated_paths = []

        @contextmanager
        def finish_waiting_migration():
            with transaction():
                if not migrated_paths:
                    with patch.object(self.app.catalog, "get_tombstone", return_value=None):
                        self.assertTrue(self.app._recover_projection_locked(projection))
                    migrated_paths.append(self.app.catalog.get_projection_state(manifest.source_id, 1)["projection_path"])
                yield

        with (
            patch.object(self.vault, "writer_transaction", new=finish_waiting_migration),
            patch.object(self.app, "_verify_provider_document", new=AsyncMock()),
            patch.object(self.app.hindsight, "delete_document", new=AsyncMock(return_value={"success": True})),
        ):
            result = asyncio.run(self.app.curate(
                {"action": "delete", "source_id": manifest.source_id, "expected_version": 1,
                 "prepared_operation_id": prepared["operation_id"]},
                idempotency_key="interleaved-delete",
            ))
        self.assertEqual(result["state"], "completed")
        self.assertFalse((self.journal / old_path).exists())
        self.assertFalse((self.journal / migrated_paths[0]).exists())
        self.assertFalse(self.app.custody.original_path(manifest.source_id, 1).exists())
