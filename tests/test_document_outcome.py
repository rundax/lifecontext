"""An import says whether it ended with problems, why, and how to retry it."""

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lifecontext.api.routes import _document
from lifecontext.catalog import CatalogConflict, CatalogError, LifeContextCatalog, OperationConflict
from lifecontext.custody import LifeContextCustody
from lifecontext.derived import DerivedStore
from lifecontext.documents import DocumentService
from lifecontext.paths import LifeContextPaths
from lifecontext.wiki import WikiService, source_page_id
from test_documents import FakeProvider


class DocumentOutcomeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.paths = LifeContextPaths(Path(self.temp.name))
        self.catalog = LifeContextCatalog(self.paths)
        self.addCleanup(lambda: self.catalog.close())
        self.custody = LifeContextCustody(self.paths, max_bytes=32 * 1024 * 1024)
        self.provider = FakeProvider()
        self.service = DocumentService(
            self.catalog, self.custody, DerivedStore(self.paths), self.provider, max_chunk_bytes=8
        )

    async def ingest(self, data=b"short", key="key", filename="sample.txt"):
        return await self.service.import_document(data, filename, "text/plain", {}, key)

    async def test_a_failed_wiki_stage_is_completed_with_errors_and_names_the_retry(self):
        def wiki(document):
            raise RuntimeError("private model failure")

        self.service.wiki_hook = wiki
        self.service.projection_hook = lambda document: {"state": "completed"}
        result = await self.ingest()
        # The catalog state is unchanged; the outcome beside it tells the truth.
        self.assertEqual(result["state"], "completed")
        document = result["document"]
        sid = document["source_id"]
        self.assertEqual(document["outcome"], "completed_with_errors")
        self.assertEqual(document["last_operation_id"], result["operation_id"])
        self.assertEqual(document["problems"], [{
            "stage": "wiki", "code": "internal_error", "reason": "internal_error",
            "next_step": f"Run: lifecontext documents retry {sid}",
        }])
        response = _document(document)
        self.assertEqual(response.outcome, "completed_with_errors")
        self.assertEqual(response.problems[0].stage, "wiki")
        self.assertEqual(response.last_operation_id, result["operation_id"])
        self.assertIsNone(response.wiki_page_id)

    async def test_a_failed_wiki_stage_names_its_stored_reason_in_the_next_step(self):
        def wiki(document):
            stage = document["stages"]["wiki"]
            self.catalog.transition_document_stage(
                document["source_id"], document["source_version"], "wiki", expected_state="running",
                expected_revision=stage["revision"], state="failed", error_code="wiki_provider_failed")
            raise RuntimeError("model down")

        self.service.wiki_hook = wiki
        result = await self.ingest()
        problem = result["document"]["problems"][0]
        self.assertEqual((problem["stage"], problem["code"]), ("wiki", "wiki_provider_failed"))
        self.assertIn("lifecontext documents retry " + result["document"]["source_id"], problem["next_step"])
        self.assertIn("doctor", problem["next_step"])

    async def test_a_failed_summary_is_listed_and_a_retry_writes_it(self):
        from wiki_fake_compiler import FakeCompiler

        compiler, down = FakeCompiler({}), [True]

        async def ask(query, **kwargs):
            # The summary request carries claims but no evidence or pages.
            if down[0] and "UNTRUSTED_DATA=" in query and not {"evidence", "pages"} & set(
                    json.loads(query.split("UNTRUSTED_DATA=", 1)[1])):
                raise ConnectionError("model unreachable")
            return await compiler(query, **kwargs)

        wiki = WikiService(self.catalog, self.custody, DerivedStore(self.paths), self.provider, self.paths, compiler=ask)

        async def update(document):
            await wiki.update_for_document(document["source_id"], document["source_version"])
            return {"state": "completed"}

        projected = []
        self.service.wiki_hook, self.service.wiki_summary_hook = update, wiki.summary_missing
        self.service.wiki_summary_retry_hook = wiki.write_missing_summary
        self.service.projection_hook = lambda document: projected.append(1) or {"state": "completed"}
        result = await self.ingest()
        document = result["document"]
        sid = document["source_id"]
        windows, summaries = len(compiler.queries["window"]), len(compiler.queries["summary"])
        # The wiki stage completed; only the summary is missing.
        self.assertEqual(document["stages"]["wiki"]["state"], "completed")
        self.assertEqual(document["outcome"], "completed_with_errors")
        self.assertEqual(document["problems"], [{
            "stage": "wiki", "code": "wiki_summary_missing", "reason": "wiki_summary_missing",
            "next_step": f"The page has no model summary; run: lifecontext documents retry {sid}",
        }])
        self.assertEqual(_document(document).problems[0].code, "wiki_summary_missing")
        self.assertEqual(self.service.operation_outcome(self.catalog.get_operation(result["operation_id"])),
                         "completed_with_errors")
        # Retrying while the model is still down changes nothing.
        await self.service.retry(sid)
        self.assertEqual(self.service.show(sid)["outcome"], "completed_with_errors")
        down[0] = False
        page = self.catalog.get_wiki_page(source_page_id(sid))
        retried = await self.service.retry(sid)
        document = retried["document"]
        self.assertEqual(document["outcome"], "completed")
        self.assertEqual(document["problems"], [])
        self.assertFalse(wiki.summary_missing(sid))
        # Each retry asked only for the summary, never the evidence windows again; the
        # same claims came back with it, and the vault note followed the new revision.
        self.assertEqual(len(compiler.queries["window"]), windows)
        self.assertEqual(len(compiler.queries["summary"]), summaries + 1)
        new = self.catalog.get_wiki_page(source_page_id(sid))
        self.assertEqual(new["current_revision"], page["current_revision"] + 1)
        before, after = (wiki.store.read(row["page_id"], row["current_revision"], self.catalog.get_wiki_revision(
            row["page_id"], row["current_revision"])["artifact_sha256"]) for row in (page, new))
        self.assertEqual((after.claims, after.links), (before.claims, before.links))
        self.assertTrue(after.summary)
        self.assertEqual(len(projected), 2)

    def window_service(self, ask):
        """A document service whose wiki compiles with ``ask`` and reports its skipped windows."""
        wiki = WikiService(self.catalog, self.custody, DerivedStore(self.paths), self.provider, self.paths, compiler=ask)

        async def update(document):
            await wiki.update_for_document(document["source_id"], document["source_version"])
            return {"state": "completed"}

        service = DocumentService(self.catalog, self.custody, DerivedStore(self.paths), self.provider,
                                  wiki_hook=update, wiki_summary_hook=wiki.summary_missing,
                                  wiki_summary_retry_hook=wiki.write_missing_summary,
                                  wiki_window_hook=wiki.window_report,
                                  projection_hook=lambda document: {"state": "completed"})
        return service, wiki

    async def test_skipped_windows_are_listed_and_a_retry_asks_only_for_them(self):
        from wiki_fake_compiler import FakeCompiler

        compiler, down = FakeCompiler({}), [True]
        compiler.model = "fake-model"

        async def ask(query, **kwargs):
            if down[0] and "Part 2 holds." in query and "evidence" in json.loads(query.split("UNTRUSTED_DATA=", 1)[1]):
                raise ConnectionError("model unreachable")
            return await compiler(query, **kwargs)

        ask.model = compiler.model
        service, wiki = self.window_service(ask)
        # Four lines of 20 KB: one evidence window each.
        text = "".join(f"Part {n} holds. " + "x" * 20_000 + "\n" for n in range(1, 5)).encode()
        result = await service.import_document(text, "parts.txt", "text/plain", {}, "parts")
        document = result["document"]
        sid = document["source_id"]
        self.assertEqual(document["stages"]["wiki"]["state"], "completed")
        self.assertEqual(document["outcome"], "completed_with_errors")
        self.assertEqual(document["problems"], [{
            "stage": "wiki", "code": "wiki_windows_skipped", "reason": "wiki_windows_skipped",
            "next_step": ("The page leaves out parts of the document the model failed on; run: "
                          f"lifecontext documents retry {sid}")}])
        self.assertEqual(_document(document).problems[0].code, "wiki_windows_skipped")
        self.assertEqual(service.operation_outcome(self.catalog.get_operation(result["operation_id"])),
                         "completed_with_errors")

        def parts():
            return sorted(claim.text for claim in wiki.store.read(*self.revision(sid)).claims)

        self.assertEqual(parts(), ["Part 1 holds.", "Part 3 holds.", "Part 4 holds."])
        windows = len(compiler.queries["window"])
        down[0] = False
        retried = await service.retry(sid)
        document = retried["document"]
        self.assertEqual((document["outcome"], document["problems"]), ("completed", []))
        self.assertEqual(parts(), ["Part 1 holds.", "Part 2 holds.", "Part 3 holds.", "Part 4 holds."])
        # Only the skipped window was asked again; the others came from the kept windows.
        self.assertEqual(len(compiler.queries["window"]), windows + 1)

    def revision(self, sid):
        page = self.catalog.get_wiki_page(source_page_id(sid))
        row = self.catalog.get_wiki_revision(page["page_id"], page["current_revision"])
        return page["page_id"], page["current_revision"], row["artifact_sha256"]

    async def test_dropped_claims_are_counted_in_documents_show(self):
        from wiki_fake_compiler import FakeCompiler

        compiler = FakeCompiler({})
        original = compiler.window

        def window(data):
            answer = original(data)
            invented = {**answer["claims"][0], "text": "An invented claim the document never makes."}
            answer["claims"] = [*answer["claims"], invented]
            return answer

        compiler.window = window
        service, _ = self.window_service(compiler)
        result = await service.import_document(b"Rent is 900 euros. It is due monthly.", "rent.txt", "text/plain", {},
                                               "rent")
        document = result["document"]
        self.assertEqual((document["outcome"], document["problems"]), ("completed", []))
        self.assertEqual(document["wiki_dropped_claims"], 1)
        self.assertEqual(_document(document).wiki_dropped_claims, 1)
        self.assertNotIn("invented", json.dumps(_document(document).model_dump(mode="json")))
        record = DerivedStore(self.paths).read_wiki_windows(document["source_id"], 1)
        [entry] = record["windows"].values()
        self.assertEqual([item["reason"] for item in entry["dropped"]], ["claim_not_in_quote"])

    async def test_a_page_in_review_conflict_is_not_flagged_and_its_retry_asks_no_summary(self):
        from wiki_fake_compiler import FakeCompiler

        compiler, asked = FakeCompiler({}), []

        async def ask(query, **kwargs):
            if "UNTRUSTED_DATA=" in query and not {"evidence", "pages"} & set(
                    json.loads(query.split("UNTRUSTED_DATA=", 1)[1])):
                asked.append(1)
                raise ConnectionError("model unreachable")
            return await compiler(query, **kwargs)

        wiki = WikiService(self.catalog, self.custody, DerivedStore(self.paths), self.provider, self.paths, compiler=ask)

        async def update(document):
            await wiki.update_for_document(document["source_id"], document["source_version"])
            return {"state": "completed"}

        self.service.wiki_hook, self.service.wiki_summary_hook = update, wiki.summary_missing
        self.service.wiki_summary_retry_hook = wiki.write_missing_summary
        self.service.projection_hook = lambda document: {"state": "completed"}
        sid = (await self.ingest())["document"]["source_id"]
        self.assertTrue(wiki.summary_missing(sid))
        # A hand edit of the vault note puts the page in review conflict; a commit is refused.
        self.catalog._connection.execute("UPDATE wiki_pages SET state='conflict' WHERE page_id=?",
                                         (source_page_id(sid),))
        self.catalog._connection.commit()
        self.assertFalse(wiki.summary_missing(sid))
        summaries = len(asked)
        await self.service.retry(sid)
        self.assertEqual(await wiki.write_missing_summary(sid, 1), [])
        self.assertEqual(len(asked), summaries)

    async def test_a_long_document_keeps_room_on_its_page_for_the_largest_summary(self):
        from wiki_fake_compiler import FakeCompiler

        from lifecontext.wiki import MAX_PATCH_BYTES, _patch_json

        compiler = FakeCompiler({})

        async def ask(query, **kwargs):
            data = json.loads(query.split("UNTRUSTED_DATA=", 1)[1]) if "UNTRUSTED_DATA=" in query else {}
            if data and not {"evidence", "pages"} & set(data):
                # The largest summary the checks accept: 5 sentences of 300 four-byte characters.
                count = len(data["claims"])
                sentence = {"text": "\U0001F600" * 300, "claims": list(range(max(1, count - 3), count + 1))}
                return {"text": json.dumps({"summary": [sentence] * 5})}
            return await compiler(query, **kwargs)

        derived = DerivedStore(self.paths)
        wiki = WikiService(self.catalog, self.custody, derived, self.provider, self.paths, compiler=ask)
        service = DocumentService(self.catalog, self.custody, derived, self.provider)

        async def update(document):
            await wiki.update_for_document(document["source_id"], document["source_version"])
            return {"state": "completed"}

        service.wiki_hook, service.wiki_summary_hook = update, wiki.summary_missing
        # Long passages whose claims filled the page to within a summary of 48 KB before.
        text = "\n\n".join(f"Paragraph {i} says " + "x" * 1100 + "." for i in range(60))
        result = await service.import_document(text.encode(), "long.txt", "text/plain", {}, "long")
        self.assertEqual(result["document"]["outcome"], "completed", result["document"]["problems"])
        page = self.catalog.get_wiki_page(source_page_id(result["document"]["source_id"]))
        stored = wiki.store.read(page["page_id"], page["current_revision"], self.catalog.get_wiki_revision(
            page["page_id"], page["current_revision"])["artifact_sha256"])
        self.assertEqual(len(stored.summary), 5)
        self.assertLessEqual(len(_patch_json(stored).encode()), MAX_PATCH_BYTES)
        self.assertGreater(len(stored.claims), 7)  # more than one window's claims

    def test_the_largest_summary_fits_its_reserve_and_a_full_page_is_not_flagged(self):
        from dataclasses import replace

        from lifecontext.wiki import (
            MAX_PATCH_BYTES, SUMMARY_RESERVE_BYTES, Citation, Claim, SummarySentence, WikiPatch,
            _patch_json, lacks_summary)

        claims = tuple(Claim(f"claim {n}", (Citation("doc_a", 1, 1, n, n + 1, "a" * 64, f"claim {n}"),))
                       for n in range(128))
        page = WikiPatch("source_doc_a", "A", 1, claims, (), ())
        largest = tuple(SummarySentence("\U0001F600" * 300, (125, 126, 127, 128)) for _ in range(5))
        grown = len(_patch_json(replace(page, summary=largest)).encode()) - len(_patch_json(page).encode())
        self.assertLessEqual(grown, SUMMARY_RESERVE_BYTES)
        self.assertTrue(lacks_summary(page))
        self.assertFalse(lacks_summary(replace(page, summary=largest)))
        # Too full to take a summary: a retry could not add one, so it is not reported.
        long = Claim("x" * 2000, (Citation("doc_a", 1, 1, 0, 1, "a" * 64, "y" * 2000),))
        full = replace(page, claims=(long,) * 11)
        self.assertGreater(len(_patch_json(full).encode()) + SUMMARY_RESERVE_BYTES, MAX_PATCH_BYTES)
        self.assertLessEqual(len(_patch_json(full).encode()), MAX_PATCH_BYTES)
        self.assertFalse(lacks_summary(full))

    async def test_the_summary_check_reads_each_page_revision_once(self):
        from wiki_fake_compiler import FakeCompiler

        from lifecontext import wiki as wiki_module

        wiki = WikiService(self.catalog, self.custody, DerivedStore(self.paths), self.provider, self.paths,
                           compiler=FakeCompiler({}))

        async def update(document):
            await wiki.update_for_document(document["source_id"], document["source_version"])
            return {"state": "completed"}

        self.service.wiki_hook, self.service.wiki_summary_hook = update, wiki.summary_missing
        self.service.projection_hook = lambda document: {"state": "completed"}
        checks, check = [], wiki_module.lacks_summary
        with patch.object(wiki_module, "lacks_summary", side_effect=lambda page: checks.append(page) or check(page)):
            result = await self.ingest()
            self.assertEqual(result["document"]["outcome"], "completed")
            # One import response looked at its page once.
            self.assertEqual(len(checks), 1)
            sid = result["document"]["source_id"]
            for _ in range(3):
                self.service.show(sid)
                self.service.list()
            self.assertEqual(len(checks), 1)
            # A new revision is a new page to look at.
            await wiki.update_for_document(sid, 1)
            self.service.show(sid)
            self.assertEqual(len(checks), 2)

    async def test_a_projected_page_names_its_vault_path(self):
        result = await self.ingest()
        sid = result["document"]["source_id"]
        self.assertIsNone(result["document"]["vault_path"])
        digest = "a" * 64
        page = {"page_id": source_page_id(sid), "current_revision": 1}
        with patch.object(self.catalog, "get_wiki_page", return_value=page):
            # The page is named only once its note is in the vault.
            self.assertIsNone(self.service.show(sid, 1)["wiki_page_id"])
            self.catalog.set_artifact_projection(
                "wiki", source_page_id(sid), 1, projection_path="wiki/Sample.md",
                desired_hash=digest, state="projected", last_projected_hash=digest)
            shown = self.service.show(sid, 1)
        # The receipt is relative to Journal; the response is relative to the vault.
        self.assertEqual(shown["vault_path"], "Journal/wiki/Sample.md")
        self.assertEqual(shown["wiki_page_id"], source_page_id(sid))

    async def test_an_unfinished_wiki_stage_is_running_only_while_the_run_lasts(self):
        seen = []

        def wiki(document):
            # Asked as if the operation had completed: the run still holds the document.
            seen.append(self.service.outcome(self.service.show(document["source_id"]), "completed"))
            return {"state": "pending"}

        self.service.wiki_hook = wiki
        self.service.projection_hook = lambda document: {"state": "completed"}
        result = await self.ingest()
        sid = result["document"]["source_id"]
        self.assertEqual(seen, ["running"])
        self.assertEqual(result["state"], "completed")
        # Nothing runs the stage now; it waits for a retry instead of looking busy.
        document = result["document"]
        self.assertEqual(document["outcome"], "completed_with_errors")
        self.assertEqual(document["problems"], [{
            "stage": "wiki", "code": "running", "reason": "running",
            "next_step": f"Run: lifecontext documents retry {sid}",
        }])
        self.assertEqual(_document(document).problems[0].stage, "wiki")
        self.assertEqual(self.service.operation_outcome(self.catalog.get_operation(result["operation_id"])),
                         "completed_with_errors")

    async def test_a_vault_note_a_category_change_put_back_to_pending_is_a_problem_a_retry_fixes(self):
        projected = []
        self.service.wiki_hook = lambda document: {"state": "completed"}
        self.service.projection_hook = lambda document: projected.append(1) or {"state": "completed"}
        document = (await self.ingest())["document"]
        sid = document["source_id"]
        self.assertEqual(document["outcome"], "completed")
        self.catalog.assign_document_category(sid, "inbox", expected_revision=document["category_revision"])
        shown = self.service.show(sid)
        self.assertEqual(shown["stages"]["projection"]["state"], "pending")
        self.assertEqual(shown["outcome"], "completed_with_errors")
        self.assertEqual(shown["problems"], [{
            "stage": "projection", "code": "pending", "reason": "pending",
            "next_step": f"Run: lifecontext documents retry {sid}",
        }])
        retried = await self.service.retry(sid)
        self.assertEqual(retried["document"]["outcome"], "completed")
        self.assertEqual(len(projected), 2)

    async def test_a_clean_import_is_completed_without_problems(self):
        self.service.wiki_hook = lambda document: {"state": "completed"}
        self.service.projection_hook = lambda document: {"state": "completed"}
        document = (await self.ingest())["document"]
        self.assertEqual(document["outcome"], "completed")
        self.assertEqual(document["problems"], [])
        self.assertEqual(_document(document).outcome, "completed")

    async def test_a_refused_format_says_why_and_what_to_do(self):
        result = await self.ingest(b"PK\x03\x04 not an odt reader", filename="letter.odt")
        self.assertEqual(result["state"], "retryable_failed")
        document = result["document"]
        self.assertEqual(document["outcome"], "retryable_failed")
        problem = document["problems"][0]
        self.assertEqual((problem["stage"], problem["code"], problem["reason"]),
                         ("extraction", "evidence_unavailable", "unsupported_format"))
        self.assertIn("PDF, DOCX", problem["next_step"])
        response = _document(document)
        self.assertEqual(response.extraction_reason, "unsupported_format")
        self.assertEqual(response.problems[0].reason, "unsupported_format")

    async def test_an_indexing_failure_is_a_retryable_outcome(self):
        self.provider.fail_at = 1
        result = await self.ingest()
        document = result["document"]
        self.assertEqual(document["outcome"], "retryable_failed")
        self.assertEqual([p["stage"] for p in document["problems"]], ["indexing"])
        failed = self.catalog.list_operations(state="retryable_failed")
        self.assertEqual([row["operation_id"] for row in failed], [result["operation_id"]])
        self.assertEqual(self.catalog.list_operations(state="completed"), [])


