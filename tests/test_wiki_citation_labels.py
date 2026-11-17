"""People's citation labels on wiki pages: title, page, slide or sheet; machine identity unchanged."""
import asyncio
import json
import unittest
from dataclasses import replace

import test_wiki

from lifecontext.wiki import (
    Citation,
    WikiPatch,
    citation_label,
    citation_place,
    render_managed,
)

LINK = "../Sources/Original.md"


def locator(kind, start, end=None, container=None):
    return {"kind": kind, "start": start, "end": start if end is None else end, "container": container, "schema_version": 1}


class CitationPlaceTests(unittest.TestCase):
    def test_places_count_the_way_people_do(self):
        self.assertEqual(citation_place(locator("page", 3), locator("page", 3)), "p. 3")
        self.assertEqual(citation_place(locator("page", 3), locator("page", 4)), "pp. 3–4")
        self.assertEqual(citation_place(locator("slide", 2), locator("slide", 2)), "slide 2")
        self.assertEqual(citation_place(locator("sheet_cell", 1, container="Budget!B4"),
                                        locator("sheet_cell", 2, container="Budget!B5")), "sheet Budget")
        self.assertEqual(citation_place(locator("lines", 10, 12), locator("lines", 13, 14)), "lines 10–14")
        self.assertIsNone(citation_place({}, {}))

    def test_label_names_the_title_and_a_version_only_when_asked(self):
        self.assertEqual(citation_label("Lease  agreement", "p. 3"), "Lease agreement, p. 3")
        self.assertEqual(citation_label("Lease", "p. 3", 2), "Lease v2, p. 3")
        self.assertEqual(citation_label("Lease", None), "Lease")
        long = citation_label("A" * 100, "p. 1")
        self.assertTrue(long.endswith("…, p. 1"))
        self.assertLess(len(long), 70)

    def test_without_a_label_resolver_the_machine_label_is_kept(self):
        citation = {"source_id": "doc_1", "source_version": 1, "extraction_revision": 1, "passage_start": 0,
                    "passage_end": 1, "original_sha256": "a" * 64, "quote": "The sky is blue."}
        patch = WikiPatch.parse(json.dumps({"page_id": test_wiki.PAGE, "title": "Original", "expected_revision": 0,
                                            "claims": [{"text": "The sky is blue.", "citations": [citation]}],
                                            "contradictions": [], "links": []}))
        self.assertIn("1. The sky is blue. [doc_1 v1/e1 passages 0:1]", render_managed(patch))
        # Plain text carries no Markdown escapes; a Markdown link escapes the brackets of its label.
        labelled = render_managed(patch, citation_label=lambda c: "Title [draft], p. 1")
        self.assertIn("1. The sky is blue. [Title [draft], p. 1]", labelled)
        linked = render_managed(patch, citation_label=lambda c: "Title [draft], p. 1", citation_link=lambda c: LINK)
        self.assertIn("1. The sky is blue. ([Title \\[draft\\], p. 1](../Sources/Original.md))", linked)

    def test_a_source_tag_tells_same_titled_documents_apart(self):
        self.assertEqual(citation_label("Lease", "p. 3", None, "doc_1a2b"), "Lease (doc_1a2b), p. 3")
        self.assertEqual(citation_label("Lease", "p. 3", 2, "doc_1a2b"), "Lease v2 (doc_1a2b), p. 3")

    def test_word_paragraph_and_table_cell_numbers_are_not_a_place(self):
        self.assertIsNone(citation_place(locator("paragraph", 14, container="section:1/body"),
                                         locator("paragraph", 15, container="section:1/body")))
        self.assertIsNone(citation_place(locator("table_cell", 1, container="section:1/body/table:2/row:1/col:3"),
                                         locator("table_cell", 1, container="section:1/body/table:2/row:1/col:3")))


