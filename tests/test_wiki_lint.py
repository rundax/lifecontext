"""Model-free wiki lint checks and the owner's review queue."""
import asyncio
import difflib
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from local_paths import scratch_root

from lifecontext.application import ApplicationError, JournalApplication
from lifecontext.catalog import CatalogBusy, LifeContextCatalog
from lifecontext.custody import SourceManifest
from lifecontext.derived import DerivedStore
from lifecontext.paths import LifeContextPaths
from lifecontext.wiki import (
    LINT_MAX_FINDINGS,
    MAX_PATCH_BYTES,
    Citation,
    WikiPatch,
    WikiService,
    source_page_id,
)

PAGE = source_page_id("doc_1")
STAGES = ("custody", "extraction", "indexing", "wiki", "projection")


class WikiFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=scratch_root())
        self.paths = LifeContextPaths(Path(self.temp.name))
        self.catalog = LifeContextCatalog(self.paths)
        self.derived = DerivedStore(self.paths)
        self.service = WikiService(self.catalog, Mock(), self.derived, SimpleNamespace(), self.paths)
        self.citation = self.add_document("doc_1", "The sky is blue.")

    def tearDown(self):
        self.catalog.close()
        self.temp.cleanup()

    def add_document(self, sid, text):
        raw = text.encode()
        digest = hashlib.sha256(raw).hexdigest()
        self.catalog.register_manifest(SourceManifest(sid, 1, digest, len(raw), {"source": "test"}), layer="document")
        self.catalog.register_document_metadata(sid, 1, filename="original.txt", title="Original", mime_type="text/plain")
        payload = {"source_id": sid, "source_version": 1, "extraction_revision": 1, "original_checksum": digest,
                   "status": "completed", "segments": [{"text": text, "start": 0, "end": len(text),
                   "locator": {"kind": "lines", "start": 1, "end": 1, "container": None, "schema_version": 1}}]}
        stored = self.derived.write(sid, 1, 1, payload)
        self.catalog.record_extraction(sid, 1, 1, original_sha256=digest, extractor="text", extractor_version="1",
                                       config_sha256="c" * 64, artifact_path=str(stored.path.relative_to(self.paths.lifeweave_home)),
                                       artifact_sha256=stored.sha256, status="completed", passage_count=1)
        self.catalog.record_provider_chunk(f"chunk_{sid}", sid, 1, 1, passage_start=0, passage_end=1, content_sha256="b" * 64)
        self.catalog.update_provider_chunk(f"chunk_{sid}", expected_revision=1, submission_state="submitted", completion_state="completed")
        for state in ("running", "completed"):
            stage = self.catalog.get_document_stage(sid, 1, "indexing")
            self.catalog.transition_document_stage(sid, 1, "indexing", expected_state=stage["state"],
                                                   expected_revision=stage["revision"], state=state)
        return {"source_id": sid, "source_version": 1, "extraction_revision": 1, "passage_start": 0,
                "passage_end": 1, "original_sha256": digest, "quote": text}

    def publish(self, page_id, title, texts=("The sky is blue.",), *, links=(), contradictions=(), summary=None):
        page = self.catalog.get_wiki_page(page_id)
        revision = page["current_revision"] if page else 0
        value = {"page_id": page_id, "title": title, "expected_revision": revision,
                 "claims": [{"text": text, "citations": [self.citation]} for text in texts],
                 "contradictions": list(contradictions), "links": list(links)}
        # Document pages carry a model summary unless a test leaves it out.
        if summary is None and page_id.startswith("source_"):
            summary = [{"text": "A short summary.", "claims": [1]}]
        if summary:
            value["summary"] = summary
        patch = WikiPatch.parse(json.dumps(value))
        stored = self.service.store.write(patch)
        self.catalog.create_wiki_page(page_id, title=title)
        return self.catalog.commit_wiki_revision(
            page_id, expected_revision=revision, artifact_path=str(stored.path.relative_to(self.paths.lifeweave_home)),
            artifact_sha256=stored.sha256, dependencies=[Citation.parse(self.citation).dependency()])

    def contradiction(self):
        claim = {"text": "The sky is blue.", "citations": [self.citation]}
        return {"left": claim, "right": claim, "left_date": "2024-01-01", "right_date": "2025-06-30"}

    @staticmethod
    def document(sid="doc_1", *, coverage=None, error_code=None, **stages):
        states = {"custody": "accepted", **{name: "completed" for name in STAGES[1:]}, **stages}
        return {"source_id": sid, "source_version": 1,
                "stages": {name: {"state": state, "revision": 1, "attempt": 1,
                                  "error_code": error_code if state in {"failed", "blocked", "conflict"} else None}
                           for name, state in states.items()},
                "coverage": coverage}

    def lint(self, documents=None):
        return self.service.lint([self.document()] if documents is None else documents)

    @staticmethod
    def found(report, code=None):
        return [(item["code"], item["severity"], item["page_id"], item["source_id"], item["detail"])
                for item in report["findings"] if code is None or item["code"] == code]


