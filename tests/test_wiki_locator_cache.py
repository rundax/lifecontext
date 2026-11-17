"""Labels read each extraction artifact about once, however many revisions of a page cite it."""
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import test_wiki

from lifecontext.derived import DerivedError


class LocatorCacheTests(unittest.TestCase):
    def fixture(self):
        fixture = test_wiki.WikiTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        return fixture

    def test_locators_are_read_about_once_per_document_however_many_revisions_cite_them(self):
        fixture = self.fixture()
        citations = [fixture.add_evidence(f"doc_{n}", f"Fact {n} holds.") for n in range(2, 14)]
        for count in range(1, len(citations) + 1):
            claims = [{"text": c["quote"], "citations": [c]} for c in citations[:count]]
            fixture.commit("plain_page", "Plain", claims)
        fixture.service._locator_cache.clear()
        with patch.object(fixture.derived, "read", wraps=fixture.derived.read) as read:
            for revision in range(1, len(citations) + 1):
                fixture.service.show("plain_page", revision)
        # 12 documents over 12 revisions: one read each, not 1 + 2 + ... + 12.
        self.assertEqual(read.call_count, len(citations))

    def test_threads_share_a_bounded_cache(self):
        fixture = self.fixture()
        for n in range(2, 14):
            fixture.add_evidence(f"doc_{n}", f"Fact {n} holds.")
        rows = {f"doc_{n}": fixture.catalog.list_extractions(f"doc_{n}", 1) for n in range(2, 14)}
        service = fixture.service

        def view(n):
            return service._extraction_view(f"doc_{2 + n % 12}", 1, 1)

        with patch.object(service.catalog, "list_extractions", side_effect=lambda sid, version: rows[sid]), \
                patch("lifecontext.wiki.LOCATOR_CACHE_ENTRIES", 4), ThreadPoolExecutor(8) as pool:
            views = list(pool.map(view, range(400)))
        self.assertTrue(all(item is not None and item["locators"] for item in views))
        self.assertLessEqual(len(service._locator_cache), 4)

    def test_a_damaged_artifact_is_opened_once_not_on_every_render(self):
        fixture = self.fixture()
        citation = fixture.add_evidence("doc_2", "Grass is green.")
        fixture.commit("plain_page", "Plain", [{"text": "Grass is green.", "citations": [citation]}])
        artifact = fixture.paths.lifeweave_home / fixture.catalog.list_extractions("doc_2", 1)[-1]["artifact_path"]
        artifact.write_text("{not json")
        with patch.object(fixture.derived, "read", wraps=fixture.derived.read) as read:
            first = fixture.service.show("plain_page")["managed"]
            second = fixture.service.show("plain_page")["managed"]
        self.assertIn("1. Grass is green. [Original]", first)
        self.assertEqual(first, second)
        self.assertEqual(read.call_count, 1)

    def test_a_storage_error_is_not_remembered(self):
        fixture = self.fixture()
        citation = fixture.add_evidence("doc_2", "Grass is green.")
        fixture.commit("plain_page", "Plain", [{"text": "Grass is green.", "citations": [citation]}])
        real = fixture.derived.read
        outage = [True]

        def read(*args, **kwargs):
            if outage[0]:
                raise DerivedError("unsafe derived storage")
            return real(*args, **kwargs)

        with patch.object(fixture.derived, "read", side_effect=read):
            self.assertIn("1. Grass is green. [Original]", fixture.service.show("plain_page")["managed"])
            outage[0] = False
            self.assertIn("1. Grass is green. [Original, line 1]", fixture.service.show("plain_page")["managed"])

    def test_the_cache_is_bounded_by_the_locators_it_holds(self):
        fixture = self.fixture()
        for n in range(2, 8):
            fixture.add_evidence(f"doc_{n}", f"Fact {n} holds.")
        with patch("lifecontext.wiki.LOCATOR_CACHE_LOCATORS", 3):
            for n in range(2, 8):
                self.assertIsNotNone(fixture.service._extraction_view(f"doc_{n}", 1, 1))
        # One locator each: the three most recently used stay.
        self.assertEqual([key[0] for key in fixture.service._locator_cache], ["doc_5", "doc_6", "doc_7"])

    def test_an_extraction_larger_than_the_cap_is_read_each_time_not_cached(self):
        fixture = self.fixture()
        fixture.add_evidence("doc_2", "Fact holds.")
        with patch("lifecontext.wiki.LOCATOR_CACHE_LOCATORS", 0), \
                patch.object(fixture.derived, "read", wraps=fixture.derived.read) as read:
            first = fixture.service._extraction_view("doc_2", 1, 1)
            second = fixture.service._extraction_view("doc_2", 1, 1)
        self.assertEqual(first, second)
        self.assertEqual(len(first["locators"]), 1)
        self.assertEqual(len(fixture.service._locator_cache), 0)
        self.assertEqual(read.call_count, 2)


if __name__ == "__main__":
    unittest.main()
