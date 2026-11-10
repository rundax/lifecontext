"""An import says whether it ended with problems, why, and how to retry it."""

import tempfile
import unittest
from pathlib import Path

from lifecontext.api.routes import _document
from lifecontext.catalog import LifeContextCatalog
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


if __name__ == "__main__":
    unittest.main()
