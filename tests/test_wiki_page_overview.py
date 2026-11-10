"""A document page says what its claims cover."""
import unittest

import test_wiki_claims_per_window

from lifecontext.wiki import coverage_line, source_page_id


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

if __name__ == "__main__":
    unittest.main()
