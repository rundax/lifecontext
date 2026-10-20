"""Forgetting a document withdraws its text from every wiki file and from later model requests."""
import asyncio
import json
import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from test_document_bundle_application import Provider
from wiki_fake_compiler import FakeCompiler

from lifecontext.application import ApplicationError, JournalApplication
from lifecontext.brief import render_brief
from lifecontext.custody import CustodyError
from lifecontext.paths import LifeContextPaths
from lifecontext.vault import ManagedVault, VaultError
from lifecontext.wiki import (
    ERASED,
    USER_END,
    USER_START,
    WikiPatch,
    WikiRevisionStore,
    WikiService,
    erase_patch,
    source_page_id,
)

TOPICS = {"coffee": ("topic", "Coffee"), "copywriting": ("topic", "Copywriting")}
# What the forgotten document of ``import_pair`` says, is called and names.
SECRETS = ("Coffee is a stimulant.", "stimulant", "Copywriting", "persuasive", "Secret notes")


class ForgetWithdrawsWikiTextTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        home = Path(temporary.name) / "home"
        home.mkdir(mode=0o700)
        vault = home / "vault"
        self.journal = vault / "Journal"
        (self.journal / "entries").mkdir(parents=True, mode=0o700)
        vault.chmod(0o700)
        self.journal.chmod(0o700)
        self.compiler = FakeCompiler(TOPICS)
        with patch.dict(os.environ, {"HOME": str(home)}):
            self.app = JournalApplication(paths=LifeContextPaths.current(), hindsight=Provider(),
                                          vault=ManagedVault(vault, self.journal), compiler=self.compiler)
        self.addCleanup(self.app.close)

    def import_text(self, filename, text, key):
        outcome = asyncio.run(self.app.import_document(text.encode(), filename, "text/plain", {"source": "fixture"}, key))
        self.assertEqual(outcome["document"]["stages"]["wiki"]["state"], "completed", outcome)
        return outcome["document"]["source_id"]

    def wiki_files(self):
        return {path: path.read_text() for directory in ("wiki", "indices") for path in (self.journal / directory).rglob("*.md")}

    def import_pair(self):
        """A document that will be forgotten, sharing the Coffee page and a contradiction with another."""
        withdrawn = self.import_text("Secret notes.txt", "Coffee is a stimulant. Copywriting is persuasive writing.", "x")
        self.import_text("Other notes.txt", "Coffee is a sedative.", "y")
        return withdrawn

    def curate(self, action, source_id, version=1):
        prepared = asyncio.run(self.app.curate({"action": f"prepare_{action}", "source_id": source_id,
                                                "expected_version": version}, idempotency_key=f"prepare-{action}"))
        result = asyncio.run(self.app.curate({"action": action, "source_id": source_id, "expected_version": version,
                                              "prepared_operation_id": prepared["operation_id"]}, idempotency_key=action))
        self.assertEqual(result["state"], "completed", result)

    def assert_vault_withdrawn(self, *secrets):
        """No wiki file or file name holds the secrets, and the withdrawn pages carry neutral names."""
        files = self.wiki_files()
        names = sorted(str(path.relative_to(self.journal)) for path in files)
        for secret in secrets:
            for path, text in files.items():
                self.assertNotIn(secret, str(path.relative_to(self.journal)), names)
                self.assertNotIn(secret, text, path)
        self.assertIn("wiki/Withdrawn page.md", names)
        self.assertIn("wiki/history/Withdrawn page - r1.md", names)
        self.assertIn("- [Withdrawn page](../wiki/Withdrawn%20page.md)\n", files[self.journal / "indices" / "wiki.md"])
        return files

    def test_reviews_brief_and_lint_leave_out_a_forgotten_documents_contradiction(self):
        withdrawn = self.import_pair()
        reviews = self.app.list_wiki_reviews()
        self.assertEqual(len(reviews["reviews"]), 1)
        self.assertIn("Coffee is a stimulant.", json.dumps(reviews))
        self.curate("forget", withdrawn)
        self.assertEqual(self.app.list_wiki_reviews(), {"reviews": [], "omitted": 0})
        self.assertEqual(self.app.list_wiki_reviews(page_id="topic_coffee"), {"reviews": [], "omitted": 0})
        # Hidden, never resolved for the owner: it stays pending in the catalog and cannot be resolved by ID.
        pending, _ = self.app.catalog.find_wiki_reviews(state="pending")
        self.assertEqual([review["code"] for review in pending], ["contradiction"])
        with self.assertRaises(ApplicationError) as error:
            self.app.resolve_wiki_review(pending[0]["review_id"], note="Seen.", idempotency_key="resolve-hidden")
        self.assertEqual(error.exception.code, "source_not_found")
        brief = self.app.brief(since=datetime.now(UTC) - timedelta(hours=1))
        self.assertEqual(brief["contradictions"], {"items": [], "omitted": 0})
        self.assertEqual(brief["open_now"]["reviews_pending"], 0)
        _, note, log = self.app.prepare_brief()
        rendered = json.dumps(brief) + render_brief(brief) + note + log
        for secret in SECRETS:
            self.assertNotIn(secret, rendered)
        self.assertIn("a forgotten source", rendered)
        # A forgotten page cannot be rebuilt and its review is hidden: lint names neither.
        lint = self.app.wiki_lint()
        for finding in lint["findings"]:
            self.assertNotIn(finding["code"], {"contradiction_pending", "page_stale"}, finding)
            self.assertNotIn(withdrawn, json.dumps(finding))
            self.assertNotEqual(finding["page_id"], "topic_copywriting", finding)

    def test_a_forgotten_document_takes_no_new_version(self):
        withdrawn = self.import_text("Secret notes.txt", "Coffee is a stimulant.", "x")
        self.curate("forget", withdrawn)
        with self.assertRaises(ApplicationError) as error:
            asyncio.run(self.app.import_document(b"Tea is calming.", "New.txt", "text/plain", {"source": "fixture"},
                                                 "replace", source_id=withdrawn, source_version=2))
        self.assertEqual(error.exception.code, "invalid_request")
        self.assertIsNone(self.app.catalog.get_manifest(withdrawn, 2))
        self.assertNotIn("stimulant", self.app.show_wiki(source_page_id(withdrawn), 1)["managed"])

    def test_a_forgotten_version_stays_withdrawn_when_a_later_one_is_live(self):
        sid = self.import_text("Notes.txt", "Coffee is a stimulant.", "x")
        outcome = asyncio.run(self.app.import_document(b"Coffee is bitter.", "Notes.txt", "text/plain",
                                                       {"source": "fixture"}, "x2", source_id=sid, source_version=2))
        self.assertEqual(outcome["document"]["stages"]["wiki"]["state"], "completed", outcome)
        # Version 1 forgotten and version 2 live: what a replacement accepted after a forget left behind.
        self.app.catalog.set_provider_state(sid, 1, recall_visibility="not_visible")
        self.assertFalse(self.app.wiki.source_withdrawn(sid))
        self.assertTrue(self.app.wiki.source_withdrawn(sid, 1))
        first = self.app.show_wiki(source_page_id(sid), 1)
        self.assertEqual((first["withdrawn"], first["title"], first["page"]["title"]),
                         ("forgotten", "Withdrawn page", "Withdrawn page"))
        self.assertNotIn("stimulant", first["managed"])
        self.assertIn("Coffee is bitter.", self.app.show_wiki(source_page_id(sid))["managed"])
        report = self.app.reconcile_projections(limit=4096)
        self.assertEqual(report["failed"], 0, report)
        text = "\n".join(self.wiki_files().values())
        self.assertNotIn("stimulant", text)
        self.assertIn("Coffee is bitter.", text)

    def test_a_forget_renames_withdrawn_pages_and_rewrites_the_vault_at_once(self):
        withdrawn = self.import_pair()
        own = self.journal / "wiki" / "Secret notes.md"
        own.write_text(own.read_text().replace(f"{USER_START}\n\n{USER_END}", f"{USER_START}\nMy own words.\n{USER_END}"))
        self.curate("forget", withdrawn)
        titles = [page["title"] for page in self.app.list_wiki()]
        self.assertEqual(sorted(titles), ["Coffee", "Index", "Other notes", "Withdrawn page", "Withdrawn page"])
        self.assertEqual([page["title"] for page in self.app.wiki_status()["pages"]], titles)
        self.assertEqual(self.app.show_wiki(source_page_id(withdrawn))["page"]["title"], "Withdrawn page")
        self.assertEqual(self.app.show_wiki("topic_copywriting")["page"]["title"], "Withdrawn page")
        # No reconcile: the forget itself rewrote every page that ever cited the document, and the index.
        files = self.assert_vault_withdrawn(*SECRETS)
        mine = [text for text in files.values() if f"page_id: {source_page_id(withdrawn)}\n" in text]
        self.assertEqual(len(mine), 2)
        self.assertIn("My own words.", "".join(mine))
        self.assertIn("Coffee is a sedative.", "\n".join(files.values()))
        # Reconciling again moves and rewrites nothing.
        report = self.app.reconcile_projections(limit=4096)
        self.assertEqual(report["failed"], 0, report)
        self.assertEqual(self.wiki_files(), files)

    def test_a_hand_edited_history_file_stays_and_never_holds_its_page_back(self):
        withdrawn = self.import_pair()
        edited = self.journal / "wiki" / "history" / "Secret notes - r1.md"
        text = edited.read_text().replace("Coffee is a stimulant.", "Coffee is a stimulant, I noted.", 1)
        self.assertIn("I noted", text)
        edited.write_text(text)
        self.curate("forget", withdrawn)
        self.assertEqual(edited.read_text(), text)
        own = sorted(path.relative_to(self.journal).as_posix() for path, body in self.wiki_files().items()
                     if path != edited and f"page_id: {source_page_id(withdrawn)}\n" in body)
        self.assertEqual(own, ["wiki/Withdrawn page.md"])
        page = (self.journal / own[0]).read_text()
        self.assertIn("Withdrawn: this page's sources were forgotten.", page)
        for secret in ("stimulant", "persuasive", "title: Secret notes", "Secret%20notes"):
            self.assertNotIn(secret, page)
        # The edited history file keeps its name, so the page names that revision without a link.
        self.assertIn("\nHistory: r1\n", page)

    def interrupt_the_page_write(self, error, action="forget"):
        """Forget or delete, failing once where the withdrawn page is written after its history files moved."""
        withdrawn = self.import_pair()
        write, armed = ManagedVault.safe_write_bytes, [True]

        def fail_once(vault, path, data, **kwargs):
            if armed[0] and str(path) == "wiki/Withdrawn page.md":
                armed[0] = False
                raise error
            return write(vault, path, data, **kwargs)

        with patch.object(ManagedVault, "safe_write_bytes", fail_once):
            self.curate(action, withdrawn)
        self.assertFalse(armed[0])
        self.assertTrue((self.journal / "wiki" / "Secret notes.md").exists())
        for _ in range(2):
            report = self.app.reconcile_projections(limit=4096)
            self.assertEqual(report["failed"], 0, report)
        # No hand edit was made, so no review is opened and the page is not left in conflict.
        self.assertEqual(self.app.catalog.find_wiki_reviews()[1], 1)
        self.assertNotEqual(self.app.catalog.get_wiki_page(source_page_id(withdrawn))["state"], "conflict")
        self.assert_vault_withdrawn(*SECRETS)

    def test_a_failed_page_write_after_the_history_moved_is_repaired_by_reconcile(self):
        self.interrupt_the_page_write(VaultError("vault_unavailable"))

    def test_a_process_stopped_after_the_history_moved_is_repaired_by_reconcile(self):
        self.interrupt_the_page_write(RuntimeError("process stopped"))

    def test_a_delete_whose_page_write_failed_is_repaired_after_the_title_was_withdrawn(self):
        # The erasure had already given the page the withdrawn title when the file, still
        # naming the document, is written again: not a hand edit, and the text goes.
        self.interrupt_the_page_write(VaultError("vault_unavailable"), action="delete")
        self.assertEqual(sum(page["title"] == "Withdrawn page" for page in self.app.catalog.list_wiki_pages()
                             if page["page_id"].startswith("source_")), 1)

    def test_a_forgotten_document_is_never_indexed_again(self):
        withdrawn = self.import_text("Secret notes.txt", "Coffee is a stimulant.", "x")
        self.curate("forget", withdrawn)
        with self.assertRaises(ApplicationError) as error:
            asyncio.run(self.app.curate({"action": "reindex", "source_id": withdrawn}, idempotency_key="reindex"))
        self.assertEqual(error.exception.code, "conflict")
        self.assertEqual({chunk["submission_state"] for chunk in self.app.catalog.list_provider_chunks(withdrawn, 1)},
                         {"retired"})
        self.assertEqual(self.app.catalog.get_provider_state(withdrawn, 1)["recall_visibility"], "not_visible")

    def test_recall_never_cites_a_forgotten_version(self):
        sid = self.import_text("Secret notes.txt", "Coffee is a stimulant.", "x")
        chunk = self.app.catalog.list_provider_chunks(sid, 1)[0]
        self.assertEqual(self.app._resolve_provider_reference({"document_id": chunk["provider_id"]})["source_id"], sid)
        # A version that can no longer be recalled, with a chunk the provider still returns.
        self.app.catalog.set_provider_state(sid, 1, recall_visibility="not_visible")
        with self.assertRaises(ApplicationError) as error:
            self.app._resolve_provider_reference({"document_id": chunk["provider_id"]})
        self.assertEqual(error.exception.code, "evidence_unavailable")

    def test_a_delete_rewrites_the_vault_at_once(self):
        deleted = self.import_pair()
        self.curate("delete", deleted)
        self.assertEqual(self.app.list_wiki_reviews(), {"reviews": [], "omitted": 0})
        files = self.assert_vault_withdrawn(*SECRETS)
        self.assertIn("Coffee is a sedative.", "\n".join(files.values()))
        listed = json.dumps(self.app.list_documents(limit=100))
        self.assertNotIn(deleted, listed)
        self.assertNotIn("Secret notes", listed)
        self.assertIn("Other notes", listed)
        # A deleted document is not found, like one never imported.
        for source_id in (deleted, "doc_never_imported"):
            with self.subTest(source_id=source_id), self.assertRaises(ApplicationError) as error:
                self.app.show_document(source_id)
            self.assertEqual(error.exception.code, "source_not_found")

    def home_files_holding(self, *needles):
        """Every file under the application's home, the vault, the extraction artifacts and the
        catalog file included, that holds any needle."""
        home = self.app.paths.lifeweave_home.parent
        return sorted(str(path.relative_to(home)) for path in home.rglob("*")
                      if path.is_file() and any(needle.encode() in path.read_bytes() for needle in needles))

    def revision_rows(self):
        return {(page["page_id"], row["revision"]): row
                for page in self.app.catalog.list_wiki_pages() for row in self.app.catalog.list_wiki_revisions(page["page_id"])}

    def test_a_delete_erases_the_documents_text_from_revisions_and_reviews(self):
        quotes = ("Coffee is a stimulant.", "Copywriting is persuasive writing.", "stimulant", "persuasive")
        deleted = self.import_pair()
        catalog, store = self.app.catalog, self.app.wiki.store
        other_page = next(page["page_id"] for page in catalog.list_wiki_pages()
                          if page["page_id"].startswith("source_") and page["page_id"] != source_page_id(deleted))
        other_id = other_page.removeprefix("source_")
        before = self.revision_rows()
        self.assertEqual(len(catalog.list_wiki_revisions("topic_coffee")), 2)
        # Its own page, a shared page with history and a contradiction review quote it.
        stored = "\n".join(path.read_text() for path in (self.app.paths.lifeweave_home / "wiki_revisions").rglob("*.json"))
        for quote in quotes[:2]:
            self.assertIn(quote, stored)
        self.assertIn("Coffee is a stimulant.", json.dumps(catalog.find_wiki_reviews()[0]))
        self.assertEqual(catalog.get_wiki_page(source_page_id(deleted))["title"], "Secret notes")
        derived = self.app.paths.lifeweave_home / "derived"
        self.assertEqual(sorted(path.name for path in derived.iterdir()), sorted([deleted, other_id]))
        self.curate("delete", deleted)
        self.assertEqual(self.home_files_holding(*quotes), [])
        # The extracted text goes with the document; another document's stays.
        self.assertEqual([path.name for path in derived.iterdir()], [other_id])
        # The document's own page takes the title it is shown under, in the catalog and in its artifacts.
        self.assertEqual(catalog.get_wiki_page(source_page_id(deleted))["title"], "Withdrawn page")
        revisions = self.app.paths.lifeweave_home / "wiki_revisions"
        self.assertNotIn("Secret notes", "\n".join(path.read_text() for path in revisions.rglob("*.json")))
        self.assertIn("Withdrawn page", "\n".join(path.read_text() for path in (revisions / source_page_id(deleted)).glob("*.json")))
        after = self.revision_rows()
        text = json.dumps([catalog.find_wiki_reviews()[0], list(after.values())]) + "\n".join(catalog._connection.iterdump())
        for quote in quotes:
            self.assertNotIn(quote, text)
        # The shape stays: the same revisions and dependencies, each read through its new checksum.
        self.assertEqual({key: (after[key]["parent_revision"], after[key]["dependencies"]) for key in before},
                         {key: (row["parent_revision"], row["dependencies"]) for key, row in before.items()})
        for (page_id, revision), row in after.items():
            self.assertEqual(store.read(page_id, revision, row["artifact_sha256"]).page_id, page_id)
        self.assertNotEqual(after["topic_coffee", 2]["artifact_sha256"], before["topic_coffee", 2]["artifact_sha256"])
        # What other documents said is untouched, byte for byte.
        self.assertEqual(after[other_page, 1]["artifact_sha256"], before[other_page, 1]["artifact_sha256"])
        self.assertIn("Coffee is a sedative.", self.app.show_wiki("topic_coffee")["managed"])
        self.assertIn("Coffee is a sedative.", self.app.show_wiki(other_page)["managed"])
        self.assertEqual([review["code"] for review in catalog.find_wiki_reviews()[0]], ["contradiction"])
        # The vault files written before the erasure are what a fresh render gives, and the checks pass.
        files = self.wiki_files()
        report = self.app.reconcile_projections(limit=4096)
        self.assertEqual(report["failed"], 0, report)
        self.assertEqual(self.wiki_files(), files)
        # An export refuses or leaves the text out.
        try:
            exported = self.app.export_bundle()
        except ApplicationError:
            exported = b""
        for quote in quotes:
            self.assertNotIn(quote.encode(), exported)
        # Repeating the erasure changes nothing, and removes an artifact an interrupted one left behind.
        directory = self.app.paths.lifeweave_home / "wiki_revisions" / "topic_coffee"
        (directory / f"r2-{'0' * 64}.json").write_text(quotes[0])
        self.app.wiki.erase_source(deleted)
        self.assertEqual(sorted(path.name for path in directory.iterdir()),
                         sorted(f"r{n}-{row['artifact_sha256']}.json" for (page, n), row in after.items() if page == "topic_coffee"))
        self.assertEqual({key: row["artifact_sha256"] for key, row in self.revision_rows().items()},
                         {key: row["artifact_sha256"] for key, row in after.items()})
        self.assertEqual(self.home_files_holding(*quotes), [])

    def test_a_reconcile_erases_what_an_earlier_delete_left_and_repeats_safely(self):
        quotes = ("Coffee is a stimulant.", "Copywriting is persuasive writing.", "stimulant", "persuasive")
        # A delete finished before the erasure existed: its text is still stored.
        with patch.object(JournalApplication, "_erase_deleted_text"):
            deleted = self.import_pair()
            self.curate("delete", deleted)
        self.assertNotEqual(self.home_files_holding(*quotes), [])
        self.assertEqual(self.app.catalog.get_wiki_page(source_page_id(deleted))["title"], "Secret notes")
        files = self.wiki_files()
        report = self.app.reconcile_projections(limit=4096)
        self.assertEqual(report["failed"], 0, report)
        self.assertEqual(self.home_files_holding(*quotes), [])
        self.assertEqual(self.app.catalog.get_wiki_page(source_page_id(deleted))["title"], "Withdrawn page")
        self.assertFalse((self.app.paths.lifeweave_home / "derived" / deleted).exists())
        self.assertEqual(self.wiki_files(), files)
        digests = {key: row["artifact_sha256"] for key, row in self.revision_rows().items()}
        # A document that was erased is not scanned again.
        with patch.object(WikiService, "erase_source") as erase:
            self.assertEqual(self.app.reconcile_projections(limit=4096)["failed"], 0)
        erase.assert_not_called()
        self.assertEqual({key: row["artifact_sha256"] for key, row in self.revision_rows().items()}, digests)
        self.assertIn("Coffee is a sedative.", self.app.show_wiki("topic_coffee")["managed"])

    def test_a_missing_or_corrupt_revision_file_does_not_hold_a_delete_back(self):
        deleted = self.import_pair()
        directory = self.app.paths.lifeweave_home / "wiki_revisions" / "topic_coffee"
        files = sorted(directory.glob("r*.json"))
        self.assertEqual(len(files), 2)
        files[0].unlink()
        files[1].write_text("not a revision")
        with self.assertLogs("lifecontext.wiki", "WARNING") as logs:
            self.curate("delete", deleted)
        self.assertTrue(any("has no file" in line for line in logs.output), logs.output)
        self.assertTrue(any("is corrupt" in line for line in logs.output), logs.output)
        # The corrupt file is removed rather than left holding text; the missing one has nothing to remove.
        self.assertEqual(sorted(path.name for path in directory.glob("r*.json")), sorted(
            f"r{revision}-{row['artifact_sha256']}.json" for (page, revision), row in self.revision_rows().items()
            if page == "topic_coffee" and revision > 2))
        self.assertEqual({item["progress"]["custody_delete"] for item in self.app.catalog.list_tombstones(deleted)}, {"completed"})
        self.assertEqual(self.app.catalog.get_wiki_page(source_page_id(deleted))["title"], "Withdrawn page")
        # The files that cannot be read stay as they are; every other stored revision is erased.
        held = self.home_files_holding("Coffee is a stimulant.", "persuasive")
        self.assertEqual([path for path in held if "wiki_revisions" in path and "topic_coffee" not in path], [])

    def test_a_read_failure_that_is_not_a_missing_or_corrupt_file_leaves_the_delete_for_the_retry(self):
        deleted = self.import_pair()
        with patch.object(WikiRevisionStore, "read", side_effect=CustodyError("unsafe custody file")), \
                self.assertRaises(Exception):  # noqa: B017
            self.curate("delete", deleted)
        self.assertEqual({item["progress"]["custody_delete"] for item in self.app.catalog.list_tombstones(deleted)}, {"running"})
        self.assertEqual(self.app.catalog.get_wiki_page(source_page_id(deleted))["title"], "Secret notes")
        asyncio.run(self.app._delete_confirmed_source(deleted, self.app.catalog.list_source_manifests(deleted)))
        self.assertEqual({item["progress"]["custody_delete"] for item in self.app.catalog.list_tombstones(deleted)}, {"completed"})
        # The vault pages the failed run could not write are repaired by a reconcile.
        self.assertEqual(self.app.reconcile_projections(limit=4096)["failed"], 0)
        self.assertEqual(self.home_files_holding("Coffee is a stimulant.", "persuasive"), [])

    def test_a_withdrawn_pages_hand_edited_block_is_a_conflict_but_a_stale_title_is_not(self):
        deleted = self.import_pair()
        self.curate("delete", deleted)
        page_id = source_page_id(deleted)
        path = self.journal / "wiki" / "Withdrawn page.md"
        text = path.read_text()
        self.assertIn("title: Withdrawn page\n", text)
        # A file still naming the document, as when the projection after the delete was deferred.
        path.write_text(text.replace("title: Withdrawn page\n", "title: Secret notes\n"))
        self.assertEqual(self.app.document_projections.project_wiki(page_id)["state"], "completed")
        self.assertEqual(path.read_text(), text)
        # An edit of the generated block is still a manual edit.
        edited = text.replace("Withdrawn: this page's sources were forgotten.", "Withdrawn: edited by hand.")
        self.assertNotEqual(edited, text)
        path.write_text(edited)
        self.assertEqual(self.app.document_projections.project_wiki(page_id), {"state": "conflict", "error": "managed_edit"})
        self.assertEqual(path.read_text(), edited)
        self.assertEqual([review["code"] for review in self.app.catalog.list_wiki_reviews(page_id)
                          if review["code"] == "manual_edit"], ["manual_edit"])

    def test_a_failed_erasure_leaves_the_delete_unfinished_for_the_retry(self):
        deleted = self.import_pair()
        with patch.object(WikiService, "erase_source", side_effect=OSError("disk")), self.assertRaises(Exception):  # noqa: B017
            self.curate("delete", deleted)
        self.assertEqual({item["progress"]["custody_delete"] for item in self.app.catalog.list_tombstones(deleted)}, {"running"})
        # A reconcile does not touch a delete still under way.
        self.assertEqual(self.app.reconcile_projections(limit=4096)["failed"], 0)
        self.assertNotEqual(self.home_files_holding("Coffee is a stimulant."), [])
        manifests = self.app.catalog.list_source_manifests(deleted)
        asyncio.run(self.app._delete_confirmed_source(deleted, manifests))
        self.assertEqual({item["progress"]["custody_delete"] for item in self.app.catalog.list_tombstones(deleted)}, {"completed"})
        self.assertEqual(self.home_files_holding("Coffee is a stimulant.", "persuasive"), [])

    def test_a_delete_rewrites_the_briefs_that_name_the_document(self):
        deleted = self.import_pair()
        self.app.project_brief()
        briefs = sorted((self.journal / "wiki" / "Briefs").glob("*.md"))
        self.assertEqual(len(briefs), 1)
        self.assertIn("Secret notes", briefs[0].read_text())
        self.curate("delete", deleted)
        self.assert_vault_withdrawn(*SECRETS)
        for path in (briefs[0], self.journal / "wiki" / "Log.md"):
            self.assertNotIn(deleted, path.read_text())
        self.assertIn("Other notes", briefs[0].read_text())

    def test_a_delete_rewrites_briefs_that_quote_the_document_without_naming_it(self):
        deleted = self.import_text("Secret notes.txt", "Coffee is a stimulant. Copywriting is persuasive writing.", "x")
        with self.app.catalog.transaction() as connection:
            connection.execute("UPDATE document_stages SET updated_at=datetime('now','-2 days')")
            connection.execute("UPDATE wiki_revisions SET created_at=datetime('now','-2 days')")
        earlier = (datetime.now(UTC) - timedelta(days=2)).astimezone().date()
        self.import_text("Other notes.txt", "Coffee is a sedative.", "y")
        self.app.project_brief(day=earlier)
        self.app.project_brief()
        first, today = sorted((self.journal / "wiki" / "Briefs").glob("*.md"))
        self.assertIn(deleted, first.read_text())
        # Today's brief quotes the document in a contradiction without naming it.
        self.assertIn("Coffee is a stimulant.", today.read_text())
        self.assertNotIn(deleted, today.read_text())
        with patch.object(WikiService, "lint", autospec=True, side_effect=WikiService.lint) as lint:
            self.curate("delete", deleted)
        self.assertEqual(lint.call_count, 1)
        for path in (first, today, self.journal / "wiki" / "Log.md"):
            text = path.read_text()
            for secret in (deleted, *SECRETS):
                self.assertNotIn(secret, text, path)
        self.assertIn("Other notes", today.read_text())

    def test_forget_then_reconcile_leaves_no_quote_in_any_wiki_file_or_later_request(self):
        forgotten = self.import_text("Secret notes.txt", "Coffee is a stimulant. Copywriting is persuasive writing.", "x")
        self.import_text("Other notes.txt", "Coffee is a sedative.", "y")
        before = "\n".join(self.wiki_files().values())
        for text in ("Coffee is a stimulant.", "Copywriting is persuasive writing.", "Coffee is a sedative."):
            self.assertIn(text, before)
        self.assertIn("> [!note] Contradiction", before)
        prepared = asyncio.run(self.app.curate({"action": "prepare_forget", "source_id": forgotten, "expected_version": 1},
                                               idempotency_key="prepare-forget"))
        result = asyncio.run(self.app.curate({"action": "forget", "source_id": forgotten, "expected_version": 1,
                                              "prepared_operation_id": prepared["operation_id"]}, idempotency_key="forget"))
        self.assertEqual(result["state"], "completed")
        report = self.app.reconcile_projections(limit=4096)
        self.assertEqual(report["failed"], 0, report)
        files = self.wiki_files()
        self.assertTrue(any("/history/" in str(path) for path in files))
        for path, text in files.items():
            for secret in ("Coffee is a stimulant.", "Copywriting is persuasive writing.", "title: Secret notes"):
                self.assertNotIn(secret, text, path)
        after = "\n".join(files.values())
        self.assertIn("Coffee is a sedative.", after)
        self.assertIn("Withdrawn: this page's sources were forgotten.", after)
        self.assertNotIn("[Secret notes]", files[self.journal / "indices" / "wiki.md"])
        self.import_text("Third notes.txt", "Coffee is bitter.", "z")
        query = self.compiler.queries["routing"][-1]
        self.assertNotIn("stimulant", query)
        self.assertNotIn("Copywriting", query)
        self.assertIn("Coffee is a sedative.", query)