class ServiceLabelTests(unittest.TestCase):
    def fixture(self):
        fixture = test_wiki.WikiTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        return fixture

    def test_show_labels_with_page_and_keeps_the_stored_citation(self):
        fixture = self.fixture()
        fixture.payload["segments"][0]["locator"] = locator("page", 7)
        citation = fixture.add_evidence("doc_2", "Rent is due monthly.")
        fixture.value = {"page_id": "source_doc_2", "title": "Lease", "expected_revision": 0, "contradictions": [],
                         "links": [], "claims": [{"text": "Rent is due monthly.", "citations": [citation]}]}
        asyncio.run(fixture.service.update_for_document("doc_2", 1))
        managed = fixture.service.show("source_doc_2")["managed"]
        self.assertIn("1. Rent is due monthly. [Original, p. 7]", managed)
        self.assertNotIn("passages", managed)
        stored = fixture.current("source_doc_2").claims[0].citations[0]
        self.assertEqual(stored, Citation(**citation))

    def test_version_is_named_only_when_the_document_has_several(self):
        fixture = self.fixture()
        fixture.update()
        self.assertIn("[Original, line 1]", fixture.service.show(test_wiki.PAGE)["managed"])
        citation = fixture.add_evidence("doc_1", "The sky is grey.", version=2)
        fixture.value = {**fixture.value, "expected_revision": 1,
                         "claims": [{"text": "The sky is grey.", "citations": [citation]}]}
        asyncio.run(fixture.service.update_for_document("doc_1", 2))
        self.assertIn("1. The sky is grey. [Original v2, line 1]", fixture.service.show(test_wiki.PAGE)["managed"])

    def plain_page(self, fixture, *citations):
        """A page that is neither a document's own nor a shared one, citing each text as given."""
        claims = [{"text": c["quote"], "citations": [c]} for c in citations]
        fixture.commit("plain_page", "Plain", claims)
        return fixture.service.show("plain_page")

    def test_a_missing_document_page_falls_back_to_the_metadata_title(self):
        fixture = self.fixture()
        shown = self.plain_page(fixture, fixture.add_evidence("doc_2", "Grass is green."))
        self.assertIn("1. Grass is green. [Original, line 1]", shown["managed"])

    def test_word_citations_carry_the_title_alone(self):
        fixture = self.fixture()
        fixture.payload["segments"][0]["locator"] = locator("paragraph", 14, container="section:1/body")
        shown = self.plain_page(fixture, fixture.add_evidence("doc_2", "Grass is green."))
        self.assertIn("1. Grass is green. [Original]", shown["managed"])

    def test_an_unreadable_artifact_gives_a_title_only_label(self):
        fixture = self.fixture()
        citation = fixture.add_evidence("doc_2", "Grass is green.")
        shown = self.plain_page(fixture, citation)
        artifact = fixture.paths.lifeweave_home / fixture.catalog.list_extractions("doc_2", 1)[-1]["artifact_path"]
        artifact.write_text("{not json")
        fixture.service._locator_cache.clear()
        self.assertIn("1. Grass is green. [Original]", fixture.service.show("plain_page")["managed"])
        self.assertIn("[Original, line 1]", shown["managed"])
        artifact.unlink()
        fixture.service._locator_cache.clear()
        self.assertIn("1. Grass is green. [Original]", fixture.service.show("plain_page")["managed"])

    def test_a_passage_range_beyond_the_locators_gives_a_title_only_label(self):
        fixture = self.fixture()
        fixture.update()
        label = fixture.service._citation_labels()
        stored = fixture.current().claims[0].citations[0]
        self.assertEqual(label(stored), "Original, line 1")
        self.assertEqual(label(replace(stored, passage_end=9)), "Original")

    def test_two_documents_with_the_same_title_get_a_source_tag_on_that_page_only(self):
        fixture = self.fixture()
        grass = fixture.add_evidence("doc_2", "Grass is green.")
        shown = self.plain_page(fixture, grass, fixture.add_evidence("doc_3", "Water is wet."))
        self.assertIn("1. Grass is green. [Original (doc_2), line 1]", shown["managed"])
        self.assertIn("2. Water is wet. [Original (doc_3), line 1]", shown["managed"])
        # A page that cites one of them alone needs no tag.
        fixture.commit("other_page", "Other", [{"text": "Grass is green.", "citations": [grass]}])
        self.assertIn("[Original, line 1]", fixture.service.show("other_page")["managed"])

    def test_show_lists_the_source_behind_each_label(self):
        fixture = self.fixture()
        shown = self.plain_page(fixture, fixture.add_evidence("doc_2", "Grass is green."),
                                fixture.add_evidence("doc_3", "Water is wet."))
        self.assertEqual(shown["citations"], [
            {"claim": 1, "source_id": "doc_2", "source_version": 1, "extraction_revision": 1, "passage_start": 0,
             "passage_end": 1, "label": "Original (doc_2), line 1"},
            {"claim": 2, "source_id": "doc_3", "source_version": 1, "extraction_revision": 1, "passage_start": 0,
             "passage_end": 1, "label": "Original (doc_3), line 1"}])


if __name__ == "__main__":
    unittest.main()