class OperationOutcomeTests(unittest.IsolatedAsyncioTestCase):
    setUp = DocumentOutcomeTests.setUp
    ingest = DocumentOutcomeTests.ingest

    async def test_operation_status_and_list_show_completed_with_errors_and_filter_by_it(self):
        from lifecontext.application import JournalApplication

        def wiki(document):
            raise RuntimeError("private model failure")

        self.service.wiki_hook = wiki
        self.service.projection_hook = lambda document: {"state": "completed"}
        broken = await self.ingest(key="broken")
        self.service.wiki_hook = lambda document: {"state": "completed"}
        fine = await self.ingest(b"another document", key="fine")
        plain = self.catalog.begin_operation("plain", "c" * 64, "retain")
        application = JournalApplication.__new__(JournalApplication)
        application.catalog, application.documents = self.catalog, self.service
        self.assertEqual(application.operation_status(broken["operation_id"])["outcome"], "completed_with_errors")
        self.assertEqual(application.operation_status(fine["operation_id"])["outcome"], "completed")
        self.assertNotIn("outcome", application.operation_status(plain["operation_id"]))
        listed = application.list_operations()
        self.assertEqual([item["operation_id"] for item in listed],
                         [plain["operation_id"], fine["operation_id"], broken["operation_id"]])
        self.assertNotIn("outcome", listed[0])
        found = application.list_operations(outcome="completed_with_errors")
        self.assertEqual([item["operation_id"] for item in found], [broken["operation_id"]])
        self.assertEqual([item["operation_id"] for item in application.list_operations(outcome="completed", limit=1)],
                         [fine["operation_id"]])
        # The list reads each operation once and every stage in one query, not per operation.
        with patch.object(self.catalog, "get_operation", side_effect=AssertionError("per operation read")), \
                patch.object(self.catalog, "get_document_stage", side_effect=AssertionError("per stage read")):
            self.assertEqual(application.list_operations(outcome="completed_with_errors"), found)


class DeletedDocumentRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_deleted_document_is_not_found_by_its_source_id_but_an_operation_id_is_left_to_the_service(self):
        from unittest.mock import AsyncMock, Mock

        from lifecontext.application import ApplicationError, JournalApplication

        application = JournalApplication.__new__(JournalApplication)
        application.catalog = Mock(list_tombstones=Mock(return_value=[{"tombstone_type": "deleted"}]))
        application.documents = Mock(retry=AsyncMock(side_effect=CatalogError("document operation unavailable")))
        with self.assertRaises(ApplicationError) as caught:
            await application.retry_document("doc_gone")
        self.assertEqual(caught.exception.code, "source_not_found")
        application.documents.retry.assert_not_awaited()
        with self.assertRaises(ApplicationError) as caught:
            await application.retry_document("op_" + "a" * 32)
        self.assertEqual(caught.exception.code, "invalid_request")
        application.documents.retry.assert_awaited_once()


class DocumentRunGuardTests(unittest.IsolatedAsyncioTestCase):
    setUp = DocumentOutcomeTests.setUp
    ingest = DocumentOutcomeTests.ingest

    async def test_a_retry_while_the_import_runs_reports_it_and_starts_nothing(self):
        release, started = asyncio.Event(), asyncio.Event()
        retain = self.provider.retain

        async def slow(*args, **kwargs):
            started.set()
            await release.wait()
            return await retain(*args, **kwargs)

        self.provider.retain = slow
        running = asyncio.create_task(self.ingest())
        await started.wait()
        (operation,) = self.catalog.list_operations()
        sid = operation["result"]["recovery_intent"]["source_id"]
        for target in (operation["operation_id"], sid):
            for key in (None, "same-key"):
                with self.subTest(target=target, key=key):
                    seen = await self.service.retry(target, idempotency_key=key)
                    self.assertEqual((seen["operation_id"], seen["state"]), (operation["operation_id"], "running"))
        self.assertEqual(len(self.provider.documents), 0)
        self.assertEqual([row["operation_id"] for row in self.catalog.list_operations()], [operation["operation_id"]])
        release.set()
        finished = await running
        self.assertEqual(finished["state"], "completed")
        self.assertFalse(self.service._inflight)
        # Once it is done a retry is an ordinary one again.
        self.assertEqual((await self.service.retry(sid))["state"], "completed")

    async def test_two_retries_with_one_new_key_run_once(self):
        calls = []

        async def wiki(document):
            calls.append(1)
            await asyncio.sleep(0)
            if len(calls) == 1:
                raise RuntimeError("model down")
            return {"state": "completed"}

        self.service.wiki_hook = wiki
        first = await self.ingest()
        sid = first["document"]["source_id"]
        one, two = await asyncio.gather(self.service.retry(sid, idempotency_key="k"),
                                        self.service.retry(sid, idempotency_key="k"))
        self.assertEqual(one["operation_id"], two["operation_id"])
        self.assertEqual(len(calls), 2)
        self.assertFalse(self.service._inflight)


