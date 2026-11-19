"""A model quote that matches its passage once normalised, or nearly, is stored as the source text."""
import unittest

import test_wiki
from wiki_fake_compiler import FakeCompiler


class WindowFixture(unittest.TestCase):
    def segments(self, texts):
        """doc_1 extraction revision 2 with one segment per text, in one evidence window."""
        fixture = test_wiki.WikiTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        segments, offset = [], 0
        for number, text in enumerate(texts, 1):
            segments.append({"text": text, "start": offset, "end": offset + len(text),
                             "locator": {"kind": "page", "start": number, "end": number, "container": None,
                                         "schema_version": 1}})
            offset += len(text)
        stored = fixture.derived.write("doc_1", 1, 2, {**fixture.payload, "extraction_revision": 2, "segments": segments})
        fixture.catalog.record_extraction("doc_1", 1, 2, original_sha256=fixture.digest, extractor="pdf",
                                          extractor_version="1", config_sha256="c" * 64,
                                          artifact_path=str(stored.path.relative_to(fixture.paths.lifeweave_home)),
                                          artifact_sha256=stored.sha256, status="completed", passage_count=len(texts))
        fixture.catalog.record_provider_chunk("chunk_texts", "doc_1", 1, 2, passage_start=0, passage_end=len(texts),
                                              content_sha256="b" * 64)
        fixture.catalog.update_provider_chunk("chunk_texts", expected_revision=1, submission_state="submitted",
                                              completion_state="completed")
        fixture.service.compiler = FakeCompiler({})
        return fixture


SENTENCE = "The tenant said the rent of 900 euros is due on the first day of each month."


class QuoteMatchTests(WindowFixture):
    def answer(self, fixture, *items):
        """The model answers ``items`` (text, quote) on page 1, then a valid claim on its first sentence,
        which the page keeps when no item is valid (the window is asked for one claim)."""
        def window(data):
            citation = data["example_patch"]["claims"][0]["citations"][0]
            self.anchor = data["evidence"]["segments"][0]["text"].split(".")[0]

            def claim(text, quote):
                return {"text": text, "citations": [{**citation, "passage_start": 0, "passage_end": 1, "quote": quote}]}
            return {**data["example_patch"], "claims": [*(claim(text, quote) for text, quote in items),
                                                        claim(self.anchor, self.anchor)]}
        fixture.service.compiler.window = window

    def claims(self, fixture):
        """The page's claims other than the fallback on the first sentence."""
        return [(claim.text, claim.citations[0].quote) for claim in fixture.current().claims if claim.text != self.anchor]

    def test_a_quote_that_matches_once_normalised_is_stored_as_the_exact_source_text(self):
        source = "Header.\nThe tenant said “the rent is due” on the first—and   only—day of each infor-\nmation cycle."
        fixture = self.segments([source])
        quote = 'The tenant said "the rent is due" on the first-and only-day of each information cycle.'
        self.answer(fixture, ('"the rent is due" on the first', quote))
        fixture.update()
        stored_quote = source[len("Header.\n"):]
        self.assertEqual(self.claims(fixture), [("“the rent is due” on the first", stored_quote)])
        self.assertIn(stored_quote, source)

    def test_a_near_quote_is_stored_as_the_source_span_and_its_claim_taken_from_it(self):
        fixture = self.segments([f"Intro line. {SENTENCE} Closing words here."])
        typo = SENTENCE.replace("tenant", "tennant").replace("each", "eahc")
        self.answer(fixture, (typo, typo))
        fixture.update()
        self.assertEqual(self.claims(fixture), [(SENTENCE, SENTENCE)])

    def test_an_ambiguous_near_quote_is_rejected(self):
        twice = SENTENCE.replace("900", "950")
        fixture = self.segments([f"{SENTENCE} Then, a year later: {twice}"])
        typo = SENTENCE.replace("900", "9O0").replace("tenant", "tennant")
        self.answer(fixture, (typo, typo))
        fixture.update()
        self.assertEqual(self.claims(fixture), [])

    def test_a_paraphrase_is_rejected(self):
        fixture = self.segments([SENTENCE])
        paraphrase = "On the first day of each month the tenant said the rent of 900 euros is due."
        self.answer(fixture, (paraphrase, paraphrase), ("Rent is due monthly.", "the rent of 900 euros is due"))
        fixture.update()
        self.assertEqual(self.claims(fixture), [])

    def test_a_short_quote_is_never_near_matched(self):
        fixture = self.segments([SENTENCE])
        self.answer(fixture, ("rent of 9OO euros", "rent of 9OO euros"))
        fixture.update()
        self.assertEqual(self.claims(fixture), [])


if __name__ == "__main__":
    unittest.main()