class WikiLintTests(WikiFixture):
    def test_a_healthy_wiki_has_no_findings(self):
        self.publish("topic_sky", "Sky", links=[PAGE])
        self.publish(PAGE, "Original", links=["topic_sky"])
        report = self.lint()
        self.assertEqual(report, {"findings": [], "counts": {"error": 0, "warning": 0, "info": 0}, "omitted": 0,
                                  "documents_omitted": 0})

    def test_stale_page_names_the_rebuild_command(self):
        self.publish(PAGE, "Original")
        self.catalog.mark_dependent_wiki_stale("doc_1", 1)
        report = self.lint()
        self.assertEqual(self.found(report), [("page_stale", "warning", PAGE, "doc_1", None), ("source_unrouted", "info", PAGE, "doc_1", None)])
        self.assertIn("lifecontext wiki rebuild --source-id doc_1", report["findings"][0]["hint"])

    def test_conflict_page_and_projection_conflict_are_errors(self):
        row = self.publish(PAGE, "Original")
        self.catalog.record_wiki_review("projection_review", PAGE, row["revision"], code="manual_edit",
                                        details={"expected_hash": "a" * 64, "actual_hash": "b" * 64})
        self.catalog.set_artifact_projection("wiki", PAGE, 1, projection_path="wiki/Original.md",
                                             desired_hash="c" * 64, state="conflict")
        self.catalog.set_artifact_projection("document", "doc_1_note", 1, projection_path="documents/inbox/doc_1-v1.md",
                                             desired_hash="d" * 64, state="conflict")
        report = self.lint()
        self.assertEqual(self.found(report), [
            ("page_conflict", "error", PAGE, None, "projection_review"),
            ("projection_conflict", "error", None, "doc_1", "document doc_1_note r1"),
            ("projection_conflict", "error", PAGE, None, f"wiki {PAGE} r1"),
            ("source_unrouted", "info", PAGE, "doc_1", None),
        ])
        self.assertEqual(report["counts"], {"error": 3, "warning": 0, "info": 1})
        self.assertTrue(all(item["hint"].count("lifecontext ") >= 1 for item in report["findings"]))
        hint = report["findings"][0]["hint"]
        self.assertLess(hint.index("Undo the hand edit"), hint.index("resolve"))
        self.assertLess(hint.index("resolve"), hint.index("lifecontext wiki rebuild"))

    def test_projection_conflict_superseded_by_a_later_revision_is_not_reported(self):
        self.publish(PAGE, "Original")
        self.catalog.set_artifact_projection("wiki", PAGE, 1, projection_path="wiki/Original.md",
                                             desired_hash="c" * 64, state="conflict")
        self.catalog.set_artifact_projection("wiki", PAGE, 2, projection_path="wiki/Original.md",
                                             desired_hash="d" * 64, state="projected", last_projected_hash="d" * 64)
        self.assertEqual(self.found(self.lint(), "projection_conflict"), [])

    def test_pending_contradiction_until_the_owner_resolves_it(self):
        row = self.publish(PAGE, "Original", contradictions=[self.contradiction()])
        self.catalog.record_wiki_review("review_contradiction", PAGE, row["revision"], code="contradiction",
                                        details={"sources": self.contradiction()})
        self.assertEqual(self.found(self.lint()), [
            ("contradiction_pending", "warning", PAGE, None, "review_contradiction"), ("source_unrouted", "info", PAGE, "doc_1", None)])
        self.catalog.resolve_wiki_review("review_contradiction", note="Both readings are kept on purpose.")
        self.assertEqual(self.found(self.lint()), [("source_unrouted", "info", PAGE, "doc_1", None)])

    def test_document_stages_report_the_first_unfinished_stage(self):
        self.publish(PAGE, "Original")
        self.add_document("doc_2", "Grass is green.")
        self.publish(source_page_id("doc_2"), "Second")
        report = self.lint([
            self.document("doc_1", extraction="failed", indexing="pending", wiki="pending", projection="pending",
                          error_code="provider_unavailable"),
            self.document("doc_2", wiki="running", projection="pending"),
        ])
        self.assertEqual(self.found(report), [
            ("stage_failed", "error", None, "doc_1", "extraction: failed (provider_unavailable)"),
            ("stage_pending", "warning", None, "doc_2", "wiki: running"),
        ])

    def test_indexed_document_without_a_source_page(self):
        report = self.lint()
        self.assertEqual(self.found(report), [("source_page_missing", "warning", PAGE, "doc_1", None)])
        self.assertIn("lifecontext wiki rebuild --source-id doc_1", report["findings"][0]["hint"])
        self.assertEqual(self.found(self.lint([self.document(indexing="running", wiki="pending", projection="pending")]),
                                    "source_page_missing"), [])
        # A failed or pending wiki stage already explains the missing page.
        self.assertEqual(self.found(self.lint([self.document(wiki="failed", projection="pending", error_code="reflect_failed")])),
                         [("stage_failed", "error", None, "doc_1", "wiki: failed (reflect_failed)")])

    def test_orphan_shared_pages_and_links_to_missing_pages(self):
        self.publish("topic_sky", "Sky")
        self.publish("entity_sun", "Sun")
        self.publish(PAGE, "Original", links=["topic_sky", "topic_gone"])
        self.assertEqual(self.found(self.lint()), [
            ("link_missing", "warning", PAGE, None, "topic_gone"),
            ("page_orphan", "info", "entity_sun", None, None),
        ])

    def test_near_duplicate_shared_titles(self):
        self.publish(PAGE, "Original", links=["topic_a", "topic_b", "topic_c", "topic_d", "topic_e", "topic_f"])
        for page_id, title in (("topic_a", "Copy-writing"), ("topic_b", "COPYWRITING!"), ("topic_c", "Machine learning"),
                               ("topic_d", "Machine learnin"), ("topic_e", "Physics"), ("topic_f", "Chemistry")):
            self.publish(page_id, title)
        self.assertEqual(self.found(self.lint()), [
            ("title_near_duplicate", "warning", "topic_a", None, "topic_b"),
            ("title_near_duplicate", "warning", "topic_c", None, "topic_d"),
        ])

    def test_near_duplicate_search_skips_titles_of_distant_length(self):
        titles = ["x" * round(10 * 1.3 ** step) for step in range(13)]
        self.publish(PAGE, "Original", links=[f"topic_{step:02}" for step in range(13)])
        for step, title in enumerate(titles):
            self.publish(f"topic_{step:02}", title)
        with patch.object(difflib.SequenceMatcher, "real_quick_ratio", autospec=True,
                          side_effect=difflib.SequenceMatcher.real_quick_ratio) as compared:
            self.assertEqual(self.found(self.lint(), "title_near_duplicate"), [])
        self.assertEqual(compared.call_count, 0)

    def test_pages_near_the_claim_or_byte_bound(self):
        self.publish(PAGE, "Original", [f"The sky is blue. {index}" for index in range(116)])
        self.publish("topic_big", "Big", ["x" * 2040 + str(index) for index in range(20)])
        self.publish("topic_fits", "Fits", [f"claim {index}" for index in range(115)])
        self.publish("entity_hub", "Hub", links=["topic_big", "topic_fits"])
        self.publish(PAGE, "Original", [f"The sky is blue. {index}" for index in range(116)], links=["entity_hub"])
        found = self.found(self.lint(), "page_near_limit")
        self.assertEqual([item[2] for item in found], [PAGE, "topic_big"])
        self.assertRegex(found[0][4], rf"^116/128 claims, \d+/{MAX_PATCH_BYTES} bytes$")
        self.assertRegex(found[1][4], rf"^20/128 claims, 4[3-7]\d\d\d/{MAX_PATCH_BYTES} bytes$")

    def test_coverage_gaps(self):
        self.publish(PAGE, "Original")
        coverage = {"unit": "page", "total": 4, "resolved": 2, "text": 2, "ocr": 0, "blank": 0, "vision": 0,
                    "unresolved": ["3", "4"], "partial": ["figure_not_read:2"], "complete": False}
        self.assertEqual(self.found(self.lint([self.document(coverage=coverage)])), [
            ("coverage_unresolved", "warning", None, "doc_1", "2 unresolved"),
            ("coverage_partial", "info", None, "doc_1", "1 partial"),
            ("source_unrouted", "info", PAGE, "doc_1", None),
        ])

    def test_pending_category_proposals(self):
        self.publish(PAGE, "Original")
        self.catalog.record_category_proposal("proposal_1", name="Work", description="Broad", rationale="Fits",
                                              documents=[{"source_id": "doc_1", "source_version": 1}],
                                              closest_category_id="inbox")
        self.assertEqual(self.found(self.lint()), [("category_proposal_pending", "info", None, None, "proposal_1"), ("source_unrouted", "info", PAGE, "doc_1", None)])

    def test_document_page_without_a_summary(self):
        self.publish(PAGE, "Original", summary=[])
        report = self.lint()
        self.assertEqual(self.found(report), [("source_unrouted", "info", PAGE, "doc_1", None), ("summary_missing", "info", PAGE, "doc_1", None)])
        self.assertTrue(all("lifecontext wiki rebuild --source-id doc_1" in item["hint"] for item in report["findings"]))
        # Only pages of current documents are checked.
        self.assertEqual(self.found(self.lint([])), [])

    def test_a_document_no_shared_page_cites(self):
        self.publish(PAGE, "Original")
        report = self.lint()
        self.assertEqual(self.found(report), [("source_unrouted", "info", PAGE, "doc_1", None)])
        self.assertIn("lifecontext wiki rebuild --source-id doc_1", report["findings"][0]["hint"])
        self.assertEqual(self.found(self.lint([self.document(wiki="running", projection="pending")]), "source_unrouted"), [])
        self.publish("topic_sky", "Sky", links=[PAGE])
        self.assertEqual(self.found(self.lint(), "source_unrouted"), [])

    def test_a_shared_page_with_more_contributors_than_links(self):
        citations = [self.citation] + [self.add_document(f"doc_{n:02}", f"Fact {n}.") for n in range(2, 35)]
        for citation in citations:
            claim = {"text": citation["quote"], "citations": [citation]}
            patch = WikiPatch.parse(json.dumps({"page_id": source_page_id(citation["source_id"]), "title": "Doc",
                                                "expected_revision": 0, "claims": [claim], "contradictions": [],
                                                "links": ["topic_facts"], "summary": [{"text": "A fact.", "claims": [1]}]}))
            stored = self.service.store.write(patch)
            self.catalog.create_wiki_page(patch.page_id, title="Doc")
            self.catalog.commit_wiki_revision(patch.page_id, expected_revision=0,
                artifact_path=str(stored.path.relative_to(self.paths.lifeweave_home)),
                artifact_sha256=stored.sha256, dependencies=[Citation.parse(citation).dependency()])
        claims = [{"text": c["quote"], "citations": [c]} for c in citations]
        links = [source_page_id(c["source_id"]) for c in citations][:32]
        patch = WikiPatch.parse(json.dumps({"page_id": "topic_facts", "title": "Facts", "expected_revision": 0,
                                            "claims": claims, "contradictions": [], "links": links}))
        stored = self.service.store.write(patch)
        self.catalog.create_wiki_page("topic_facts", title="Facts")
        self.catalog.commit_wiki_revision("topic_facts", expected_revision=0,
            artifact_path=str(stored.path.relative_to(self.paths.lifeweave_home)),
            artifact_sha256=stored.sha256, dependencies=[Citation.parse(c).dependency() for c in citations])
        self.assertEqual(self.found(self.lint(), "page_links_capped"),
                         [("page_links_capped", "info", "topic_facts", None, "32/34 links")])

    def test_unreadable_revision_is_reported_not_raised(self):
        row = self.publish(PAGE, "Original")
        (self.paths.lifeweave_home / row["artifact_path"]).write_text("{}")
        self.assertEqual(self.found(self.lint()), [("revision_unreadable", "error", PAGE, None, "r1")])

    def test_findings_are_ordered_capped_and_counted(self):
        self.publish(PAGE, "Original")
        documents = [self.document(f"doc_x{index:03}", extraction="failed", indexing="pending", wiki="pending",
                                   projection="pending", error_code="provider_unavailable") for index in range(LINT_MAX_FINDINGS)]
        documents.append(self.document("doc_y", coverage={"unresolved": [], "partial": ["chart_not_read:1"]}))
        report = self.lint(documents)
        self.assertEqual(len(report["findings"]), LINT_MAX_FINDINGS)
        # doc_y is indexed without a page (warning) and partly unread (info).
        self.assertEqual(report["counts"], {"error": LINT_MAX_FINDINGS, "warning": 1, "info": 1})
        self.assertEqual(report["omitted"], 2)
        self.assertEqual({item["severity"] for item in report["findings"]}, {"error"})
        self.assertEqual(report["findings"][0]["source_id"], "doc_x000")

    def test_lint_never_writes(self):
        row = self.publish(PAGE, "Original", links=["topic_gone"])
        self.catalog.mark_dependent_wiki_stale("doc_1", 1)
        before = (self.paths.catalog_path.read_bytes(), sorted(p.name for p in self.paths.lifeweave_home.rglob("*")))
        report = self.lint()
        self.assertTrue(report["findings"])
        self.assertEqual((self.paths.catalog_path.read_bytes(), sorted(p.name for p in self.paths.lifeweave_home.rglob("*"))), before)
        self.assertEqual(self.catalog.get_wiki_page(PAGE)["current_revision"], row["revision"])


