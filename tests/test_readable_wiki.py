"""Readable wiki projections retain identity, user sections, and recovery proofs."""

import hashlib
import re
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import unquote

import test_document_projection as fixtures
import yaml

from lifecontext.document_projection import (
    MANAGED_END,
    MANAGED_START,
    USER_END,
    USER_START,
)
from lifecontext.vault import VaultError


class ReadableWikiTests(unittest.TestCase):
    def setUp(self):
        fixtures.ProjectionTests.setUp(self)
        manifest = self.custody.resolve_exact("doc_test", 1).manifest
        self.catalog.record_extraction("doc_test", 1, 1, original_sha256=manifest.sha256,
            extractor="fixture", extractor_version="1", config_sha256="c" * 64,
            artifact_path="derived/fixture.json", artifact_sha256="e" * 64,
            status="completed", passage_count=1)
        self.catalog.record_provider_chunk("wiki_fixture", "doc_test", 1, 1,
            passage_start=0, passage_end=1, content_sha256="b" * 64)
        self.catalog.update_provider_chunk("wiki_fixture", expected_revision=1,
            submission_state="submitted", completion_state="completed")
        for state in ("running", "completed"):
            stage = self.catalog.get_document_stage("doc_test", 1, "indexing")
            self.catalog.transition_document_stage("doc_test", 1, "indexing",
                expected_state=stage["state"], expected_revision=stage["revision"], state=state)
        self.dependency = {"source_id": "doc_test", "source_version": 1, "extraction_revision": 1,
            "passage_start": 0, "passage_end": 1, "original_sha256": manifest.sha256}
        self.revisions = {}
        self.service.wiki_service = SimpleNamespace(
            show=self.show, legacy_managed=lambda page_id, revision: self.revisions[(page_id, revision)],
            forgotten=lambda page_id, revision=None, cache=None: False)

    def add_page(self, page_id="topic_hash", title="Readable topic", revision=1):
        self.catalog.create_wiki_page(page_id, title=title)
        managed = f"{MANAGED_START}\n# {title}\n\n- Evidence {revision}.\n{MANAGED_END}\n"
        digest = hashlib.sha256(managed.encode()).hexdigest()
        self.catalog.commit_wiki_revision(
            page_id, expected_revision=revision - 1,
            artifact_path=f"wiki_revisions/{page_id}/r{revision}.json",
            artifact_sha256=digest, dependencies=[self.dependency],
        )
        self.revisions[(page_id, revision)] = managed
        return managed

    def show(self, page_id, revision=None, **render):
        page = self.catalog.get_wiki_page(page_id)
        revision = page["current_revision"] if revision is None else revision
        return {"page": page, "revision": self.catalog.get_wiki_revision(page_id, revision),
                "managed": self.revisions[(page_id, revision)]}

    def current_path(self, page_id="topic_hash", revision=1):
        return self.journal / self.catalog.get_artifact_projection("wiki", page_id, revision)["projection_path"]

    def legacy(self, page_id="topic_hash", title="Readable topic"):
        managed = self.add_page(page_id, title)
        history = f"wiki/history/{page_id}-r1.md"
        self.vault.safe_write_bytes(history, managed.encode(), expected_hash=None, artifact_id=page_id + "_history")
        digest = hashlib.sha256(managed.encode()).hexdigest()
        self.catalog.set_artifact_projection("wiki", page_id + "_history", 1,
            projection_path=history, desired_hash=digest, state="projected", last_projected_hash=digest)
        path = f"wiki/{page_id}.md"
        user = f"{USER_START}\nPersonal notes — unchanged.\r\n{USER_END}\n"
        text = managed + self.service._history_links(page_id, 1, legacy=True) + user
        self.vault.safe_write_bytes(path, text.encode(), expected_hash=None, artifact_id=page_id)
        digest = hashlib.sha256(text.encode()).hexdigest()
        self.catalog.set_artifact_projection("wiki", page_id, 1,
            projection_path=path, desired_hash=digest, state="projected", last_projected_hash=digest)
        return self.journal / path, self.journal / history, user.encode()

    def test_briefs_log_and_moves_do_not_count_toward_the_page_limit(self):
        rows = [(f"brief_{number:04}", f"wiki/Briefs/{number:04} - Brief.md") for number in range(4096)]
        rows += [("log", "wiki/Log.md")] + [(f"move_{number:064x}", f"wiki/Old {number}.md") for number in range(8)]
        with self.catalog.transaction() as connection:
            connection.executemany(
                "INSERT INTO artifact_projections(layer,artifact_id,revision,projection_path,desired_hash,state) "
                "VALUES('wiki',?,1,?,?,'not_projected')", [(artifact, path, "a" * 64) for artifact, path in rows])
        self.add_page()
        self.add_page("topic_log", "Log")
        self.assertEqual(self.service.project_wiki("topic_hash")["state"], "completed")
        self.assertEqual(self.service.project_wiki("topic_log")["state"], "completed")
        self.assertEqual(self.current_path("topic_log").name, "Log (2).md")

    def test_title_filenames_metadata_and_links(self):
        self.add_page(title="Навігація з телефона")
        result = self.service.project_wiki("topic_hash")
        self.assertEqual(result["state"], "completed", result)
        page = self.current_path()
        self.assertEqual(page.name, "Навігація з телефона.md")
        fields = yaml.safe_load(page.read_text().split("---", 2)[1])
        self.assertEqual(fields["page_id"], "topic_hash")
        self.assertEqual(fields["aliases"], ["topic_hash"])
        history = self.catalog.get_artifact_projection("wiki", "topic_hash_history", 1)
        self.assertEqual(history["projection_path"], "wiki/history/Навігація з телефона - r1.md")
        self.assertTrue((self.journal / history["projection_path"]).is_file())
        for target in re.findall(r"\]\(([^)]+)\)", page.read_text()):
            self.assertTrue((page.parent / unquote(target)).is_file())
        index = self.journal / "indices/wiki.md"
        self.assertIn("Навігація з телефона", index.read_text())
        self.assertIn("%D0", index.read_text())
        self.assertEqual(self.service.project_wiki("topic_hash")["rebuilt"], 0)

    def test_legacy_migration_preserves_user_bytes_and_revision_records(self):
        old, history, user = self.legacy()
        before = self.catalog.list_wiki_revisions("topic_hash")
        self.assertEqual(self.service.project_wiki("topic_hash")["state"], "completed")
        self.assertFalse(old.exists())
        self.assertFalse(history.exists())
        self.assertTrue(self.current_path().read_bytes().endswith(user))
        self.assertEqual(self.catalog.list_wiki_revisions("topic_hash"), before)
        self.assertIn("../wiki/Readable%20topic.md", (self.journal / "indices/wiki.md").read_text())

    def test_document_projection_also_projects_wiki_pages_citing_it(self):
        self.add_page()
        self.assertIsNone(self.catalog.get_artifact_projection("wiki", "topic_hash", 1))
        with patch.object(self.service, "project_document", return_value={"state": "completed"}):
            result = self.service.project_document_with_wiki("doc_test", 1)
        self.assertEqual(result["state"], "completed", result)
        self.assertTrue(self.current_path().is_file())
        with patch.object(self.service, "project_document", return_value={"state": "conflict"}):
            self.assertEqual(self.service.project_document_with_wiki("doc_test", 1)["state"], "conflict")

    def test_duplicate_titles_and_human_file_collision(self):
        self.vault.safe_write_bytes("wiki/Readable topic.md", b"Human note\n", expected_hash=None, artifact_id="human")
        for page_id in ("topic_first", "topic_second"):
            self.add_page(page_id)
            self.assertEqual(self.service.project_wiki(page_id)["state"], "completed")
        self.assertEqual(self.current_path("topic_first").name, "Readable topic (2).md")
        self.assertEqual(self.current_path("topic_second").name, "Readable topic (3).md")
        self.assertEqual((self.journal / "wiki/Readable topic.md").read_bytes(), b"Human note\n")

    def test_managed_edit_does_not_migrate_either_file(self):
        old, history, _ = self.legacy()
        changed = old.read_bytes().replace(b"# Readable topic", b"# Manual edit")
        old.write_bytes(changed)
        result = self.service.project_wiki("topic_hash")
        self.assertEqual(result["state"], "conflict")
        self.assertEqual(old.read_bytes(), changed)
        self.assertTrue(history.exists())
        self.assertFalse((self.journal / "wiki/Readable topic.md").exists())

    def test_user_section_changes_and_new_revision_keep_history_distinct(self):
        self.add_page()
        self.assertEqual(self.service.project_wiki("topic_hash")["state"], "completed")
        page = self.current_path()
        page.write_bytes(page.read_bytes().replace((USER_START + "\n\n").encode(), (USER_START + "\nMy phone notes\n").encode()))
        self.add_page(revision=2)
        result = self.service.project_wiki("topic_hash")
        self.assertEqual(result["state"], "completed", result)
        self.assertEqual(self.current_path(revision=2), page)
        self.assertIn(b"My phone notes", page.read_bytes())
        for revision in (1, 2):
            receipt = self.catalog.get_artifact_projection("wiki", "topic_hash_history", revision)
            self.assertTrue(receipt["projection_path"].endswith(f" - r{revision}.md"))
            self.assertIn(f"Evidence {revision}".encode(), (self.journal / receipt["projection_path"]).read_bytes())

    def test_history_move_failure_can_retry_without_treating_old_links_as_edits(self):
        old, old_history, user = self.legacy()
        remove = self.vault.safe_remove_artifact

        def fail_history_move(path, **kwargs):
            if path == "wiki/history/topic_hash-r1.md" and kwargs.get("staged_name") is None:
                raise VaultError("vault_unavailable")
            return remove(path, **kwargs)

        with patch.object(self.vault, "safe_remove_artifact", side_effect=fail_history_move):
            self.assertEqual(self.service.project_wiki("topic_hash")["state"], "conflict")
        self.assertTrue(old.exists())
        self.assertTrue(old_history.exists())
        result = self.service.project_wiki("topic_hash")
        self.assertEqual(result["state"], "completed", result)
        self.assertTrue(self.current_path().read_bytes().endswith(user))
        self.assertFalse(old.exists())
        self.assertFalse(old_history.exists())

    def test_current_move_crash_resumes_with_user_notes(self):
        old, _, user = self.legacy()
        remove = self.vault.safe_remove_artifact

        def crash_after_publish(path, **kwargs):
            if path == "wiki/topic_hash.md" and kwargs.get("staged_name") is None:
                raise SystemExit("crash")
            return remove(path, **kwargs)

        with patch.object(self.vault, "safe_remove_artifact", side_effect=crash_after_publish), self.assertRaises(SystemExit):
            self.service.project_wiki("topic_hash")
        self.assertTrue(old.exists())
        self.assertTrue(self.current_path().read_bytes().endswith(user))
        result = self.service.project_wiki("topic_hash")
        self.assertEqual(result["state"], "completed", result)
        self.assertFalse(old.exists())

    def test_new_revision_migration_removes_legacy_page(self):
        old, _, user = self.legacy()
        self.add_page(revision=2)
        result = self.service.project_wiki("topic_hash")
        self.assertEqual(result["state"], "completed", result)
        self.assertFalse(old.exists())
        self.assertTrue(self.current_path(revision=2).read_bytes().endswith(user))

    def test_crash_before_new_filename_publication_preserves_legacy_notes(self):
        old, _, user = self.legacy()
        from lifecontext import vault as vault_module

        link = vault_module.os.link

        def crash_before_link(source, destination, **kwargs):
            if destination == "Readable topic.md":
                raise SystemExit("crash")
            return link(source, destination, **kwargs)

        with patch("lifecontext.vault.os.link", side_effect=crash_before_link), self.assertRaises(SystemExit):
            self.service.project_wiki("topic_hash")
        self.assertTrue(old.read_bytes().endswith(user))
        result = self.service.project_wiki("topic_hash")
        self.assertEqual(result["state"], "completed", result)
        self.assertTrue(self.current_path().read_bytes().endswith(user))
        self.assertFalse(old.exists())

    def test_removing_generated_metadata_is_not_a_user_section_edit(self):
        self.add_page("index", title="Index")
        self.assertEqual(self.service.project_wiki("index")["state"], "completed")
        page = self.current_path("index")
        text = page.read_text()
        modified = text[text.index(MANAGED_START):]
        page.write_text(modified)
        self.assertEqual(self.service.project_wiki("index")["state"], "conflict")
        self.assertEqual(page.read_text(), modified)

    def test_same_path_revision_crash_before_replace_recovers(self):
        self.add_page()
        self.assertEqual(self.service.project_wiki("topic_hash")["state"], "completed")
        page = self.current_path()
        first = page.read_bytes().replace((USER_START + "\n\n").encode(), (USER_START + "\nPhone notes\n").encode())
        page.write_bytes(first)
        self.add_page(revision=2)
        from lifecontext import vault as vault_module

        replace = vault_module.os.replace

        def crash_before_replace(source, destination, **kwargs):
            if destination == page.name:
                raise SystemExit("crash")
            return replace(source, destination, **kwargs)

        with patch("lifecontext.vault.os.replace", side_effect=crash_before_replace), self.assertRaises(SystemExit):
            self.service.project_wiki("topic_hash")
        self.assertEqual(page.read_bytes(), first)
        result = self.service.project_wiki("topic_hash")
        self.assertEqual(result["state"], "completed", result)
        self.assertIn(b"Evidence 2", page.read_bytes())
        self.assertIn(b"Phone notes", page.read_bytes())

    def test_interrupted_revision_recovers_after_canonical_revision_advances(self):
        from lifecontext import vault as vault_module

        for renamed in (False, True):
            with self.subTest(renamed=renamed):
                page_id = "topic_renamed" if renamed else "topic_same_path"
                title = "Renamed topic" if renamed else "Same path topic"
                if renamed:
                    old, _, user = self.legacy(page_id, title)
                else:
                    self.add_page(page_id, title)
                    self.assertEqual(self.service.project_wiki(page_id)["state"], "completed")
                    old = self.current_path(page_id)
                    user = f"{USER_START}\nPhone notes\n{USER_END}\n".encode()
                    old.write_bytes(old.read_bytes().replace(
                        f"{USER_START}\n\n{USER_END}\n".encode(), user))
                self.add_page(page_id, title, revision=2)
                operation = vault_module.os.link if renamed else vault_module.os.replace

                def crash(source, destination, *, current_title=title, current_operation=operation, **kwargs):
                    if destination == current_title + ".md":
                        raise SystemExit("crash")
                    return current_operation(source, destination, **kwargs)

                with patch("lifecontext.vault.os." + ("link" if renamed else "replace"), side_effect=crash), self.assertRaises(SystemExit):
                    self.service.project_wiki(page_id)
                self.add_page(page_id, title, revision=3)
                result = self.service.project_wiki(page_id)
                self.assertEqual(result["state"], "completed", result)
                current = self.current_path(page_id, revision=3)
                self.assertIn(b"Evidence 3", current.read_bytes())
                self.assertTrue(current.read_bytes().endswith(user))
                if renamed:
                    self.assertFalse(old.exists())

    def test_schema_title_and_unicode_equivalent_names_are_reserved(self):
        schema = self.journal / "wiki/SCHEMA.md"
        schema.parent.mkdir(mode=0o700)
        schema.write_bytes(b"Protected schema\n")
        schema.chmod(0o600)
        self.add_page(title="SCHEMA")
        self.assertEqual(self.service.project_wiki("topic_hash")["state"], "completed")
        self.assertEqual(self.current_path().name, "SCHEMA (2).md")
        self.assertEqual(schema.read_bytes(), b"Protected schema\n")
        self.add_page("topic_accent", title="Café")
        human = self.journal / "wiki/Cafe\u0301.md"
        human.write_bytes(b"Human\n")
        human.chmod(0o600)
        self.assertEqual(self.service.project_wiki("topic_accent")["state"], "completed")
        self.assertEqual(self.current_path("topic_accent").name, "Café (2).md")

    def test_unsafe_and_long_titles_are_sanitized(self):
        self.add_page(title="../../Unsafe [title]: " + "é" * 100)
        result = self.service.project_wiki("topic_hash")
        self.assertEqual(result["state"], "completed", result)
        name = self.current_path().name
        self.assertLessEqual(len(name.encode()), 183)
        self.assertNotIn("[", name)
        self.assertNotIn(":", name)
        self.assertFalse(name.startswith("."))

    def test_reserved_windows_titles_are_readable_and_writable(self):
        for number, title in enumerate(("CONIN$", "CONOUT$", "COM¹", "LPT²")):
            page_id = f"topic_reserved_{number}"
            self.add_page(page_id, title)
            result = self.service.project_wiki(page_id)
            self.assertEqual(result["state"], "completed", result)
            self.assertTrue(self.current_path(page_id).name.startswith("Wiki page - "))
