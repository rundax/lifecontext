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
from lifecontext.paths import LifeContextPaths
from lifecontext.vault import ManagedVault, VaultError
from lifecontext.wiki import USER_END, USER_START, WikiService, source_page_id

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

    def interrupt_the_page_write(self, error):
        """Forget, failing once where the withdrawn page is written after its history files moved."""
        withdrawn = self.import_pair()
        write, armed = ManagedVault.safe_write_bytes, [True]

        def fail_once(vault, path, data, **kwargs):
            if armed[0] and str(path) == "wiki/Withdrawn page.md":
                armed[0] = False
                raise error
            return write(vault, path, data, **kwargs)

        with patch.object(ManagedVault, "safe_write_bytes", fail_once):
            self.curate("forget", withdrawn)
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


if __name__ == "__main__":
    unittest.main()