class ErasePatchTests(unittest.TestCase):
    @staticmethod
    def citation(source_id, quote):
        return {"source_id": source_id, "source_version": 1, "extraction_revision": 1, "passage_start": 0,
                "passage_end": 1, "original_sha256": "a" * 64, "quote": quote}

    def test_only_the_deleted_documents_text_goes_and_the_shape_stays(self):
        gone, live = self.citation("doc_gone", "Tea is calming. Rain is wet."), self.citation("doc_live", "Rain is wet. Ice is cold.")
        claims = [{"text": "Tea is calming.", "citations": [gone]},
                  {"text": "Rain is wet.", "citations": [gone, live]},
                  {"text": "Ice is cold.", "citations": [live]}]
        value = {"page_id": "topic_weather", "title": "Weather", "expected_revision": 0, "claims": claims,
                 "contradictions": [{"left": claims[0], "right": claims[2], "left_date": "2026-01-01", "right_date": "2026-01-02"}],
                 "links": ["source_doc_gone"],
                 "summary": [{"text": "Tea calms.", "claims": [1]}, {"text": "Ice is cold.", "claims": [3]}]}
        patch = WikiPatch.parse(json.dumps(value))
        erased = erase_patch(patch, "doc_gone")
        self.assertEqual(erase_patch(erased, "doc_gone"), erased)
        self.assertEqual([claim.text for claim in erased.claims], [ERASED, "Rain is wet.", "Ice is cold."])
        # The shared quote is kept for the live citation only; the live claim is byte for byte what it was.
        self.assertEqual([[c.quote for c in claim.citations] for claim in erased.claims],
                         [[ERASED], [ERASED, live["quote"]], [live["quote"]]])
        self.assertEqual(erased.claims[2], patch.claims[2])
        self.assertEqual([c.dependency() for claim in erased.claims for c in claim.citations],
                         [c.dependency() for claim in patch.claims for c in claim.citations])
        self.assertEqual((erased.contradictions[0].left.text, erased.contradictions[0].right), (ERASED, patch.claims[2]))
        self.assertEqual([sentence.text for sentence in erased.summary], ["Ice is cold."])
        self.assertEqual((erased.title, erased.links, erased.expected_revision), (patch.title, patch.links, 0))
        self.assertEqual(erase_patch(patch, "doc_other"), patch)
        # The document's own page takes the withdrawn title; a shared page keeps its topic.
        own = WikiPatch.parse(json.dumps({**value, "page_id": source_page_id("doc_gone"), "title": "Secret notes"}))
        self.assertEqual(erase_patch(own, "doc_gone").title, "Withdrawn page")
        self.assertEqual(erase_patch(erase_patch(own, "doc_gone"), "doc_gone"), erase_patch(own, "doc_gone"))
        self.assertEqual(erase_patch(own, "doc_other").title, "Secret notes")


if __name__ == "__main__":
    unittest.main()
