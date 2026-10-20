"""Local disposable vault projection and custody safety contracts."""
import hashlib
import os
import re
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import quote, unquote

from lifecontext.catalog import LifeContextCatalog
from lifecontext.custody import LifeContextCustody
from lifecontext.derived import DerivedStore
from lifecontext.document_projection import (
    USER_START,
    DocumentProjectionService,
)
from lifecontext.paths import LifeContextPaths
from lifecontext.vault import ManagedVault, VaultError


class ProjectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        root = base / "vault"
        self.journal = root / "Journal"
        root.mkdir(mode=0o700)
        self.journal.mkdir(mode=0o700)
        self.vault = ManagedVault(root, self.journal)
        self.addCleanup(self.vault.close)
        self.catalog = LifeContextCatalog(LifeContextPaths(base / "custody"))
        self.addCleanup(self.catalog.close)
        self.custody = LifeContextCustody(LifeContextPaths(base / "custody"), max_bytes=1024 * 1024)
        self.service = DocumentProjectionService(self.catalog, self.custody,
            DerivedStore(LifeContextPaths(base / "custody")), self.vault)
        self.data = b"\x00\xff\x80binary\r\n"
        manifest = self.custody.write_version("doc_test", 1, self.data, provenance={"source": "test"})
        self.catalog.register_manifest(manifest, layer="document")
        self.catalog.register_document_metadata("doc_test", 1, filename="original.bin",
                                               title="Binary", mime_type="application/octet-stream")

    def move_outage(self, path, **kwargs):
        if kwargs.get("staged_name") is not None:
            return ManagedVault.safe_remove_artifact(self.vault, path, **kwargs)
        raise VaultError("vault_unavailable")

    def test_binary_and_double_reconcile_and_missing_rebuild(self):
        result = self.service.reconcile_documents()
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["rebuilt"], 2)
        path = self.journal / "documents/inbox/attachments/doc_test-v1--original.bin"
        self.assertEqual(path.read_bytes(), self.data)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),
                         self.custody.resolve_exact("doc_test", 1).manifest.sha256)
        self.assertEqual(self.service.reconcile_documents()["rebuilt"], 0)
        path.unlink()
        self.assertEqual(self.service.reconcile_documents()["rebuilt"], 1)
        self.assertEqual(self.service.reconcile_documents()["rebuilt"], 0)

    def test_long_valid_projection_name_uses_bounded_staging_names(self):
        source_id = "doc_" + "a" * 96
        manifest = self.custody.write_version(source_id, 1, self.data, provenance={"source": "test"})
        self.catalog.register_manifest(manifest, layer="document")
        self.catalog.register_document_metadata(source_id, 1, filename="b" * 120,
            title="Long name", mime_type="application/octet-stream")
        self.assertEqual(self.service.project_document(source_id, 1)["state"], "completed")
        proof = self.catalog.get_artifact_publication(source_id + "_attachment", 1)
        self.assertLess(len(proof["staged_name"]), 64)
        self.service.remove_document(source_id, 1)

    def test_edited_attachment_preserved_not_ingested(self):
        self.service.project_document("doc_test", 1)
        path = self.journal / "documents/inbox/attachments/doc_test-v1--original.bin"
        path.write_bytes(b"edited")
        self.assertEqual(self.service.reconcile_documents()["state"], "conflict")
        self.assertEqual(path.read_bytes(), b"edited")
        self.assertEqual(self.custody.resolve_exact("doc_test", 1).original_bytes, self.data)

    def test_category_move_and_edited_old_path_preserved(self):
        self.service.project_document("doc_test", 1)
        original_get = self.catalog.get_document_metadata
        self.catalog.get_document_metadata = lambda *args: {**original_get(*args), "category_id": "cat_stable"}
        self.assertEqual(self.service.project_document("doc_test", 1)["state"], "completed")
        old = self.journal / "documents/inbox/attachments/doc_test-v1--original.bin"
        new = self.journal / "documents/cat_stable/attachments/doc_test-v1--original.bin"
        self.assertFalse(old.exists())
        self.assertEqual(new.read_bytes(), self.data)
        new.write_bytes(b"manual")
        self.catalog.get_document_metadata = lambda *args: {**original_get(*args), "category_id": "cat_merge"}
        self.assertEqual(self.service.project_document("doc_test", 1)["state"], "conflict")
        self.assertEqual(new.read_bytes(), b"manual")

    def test_failed_move_records_both_paths_for_delete(self):
        self.service.project_document("doc_test", 1)
        original_get = self.catalog.get_document_metadata
        self.catalog.get_document_metadata = lambda *args: {**original_get(*args), "category_id": "health"}
        with patch.object(self.vault, "safe_remove_artifact", side_effect=self.move_outage):
            self.assertEqual(self.service.project_document("doc_test", 1)["state"], "conflict")
        paths = list(self.journal.glob("documents/**/*"))
        copies = [p for p in paths if p.is_file()]
        self.assertEqual(len(copies), 4)
        restarted = DocumentProjectionService(self.catalog, self.custody, self.service.derived, self.vault)
        restarted.remove_document("doc_test", 1)
        self.assertTrue(all(not p.exists() for p in copies))

    def test_failed_move_recovery_preserves_edited_destination(self):
        self.service.project_document("doc_test", 1)
        original_get = self.catalog.get_document_metadata
        self.catalog.get_document_metadata = lambda *args: {**original_get(*args), "category_id": "health"}
        with patch.object(self.vault, "safe_remove_artifact", side_effect=self.move_outage):
            self.service.project_document("doc_test", 1)
        path = self.journal / "documents/health/attachments/doc_test-v1--original.bin"
        path.write_bytes(b"manual")
        restarted = DocumentProjectionService(self.catalog, self.custody, self.service.derived, self.vault)
        self.assertEqual(restarted.project_document("doc_test", 1)["state"], "conflict")
        with self.assertRaises(VaultError):
            restarted.remove_document("doc_test", 1)
        self.assertEqual(path.read_bytes(), b"manual")
        path.write_bytes(self.data)
        restarted.remove_document("doc_test", 1, allow_missing=True)
        self.assertFalse(path.exists())

    def test_failed_move_retry_converges_without_third_copy(self):
        self.service.project_document("doc_test", 1)
        original_get = self.catalog.get_document_metadata
        category = {"value": "health"}
        self.catalog.get_document_metadata = lambda *args: {**original_get(*args), "category_id": category["value"]}
        with patch.object(self.vault, "safe_remove_artifact", side_effect=self.move_outage):
            self.service.project_document("doc_test", 1)
            category["value"] = "work"
            self.assertEqual(self.service.project_document("doc_test", 1)["state"], "conflict")
        self.assertFalse((self.journal / "documents/work").exists())
        restarted = DocumentProjectionService(self.catalog, self.custody, self.service.derived, self.vault)
        self.assertEqual(restarted.project_document("doc_test", 1)["state"], "completed")
        self.assertFalse((self.journal / "documents/inbox/attachments/doc_test-v1--original.bin").exists())
        self.assertFalse((self.journal / "documents/health/attachments/doc_test-v1--original.bin").exists())
        self.assertEqual(restarted.project_document("doc_test", 1)["rebuilt"], 0)

    def test_move_crashes_before_and_after_publication_remain_deletable(self):
        for published in (False, True):
            with self.subTest(published=published):
                self.service.project_document("doc_test", 1)
                original_get = self.catalog.get_document_metadata
                self.catalog.get_document_metadata = lambda *args, original_get=original_get: {**original_get(*args), "category_id": "health"}
                original_link = os.link

                def crash(*args, published=published, original_link=original_link, **kwargs):
                    if args[1].endswith(".hold"):
                        return original_link(*args, **kwargs)
                    if published:
                        original_link(*args, **kwargs)
                    raise SystemExit("interrupted")

                with patch("lifecontext.vault.os.link", side_effect=crash), self.assertRaises(SystemExit):
                    self.service.project_document("doc_test", 1)
                restarted = DocumentProjectionService(self.catalog, self.custody, self.service.derived, self.vault)
                restarted.remove_document("doc_test", 1)
                self.assertEqual([p for p in self.journal.glob("documents/**/*") if p.is_file()], [])
                self.catalog.get_document_metadata = original_get

    def test_source_note_update_crash_tracks_both_checksum_baselines(self):
        for published in (False, True):
            with self.subTest(published=published):
                self.service.project_document("doc_test", 1)
                original_get = self.catalog.get_document_metadata
                self.catalog.get_document_metadata = lambda *args, original_get=original_get: {**original_get(*args), "title": "Updated title"}
                original_replace = os.replace

                def crash(*args, published=published, original_replace=original_replace, **kwargs):
                    if published:
                        original_replace(*args, **kwargs)
                    raise SystemExit("interrupted")

                with patch("lifecontext.vault.os.replace", side_effect=crash), self.assertRaises(SystemExit):
                    self.service.project_document("doc_test", 1)
                restarted = DocumentProjectionService(self.catalog, self.custody, self.service.derived, self.vault)
                restarted.remove_document("doc_test", 1)
                self.assertEqual([p for p in self.journal.glob("documents/**/*") if p.is_file()], [])
                self.catalog.get_document_metadata = original_get

    def test_move_restart_after_old_unlink_converges(self):
        self.service.project_document("doc_test", 1)
        original_get = self.catalog.get_document_metadata
        self.catalog.get_document_metadata = lambda *args: {**original_get(*args), "category_id": "health"}
        original_remove = self.vault.safe_remove_artifact

        def crash(path, **kwargs):
            result = original_remove(path, **kwargs)
            if kwargs.get("staged_name") is not None:
                return result
            raise SystemExit("interrupted")

        with patch.object(self.vault, "safe_remove_artifact", side_effect=crash), self.assertRaises(SystemExit):
            self.service.project_document("doc_test", 1)
        restarted = DocumentProjectionService(self.catalog, self.custody, self.service.derived, self.vault)
        self.assertEqual(restarted.project_document("doc_test", 1)["state"], "completed")
        self.assertEqual(restarted.project_document("doc_test", 1)["rebuilt"], 0)
        self.assertEqual(len([p for p in self.journal.glob("documents/**/*") if p.is_file()]), 2)

    def test_failed_move_source_note_edit_is_preserved(self):
        self.service.project_document("doc_test", 1)
        original_get = self.catalog.get_document_metadata
        self.catalog.get_document_metadata = lambda *args: {**original_get(*args), "category_id": "health"}
        with patch.object(self.vault, "safe_remove_artifact", side_effect=self.move_outage):
            self.service.project_document("doc_test", 1)
        path = self.journal / "documents/health/doc_test-v1.md"
        baseline = path.read_bytes()
        path.write_bytes(b"manual source note")
        self.assertEqual(self.service.project_document("doc_test", 1)["state"], "conflict")
        with self.assertRaises(VaultError):
            self.service.remove_document("doc_test", 1)
        self.assertEqual(path.read_bytes(), b"manual source note")
        path.write_bytes(baseline)
        self.service.remove_document("doc_test", 1, allow_missing=True)
        self.assertFalse(path.exists())

    @unittest.skipUnless(sys.platform == "linux", "deterministic SIGKILL fixture uses procfs")
    def test_actual_sigkill_before_inode_proof_keeps_unproved_stage_and_custody(self):
        script = """
import os, signal, stat, sys
from pathlib import Path
from lifecontext.catalog import LifeContextCatalog
from lifecontext.custody import LifeContextCustody
from lifecontext.derived import DerivedStore
from lifecontext.document_projection import DocumentProjectionService
from lifecontext.paths import LifeContextPaths
from lifecontext.vault import ManagedVault
paths = LifeContextPaths(Path(sys.argv[1]))
catalog = LifeContextCatalog(paths)
custody = LifeContextCustody(paths, max_bytes=1024 * 1024)
vault = ManagedVault(Path(sys.argv[2]), Path(sys.argv[2]) / 'Journal')
service = DocumentProjectionService(catalog, custody, DerivedStore(paths), vault)
original_get = catalog.get_document_metadata
catalog.get_document_metadata = lambda *args: {**original_get(*args), 'category_id': 'health'}
if sys.argv[3] == 'before-create':
    original_set = catalog.set_artifact_publication
    def save(*args, **kwargs):
        original_set(*args, **kwargs)
        if kwargs.get('state') == 'planned':
            os.kill(os.getpid(), signal.SIGKILL)
    catalog.set_artifact_publication = save
else:
    original_fsync = os.fsync
    def fsync(descriptor):
        original_fsync(descriptor)
        if stat.S_ISREG(os.fstat(descriptor).st_mode):
            name = Path(os.readlink('/proc/self/fd/' + str(descriptor))).name
            if name.startswith('.publication.') and name.endswith('.tmp'):
                os.kill(os.getpid(), signal.SIGKILL)
    os.fsync = fsync
service.project_document('doc_test', 1)
raise AssertionError('kill point was not reached')
"""
        for mode in ("after-write", "before-create"):
            with self.subTest(mode=mode):
                self.service.project_document("doc_test", 1)
                result = subprocess.run([sys.executable, "-c", script,
                    str(self.journal.parent.parent / "custody"), str(self.journal.parent), mode],
                    capture_output=True, timeout=30, check=False)
                self.assertEqual(result.returncode, -signal.SIGKILL, result.stderr.decode())
                proof = self.catalog.get_artifact_publication("doc_test_attachment", 1)
                self.assertEqual(proof["state"], "planned")
                self.assertIsNone(proof["device"])
                self.assertIsNone(proof["inode"])
                staged = (self.journal / proof["projection_path"]).parent / proof["staged_name"][:-5]
                restarted = DocumentProjectionService(self.catalog, self.custody, self.service.derived, self.vault)
                original_get = self.catalog.get_document_metadata
                self.catalog.get_document_metadata = lambda *args, original_get=original_get: {**original_get(*args), "category_id": "health"}
                if mode == "after-write":
                    self.assertEqual(staged.read_bytes(), self.data)
                    self.assertEqual(restarted.project_document("doc_test", 1)["state"], "conflict")
                    with self.assertRaises(VaultError) as caught:
                        restarted.remove_document("doc_test", 1)
                    self.assertEqual(caught.exception.code, "conflict")
                    self.assertEqual(staged.read_bytes(), self.data)
                    self.assertEqual(self.custody.resolve_exact("doc_test", 1).original_bytes, self.data)
                    staged.unlink()
                else:
                    self.assertFalse(staged.exists())
                self.assertEqual(restarted.project_document("doc_test", 1)["state"], "completed")
                self.assertEqual(restarted.project_document("doc_test", 1)["rebuilt"], 0)
                restarted.remove_document("doc_test", 1)
                self.assertEqual([p for p in self.journal.glob("documents/**/*") if p.is_file()], [])
                self.catalog.get_document_metadata = original_get

    def test_planned_stage_exclusive_create_race_never_removes_foreign_file(self):
        self.service.project_document("doc_test", 1)
        original_get = self.catalog.get_document_metadata
        self.catalog.get_document_metadata = lambda *args: {**original_get(*args), "category_id": "health"}
        original_open = os.open

        def competing_create(name, flags, *args, **kwargs):
            if isinstance(name, str) and name.startswith(".publication.") and name.endswith(".tmp"):
                descriptor = original_open(name, flags, *args, **kwargs)
                os.write(descriptor, self.data)
                os.fsync(descriptor)
                os.close(descriptor)
            return original_open(name, flags, *args, **kwargs)

        with patch("lifecontext.vault.os.open", side_effect=competing_create), self.assertRaises(FileExistsError):
            self.service.project_document("doc_test", 1)
        proof = self.catalog.get_artifact_publication("doc_test_attachment", 1)
        staged = (self.journal / proof["projection_path"]).parent / proof["staged_name"][:-5]
        self.assertEqual(staged.read_bytes(), self.data)
        with self.assertRaises(VaultError):
            self.service.remove_document("doc_test", 1)
        self.assertEqual(staged.read_bytes(), self.data)
        self.assertEqual(self.custody.resolve_exact("doc_test", 1).original_bytes, self.data)

    def test_foreign_identical_create_crash_is_never_adopted_or_deleted(self):
        self.service.project_document("doc_test", 1)
        original_get = self.catalog.get_document_metadata
        self.catalog.get_document_metadata = lambda *args: {**original_get(*args), "category_id": "health"}
        original_link = os.link
        foreign_identity = []

        def crash(*args, **kwargs):
            if args[1].endswith(".hold"):
                return original_link(*args, **kwargs)
            descriptor = os.open(args[1], os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600, dir_fd=kwargs["dst_dir_fd"])
            os.write(descriptor, self.data)
            os.fsync(descriptor)
            info = os.fstat(descriptor)
            foreign_identity.append((info.st_dev, info.st_ino))
            os.close(descriptor)
            raise SystemExit("interrupted before FileExists rollback")

        with patch("lifecontext.vault.os.link", side_effect=crash), self.assertRaises(SystemExit):
            self.service.project_document("doc_test", 1)
        proof = self.catalog.get_artifact_publication("doc_test_attachment", 1)
        self.assertNotEqual((proof["device"], proof["inode"]), foreign_identity[0])
        restarted = DocumentProjectionService(self.catalog, self.custody, self.service.derived, self.vault)
        self.assertEqual(restarted.project_document("doc_test", 1)["state"], "conflict")
        self.assertEqual(self.catalog.get_artifact_projection("document", "doc_test_attachment", 1)["state"], "pending")
        with self.assertRaises(VaultError):
            restarted.remove_document("doc_test", 1)
        path = self.journal / "documents/health/attachments/doc_test-v1--original.bin"
        self.assertEqual(path.read_bytes(), self.data)
        self.assertEqual((path.stat().st_dev, path.stat().st_ino), foreign_identity[0])
        self.assertEqual(self.custody.resolve_exact("doc_test", 1).original_bytes, self.data)

    def test_legitimate_publication_before_ack_recovers_by_inode(self):
        self.service.project_document("doc_test", 1)
        original_get = self.catalog.get_document_metadata
        self.catalog.get_document_metadata = lambda *args: {**original_get(*args), "category_id": "health"}
        original_link = os.link

        def crash(*args, **kwargs):
            result = original_link(*args, **kwargs)
            if not args[1].endswith(".hold"):
                raise SystemExit("interrupted after publication")
            return result

        with patch("lifecontext.vault.os.link", side_effect=crash), self.assertRaises(SystemExit):
            self.service.project_document("doc_test", 1)
        proof = self.catalog.get_artifact_publication("doc_test_attachment", 1)
        path = self.journal / proof["projection_path"]
        self.assertEqual((path.stat().st_dev, path.stat().st_ino), (proof["device"], proof["inode"]))
        restarted = DocumentProjectionService(self.catalog, self.custody, self.service.derived, self.vault)
        self.assertEqual(restarted.project_document("doc_test", 1)["state"], "completed")
        self.assertEqual(restarted.project_document("doc_test", 1)["rebuilt"], 0)
        self.assertEqual(self.catalog.get_artifact_publication("doc_test_attachment", 1)["state"], "completed")
        self.assertEqual(len([p for p in self.journal.glob("documents/**/*") if p.is_file()]), 2)

    def test_move_create_race_does_not_adopt_destination(self):
        self.service.project_document("doc_test", 1)
        original_get = self.catalog.get_document_metadata
        self.catalog.get_document_metadata = lambda *args: {**original_get(*args), "category_id": "health"}
        original_link = os.link

        def competing_create(*args, **kwargs):
            destination = args[1]
            if destination.endswith(".hold"):
                return original_link(*args, **kwargs)
            descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600, dir_fd=kwargs["dst_dir_fd"])
            os.write(descriptor, self.data)
            os.close(descriptor)
            return original_link(*args, **kwargs)

        with patch("lifecontext.vault.os.link", side_effect=competing_create):
            self.assertEqual(self.service.project_document("doc_test", 1)["state"], "conflict")
        path = self.journal / "documents/health/attachments/doc_test-v1--original.bin"
        self.assertEqual(path.read_bytes(), self.data)
        self.service.remove_document("doc_test", 1)
        self.assertEqual(path.read_bytes(), self.data)

    def test_no_adoption_and_explicit_cas(self):
        path = "documents/cat/attachments/doc--original.bin"
        self.vault.safe_write_bytes(path, self.data, expected_hash=None, artifact_id="doc")
        for expected in (None, "0" * 64):
            with self.assertRaises(VaultError):
                self.vault.safe_write_bytes(path, b"new", expected_hash=expected, artifact_id="doc")
        self.vault.safe_write_bytes(path, b"new", expected_hash=hashlib.sha256(self.data).hexdigest(), artifact_id="doc")
        self.assertEqual(self.vault.read_artifact(path), b"new")

    def test_symlink_traversal_and_schema_protected(self):
        for path in ("documents/../escape.bin", "documents/cat/../../escape", "wiki/SCHEMA.md"):
            with self.assertRaises(VaultError):
                self.vault.safe_write_bytes(path, b"bad", expected_hash=None, artifact_id="doc")
        outside = self.journal.parent / "outside"
        outside.mkdir(mode=0o700)
        (self.journal / "documents").symlink_to(outside, target_is_directory=True)
        self.assertEqual(self.service.project_document("doc_test", 1)["state"], "conflict")
        self.assertEqual(list(outside.iterdir()), [])

    def test_document_artifact_removal_is_checksum_pinned_and_acknowledged(self):
        self.service.project_document("doc_test", 1)
        receipts = [self.catalog.get_artifact_projection("document", "doc_test_" + suffix, 1)
                    for suffix in ("attachment", "note")]
        self.service.remove_document("doc_test", 1)
        for receipt in receipts:
            self.assertFalse((self.journal / receipt["projection_path"]).exists())
            self.assertEqual(self.catalog.get_artifact_projection("document", receipt["artifact_id"], 1)["state"], "not_projected")
        self.service.remove_document("doc_test", 1)
        self.assertEqual(self.custody.resolve_exact("doc_test", 1).original_bytes, self.data)

    def test_missing_artifact_removal_preserves_and_reports_sync_conflict_copy(self):
        self.service.project_document("doc_test", 1)
        receipt = self.catalog.get_artifact_projection("document", "doc_test_attachment", 1)
        path = self.journal / receipt["projection_path"]
        conflict = path.with_name(path.name + ".sync-conflict-device")
        conflict.write_bytes(self.data)
        path.unlink()
        with self.assertRaises(VaultError) as caught:
            self.service.remove_document("doc_test", 1, allow_missing=True)
        self.assertEqual(caught.exception.code, "sync_conflict")
        self.assertEqual(conflict.read_bytes(), self.data)
        self.assertEqual(self.catalog.get_artifact_projection("document", "doc_test_attachment", 1)["state"], "conflict")

    def test_document_artifact_removal_does_not_adopt_unacknowledged_copy(self):
        path = "documents/inbox/attachments/doc_test-v1--original.bin"
        self.vault.safe_write_bytes(path, self.data, expected_hash=None, artifact_id="doc_test_attachment")
        with self.assertRaises(VaultError) as caught:
            self.service.remove_document("doc_test", 1, allow_missing=True)
        self.assertEqual(caught.exception.code, "conflict")
        self.assertEqual(self.vault.read_artifact(path), self.data)

    def test_missing_removal_retry_preserves_path_and_schema_protections(self):
        for path in ("documents/../escape.bin", "wiki/SCHEMA.md"):
            with self.subTest(path=path), self.assertRaises(VaultError):
                self.vault.safe_remove_artifact(path, expected_hash="0" * 64,
                    artifact_id="doc_test_attachment", allow_missing=True)
        outside = self.journal.parent / "outside"
        outside.mkdir(mode=0o700)
        original = outside / "original.bin"
        original.write_bytes(self.data)
        (self.journal / "documents").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(VaultError):
            self.vault.safe_remove_artifact("documents/original.bin", expected_hash=hashlib.sha256(self.data).hexdigest(),
                artifact_id="doc_test_attachment", allow_missing=True)
        self.assertEqual(original.read_bytes(), self.data)

    def test_stored_wiki_revisions_rebuild_and_advance(self):
        from test_wiki import PAGE, WikiTests
        fixture = WikiTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture.update()
        projection = DocumentProjectionService(fixture.catalog, fixture.custody,
            fixture.derived, self.vault, fixture.service)
        self.assertEqual(projection.project_wiki(PAGE)["state"], "completed")
        receipt = fixture.catalog.get_artifact_projection("wiki", PAGE, 1)
        page = self.journal / receipt["projection_path"]
        page.write_text(page.read_text().replace(USER_START + "\n\n", USER_START + "\nPrivate note\n"))
        fixture.value["expected_revision"] = 1
        fixture.update()
        self.assertEqual(projection.project_wiki(PAGE)["state"], "completed")
        self.assertIn("Private note", page.read_text())
        self.assertIn("[r2]", page.read_text())
        self.assertEqual(projection.project_wiki(PAGE)["rebuilt"], 0)
        page.write_text(page.read_text().replace("# Original", "# Edited"))
        self.assertEqual(projection.project_wiki(PAGE)["state"], "conflict")
        self.assertEqual(fixture.catalog.list_wiki_reviews(PAGE)[0]["code"], "manual_edit")
        page.unlink()
        self.assertEqual(projection.project_wiki(PAGE)["state"], "completed")
        self.assertIn("# Original", page.read_text())

    def wiki_fixture(self):
        from test_wiki import WikiTests
        fixture = WikiTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        projection = DocumentProjectionService(fixture.catalog, fixture.custody, fixture.derived, self.vault, fixture.service)
        return fixture, projection

    @staticmethod
    def publish(fixture, page_id, title, claims, links=()):
        import json

        from lifecontext.wiki import WikiPatch
        page = fixture.catalog.get_wiki_page(page_id)
        revision = page["current_revision"] if page else 0
        patch = WikiPatch.parse(json.dumps({"page_id": page_id, "title": title, "expected_revision": revision,
                                            "claims": claims, "contradictions": [], "links": list(links)}))
        stored = fixture.service.store.write(patch)
        fixture.catalog.create_wiki_page(page_id, title=title)
        fixture.catalog.commit_wiki_revision(page_id, expected_revision=revision,
            artifact_path=str(stored.path.relative_to(fixture.paths.lifeweave_home)),
            artifact_sha256=stored.sha256, dependencies=fixture.service._validate(patch))

    def test_wiki_state_and_contradictions_render_without_a_new_revision(self):
        from test_wiki import PAGE

        from lifecontext.wiki import STALE_CALLOUT
        fixture, projection = self.wiki_fixture()
        claim = fixture.value["claims"][0]
        fixture.value["contradictions"] = [{"left": claim, "right": {"text": "sky is blue", "citations": [fixture.citation]},
                                            "left_date": "2026-01-01", "right_date": "2026-02-01"}]
        fixture.update()
        self.assertEqual(projection.project_wiki(PAGE)["state"], "completed")
        page = self.journal / fixture.catalog.get_artifact_projection("wiki", PAGE, 1)["projection_path"]
        text = page.read_text()
        self.assertIn("\nstate: current\n", text.split("---", 2)[1])
        self.assertNotIn(STALE_CALLOUT, text)
        self.assertIn("\n> [!note] Contradiction (2026-01-01 / 2026-02-01)\n"
                      "> - The sky is blue. (doc_1 v1/e1 passages 0:1)\n"
                      "> - sky is blue (doc_1 v1/e1 passages 0:1)\n", text)
        user = f"{USER_START}\nMy notes — kept byte for byte.\r\n<!-- life-context:user:end -->\n".encode()
        page.write_bytes(page.read_bytes().replace(f"{USER_START}\n\n<!-- life-context:user:end -->\n".encode(), user))
        fixture.catalog.mark_dependent_wiki_stale("doc_1", 1)
        self.assertEqual(projection.project_wiki(PAGE)["state"], "completed")
        text = page.read_text()
        self.assertIn("\nstate: stale\n", text.split("---", 2)[1])
        self.assertIn("<!-- life-context:managed:start -->\n" + STALE_CALLOUT + "\n\n# Original\n", text)
        self.assertTrue(page.read_bytes().endswith(user))
        self.assertEqual(len(fixture.catalog.list_wiki_revisions(PAGE)), 1)
        self.assertEqual(projection.project_wiki(PAGE)["rebuilt"], 0)
        # A hand edit of the managed block, callout included, stays a conflict.
        edited = page.read_bytes().replace(b"rebuild pending", b"rebuilt")
        page.write_bytes(edited)
        self.assertEqual(projection.project_wiki(PAGE)["state"], "conflict")
        self.assertEqual(page.read_bytes(), edited)
        self.assertIn("manual_edit", [review["code"] for review in fixture.catalog.list_wiki_reviews(PAGE)])

    def test_wiki_citations_link_to_the_projected_source_note_heading(self):
        from lifecontext.wiki import source_page_id
        fixture, projection = self.wiki_fixture()
        fixture.payload["segments"][0]["locator"] = {"kind": "page", "start": 3, "end": 3, "container": None, "schema_version": 1}
        citation = fixture.add_evidence("doc_2", "Grass is green.")
        fixture.catalog.set_artifact_projection("document", "doc_2_note", 1, projection_path="documents/inbox/doc_2-v1.md",
                                                desired_hash="a" * 64, state="projected", last_projected_hash="a" * 64)
        self.publish(fixture, source_page_id("doc_2"), "Grass", [{"text": "Grass is green.", "citations": [citation]}])
        self.publish(fixture, source_page_id("doc_1"), "Sky", [{"text": "The sky is blue.", "citations": [fixture.citation]}])
        for page_id, line in ((source_page_id("doc_2"), "1. Grass is green. ([doc_2 v1/e1 passages 0:1](../documents/inbox/doc_2-v1.md#Page%203))"),
                              (source_page_id("doc_1"), "1. The sky is blue. (doc_1 v1/e1 passages 0:1)")):
            with self.subTest(page_id=page_id):
                self.assertEqual(projection.project_wiki(page_id)["state"], "completed")
                receipt = fixture.catalog.get_artifact_projection("wiki", page_id, 1)
                self.assertIn("\n" + line + "\n", (self.journal / receipt["projection_path"]).read_text())
        # The plain text shown by the CLI and API keeps the bracketed citation.
        self.assertIn("1. Grass is green. [doc_2 v1/e1 passages 0:1]", fixture.service.show(source_page_id("doc_2"))["managed"])

    def test_wiki_related_links_resolve_once_the_target_is_projected(self):
        from test_wiki import PAGE
        fixture, projection = self.wiki_fixture()
        claims = [{"text": "The sky is blue.", "citations": [fixture.citation]}]
        self.publish(fixture, "topic_sky", "Sky [colour]", claims, links=[PAGE])
        self.publish(fixture, PAGE, "Original", claims, links=["topic_sky"])
        self.assertEqual(projection.project_wiki(PAGE)["state"], "completed")
        page = self.journal / fixture.catalog.get_artifact_projection("wiki", PAGE, 1)["projection_path"]
        self.assertIn("\nRelated: Sky [colour]\n", page.read_text())
        page.write_bytes(page.read_bytes().replace(f"{USER_START}\n\n".encode(), f"{USER_START}\nMine\n".encode()))
        self.assertEqual(projection.project_wiki("topic_sky")["state"], "completed")
        topic = self.journal / fixture.catalog.get_artifact_projection("wiki", "topic_sky", 1)["projection_path"]
        self.assertIn(f"\nRelated: [Original]({quote(page.name)})\n", topic.read_text())
        # Reconcile re-renders the earlier page now that its target exists.
        self.assertEqual(projection.reconcile_documents(documents=[])["state"], "completed")
        self.assertIn(f"\nRelated: [Sky \\[colour\\]]({quote(topic.name)})\n", page.read_text())
        self.assertIn("\nMine\n", page.read_text())
        self.assertEqual(len(fixture.catalog.list_wiki_revisions(PAGE)), 1)
        for target in re.findall(r"\]\(([^)#]+)\)", page.read_text() + topic.read_text()):
            self.assertTrue((page.parent / unquote(target)).is_file(), target)

    def test_new_linked_wiki_pages_resolve_in_one_projection(self):
        from test_wiki import PAGE
        fixture, projection = self.wiki_fixture()
        claims = [{"text": "The sky is blue.", "citations": [fixture.citation]}]
        self.publish(fixture, "topic_sky", "Sky", claims, links=[PAGE])
        self.publish(fixture, PAGE, "Original", claims, links=["topic_sky"])
        self.assertEqual({r["state"] for r in projection.project_wiki_pages([PAGE, "topic_sky"])}, {"completed"})
        page = self.journal / fixture.catalog.get_artifact_projection("wiki", PAGE, 1)["projection_path"]
        self.assertIn("\nRelated: [Sky](Sky.md)\n", page.read_text())

    def test_a_pre_front_matter_file_of_a_real_revision_migrates(self):
        from test_wiki import PAGE

        from lifecontext.wiki import render_managed_v11
        fixture, projection = self.wiki_fixture()
        fixture.update()
        managed = render_managed_v11(fixture.current())
        history = f"wiki/history/{PAGE}-r1.md"
        self.vault.safe_write_bytes(history, managed.encode(), expected_hash=None, artifact_id=PAGE + "_history")
        digest = hashlib.sha256(managed.encode()).hexdigest()
        fixture.catalog.set_artifact_projection("wiki", PAGE + "_history", 1, projection_path=history,
                                                desired_hash=digest, state="projected", last_projected_hash=digest)
        user = f"{USER_START}\nNotes from v1.0.\n<!-- life-context:user:end -->\n"
        text = managed + projection._history_links(PAGE, 1, legacy=True) + user
        self.vault.safe_write_bytes(f"wiki/{PAGE}.md", text.encode(), expected_hash=None, artifact_id=PAGE)
        digest = hashlib.sha256(text.encode()).hexdigest()
        fixture.catalog.set_artifact_projection("wiki", PAGE, 1, projection_path=f"wiki/{PAGE}.md",
                                                desired_hash=digest, state="projected", last_projected_hash=digest)
        result = projection.project_wiki(PAGE)
        self.assertEqual(result["state"], "completed", result)
        self.assertFalse((self.journal / f"wiki/{PAGE}.md").exists())
        page = self.journal / fixture.catalog.get_artifact_projection("wiki", PAGE, 1)["projection_path"]
        self.assertEqual(page.name, "Original.md")
        self.assertTrue(page.read_text().endswith(user))
        self.assertIn("1. The sky is blue.", page.read_text())

    def test_wiki_user_sections_history_and_schema(self):
        self.catalog.create_wiki_page("topic", title="Topic")
        self.catalog.list_wiki_revisions = lambda page_id: [{"revision": 1}]
        self.catalog.record_wiki_review = lambda *args, **kwargs: None
        managed = "<!-- life-context:managed:start -->\n# Topic\n<!-- life-context:managed:end -->\n"
        self.service.wiki_service = SimpleNamespace(show=lambda *args, **render: {
            "revision": {"revision": 1}, "managed": managed}, forgotten=lambda *args, **kwargs: False)
        wiki = self.journal / "wiki"
        wiki.mkdir(mode=0o700)
        schema = wiki / "SCHEMA.md"
        schema.write_text("protected")
        schema.chmod(0o600)
        self.assertEqual(self.service.project_wiki("topic")["state"], "completed")
        receipt = self.catalog.get_artifact_projection("wiki", "topic", 1)
        page = self.journal / receipt["projection_path"]
        text = page.read_text().replace(USER_START + "\n\n", USER_START + "\nMy notes\n")
        page.write_text(text)
        self.assertEqual(self.service.project_wiki("topic")["state"], "completed")
        self.assertIn("My notes", page.read_text())
        history = self.catalog.get_artifact_projection("wiki", "topic_history", 1)
        self.assertTrue((self.journal / history["projection_path"]).exists())
        page.write_text(page.read_text().replace("# Topic", "# Manual"))
        self.assertEqual(self.service.project_wiki("topic")["state"], "conflict")
        self.assertIn("# Manual", page.read_text())
        self.assertEqual(schema.read_text(), "protected")


