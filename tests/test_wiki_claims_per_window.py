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

    def test_one_window_claim_failing_its_quote_still_rejects_the_response(self):
        fixture = self.fixture(pages=3, sentences=2, filler=100)
        compiler = fixture.service.compiler
        original = compiler.window

        def window(data):
            answer = original(data)
            answer["claims"][-1] = {**answer["claims"][-1], "text": "A paraphrase the page never says."}
            return answer

        compiler.window = window
        with self.assertRaises(WikiError):
            fixture.update()
        self.assertEqual(fixture.catalog.get_document_stage("doc_1", 1, "wiki")["state"], "failed")
        self.assertIsNone(fixture.catalog.get_wiki_page(source_page_id("doc_1")))


if __name__ == "__main__":
    unittest.main()
