"""One bad model answer no longer discards the wiki page: skipped windows, kept windows, matched quotes."""
import asyncio
import json
import time
import unittest
from unittest.mock import AsyncMock, patch

import test_wiki
import test_wiki_claims_per_window
from wiki_fake_compiler import FakeCompiler

from lifecontext.derived import WIKI_WINDOWS
from lifecontext.wiki import (
    Citation,
    Claim,
    WikiError,
    WikiProviderError,
    _locate,
    match_budget,
    source_page_id,
)

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

    def test_a_document_withdrawn_during_a_compile_is_never_sent_again(self):
        for withdrawn_at in (0, 2):
            with self.subTest(window=withdrawn_at):
                fixture = self.pages(5)

                def fail(data, fixture=fixture, withdrawn_at=withdrawn_at):
                    if data["evidence"]["segments"][0]["passage_index"] == withdrawn_at:
                        # Forgotten while the model reads this window.
                        fixture.catalog.source_withdrawn = lambda *args: True

                asked = self.asking(fixture, fail)
                with self.assertRaises(WikiError):
                    fixture.update()
                self.assertEqual(asked, list(range(withdrawn_at + 1)))
                stage = fixture.catalog.get_document_stage("doc_1", 1, "wiki")
                self.assertEqual((stage["state"], stage["error_code"]), ("failed", "wiki_update_rejected"))

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


class Flaky(Exception):
    """A failed compile call carrying the compiler's hint (see hindsight.compile_retry)."""

    def __init__(self, retry):
        super().__init__("provider text")
        self.retry = retry


