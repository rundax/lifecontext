"""The index fills its 48 KB page entry by entry, checks only what fits, and history files stay put."""
import asyncio
import unittest
from unittest.mock import patch

import test_wiki

from lifecontext.wiki import (
    INDEX_MORE,
    INDEX_PAGE,
    MIN_INDEX_ENTRY_BYTES,
    WikiPatch,
    _patch_json,
    source_page_id,
)


class IndexTests(unittest.TestCase):
    def fixture(self):
        fixture = test_wiki.WikiTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        return fixture

    def document(self, fixture, number, title, text):
        """A document with its own page of one claim, published directly."""
        sid = f"doc_{number}"
        citation = fixture.add_evidence(sid, text)
        fixture.commit(source_page_id(sid), title, [{"text": text, "citations": [citation]}])
        return source_page_id(sid)

    def test_an_entry_too_big_for_the_page_is_skipped_and_later_small_ones_still_fit(self):
        fixture = self.fixture()
        a = self.document(fixture, 2, "A small", "Grass is green.")
        big = self.document(fixture, 3, "B big", "Water is wet. " + "x" * 1900)
        c = self.document(fixture, 4, "C small", "Fire is hot.")
        d = self.document(fixture, 5, "D small", "Ice is cold.")
        claims = [fixture.current(page).claims[0] for page in (a, c)]
        two = len(_patch_json(WikiPatch(INDEX_PAGE, "Index", 0, tuple(claims), (), (a, c))).encode())
        # Room for two small entries and less than a minimal entry besides.
        with patch("lifecontext.wiki.MAX_PATCH_BYTES", two + MIN_INDEX_ENTRY_BYTES - 1), \
                patch.object(fixture.service, "_validate", wraps=fixture.service._validate) as validated:
            fixture.service._publish_index()
        self.assertEqual(fixture.current(INDEX_PAGE).links, (a, c))
        self.assertNotIn(big, fixture.current(INDEX_PAGE).links)
        self.assertNotIn(d, fixture.current(INDEX_PAGE).links)
        # Only the entries that fit were checked against their originals.
        self.assertEqual(validated.call_count, 2)

    def test_a_smaller_entry_after_a_skipped_one_still_fits(self):
        fixture = self.fixture()
        a = self.document(fixture, 2, "A small", "Grass is green.")
        big = self.document(fixture, 3, "B big", "Water is wet. " + "x" * 1900)
        c = self.document(fixture, 4, "C small", "Fire is hot.")
        claims = [fixture.current(page).claims[0] for page in (a, c)]
        two = len(_patch_json(WikiPatch(INDEX_PAGE, "Index", 0, tuple(claims), (), (a, c))).encode())
        with patch("lifecontext.wiki.MAX_PATCH_BYTES", two + MIN_INDEX_ENTRY_BYTES + 100):
            fixture.service._publish_index()
        self.assertEqual(fixture.current(INDEX_PAGE).links, (a, c))
        self.assertNotIn(big, fixture.current(INDEX_PAGE).links)

    def test_no_index_entry_is_smaller_than_the_floor(self):
        fixture = self.fixture()
        a = self.document(fixture, 2, "A", "Hi.")
        claim = fixture.current(a).claims[0]
        empty = len(_patch_json(WikiPatch(INDEX_PAGE, "Index", 0, (), (), ())).encode())
        one = len(_patch_json(WikiPatch(INDEX_PAGE, "Index", 0, (claim,), (), (a,))).encode())
        self.assertGreaterEqual(one - empty, MIN_INDEX_ENTRY_BYTES)

    def test_history_revisions_do_not_carry_todays_count_of_unlisted_documents(self):
        fixture = self.fixture()
        fixture.update()
        citation = fixture.add_evidence("doc_0", "Grass is green.")
        fixture.value = {"page_id": source_page_id("doc_0"), "title": "Original", "expected_revision": 0,
                         "claims": [{"text": "Grass is green.", "citations": [citation]}], "contradictions": [], "links": []}
        with patch("lifecontext.wiki.MAX_INDEX_ENTRIES", 1):
            asyncio.run(fixture.service.update_for_document("doc_0", 1))
        self.assertEqual(fixture.catalog.get_wiki_page(INDEX_PAGE)["current_revision"], 2)
        more = INDEX_MORE.format(count=1)
        self.assertIn(more, fixture.service.show(INDEX_PAGE)["managed"])
        self.assertIn(more, fixture.service.show(INDEX_PAGE, 2)["managed"])
        self.assertNotIn("more:", fixture.service.show(INDEX_PAGE, 1)["managed"])
        # The vault renders the current revision's history file the same way it will once superseded.
        self.assertNotIn("more:", fixture.service.show(INDEX_PAGE, 2, history=True)["managed"])
        self.assertEqual(fixture.service.show(INDEX_PAGE, 1)["managed"], fixture.service.show(INDEX_PAGE, 1, history=True)["managed"])


if __name__ == "__main__":
    unittest.main()
