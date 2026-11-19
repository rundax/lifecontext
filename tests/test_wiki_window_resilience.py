"""One bad model answer no longer discards the wiki page: skipped windows, kept windows, matched quotes."""
import asyncio
import json
import unittest

import test_wiki
import test_wiki_claims_per_window
from wiki_fake_compiler import FakeCompiler

from lifecontext.derived import WIKI_WINDOWS
from lifecontext.wiki import WikiError, WikiProviderError, source_page_id

PAGE = source_page_id("doc_1")


def data_of(query):
    return json.loads(query.split("UNTRUSTED_DATA=", 1)[1]) if "UNTRUSTED_DATA=" in query else {}


class WindowFixture(unittest.TestCase):
    def pages(self, pages, *, model="fake-model"):
        """doc_1 with ``pages`` one-page evidence windows of five facts each, compiled by the fake model."""
        # 12.5 KB of filler keeps two pages over the 24 KB window budget.
        fixture = test_wiki_claims_per_window.ClaimsPerWindowTests.fixture(self, pages=pages, sentences=5, filler=12_500)
        fixture.service.compiler.model = model
        return fixture

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

    def asking(self, fixture, fail=lambda data: None):
        """Route the fixture's model calls through ``fail(data)``, which may raise; returns the window asks."""
        compiler, asked = fixture.service.compiler, []

        async def ask(query, **kwargs):
            data = data_of(query)
            if "evidence" in data:
                asked.append(data["evidence"]["segments"][0]["passage_index"])
                fail(data)
            return await compiler(query, **kwargs)

        if hasattr(compiler, "model"):
            ask.model = compiler.model
        fixture.service.compiler = ask
        return asked

    @staticmethod
    def pages_cited(fixture):
        return sorted({claim.citations[0].passage_start for claim in fixture.current().claims})

    @staticmethod
    def record(fixture):
        return fixture.derived.read_wiki_windows("doc_1", 1)


class SkippedWindowTests(WindowFixture):
    def test_a_window_that_still_fails_is_skipped_and_the_page_publishes_from_the_others(self):
        fixture = self.pages(4)

        def fail(data):
            if data["evidence"]["segments"][0]["passage_index"] == 2:
                raise RuntimeError("bad request")

        asked = self.asking(fixture, fail)
        with self.assertLogs("lifecontext.wiki", "WARNING") as logged:
            fixture.update()
        self.assertEqual(fixture.catalog.get_document_stage("doc_1", 1, "wiki")["state"], "completed")
        self.assertEqual(self.pages_cited(fixture), [0, 1, 3])
        self.assertEqual(fixture.service.window_report("doc_1", 1), {"skipped": 1, "dropped": 0})
        self.assertIn("WARNING:lifecontext.wiki:Wiki window 3 of doc_1 skipped: RuntimeError", logged.output)
        # The provider's own retries are the compiler's; the window itself was asked once.
        self.assertEqual(asked.count(2), 1)

    def test_after_two_windows_in_a_row_fail_at_the_provider_the_rest_are_skipped_unasked(self):
        fixture = self.pages(6)

        def fail(data):
            if data["evidence"]["segments"][0]["passage_index"] >= 2:
                raise ConnectionError("down")

        asked = self.asking(fixture, fail)
        fixture.update()
        self.assertEqual(asked, [0, 1, 2, 3])
        self.assertEqual(self.pages_cited(fixture), [0, 1])
        self.assertEqual(fixture.service.window_report("doc_1", 1)["skipped"], 4)

    def test_when_no_window_validates_the_stage_keeps_todays_failure_code(self):
        fixture = self.pages(2)

        def fail(data):
            raise ConnectionError("down")

        self.asking(fixture, fail)
        with self.assertRaises(WikiError):
            fixture.update()
        stage = fixture.catalog.get_document_stage("doc_1", 1, "wiki")
        self.assertEqual((stage["state"], stage["error_code"]), ("failed", "wiki_provider_failed"))
        self.assertIsNone(fixture.catalog.get_wiki_page(PAGE))

    def test_a_retry_asks_only_for_the_skipped_window_and_fills_it(self):
        fixture, down = self.pages(4), [True]

        def fail(data):
            if down[0] and data["evidence"]["segments"][0]["passage_index"] == 2:
                raise WikiProviderError("TimeoutError")

        asked = self.asking(fixture, fail)
        fixture.update()
        self.assertNotIn(2, self.pages_cited(fixture))
        down[0] = False
        asked.clear()
        fixture.update()
        self.assertEqual(asked, [2])
        self.assertEqual(self.pages_cited(fixture), [0, 1, 2, 3])
        self.assertEqual(fixture.service.window_report("doc_1", 1), {"skipped": 0, "dropped": 0})