class DocumentRetryBySourceTests(unittest.IsolatedAsyncioTestCase):
    setUp = DocumentOutcomeTests.setUp
    ingest = DocumentOutcomeTests.ingest

    def flaky_wiki(self, failures=1):
        calls = []

        def wiki(document):
            calls.append(document["source_id"])
            if len(calls) <= failures:
                raise RuntimeError("private model failure")
            return {"state": "completed"}

        self.service.wiki_hook = wiki
        self.service.projection_hook = lambda document: {"state": "completed"}
        return calls

    async def test_a_source_id_retry_reruns_a_failed_wiki_stage_of_a_completed_import(self):
        calls = self.flaky_wiki()
        first = await self.ingest()
        sid = first["document"]["source_id"]
        self.assertEqual(first["document"]["outcome"], "completed_with_errors")
        retried = await self.service.retry(sid)
        self.assertNotEqual(retried["operation_id"], first["operation_id"])
        self.assertEqual(retried["state"], "completed")
        self.assertEqual(retried["document"]["stages"]["wiki"]["state"], "completed")
        self.assertEqual(retried["document"]["outcome"], "completed")
        self.assertEqual(retried["document"]["last_operation_id"], retried["operation_id"])
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(self.provider.calls), 1)  # indexing is not repeated
        # The old operation ID reaches the same document; nothing is left to run.
        again = await self.service.retry(first["operation_id"])
        self.assertEqual(again["operation_id"], first["operation_id"])
        self.assertEqual(len(calls), 2)

    async def test_a_retry_idempotency_key_replays_instead_of_running_again(self):
        calls = self.flaky_wiki(failures=2)
        first = await self.ingest()
        sid = first["document"]["source_id"]
        one = await self.service.retry(sid, idempotency_key="retry-key")
        self.assertEqual(one["document"]["outcome"], "completed_with_errors")
        two = await self.service.retry(sid, idempotency_key="retry-key")
        self.assertEqual(two["operation_id"], one["operation_id"])
        self.assertEqual(len(calls), 2)
        with self.assertRaises(OperationConflict):
            await self.service.retry(sid, rerun_extraction=True, idempotency_key="retry-key")
        three = await self.service.retry(sid, idempotency_key="another-key")
        self.assertEqual(three["document"]["outcome"], "completed")
        self.assertEqual(len(calls), 3)

    async def test_a_rerun_extraction_with_a_key_allocates_one_revision(self):
        first = await self.ingest()
        sid = first["document"]["source_id"]
        one = await self.service.retry(sid, rerun_extraction=True, idempotency_key="rerun-key")
        two = await self.service.retry(sid, rerun_extraction=True, idempotency_key="rerun-key")
        self.assertEqual(one["operation_id"], two["operation_id"])
        self.assertEqual([r["extraction_revision"] for r in self.catalog.list_extractions(sid, 1)], [1, 2])

    async def test_a_source_id_retry_reads_a_refused_file_again(self):
        first = await self.ingest(b"PK\x03\x04 not an odt reader", filename="letter.odt")
        sid = first["document"]["source_id"]
        retried = await self.service.retry(sid)
        self.assertEqual(retried["state"], "retryable_failed")
        self.assertEqual([r["extraction_revision"] for r in self.catalog.list_extractions(sid, 1)], [1, 2])

    async def test_a_forgotten_document_is_not_retried_by_its_source_id(self):
        first = await self.ingest()
        with patch.object(self.catalog, "source_withdrawn", return_value=True), self.assertRaises(CatalogConflict):
            await self.service.retry(first["document"]["source_id"])

    async def test_a_retry_never_rebuilds_the_page_of_an_old_version(self):
        calls = self.flaky_wiki()
        first = await self.ingest()
        sid = first["document"]["source_id"]
        await self.service.import_document(b"second version", "sample.txt", "text/plain", {}, "v2",
                                           source_id=sid, source_version=2)
        self.assertEqual(len(calls), 2)
        before = len(calls)
        old = await self.service.retry(first["operation_id"])
        self.assertEqual(old["document"]["source_version"], 1)
        rerun = await self.service.retry(first["operation_id"], rerun_extraction=True)
        self.assertEqual(rerun["state"], "completed")
        self.assertEqual(len(calls), before)
        self.assertEqual(self.service._unfinished_stages(sid, 1), [])

    async def test_an_unfinished_import_of_an_old_version_retried_by_its_operation_id_leaves_the_page(self):
        calls = []
        self.service.wiki_hook = lambda document: calls.append(("wiki", document["source_version"])) or {
            "state": "completed"}
        self.service.projection_hook = lambda document: calls.append(("projection", document["source_version"])) or {
            "state": "completed"}
        self.provider.fail_at = 1
        first = await self.ingest()
        self.assertEqual(first["state"], "retryable_failed")
        sid = first["document"]["source_id"]
        await self.service.import_document(b"second version", "sample.txt", "text/plain", {}, "v2",
                                           source_id=sid, source_version=2)
        before = list(calls)
        self.assertIn(("wiki", 2), before)
        retried = await self.service.retry(first["operation_id"])
        self.assertEqual((retried["operation_id"], retried["state"]), (first["operation_id"], "completed"))
        # Its evidence is indexed, but the page and note stay those of version 2.
        self.assertEqual(calls, before)

    async def test_list_asks_for_every_latest_operation_in_one_query(self):
        await self.ingest(key="one")
        await self.ingest(b"another document", key="two")
        with patch.object(self.catalog, "latest_document_operation", side_effect=AssertionError("per document scan")):
            documents = self.service.list()
        self.assertEqual(len(documents), 2)
        self.assertTrue(all(document["last_operation_id"] and document["outcome"] == "completed" for document in documents))
        self.assertEqual({d["last_operation_id"] for d in documents},
                         {row["operation_id"] for row in self.catalog.list_operations()})

    async def test_an_unknown_identifier_is_refused(self):
        for target in ("doc_missing", "op_" + "0" * 32, "../etc"):
            with self.subTest(target=target), self.assertRaises(CatalogError):
                await self.service.retry(target)