class ReadableSourceNoteTests(unittest.TestCase):
    def test_note_has_coverage_unread_list_and_page_headings(self):
        from lifecontext.document_projection import _readable_extraction

        payload = {
            "format": "pdf",
            "segments": [
                {"text": "First page\n", "locator": {"kind": "page", "start": 1, "end": 1, "container": None}},
                {"text": "[Model-read page 2 by m; verify against the original]\nNotes\n",
                 "locator": {"kind": "page", "start": 2, "end": 2, "container": "vision:m:page:page:2"}},
            ],
            "warnings": [
                "figure_not_read:1; images or drawings on this page were not read",
                "vision_page:2; read by m; verify against the original",
                "blank_page:3; no visible content",
            ],
        }
        self.assertEqual(
            _readable_extraction(payload),
            [
                "",
                "Coverage: 3 of 3 pages resolved (1 text layer, 0 OCR, 1 model-read, 1 blank)",
                "", "## Not read", "", "- page 1: images or drawings not read",
                "", "## Page 1", "", "First page",
                "", "## Page 2", "", "[Model-read page 2 by m; verify against the original]\nNotes",
            ],
        )
        sheet = _readable_extraction({
            "format": "xlsx",
            "segments": [{"text": "Salary\n", "locator": {"kind": "sheet_cell", "start": 1, "end": 1, "container": "Budget!A1"}}],
            "warnings": [],
        })
        self.assertEqual(sheet[-3:], ["## Sheet Budget", "", "A1: Salary"])


class IndexProjectionTests(unittest.TestCase):
    def test_a_conflicted_index_never_holds_back_a_documents_pages(self):
        service = DocumentProjectionService.__new__(DocumentProjectionService)
        service.catalog = unittest.mock.Mock()
        index = {"page_id": "index", "current_revision": 3, "state": "current"}
        service.catalog.get_wiki_page.return_value = index
        service.catalog.list_wiki_pages_citing.return_value = ["source_doc_1"]
        projected = []

        def project_wiki(page_id):
            projected.append(page_id)
            return {"state": "conflict", "error": "manual_edit"} if page_id == "index" else {"state": "completed"}

        service.catalog.list_artifact_projections.return_value = []
        service.project_document = lambda sid, version: {"state": "completed", "artifacts": []}
        service.project_wiki = project_wiki
        self.assertEqual(service.project_document_with_wiki("doc_1", 1)["state"], "completed")
        self.assertEqual(projected, ["source_doc_1", "index"])
        service.catalog.list_wiki_pages_citing.assert_called_with("doc_1", any_revision=True)
        index["state"] = "conflict"
        projected.clear()
        service.project_document_with_wiki("doc_1", 1)
        self.assertEqual(projected, ["source_doc_1"])