class KeptWindowTests(WindowFixture):
    def test_a_rebuild_asks_the_model_only_for_windows_it_does_not_have(self):
        fixture = self.pages(4)
        asked = self.asking(fixture)
        fixture.update()
        self.assertEqual(len(asked), 4)
        first = fixture.current().claims
        asked.clear()
        fixture.update()
        self.assertEqual(asked, [])
        self.assertEqual(fixture.current().claims, first)
        # Another model is asked again for every window.
        fixture.service.compiler.model = "other-model"
        fixture.update()
        self.assertEqual(len(asked), 4)

    def test_without_a_model_name_nothing_is_reused(self):
        fixture = self.pages(2, model=None)
        del fixture.service.compiler.model
        asked = self.asking(fixture)
        fixture.update()
        fixture.update()
        self.assertEqual(len(asked), 4)

    def test_a_kept_window_whose_claims_no_longer_validate_is_asked_again(self):
        fixture = self.pages(3)
        asked = self.asking(fixture)
        fixture.update()
        record = self.record(fixture)
        key, entry = next((key, entry) for key, entry in record["windows"].items()
                          if entry["patch"]["claims"][0]["citations"][0]["passage_start"] == 1)
        claim = entry["patch"]["claims"][0]
        claim["citations"][0]["quote"] = claim["text"] = "A sentence page 2 never had."
        fixture.derived.write_wiki_windows("doc_1", 1, record)
        asked.clear()
        fixture.update()
        self.assertEqual(asked, [1])
        self.assertNotIn("A sentence page 2 never had.", [claim.text for claim in fixture.current().claims])
        self.assertEqual(self.record(fixture)["windows"][key]["patch"]["claims"][0]["citations"][0]["passage_start"], 1)

    def test_kept_windows_from_another_extraction_are_not_used(self):
        fixture = self.pages(2)
        asked = self.asking(fixture)
        fixture.update()
        record = self.record(fixture)
        fixture.derived.write_wiki_windows("doc_1", 1, {**record, "extraction_revision": 1})
        asked.clear()
        fixture.update()
        self.assertEqual(len(asked), 2)

    def test_the_record_sits_beside_the_extractions_and_stays_out_of_their_inventory(self):
        fixture = self.pages(2)
        fixture.update()
        path = fixture.paths.lifeweave_home / "derived" / "doc_1" / "v1" / WIKI_WINDOWS
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(fixture.derived.list_inventory(), [("doc_1", 1, 1), ("doc_1", 1, 2)])
        fixture.derived.remove_wiki_windows("doc_1")
        self.assertFalse(path.exists())
        self.assertEqual(fixture.derived.list_inventory(), [("doc_1", 1, 1), ("doc_1", 1, 2)])


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

    def dropped(self, fixture):
        entries = self.record(fixture)["windows"].values()
        return [item["reason"] for entry in entries for item in entry["dropped"]]

    def test_a_quote_that_matches_once_normalised_is_stored_as_the_exact_source_text(self):
        source = "Header.\nThe tenant said “the rent is due” on the first—and   only—day of each infor-\nmation cycle."
        fixture = self.segments([source])
        quote = 'The tenant said "the rent is due" on the first-and only-day of each information cycle.'
        self.answer(fixture, ('"the rent is due" on the first', quote))
        fixture.update()
        stored_quote = source[len("Header.\n"):]
        self.assertEqual(self.claims(fixture), [("“the rent is due” on the first", stored_quote)])
        self.assertIn(stored_quote, source)
        self.assertEqual(self.dropped(fixture), [])

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
        self.assertEqual(self.dropped(fixture), ["quote_not_found"])

    def test_a_paraphrase_is_rejected(self):
        fixture = self.segments([SENTENCE])
        paraphrase = "On the first day of each month the tenant said the rent of 900 euros is due."
        self.answer(fixture, (paraphrase, paraphrase), ("Rent is due monthly.", "the rent of 900 euros is due"))
        fixture.update()
        self.assertEqual(self.claims(fixture), [])
        self.assertEqual(self.dropped(fixture), ["quote_not_found", "claim_not_in_quote"])

    def test_a_short_quote_is_never_near_matched(self):
        fixture = self.segments([SENTENCE])
        self.answer(fixture, ("rent of 9OO euros", "rent of 9OO euros"))
        fixture.update()
        self.assertEqual(self.claims(fixture), [])
        self.assertEqual(self.dropped(fixture), ["quote_not_found"])

    def test_dropped_claims_are_kept_with_their_reason_and_counted_without_their_text(self):
        fixture = self.segments([SENTENCE])
        self.answer(fixture, ("An invented claim.", "An invented quote the page lacks entirely."))
        with self.assertLogs("lifecontext.wiki", "WARNING") as logged:
            fixture.update()
        entry = next(iter(self.record(fixture)["windows"].values()))
        self.assertEqual(entry["dropped"], [{"kind": "claim", "reason": "quote_not_found", "claim": {
            "text": "An invented claim.", "citations": [{
                "source_id": "doc_1", "source_version": 1, "extraction_revision": 2, "passage_start": 0,
                "passage_end": 1, "original_sha256": fixture.digest,
                "quote": "An invented quote the page lacks entirely."}]}}])
        self.assertEqual(fixture.service.window_report("doc_1", 1), {"skipped": 0, "dropped": 1})
        self.assertNotIn("invented", "\n".join(logged.output))


if __name__ == "__main__":
    unittest.main()
