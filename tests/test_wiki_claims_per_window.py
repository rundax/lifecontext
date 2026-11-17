"""Several checked claims per evidence window, spread over the whole document under the page cap."""
import json
import math
import unittest

import test_wiki
from wiki_fake_compiler import FakeCompiler

from lifecontext.wiki import (
    COMPILE_INSTRUCTIONS,
    MAX_SOURCE_CLAIMS,
    WikiError,
    source_page_id,
)


class ClaimsPerWindowTests(unittest.TestCase):
    def fixture(self, pages, sentences, filler):
        """doc_1 extraction revision 2: one segment per page, ``sentences`` short facts and then filler."""
        fixture = test_wiki.WikiTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        segments, offset = [], 0
        for page in range(1, pages + 1):
            text = " ".join(f"Page {page} fact {n} holds." for n in range(sentences)) + " " + "x" * filler
            segments.append({"text": text, "start": offset, "end": offset + len(text),
                             "locator": {"kind": "page", "start": page, "end": page, "container": None, "schema_version": 1}})
            offset += len(text)
        payload = {**fixture.payload, "extraction_revision": 2, "segments": segments}
        stored = fixture.derived.write("doc_1", 1, 2, payload)
        fixture.catalog.record_extraction("doc_1", 1, 2, original_sha256=fixture.digest, extractor="pdf",
                                          extractor_version="1", config_sha256="c" * 64,
                                          artifact_path=str(stored.path.relative_to(fixture.paths.lifeweave_home)),
                                          artifact_sha256=stored.sha256, status="completed", passage_count=pages)
        fixture.catalog.record_provider_chunk("chunk_pages", "doc_1", 1, 2, passage_start=0, passage_end=pages,
                                              content_sha256="b" * 64)
        fixture.catalog.update_provider_chunk("chunk_pages", expected_revision=1, submission_state="submitted",
                                              completion_state="completed")
        fixture.service.compiler = FakeCompiler({})
        return fixture

    def test_a_short_document_gets_a_claim_per_3_kb_of_its_window(self):
        fixture = self.fixture(pages=10, sentences=1, filler=900)
        fixture.update()
        windows = fixture.service.compiler.queries["window"]
        self.assertEqual(len(windows), 1)
        self.assertIn("up to max_claims", COMPILE_INSTRUCTIONS)
        data = json.loads(windows[0].split("UNTRUSTED_DATA=", 1)[1])
        size = sum(len(segment["text"].encode()) for segment in data["evidence"]["segments"])
        self.assertEqual(data["max_claims"], math.ceil(size / 3000))
        self.assertEqual(data["max_claims"], 4)
        self.assertEqual(len(data["example_patch"]["claims"]), 2)
        claims = fixture.current().claims
        self.assertEqual(len(claims), 4)
        pages = [claim.citations[0].passage_start for claim in claims]
        self.assertEqual(len(set(pages)), 4)
        managed = fixture.service.show(source_page_id("doc_1"))["managed"]
        for page in pages:
            self.assertIn(f"[Original, p. {page + 1}]", managed)

    def test_claims_spread_over_every_window_under_the_page_cap(self):
        # Twelve one-page windows of five facts each: a quota of four per window
        # keeps the last pages on the page instead of cutting them at the cap.
        fixture = self.fixture(pages=12, sentences=5, filler=20_000)
        fixture.update()
        windows = fixture.service.compiler.queries["window"]
        self.assertEqual(len(windows), 12)
        self.assertEqual({json.loads(query.split("UNTRUSTED_DATA=", 1)[1])["max_claims"] for query in windows}, {4})
        claims = fixture.current().claims
        self.assertEqual(len(claims), MAX_SOURCE_CLAIMS)
        per_page = {}
        for claim in claims:
            per_page[claim.citations[0].passage_start] = per_page.get(claim.citations[0].passage_start, 0) + 1
        self.assertEqual(per_page, {page: 4 for page in range(12)})

    def altered(self, fixture, change):
        """Make the model's window answer pass through ``change(answer, data)``."""
        compiler = fixture.service.compiler
        original = compiler.window

        def window(data):
            return change(original(data), data)

        compiler.window = window

    def test_a_claim_failing_its_quote_is_dropped_and_the_others_stay(self):
        fixture = self.fixture(pages=10, sentences=1, filler=900)

        def change(answer, data):
            self.assertEqual(len(answer["claims"]), 4)
            answer["claims"][1] = {**answer["claims"][1], "text": "A paraphrase the page never says."}
            return answer

        self.altered(fixture, change)
        fixture.update()
        claims = fixture.current().claims
        self.assertEqual(len(claims), 3)
        self.assertNotIn("A paraphrase the page never says.", [claim.text for claim in claims])
        self.assertEqual(fixture.catalog.get_document_stage("doc_1", 1, "wiki")["state"], "completed")

    def test_a_quote_the_page_does_not_hold_byte_for_byte_is_dropped(self):
        fixture = self.fixture(pages=10, sentences=1, filler=900)

        def change(answer, data):
            claim = answer["claims"][0]
            citation = {**claim["citations"][0], "quote": claim["citations"][0]["quote"].replace(" ", "  ", 1)}
            answer["claims"][0] = {**claim, "text": citation["quote"], "citations": [citation]}
            return answer

        self.altered(fixture, change)
        fixture.update()
        self.assertEqual(len(fixture.current().claims), 3)

    def test_a_window_where_every_claim_fails_rejects_the_response(self):
        fixture = self.fixture(pages=3, sentences=2, filler=100)

        def change(answer, data):
            answer["claims"] = [{**claim, "text": "A paraphrase the page never says."} for claim in answer["claims"]]
            return answer

        self.altered(fixture, change)
        with self.assertRaises(WikiError):
            fixture.update()
        self.assertEqual(fixture.catalog.get_document_stage("doc_1", 1, "wiki")["state"], "failed")
        self.assertIsNone(fixture.catalog.get_wiki_page(source_page_id("doc_1")))
        # The rejected response was regenerated once, never accepted.
        self.assertEqual(len(fixture.service.compiler.queries["window"]), 2)

    def test_a_window_keeps_at_most_the_claims_it_was_asked_for(self):
        fixture = self.fixture(pages=10, sentences=1, filler=900)
        self.altered(fixture, lambda answer, data: type(fixture.service.compiler).window({**data, "max_claims": 8}))
        fixture.update()
        data = json.loads(fixture.service.compiler.queries["window"][0].split("UNTRUSTED_DATA=", 1)[1])
        self.assertEqual(data["max_claims"], 4)
        claims = fixture.current().claims
        self.assertEqual(len(claims), 4)
        self.assertEqual(len({claim.text for claim in claims}), 4)

    def test_a_repeated_claim_does_not_use_up_a_slot(self):
        fixture = self.fixture(pages=10, sentences=1, filler=900)

        def change(answer, data):
            first, *rest = answer["claims"]
            answer["claims"] = [first, first, *rest]
            return answer

        self.altered(fixture, change)
        fixture.update()
        texts = [claim.text for claim in fixture.current().claims]
        self.assertEqual(len(texts), 4)
        self.assertEqual(len(set(texts)), 4)

    def test_dropped_claims_are_logged_with_counts_and_codes_only(self):
        fixture = self.fixture(pages=10, sentences=1, filler=900)

        def change(answer, data):
            answer["claims"][1] = {**answer["claims"][1], "text": "A paraphrase the page never says."}
            return answer

        self.altered(fixture, change)
        with self.assertLogs("lifecontext.wiki", "WARNING") as logged:
            fixture.update()
        dropped = [line for line in logged.output if "dropped" in line]
        self.assertEqual(len(dropped), 1)
        self.assertIn("doc_1", dropped[0])
        self.assertIn("1 of 4", dropped[0])
        self.assertIn("wiki claim requires literal quote support", dropped[0])
        self.assertNotIn("paraphrase", dropped[0])
        self.assertNotIn("fact", dropped[0])

    def test_a_valid_contradiction_does_not_rescue_a_window_without_a_valid_claim(self):
        fixture = self.fixture(pages=3, sentences=2, filler=100)

        def change(answer, data):
            side = answer["claims"][0]
            answer["contradictions"] = [{"left": side, "right": side, "left_date": "2024-01-01", "right_date": "2025-06-30"}]
            answer["claims"] = [{**claim, "text": "A paraphrase the page never says."} for claim in answer["claims"]]
            return answer

        self.altered(fixture, change)
        with self.assertRaises(WikiError):
            fixture.update()
        self.assertEqual(fixture.catalog.get_document_stage("doc_1", 1, "wiki")["state"], "failed")
        # The window was regenerated once before the document failed.
        self.assertEqual(len(fixture.service.compiler.queries["window"]), 2)


if __name__ == "__main__":
    unittest.main()
