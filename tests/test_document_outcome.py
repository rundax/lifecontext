"""An import says whether it ended with problems, why, and how to retry it."""

import tempfile
import unittest
from pathlib import Path

from lifecontext.api.routes import _document
from lifecontext.catalog import CatalogError, LifeContextCatalog, OperationConflict
from lifecontext.custody import LifeContextCustody
from lifecontext.derived import DerivedStore
from lifecontext.documents import DocumentService
from lifecontext.paths import LifeContextPaths
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
        document = (await self.ingest())["document"]
        self.assertEqual(document["outcome"], "retryable_failed")
        self.assertEqual([p["stage"] for p in document["problems"]], ["indexing"])


class DocumentRetryBySourceTests(DocumentOutcomeTests):
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

    async def test_an_unknown_identifier_is_refused(self):
        for target in ("doc_missing", "op_" + "0" * 32, "../etc"):
            with self.subTest(target=target), self.assertRaises(CatalogError):
                await self.service.retry(target)


if __name__ == "__main__":
    unittest.main()