class WikiReviewQueueTests(WikiFixture):
    def application(self):
        app = JournalApplication.__new__(JournalApplication)
        app.catalog = self.catalog
        app.wiki = self.service
        return app

    def review(self):
        row = self.publish(PAGE, "Original", contradictions=[self.contradiction()])
        self.catalog.record_wiki_review("review_1", PAGE, row["revision"], code="contradiction",
                                        details={"sources": self.contradiction()})
        return row

    def test_resolve_records_the_note_and_never_edits_claims(self):
        row = self.review()
        app = self.application()
        listed = app.list_wiki_reviews()
        self.assertEqual([item["review_id"] for item in listed["reviews"]], ["review_1"])
        self.assertIsNone(listed["reviews"][0]["resolution"])
        result = app.resolve_wiki_review("review_1", note="Both dates are right; the source changed.",
                                         idempotency_key="resolve-1")
        self.assertEqual(result["state"], "resolved")
        self.assertEqual(result["resolution"]["note"], "Both dates are right; the source changed.")
        self.assertRegex(result["resolution"]["resolved_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertEqual(app.operation_status(result.operation_id)["state"], "completed")
        page = self.catalog.get_wiki_page(PAGE)
        self.assertEqual((page["current_revision"], page["state"]), (row["revision"], "current"))
        self.assertEqual(self.catalog.get_wiki_revision(PAGE, row["revision"])["artifact_sha256"], row["artifact_sha256"])
        self.assertEqual(app.list_wiki_reviews()["reviews"], [])
        self.assertEqual(app.list_wiki_reviews(state="resolved")["reviews"], [dict(result)])

    def test_replay_double_resolve_unknown_id_and_invalid_note(self):
        self.review()
        app = self.application()
        first = app.resolve_wiki_review("review_1", note="Keep both.", idempotency_key="resolve-1")
        replay = app.resolve_wiki_review("review_1", note="Keep both.", idempotency_key="resolve-1")
        self.assertEqual((replay.operation_id, dict(replay)), (first.operation_id, dict(first)))
        for kwargs, code in (
            ({"note": "Keep both.", "idempotency_key": "resolve-2"}, "conflict"),
            ({"note": "Another note.", "idempotency_key": "resolve-1"}, "conflict"),
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ApplicationError) as error:
                app.resolve_wiki_review("review_1", **kwargs)
            self.assertEqual(error.exception.code, code)
        with self.assertRaises(ApplicationError) as error:
            app.resolve_wiki_review("review_1", note="Keep both.", idempotency_key="resolve-2")
        self.assertEqual(error.exception.code, "conflict")
        with self.assertRaises(ApplicationError) as error:
            app.resolve_wiki_review("review_unknown", note="Keep both.", idempotency_key="resolve-3")
        self.assertEqual(error.exception.code, "source_not_found")
        for note in ("", "   ", "line\nbreak", "x" * 4097):
            with self.subTest(note=note[:8]), self.assertRaises(ApplicationError) as error:
                app.resolve_wiki_review("review_1", note=note, idempotency_key="resolve-4")
            self.assertEqual(error.exception.code, "invalid_request")
        self.assertEqual(self.catalog.find_wiki_reviews(review_id="review_1")[0][0]["resolution"]["note"], "Keep both.")

    def test_application_lint_reads_current_documents_on_its_own_connection(self):
        self.publish(PAGE, "Original")
        self.add_document("doc_2", "Grass is green.")
        app = self.application()
        app.paths, app.custody, app.derived, app.hindsight = self.paths, Mock(), self.derived, SimpleNamespace()
        app.documents = Mock()
        app.documents.coverage.return_value = {"unresolved": ["2"], "partial": []}
        report = app.wiki_lint()
        self.assertEqual(report["documents_omitted"], 0)
        self.assertEqual([item[:4] for item in self.found(report)], [
            ("coverage_unresolved", "warning", None, "doc_1"), ("coverage_unresolved", "warning", None, "doc_2"),
            ("source_page_missing", "warning", source_page_id("doc_2"), "doc_2"),
            ("stage_pending", "warning", None, "doc_1"), ("stage_pending", "warning", None, "doc_2")])
        self.assertEqual(app.documents.coverage.call_args_list[0].args[:2], ("doc_1", 1))
        with patch("lifecontext.application._MAX_REPORT_DOCUMENTS", 1):
            report = app.wiki_lint()
        self.assertEqual(report["documents_omitted"], 1)
        self.assertEqual({item["source_id"] for item in report["findings"]}, {"doc_1"})

    def test_a_busy_catalog_leaves_the_resolve_retryable(self):
        self.review()
        app = self.application()
        with (patch.object(self.catalog, "resolve_wiki_review", side_effect=CatalogBusy("catalog operation failed")),
              self.assertRaises(ApplicationError) as error):
            app.resolve_wiki_review("review_1", note="Keep both.", idempotency_key="resolve-busy")
        self.assertEqual(error.exception.code, "catalog_busy")
        self.assertEqual(self.catalog.find_wiki_reviews(review_id="review_1")[0][0]["state"], "pending")
        result = app.resolve_wiki_review("review_1", note="Keep both.", idempotency_key="resolve-busy")
        self.assertEqual(result["state"], "resolved")
        self.assertEqual(app.operation_status(result.operation_id)["state"], "completed")

    def test_startup_closes_a_review_operation_left_open(self):
        self.review()
        app = self.application()
        digest = app._request_digest("POST /v1/wiki/reviews/{review_id}/resolve", {"review_id": "review_1", "note": "n"})
        stale = self.catalog.begin_operation("resolve-crashed", digest, "wiki_review")
        report = asyncio.run(app.recover_startup())
        self.assertEqual(report["unresolved"], 0)
        self.assertEqual(self.catalog.get_operation(stale["operation_id"])["state"], "cancelled")
        self.assertEqual(self.catalog.find_wiki_reviews(review_id="review_1")[0][0]["state"], "pending")

    def test_list_filters_by_page_and_state_and_bounds_the_result(self):
        self.review()
        row = self.publish("topic_sky", "Sky")
        for index in range(60):
            self.catalog.record_wiki_review(f"review_sky_{index:02}", "topic_sky", row["revision"], code="contradiction",
                                            details={"sources": self.contradiction()})
        app = self.application()
        listed = app.list_wiki_reviews(page_id=PAGE)
        self.assertEqual([item["review_id"] for item in listed["reviews"]], ["review_1"])
        everything = app.list_wiki_reviews()
        self.assertEqual((len(everything["reviews"]), everything["omitted"]), (50, 11))
        self.assertEqual(app.list_wiki_reviews(state="resolved"), {"reviews": [], "omitted": 0})

    def test_hidden_reviews_beyond_the_first_4096_are_not_counted(self):
        # 4096 reviews to list, then 5 that quote a forgotten document.
        shown = [{"review_id": f"review_{index:05}", "code": "manual_edit"} for index in range(4096)]
        sources = {"left": {"citations": [{"source_id": "doc_gone", "source_version": 1}]},
                   "right": {"citations": [{"source_id": "doc_gone", "source_version": 1}]}}
        hidden = [{"review_id": f"review_z{index}", "code": "contradiction", "details": {"sources": sources}}
                  for index in range(5)]
        reviews = shown + hidden
        with patch.object(self.catalog, "find_wiki_reviews",
                          side_effect=lambda **kwargs: (reviews[:kwargs["limit"]], len(reviews))), \
                patch.object(self.service, "source_withdrawn", side_effect=lambda sid, version=None: sid == "doc_gone"):
            listed, total = self.service.find_reviews(limit=100)
        self.assertEqual((len(listed), total), (100, 4096))


if __name__ == "__main__":
    unittest.main()
