"""An import says whether it ended with problems, why, and how to retry it."""

import asyncio
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
from lifecontext.wiki import source_page_id
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

    async def test_a_completed_import_whose_wiki_stages_have_not_finished_is_running(self):
        self.service.wiki_hook = lambda document: {"state": "pending"}
        self.service.projection_hook = lambda document: {"state": "completed"}
        result = await self.ingest()
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["document"]["outcome"], "running")
        self.assertEqual(self.service.operation_outcome(self.catalog.get_operation(result["operation_id"])), "running")

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