class WikiReachesVaultAfterRetryTests(unittest.IsolatedAsyncioTestCase):
    setUp = DocumentOutcomeTests.setUp
    ingest = DocumentOutcomeTests.ingest

    async def test_the_page_is_projected_after_an_indexing_failure_and_a_retry(self):
        projected = []

        def project(document):
            # The vault note records the wiki state it was projected with.
            projected.append(document["stages"]["wiki"]["state"])
            return {"state": "completed"}

        self.service.wiki_hook = lambda document: {"state": "completed"}
        self.service.projection_hook = project
        self.provider.fail_at = 1
        first = await self.ingest()
        self.assertEqual(first["state"], "retryable_failed")
        self.assertEqual(projected, ["pending"])
        self.provider.fail_at = None
        recovered = await self.service.retry(first["document"]["source_id"])
        self.assertEqual(recovered["state"], "completed")
        self.assertEqual(projected, ["pending", "completed"])
        self.assertEqual(recovered["document"]["outcome"], "completed")


class ApplicationRunGuardTests(unittest.TestCase):
    """A reindex, rebuild or retry leaves a document alone while an import or retry of it runs."""

    def setUp(self):
        from test_document_bundle_application import Provider
        from wiki_fake_compiler import FakeCompiler

        from lifecontext.application import JournalApplication
        from lifecontext.vault import ManagedVault

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        home = Path(temporary.name) / "home"
        home.mkdir(mode=0o700)
        vault = home / "vault"
        # An older config may keep the Journal folder under another name.
        journal = vault / "Diary"
        (journal / "entries").mkdir(parents=True, mode=0o700)
        vault.chmod(0o700)
        journal.chmod(0o700)
        self.compiler = FakeCompiler({})
        with patch.dict(os.environ, {"HOME": str(home)}):
            self.app = JournalApplication(paths=LifeContextPaths.current(), hindsight=Provider(),
                                          vault=ManagedVault(vault, journal), compiler=self.compiler)
        self.addCleanup(self.app.close)
        outcome = asyncio.run(self.app.import_document(b"Coffee is a stimulant.", "Notes.txt", "text/plain",
                                                       {"source": "fixture"}, "import"))
        self.assertEqual(outcome["document"]["outcome"], "completed", outcome)
        self.sid = outcome["document"]["source_id"]

    def running(self):
        """As while an import or retry of the document runs."""
        self.app.documents._inflight[self.sid] = 1
        self.addCleanup(self.app.documents._inflight.pop, self.sid, None)

    def test_vault_path_starts_at_the_configured_journal_folder(self):
        self.assertTrue(self.app.show_document(self.sid)["vault_path"].startswith("Diary/wiki/"))

    def test_a_reindex_while_the_document_runs_is_a_conflict_the_same_request_retries(self):
        from lifecontext.application import ApplicationError

        page = self.app.catalog.get_wiki_page(source_page_id(self.sid))
        before = self.app.catalog.list_operations()
        self.running()
        request = {"action": "reindex", "source_id": self.sid}
        with self.assertRaises(ApplicationError) as caught:
            asyncio.run(self.app.curate(request, idempotency_key="reindex"))
        self.assertEqual(caught.exception.code, "conflict")
        # Nothing is stored under the key, so startup recovery has nothing to run, and the
        # page was not marked stale.
        self.assertEqual(self.app.catalog.list_operations(), before)
        self.assertEqual(self.app.catalog.get_wiki_page(source_page_id(self.sid))["state"], page["state"])
        self.app.documents._inflight.pop(self.sid)
        self.assertEqual(asyncio.run(self.app.curate(request, idempotency_key="reindex"))["state"], "completed")
        # Once done, the same key replays the reindex even while the document runs again.
        self.running()
        self.assertEqual(asyncio.run(self.app.curate(request, idempotency_key="reindex"))["state"], "completed")

    def test_a_rebuild_of_a_document_that_runs_is_refused_as_a_conflict_and_nothing_is_stored(self):
        from lifecontext.application import ApplicationError

        windows, before = len(self.compiler.queries["window"]), self.app.catalog.list_operations()
        self.running()
        with self.assertRaises(ApplicationError) as caught:
            asyncio.run(self.app.rebuild_wiki(source_id=self.sid, idempotency_key="rebuild"))
        self.assertEqual(caught.exception.code, "conflict")
        self.assertEqual(len(self.compiler.queries["window"]), windows)
        self.assertEqual(self.app.catalog.list_operations(), before)
        # The same request and key run once the document is free.
        self.app.documents._inflight.pop(self.sid)
        result = asyncio.run(self.app.rebuild_wiki(source_id=self.sid, idempotency_key="rebuild"))
        self.assertEqual((result["state"], result["skipped"]), ("completed", []))
        self.assertGreater(len(self.compiler.queries["window"]), windows)

    def test_a_rebuild_of_every_document_skips_and_names_one_that_runs(self):
        second = asyncio.run(self.app.import_document(b"Tea is a drink.", "Tea.txt", "text/plain",
                                                      {"source": "fixture"}, "import-two"))["document"]["source_id"]
        held = []
        update = self.app.wiki.update_for_document

        async def watch(source_id, version):
            # The rebuild holds the document it works on, as an import or retry does.
            held.append(self.app.documents.in_flight(source_id))
            return await update(source_id, version)

        self.running()
        with patch.object(self.app.wiki, "update_for_document", side_effect=watch):
            result = asyncio.run(self.app.rebuild_wiki(idempotency_key="rebuild-all"))
        self.assertEqual((result["state"], result["skipped"]), ("completed", [self.sid]))
        # Only the document rebuilt is in the operation's sources, which doctor reads.
        self.assertEqual([source["source_id"] for source in result["summary"]["sources"]], [second])
        self.assertEqual(held, [True])
        self.assertFalse(self.app.documents.in_flight(second))

    def test_a_retry_that_finds_the_document_running_does_not_classify_it(self):
        from unittest.mock import AsyncMock

        classify = AsyncMock(return_value=True)
        self.running()
        with patch.object(self.app, "_classify_document", classify):
            asyncio.run(self.app.retry_document(self.sid))
            classify.assert_not_awaited()
            self.app.documents._inflight.pop(self.sid)
            asyncio.run(self.app.retry_document(self.sid))
            classify.assert_awaited()