class CompileRetryTests(WindowFixture):
    def run_with(self, pages, failures):
        """Compile while window ``i`` raises the errors ``failures[i]`` holds, in turn; returns the
        windows asked, in order, and the waits."""
        fixture = self.pages(pages)
        left = {window: list(errors) for window, errors in failures.items()}

        def fail(data):
            errors = left.get(data["evidence"]["segments"][0]["passage_index"])
            if errors:
                raise errors.pop(0)

        asked = self.asking(fixture, fail)
        with patch("lifecontext.wiki.asyncio.sleep", new=AsyncMock()) as sleep:
            fixture.update()
        return fixture, asked, [call.args[0] for call in sleep.await_args_list]

    def test_a_transient_failure_is_asked_again_with_backoff_or_retry_after(self):
        _, asked, waits = self.run_with(2, {0: [Flaky(("transient", None)), Flaky(("transient", None))]})
        self.assertEqual((asked, waits), ([0, 0, 0, 1], [2.0, 4.0]))
        fixture, asked, waits = self.run_with(2, {0: [Flaky(("transient", 7.0))]})
        self.assertEqual((asked, waits), ([0, 0, 1], [7.0]))
        self.assertEqual(self.pages_cited(fixture), [0, 1])

    def test_a_permanent_failure_is_not_asked_again(self):
        fixture, asked, waits = self.run_with(2, {0: [Flaky(None)]})
        self.assertEqual((asked, waits), ([0, 1], []))
        self.assertEqual(self.pages_cited(fixture), [1])

    def test_three_retries_at_most_and_a_timeout_once(self):
        _, asked, waits = self.run_with(2, {0: [Flaky(("transient", None))] * 5})
        self.assertEqual((asked, waits), ([0, 0, 0, 0, 1], [2.0, 4.0, 8.0]))
        _, asked, waits = self.run_with(2, {0: [Flaky(("timeout", None))] * 5})
        self.assertEqual((asked, waits), ([0, 0, 1], [2.0]))

    def test_one_compile_retries_six_times_in_all(self):
        three = [Flaky(("transient", None))] * 3
        _, asked, waits = self.run_with(4, {0: list(three), 1: list(three), 2: [Flaky(("transient", None))]})
        self.assertEqual(asked, [0, 0, 0, 0, 1, 1, 1, 1, 2, 3])
        self.assertEqual(len(waits), 6)

    def test_the_summary_and_routing_requests_are_never_asked_again(self):
        fixture = self.pages(2)
        compiler, summaries = fixture.service.compiler, []

        async def ask(query, **kwargs):
            if "UNTRUSTED_DATA=" in query and "evidence" not in data_of(query):
                summaries.append(query)
                raise Flaky(("transient", None))
            return await compiler(query, **kwargs)

        ask.model = compiler.model
        fixture.service.compiler = ask
        with patch("lifecontext.wiki.asyncio.sleep", new=AsyncMock()) as sleep:
            fixture.update()
        # One summary request and one routing request, each asked once; the page publishes.
        self.assertEqual(len(summaries), 2)
        sleep.assert_not_awaited()
        self.assertEqual(self.pages_cited(fixture), [0, 1])

    def test_a_document_deleted_during_the_wait_is_not_asked_again(self):
        fixture = self.pages(3)

        def fail(data):
            raise Flaky(("transient", 30.0))

        asked = self.asking(fixture, fail)

        async def wait(seconds):
            fixture.catalog.source_withdrawn = lambda *args: True

        with patch("lifecontext.wiki.asyncio.sleep", new=AsyncMock(side_effect=wait)), self.assertRaises(WikiError):
            fixture.update()
        self.assertEqual(asked, [0])


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

    def invalid(self, fixture, window):
        """The model's answers for ``window`` hold only a claim its page never says."""
        compiler = fixture.service.compiler
        original = compiler.window

        def answer(data):
            result = original(data)
            if data["evidence"]["segments"][0]["passage_index"] == window:
                result["claims"] = [{**claim, "text": "A sentence the page never says."} for claim in result["claims"]]
            return result

        compiler.window = answer

    def test_a_rejected_window_is_kept_as_rejected_and_not_asked_again(self):
        fixture = self.pages(3)
        self.invalid(fixture, 1)
        asked = self.asking(fixture)
        fixture.update()
        # Asked twice (the answer and its correction), rejected, not counted as skipped.
        self.assertEqual(asked, [0, 1, 1, 2])
        self.assertEqual(fixture.service.window_report("doc_1", 1)["skipped"], 0)
        asked.clear()
        fixture.update()
        self.assertEqual(asked, [])
        self.assertEqual(self.pages_cited(fixture), [0, 2])

    def test_without_any_page_rejected_windows_are_asked_again_by_a_retry(self):
        fixture = self.pages(2)
        self.invalid(fixture, 0)
        self.invalid(fixture, 1)
        asked = self.asking(fixture)
        for _ in range(2):
            with self.assertRaises(WikiError):
                fixture.update()
        self.assertEqual(asked, [0, 0, 1, 1] * 2)
        stage = fixture.catalog.get_document_stage("doc_1", 1, "wiki")
        self.assertEqual((stage["state"], stage["error_code"]), ("failed", "wiki_update_rejected"))

    def test_windows_paid_for_are_kept_when_the_compile_is_cancelled(self):
        fixture, cancel = self.pages(3), [True]

        def fail(data):
            if cancel[0] and data["evidence"]["segments"][0]["passage_index"] == 2:
                raise asyncio.CancelledError()

        asked = self.asking(fixture, fail)
        with self.assertRaises(asyncio.CancelledError):
            fixture.update()
        cancel[0] = False
        asked.clear()
        fixture.update()
        self.assertEqual(asked, [2])

    def test_a_kept_window_brings_no_summary_or_links(self):
        fixture = self.pages(2)
        fixture.update()
        record = self.record(fixture)
        for entry in record["windows"].values():
            entry["patch"]["summary"] = [{"text": "A summary nobody checked.", "claims": [1]}]
            entry["patch"]["links"] = ["topic_planted"]
        fixture.derived.write_wiki_windows("doc_1", 1, record)
        compiler = fixture.service.compiler

        async def no_summary(query, **kwargs):
            if "claims" in data_of(query) and "pages" not in data_of(query):
                raise ConnectionError("summary down")
            return await compiler(query, **kwargs)

        no_summary.model = compiler.model
        fixture.service.compiler = no_summary
        fixture.update()
        page = fixture.current()
        # The summary request failed, so nothing may stand where the model summary goes.
        self.assertEqual(page.summary, ())
        self.assertNotIn("topic_planted", page.links)

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

    def test_a_tampered_record_counts_as_none(self):
        fixture = self.pages(2)
        asked = self.asking(fixture)
        fixture.update()
        path = fixture.paths.lifeweave_home / "derived" / "doc_1" / "v1" / WIKI_WINDOWS
        record = self.record(fixture)
        malformed = {key: value for key, value in zip(record["windows"], ({"patch": 5, "dropped": "x"}, 7), strict=True)}
        for contents in ("[" * 100_000 + "]" * 100_000, "{not json",
                         json.dumps({**record, "windows": malformed}), json.dumps({**record, "windows": []})):
            with self.subTest(contents=contents[:20]):
                path.write_text(contents)
                path.chmod(0o600)
                self.assertEqual(fixture.service.window_report("doc_1", 1), {"skipped": 0, "dropped": 0})
                asked.clear()
                fixture.update()
                # Every window is asked again, and a sound record replaces the tampered one.
                self.assertEqual(asked, [0, 1])
                self.assertEqual(len(self.record(fixture)["windows"]), 2)

    def test_a_forget_removes_staging_copies_a_stopped_write_left(self):
        fixture = self.pages(2)
        fixture.update()
        directory = fixture.paths.lifeweave_home / "derived" / "doc_1" / "v1"
        leftover = directory / ".staging-wiki-windows-0123456789abcdef"
        leftover.write_text("{}")
        leftover.chmod(0o600)
        self.assertEqual(fixture.derived.list_inventory(), [("doc_1", 1, 1), ("doc_1", 1, 2)])
        fixture.derived.remove_wiki_windows("doc_1")
        self.assertEqual(sorted(path.name for path in directory.iterdir()), ["r1.json", "r2.json"])

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

    def test_matching_stops_once_the_answer_spent_its_budget(self):
        texts = [f"Page {n} says \u201chello {n}\u201d to all." for n in range(6)]
        fixture = self.segments(texts)

        def window(data):
            citation = data["example_patch"]["claims"][0]["citations"][0]
            self.anchor = texts[0]
            claims = [{"text": text.replace("\u201c", '"').replace("\u201d", '"'),
                       "citations": [{**citation, "passage_start": n, "passage_end": n + 1,
                                      "quote": text.replace("\u201c", '"').replace("\u201d", '"')}]}
                      for n, text in enumerate(texts)]
            anchor = {"text": texts[0], "citations": [{**citation, "passage_start": 0, "passage_end": 1, "quote": texts[0]}]}
            return {**data["example_patch"], "claims": [*claims, anchor]}

        fixture.service.compiler.window = window
        # Room for two claims: each charges its passage and quote, then its text and the stored quote,
        # about 100 characters in all.
        with patch("lifecontext.wiki.MATCH_BUDGET_CHARACTERS", 210):
            fixture.update()
        self.assertEqual(self.dropped(fixture), ["quote_not_found"] * 4)

    def test_long_or_many_near_misses_cost_little_time(self):
        passage = " ".join(f"Sentence {n} says the rent of {n} euros is due on the first." for n in range(600))[:24_000]
        fixture = self.segments([passage])

        def claim(quote):
            return Claim(quote, (Citation("doc_1", 1, 2, 0, 1, fixture.digest, quote),))

        # One quote of 1,500 characters and fifty of 400, each a near miss of the passage.
        long_miss = passage[100:1600].replace("rent", "rnet")
        near_misses = [passage[at:at + 400].replace("euros", "eruos") for at in range(0, 20_000, 400)]
        budget = match_budget()
        begun = time.perf_counter()
        long_result = fixture.service._literal(claim(long_miss), budget)
        for quote in near_misses:
            fixture.service._literal(claim(quote), budget)
        elapsed = time.perf_counter() - begun
        # A quote above NEAR_MAX_QUOTE is never near-matched; the budget bounds the rest.
        self.assertEqual(long_result.citations[0].quote, long_miss)
        self.assertLess(elapsed, 2)

    def test_a_passage_is_normalised_once_per_answer_and_charged_once(self):
        passage = "The rent\u00a0is due."
        budget = match_budget()
        self.assertEqual(_locate("The rent is due.", passage, budget), (passage, False))
        left = budget["left"]
        self.assertEqual(_locate("rent is", passage, budget), ("rent\u00a0is", False))
        self.assertEqual(budget["left"], left - len("rent is"))
        self.assertIsNone(_locate("The rent is due.", "Another passage.", {"left": 10, "forms": {}}))

    def test_a_window_that_passes_after_its_correction_keeps_none_of_the_first_answers_drops(self):
        fixture = self.segments([SENTENCE])
        answers = [("An invented claim.", "An invented quote the page lacks entirely."), None]

        def window(data):
            citation = data["example_patch"]["claims"][0]["citations"][0]
            self.anchor = None
            text, quote = answers.pop(0) or (SENTENCE, SENTENCE)
            return {**data["example_patch"], "claims": [
                {"text": text, "citations": [{**citation, "passage_start": 0, "passage_end": 1, "quote": quote}]}]}

        fixture.service.compiler.window = window
        fixture.update()
        self.assertEqual(self.claims(fixture), [(SENTENCE, SENTENCE)])
        self.assertEqual(self.dropped(fixture), [])

    def test_dropped_claims_are_kept_with_their_reason_and_counted_without_their_text(self):
        fixture = self.segments([SENTENCE])
        self.answer(fixture, ("An invented claim.", "An invented quote the page lacks entirely."))
        with self.assertLogs("lifecontext.wiki", "WARNING") as logged:
            fixture.update()
        entry = next(iter(self.record(fixture)["windows"].values()))
        # The kind and the reason only: the model's text is not kept.
        self.assertEqual(entry["dropped"], [{"kind": "claim", "reason": "quote_not_found"}])
        path = fixture.paths.lifeweave_home / "derived" / "doc_1" / "v1" / WIKI_WINDOWS
        self.assertNotIn(b"invented", path.read_bytes())
        self.assertEqual(fixture.service.window_report("doc_1", 1), {"skipped": 0, "dropped": 1})
        self.assertNotIn("invented", "\n".join(logged.output))


if __name__ == "__main__":
    unittest.main()
