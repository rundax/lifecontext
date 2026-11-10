"""A document page says what its claims cover; the index counts the documents it has no room for."""
import asyncio
import unittest
from unittest.mock import patch

import test_wiki
import test_wiki_claims_per_window

from lifecontext.wiki import INDEX_MORE, coverage_line, source_page_id


def page(number):
    return {"kind": "page", "start": number, "end": number, "container": None, "schema_version": 1}


class CoverageLineTests(unittest.TestCase):
    def test_line_counts_cited_pages_and_names_unread_ones(self):
        locators = [page(n) for n in range(1, 11)]
        coverage = {"unit": "page", "total": 12, "unresolved": ["page:11", "page:12"]}
        self.assertEqual(coverage_line(locators, coverage, [(0, 1), (5, 7)]),
                         "Claims come from 3 of 12 pages; not read: pages 11, 12.")
        self.assertEqual(coverage_line(locators, {"unit": None}, [(0, 1)]), "Claims come from 1 of 10 pages.")

    def test_documents_without_pages_get_no_line(self):
        lines = [{"kind": "lines", "start": 1, "end": 3, "container": None, "schema_version": 1}]
        self.assertIsNone(coverage_line(lines, {"unit": None, "unresolved": []}, [(0, 1)]))


class PageOverviewTests(unittest.TestCase):
    def test_a_paged_document_shows_its_coverage_under_the_title(self):
        fixture = test_wiki_claims_per_window.ClaimsPerWindowTests.fixture(self, pages=10, sentences=1, filler=900)
        fixture.update()
        managed = fixture.service.show(source_page_id("doc_1"))["managed"]
        self.assertIn("# Original\n\nClaims come from 4 of 10 pages.\n\n", managed)

    def test_the_index_counts_documents_it_has_no_room_for(self):
        fixture = test_wiki.WikiTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture.update()
        self.assertNotIn("more: `lifecontext documents list`", fixture.service.show("index")["managed"])
        citation = fixture.add_evidence("doc_2", "Grass is green.")
        fixture.value = {"page_id": source_page_id("doc_2"), "title": "Original", "expected_revision": 0,
                         "claims": [{"text": "Grass is green.", "citations": [citation]}], "contradictions": [], "links": []}
        with patch("lifecontext.wiki.MAX_INDEX_ENTRIES", 1):
            asyncio.run(fixture.service.update_for_document("doc_2", 1))
        managed = fixture.service.show("index")["managed"]
        self.assertEqual(len(fixture.current("index").links), 1)
        self.assertIn("\n\n" + INDEX_MORE.format(count=1) + "\n", managed)
        self.assertIn("And 1 more: `lifecontext documents list`", managed)


if __name__ == "__main__":
    unittest.main()
