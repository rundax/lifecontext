"""Model-free daily brief and change log over catalog history."""
import hashlib
import json
import os
import tempfile
import time
import unittest
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from zoneinfo import ZoneInfo

from local_paths import scratch_root

from lifecontext.application import ApplicationError, JournalApplication
from lifecontext.brief import render_brief, render_log
from lifecontext.catalog import LifeContextCatalog
from lifecontext.custody import SourceManifest
from lifecontext.derived import DerivedStore
from lifecontext.document_projection import DocumentProjectionService
from lifecontext.paths import LifeContextPaths
from lifecontext.vault import ManagedVault
from lifecontext.wiki import Citation, WikiPatch, WikiService, source_page_id

DAY = date(2026, 10, 16)
VIENNA = ZoneInfo("Europe/Vienna")


class BriefTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=scratch_root())
        self.base = Path(self.temp.name)
        self.paths = LifeContextPaths(self.base / "home")
        self.paths.home.mkdir(mode=0o700)
        self.catalog = LifeContextCatalog(self.paths)
        self.derived = DerivedStore(self.paths)
        self.wiki = WikiService(self.catalog, Mock(), self.derived, SimpleNamespace(), self.paths)
        self.vault_root = self.base / "vault"
        self.journal = self.vault_root / "Journal"
        self.journal.mkdir(mode=0o700, parents=True)
        self.vault_root.chmod(0o700)
        self.vault = ManagedVault(self.vault_root, self.journal)
        self.citations = {}

    def tearDown(self):
        self.vault.close()
        self.catalog.close()
        self.temp.cleanup()

    def execute(self, sql, parameters=()):
        with self.catalog.transaction() as connection:
            connection.execute(sql, parameters)

    def add_document(self, sid, text, *, title="Book", accepted_at="2026-10-16 08:00:00"):
        raw = text.encode()
        digest = hashlib.sha256(raw).hexdigest()
        self.catalog.register_manifest(SourceManifest(sid, 1, digest, len(raw), {"source": "test"}), layer="document")
        self.catalog.register_document_metadata(sid, 1, filename="original.txt", title=title, mime_type="text/plain")
        payload = {"source_id": sid, "source_version": 1, "extraction_revision": 1, "original_checksum": digest,
                   "status": "completed", "segments": [{"text": text, "start": 0, "end": len(text),
                   "locator": {"kind": "lines", "start": 1, "end": 1, "container": None, "schema_version": 1}}]}
        stored = self.derived.write(sid, 1, 1, payload)
        self.catalog.record_extraction(sid, 1, 1, original_sha256=digest, extractor="text", extractor_version="1",
                                       config_sha256="c" * 64, artifact_path=str(stored.path.relative_to(self.paths.lifeweave_home)),
                                       artifact_sha256=stored.sha256, status="completed", passage_count=1)
        self.catalog.record_provider_chunk(f"chunk_{sid}", sid, 1, 1, passage_start=0, passage_end=1, content_sha256="b" * 64)
        self.catalog.update_provider_chunk(f"chunk_{sid}", expected_revision=1, submission_state="submitted", completion_state="completed")
        for name in ("extraction", "indexing"):
            for state in ("running", "completed"):
                stage = self.catalog.get_document_stage(sid, 1, name)
                self.catalog.transition_document_stage(sid, 1, name, expected_state=stage["state"],
                                                       expected_revision=stage["revision"], state=state)
        self.execute("UPDATE document_stages SET updated_at=? WHERE source_id=? AND stage='custody'", (accepted_at, sid))
        self.citations[sid] = {"source_id": sid, "source_version": 1, "extraction_revision": 1, "passage_start": 0,
                               "passage_end": 1, "original_sha256": digest, "quote": text}

    def publish(self, page_id, title, texts, *, sid="doc_1", at="2026-10-16 09:00:00", contradictions=()):
        page = self.catalog.get_wiki_page(page_id)
        revision = page["current_revision"] if page else 0
        citation = self.citations[sid]
        patch = WikiPatch.parse(json.dumps({
            "page_id": page_id, "title": title, "expected_revision": revision,
            "claims": [{"text": text, "citations": [citation]} for text in texts],
            "contradictions": list(contradictions), "links": []}))
        stored = self.wiki.store.write(patch)
        self.catalog.create_wiki_page(page_id, title=title)
        row = self.catalog.commit_wiki_revision(
            page_id, expected_revision=revision, artifact_path=str(stored.path.relative_to(self.paths.lifeweave_home)),
            artifact_sha256=stored.sha256, dependencies=[Citation.parse(citation).dependency()])
        self.execute("UPDATE wiki_revisions SET created_at=? WHERE page_id=? AND revision=?", (at, page_id, row["revision"]))
        return row

    def retain_journal(self, sid, created_at):
        raw = sid.encode()
        self.catalog.register_manifest(SourceManifest(sid, 1, hashlib.sha256(raw).hexdigest(), len(raw), {"source": "test"}))
        self.execute("UPDATE sources SET created_at=? WHERE source_id=?", (created_at, sid))

    def application(self, zone=VIENNA):
        app = JournalApplication.__new__(JournalApplication)
        app.catalog = self.catalog
        app.wiki = self.wiki
        app.vault = self.vault
        app._journal_timezone = zone
        app.paths, app.custody, app.derived, app.hindsight = self.paths, Mock(), self.derived, SimpleNamespace()
        app.documents = Mock()
        app.documents.coverage.return_value = None
        app.document_projections = DocumentProjectionService(self.catalog, Mock(), self.derived, self.vault, wiki_service=self.wiki)
        return app

    def test_window_boundaries_follow_the_configured_timezone(self):
        times = {"doc_a": "2026-10-15 21:59:59", "doc_b": "2026-10-15 22:00:00",
                 "doc_c": "2026-10-16 21:59:59", "doc_d": "2026-10-16 22:00:00"}
        for sid, accepted_at in times.items():
            self.add_document(sid, f"Text of {sid}.", accepted_at=accepted_at)
            self.publish(source_page_id(sid), f"Book {sid}", [f"Text of {sid}."], sid=sid, at=accepted_at)
            self.retain_journal("src_" + sid, accepted_at)
        vienna = self.application().brief(day=DAY)
        self.assertEqual((vienna["since"], vienna["until"], vienna["timezone"]),
                         ("2026-10-15T22:00:00Z", "2026-10-16T22:00:00Z", "Europe/Vienna"))
        self.assertEqual([item["source_id"] for item in vienna["documents"]["items"]], ["doc_b", "doc_c"])
        self.assertEqual([item["at"] for item in vienna["documents"]["items"]], ["2026-10-16 00:00", "2026-10-16 23:59"])
        self.assertEqual([item["page_id"] for item in vienna["wiki_changes"]["items"]],
                         [source_page_id("doc_b"), source_page_id("doc_c")])
        self.assertEqual(vienna["journal_entries"], 2)
        utc = self.application(ZoneInfo("UTC")).brief(day=DAY)
        self.assertEqual((utc["since"], utc["until"]), ("2026-10-16T00:00:00Z", "2026-10-17T00:00:00Z"))
        self.assertEqual([item["source_id"] for item in utc["documents"]["items"]], ["doc_c", "doc_d"])
        self.assertEqual(utc["journal_entries"], 2)
        since = self.application().brief(since=datetime(2026, 10, 16, 23, 0, tzinfo=VIENNA))
        self.assertIsNone(since["date"])
        self.assertEqual([item["source_id"] for item in since["documents"]["items"]], ["doc_c", "doc_d"])
        with self.assertRaises(ApplicationError):
            self.application().brief(since=datetime(2999, 1, 1, tzinfo=VIENNA))

    def test_claims_added_and_removed_against_the_parent_revision(self):
        self.add_document("doc_1", "a b c d")
        self.publish("topic_letters", "Letters", ["a", "b"], at="2026-10-15 10:00:00")
        self.publish("topic_letters", "Letters", ["b", "c", "d"], at="2026-10-16 10:00:00")
        brief = self.application().brief(day=DAY)
        self.assertEqual(brief["wiki_changes"]["items"], [{
            "page_id": "topic_letters", "title": "Letters", "parent_revision": 1, "revision": 2,
            "claims_added": 2, "claims_removed": 1, "at": "2026-10-16 12:00"}])
        markdown = render_brief(brief)
        self.assertIn("- 2026-10-16 12:00 Letters (topic_letters) r1 → r2: +2 / -1 claims\n", markdown)
        log = render_log(self.catalog, self.wiki, timezone=VIENNA, timezone_name="Europe/Vienna")
        lines = [line for line in log.splitlines() if line.startswith("- ")]
        self.assertEqual(lines, [
            "- 2026-10-16 12:00 · wiki · Letters (topic_letters) r1 → r2: +2 / -1 claims",
            "- 2026-10-16 10:00 · document · Book (doc_1 v1) imported",
            "- 2026-10-15 12:00 · wiki · Letters (topic_letters) r1: +2 / -0 claims",
        ])

    def test_new_contradictions_attention_stages_and_lint_summary(self):
        self.add_document("doc_1", "The sky is blue.")
        claim = {"text": "The sky is blue.", "citations": [self.citations["doc_1"]]}
        contradiction = {"left": claim, "right": claim, "left_date": "2024-01-01", "right_date": "2025-06-30"}
        row = self.publish("topic_sky", "Sky", ["The sky is blue."], contradictions=[contradiction])
        self.catalog.record_wiki_review("review_sky", "topic_sky", row["revision"], code="contradiction",
                                        details={"sources": contradiction})
        self.catalog.mark_dependent_wiki_stale("doc_1", 1)
        brief = self.application().brief(day=DAY)
        self.assertEqual(brief["contradictions"]["items"], [{
            "review_id": "review_sky", "page_id": "topic_sky", "title": "Sky", "revision": 1, "state": "pending",
            "left": {"text": "The sky is blue.", "date": "2024-01-01"},
            "right": {"text": "The sky is blue.", "date": "2025-06-30"}}])
        now = brief["open_now"]
        self.assertEqual(now["attention"]["items"], [{"page_id": "topic_sky", "title": "Sky", "state": "stale"}])
        self.assertEqual(now["stages"]["items"], [{"source_id": "doc_1", "detail": "wiki: pending", "severity": "warning"}])
        self.assertEqual(now["reviews_pending"], 1)
        # The pending wiki stage already explains doc_1's missing page.
        self.assertEqual(now["lint"]["counts"], {"error": 0, "warning": 3, "info": 1})
        self.assertEqual(now["lint"]["codes"], {"contradiction_pending": 1, "page_orphan": 1, "page_stale": 1,
                                                "stage_pending": 1})
        markdown = render_brief(brief)
        self.assertIn('- Sky (review_sky, pending): "The sky is blue." (2024-01-01) / "The sky is blue." (2025-06-30)', markdown)
        self.assertLess(markdown.index("## New contradictions"), markdown.index("## Open now"))
        self.assertLess(markdown.index("## Open now"), markdown.index("### Pages needing attention (1)"))
        self.assertIn("- Pending reviews: 1 (lifecontext wiki reviews --json)", markdown)

    def test_daylight_saving_days_are_23_and_25_hours_with_a_zone_or_local_time(self):
        self.add_document("doc_late", "Late.", accepted_at="2026-10-25 22:30:00")
        cases = {date(2026, 3, 29): ("2026-03-28T23:00:00Z", "2026-03-29T22:00:00Z"),
                 date(2026, 10, 25): ("2026-10-24T22:00:00Z", "2026-10-25T23:00:00Z")}
        previous = os.environ.get("TZ")
        self.addCleanup(time.tzset)
        self.addCleanup(lambda: os.environ.pop("TZ") if previous is None else os.environ.update(TZ=previous))
        os.environ["TZ"] = "Europe/Vienna"
        time.tzset()
        for zone, name in ((VIENNA, "Europe/Vienna"), (None, "local")):
            for day, window in cases.items():
                with self.subTest(zone=name, day=day):
                    brief = self.application(zone).brief(day=day)
                    self.assertEqual((brief["since"], brief["until"], brief["timezone"]), (*window, name))
            october = self.application(zone).brief(day=date(2026, 10, 25))
            self.assertEqual([item["at"] for item in october["documents"]["items"]], ["2026-10-25 23:30"])

    def test_an_empty_day_renders_deterministic_bytes(self):
        app = self.application()
        first = render_brief(app.brief(day=date(2020, 1, 1)))
        self.assertEqual(first, render_brief(app.brief(day=date(2020, 1, 1))))
        self.assertEqual(first.count("\nNone.\n"), 5)
        self.assertIn("0 entries retained.", first)
        self.assertTrue(first.startswith("# Brief 2020-01-01\n"))
        self.assertIn("Nothing yet.", render_log(self.catalog, self.wiki, timezone=VIENNA, timezone_name="Europe/Vienna"))

    def test_projection_writes_brief_and_log_and_keeps_a_hand_edited_brief(self):
        self.add_document("doc_1", "The sky is blue.")
        self.publish(source_page_id("doc_1"), "Book", ["The sky is blue."])
        schema = self.journal / "wiki" / "SCHEMA.md"
        schema.parent.mkdir(mode=0o700)
        schema.write_text("schema stays\n")
        schema.chmod(0o600)
        app = self.application()
        result = app.project_brief(day=DAY)
        self.assertEqual(result["projection"], {"brief": "completed", "log": "completed"})
        brief_file = self.journal / "wiki" / "Briefs" / "2026-10-16 - Brief.md"
        log_file = self.journal / "wiki" / "Log.md"
        self.assertEqual(brief_file.read_text(), render_brief(app.brief(day=DAY)))
        self.assertIn("· wiki · Book", log_file.read_text())
        self.assertEqual(app.project_brief(day=DAY)["projection"], {"brief": "completed", "log": "completed"})
        brief_file.write_text(brief_file.read_text() + "My own note.\n")
        self.publish(source_page_id("doc_1"), "Book", ["The sky is blue.", "blue"])
        result = app.project_brief(day=DAY)
        self.assertEqual(result["projection"], {"brief": "conflict", "log": "completed"})
        self.assertTrue(brief_file.read_text().endswith("My own note.\n"))
        self.assertIn("r1 → r2", log_file.read_text())
        self.assertEqual(schema.read_text(), "schema stays\n")
        receipt = self.catalog.get_artifact_projection("wiki", "brief_2026-10-16", 1)
        self.assertEqual(receipt["state"], "conflict")
        lint = self.wiki.lint([])
        self.assertIn(("projection_conflict", "wiki brief_2026-10-16 r1",
                       "Move the edited file aside, then lifecontext brief --project --date 2026-10-16 --json"),
                      [(item["code"], item["detail"], item["hint"]) for item in lint["findings"]])
        with self.assertRaises(ApplicationError):
            app.project_brief(day=None, since=datetime(2026, 10, 16, tzinfo=VIENNA))

    def tombstone(self, sid, kind):
        operation = self.catalog.begin_operation(f"{kind}-{sid}", "a" * 64, "curation")
        self.execute("INSERT INTO tombstones(source_id,source_version,tombstone_type,operation_id) VALUES(?,1,?,?)",
                     (sid, kind, operation["operation_id"]))
        self.catalog.mark_dependent_wiki_stale(sid, 1)

    def forget_without_tombstone(self, sid):
        """A document forget writes no tombstone: its versions can no longer be recalled."""
        self.catalog.set_provider_state(sid, 1, recall_visibility="not_visible")
        self.catalog.mark_dependent_wiki_stale(sid, 1)

    def test_deleted_documents_vanish_and_forgotten_ones_lose_their_titles(self):
        self.check_deleted_and_forgotten(lambda sid: self.tombstone(sid, "forgotten"))

    def test_a_document_forgotten_without_a_tombstone_loses_its_title_too(self):
        self.check_deleted_and_forgotten(self.forget_without_tombstone)

    def check_deleted_and_forgotten(self, forget):
        for sid, title in (("doc_gone", "Secret Deleted Title"), ("doc_forgot", "Private Forgotten Title"),
                           ("doc_live", "Live Book")):
            self.add_document(sid, f"Text of {sid}.", title=title)
            self.publish(source_page_id(sid), title, [f"Text of {sid}."], sid=sid)
        # Shared pages whose titles were written from one withdrawn document's claims.
        for sid, page_id, title in (("doc_gone", "topic_mixed", "Mixed"), ("doc_forgot", "topic_private", "Private Topic")):
            claim = {"text": f"Text of {sid}.", "citations": [self.citations[sid]]}
            contradiction = {"left": claim, "right": claim, "left_date": "2024-01-01", "right_date": "2025-06-30"}
            row = self.publish(page_id, title, [f"Text of {sid}."], sid=sid, contradictions=[contradiction])
            self.catalog.record_wiki_review(f"review_{sid}", page_id, row["revision"], code="contradiction",
                                            details={"sources": contradiction})
        self.tombstone("doc_gone", "deleted")
        forget("doc_forgot")
        app = self.application()
        brief = app.brief(day=DAY)
        self.assertEqual([(item["source_id"], item["title"]) for item in brief["documents"]["items"]],
                         [("doc_forgot", "a forgotten source"), ("doc_live", "Live Book")])
        self.assertEqual([(item["page_id"], item["title"]) for item in brief["wiki_changes"]["items"]],
                         [(source_page_id("doc_forgot"), "a forgotten source"), (source_page_id("doc_live"), "Live Book"),
                          ("topic_mixed", "a forgotten source"), ("topic_private", "a forgotten source")])
        self.assertEqual(brief["contradictions"]["items"], [])
        self.assertEqual(brief["open_now"]["reviews_pending"], 0)
        self.assertEqual([(item["page_id"], item["title"]) for item in brief["open_now"]["attention"]["items"]],
                         [(source_page_id("doc_forgot"), "a forgotten source"), ("topic_mixed", "a forgotten source"),
                          ("topic_private", "a forgotten source")])
        rendered = (json.dumps(brief) + render_brief(brief)
                    + render_log(self.catalog, self.wiki, timezone=VIENNA, timezone_name="Europe/Vienna"))
        for private in ("Secret Deleted Title", "Private Forgotten Title", "doc_gone", "Text of doc_gone",
                        "Text of doc_forgot", "Mixed", "Private Topic"):
            self.assertNotIn(private, rendered)
        self.assertIn("· document · a forgotten source (doc_forgot v1) imported", rendered)
        self.assertIn(f"· wiki · a forgotten source ({source_page_id('doc_forgot')}) r1", rendered)

    def test_output_holds_no_paths_or_credentials(self):
        self.add_document("doc_1", "The sky is blue.")
        self.publish(source_page_id("doc_1"), "Book", ["The sky is blue."])
        app = self.application()
        brief = app.brief(day=DAY)
        rendered = json.dumps(brief) + render_brief(brief) + render_log(self.catalog, self.wiki, timezone=VIENNA,
                                                                        timezone_name="Europe/Vienna")
        for private in (str(self.base), str(self.paths.lifeweave_home), "api.token", "LIFEWEAVE", ".lifeweave",
                        "derived/", "wiki_revisions"):
            self.assertNotIn(private, rendered)


if __name__ == "__main__":
    unittest.main()