class CitationLabelTests(unittest.TestCase):
    def test_a_document_citation_carries_its_title_and_filename(self):
        from types import SimpleNamespace

        from pydantic import ValidationError

        from lifecontext.api.models import Citation, RecallResponse
        from lifecontext.api.routes import _labelled

        metadata = {("doc_lease", 2): {"title": "Lease", "filename": "lease.pdf"}}
        application = SimpleNamespace(catalog=SimpleNamespace(
            get_document_metadata=lambda sid, version: metadata.get((sid, version))))
        evidence = {"source_id": "doc_lease", "source_version": 2, "sha256": "a" * 64}
        labelled = _labelled(application, evidence)
        self.assertEqual((labelled["title"], labelled["filename"]), ("Lease", "lease.pdf"))
        journal = {"source_id": "journal_day", "source_version": 1, "sha256": "b" * 64}
        self.assertEqual(_labelled(application, journal), journal)
        response = RecallResponse.model_validate({"results": [
            {"text": "Rent is due monthly.", "evidence": labelled},
            {"text": "A day note.", "evidence": journal}], "truncated": False})
        self.assertEqual(response.results[0].evidence.title, "Lease")
        self.assertIsNone(response.results[1].evidence.filename)
        with self.assertRaises(ValidationError):
            Citation.model_validate({**evidence, "filename": "../lease.pdf"})


if __name__ == "__main__":
    unittest.main()
