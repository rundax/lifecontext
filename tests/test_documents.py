"""Document orchestration contract against local stores and a fake provider."""

import hashlib
import io
import tempfile
import unittest
from itertools import pairwise
from pathlib import Path
from unittest.mock import patch

from docx import Document

from lifecontext.catalog import CatalogConflict, LifeContextCatalog, OperationConflict
from lifecontext.custody import LifeContextCustody
from lifecontext.derived import DerivedStore
from lifecontext.documents import DocumentService
from lifecontext.extraction import ExtractionArtifact
from lifecontext.paths import LifeContextPaths


class FakeProvider:
    def __init__(self):
        self.calls = []
        self.documents = {}
        self.fail_at = None
        self.async_ack = False
        self.delete_calls = []
        self.delete_success = True
        self.scope_tags = []

    async def retain(self, source_id, content, *, scope_tag, metadata, retain_async=False):
        self.calls.append((source_id, content, metadata))
        self.scope_tags.append((source_id, scope_tag))
        if self.fail_at == len(self.calls):
            raise RuntimeError("private provider secret")
        self.documents[source_id] = {
            "id": source_id,
            "document_metadata": metadata,
            "memory_unit_count": 0 if self.async_ack else 1,
        }
        return {"success": True, "async": self.async_ack}

    async def get_document(self, source_id):
        return self.documents.get(source_id, {})

    async def delete_document(self, source_id):
        self.delete_calls.append(source_id)
        if self.delete_success:
            self.documents.pop(source_id, None)
        return {"success": self.delete_success}


class DocumentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.paths = LifeContextPaths(Path(self.temp.name))
        self.catalog = LifeContextCatalog(self.paths)
        self.addCleanup(lambda: self.catalog.close())
        self.custody = LifeContextCustody(self.paths, max_bytes=32 * 1024 * 1024)
        self.derived = DerivedStore(self.paths)
        self.provider = FakeProvider()
        self.service = DocumentService(
            self.catalog, self.custody, self.derived, self.provider, max_chunk_bytes=8
        )

    async def ingest(
        self, data=b"hello world\nsecond line", key="key", filename="sample.txt"
    ):
        return await self.service.import_document(
            data, filename, "text/plain", {"source": "test"}, key
        )

    async def test_every_document_chunk_is_retained_in_its_own_source_version_scope(self):
        first = await self.ingest(b"one two three four five six seven eight nine ten eleven")
        sid = first["document"]["source_id"]
        await self.service.import_document(
            b"replacement text that is also long enough to need several chunks",
            "sample.txt", "text/plain", {"source": "test"}, "scope-v2",
            source_id=sid, source_version=2,
        )
        by_version = {}
        for provider_id, tag in self.provider.scope_tags:
            by_version.setdefault(tag, []).append(provider_id)
        self.assertEqual(set(by_version), {f"src:{sid}:v1", f"src:{sid}:v2"})
        self.assertGreater(len(by_version[f"src:{sid}:v1"]), 1)
        self.assertEqual(len(self.provider.scope_tags), len(self.provider.calls))

    async def test_invalid_replacement_has_no_custody_effects(self):
        manifest = self.custody.write_version("journal_fixture", 1, b"Journal", provenance={})
        self.catalog.register_manifest(manifest)
        original_inventory = self.custody.list_inventory()
        for sid, version in (("journal_fixture", 2), ("missing_document", 1)):
            with self.subTest(sid=sid), self.assertRaises(CatalogConflict):
                await self.service.import_document(b"replacement", "sample.txt", "text/plain", {}, f"invalid-{sid}", source_id=sid, source_version=version)
        self.assertEqual(self.custody.list_inventory(), original_inventory)
        first = await self.ingest(b"short")
        sid = first["document"]["source_id"]
        with self.assertRaises(CatalogConflict):
            await self.service.import_document(b"gap", "sample.txt", "text/plain", {}, "gap", source_id=sid, source_version=3)
        replacement = await self.service.import_document(b"updated", "sample.txt", "text/plain", {}, "replacement", source_id=sid, source_version=2)
        replay = await self.service.import_document(b"updated", "sample.txt", "text/plain", {}, "replacement", source_id=sid, source_version=2)
        self.assertEqual(replacement["state"], "completed")
        self.assertEqual(replacement["operation_id"], replay["operation_id"])
        self.assertNotIn((sid, 3), self.custody.list_inventory())

    async def test_existing_reconciliation_requires_exact_custody_identity(self):
        first = await self.ingest(b"short")
        sid = first["document"]["source_id"]
        before = self.custody.list_inventory()
        for data, provenance in ((b"changed", {"source": "test"}), (b"short", {"source": "changed"})):
            with self.subTest(data=data), self.assertRaises(CatalogConflict):
                await self.service.import_document(data, "sample.txt", "text/plain", provenance, "invalid-existing-" + data.decode(), source_id=sid, source_version=1, _allow_existing=True)
        self.assertEqual(self.custody.list_inventory(), before)
        with self.assertRaises(CatalogConflict):
            await self.service.import_document(b"short", "sample.txt", "text/plain", {"source": "test"}, "public-existing", source_id=sid, source_version=1)
        reconciled = await self.service.import_document(b"short", "sample.txt", "text/plain", {"source": "test"}, "verified-existing", source_id=sid, source_version=1, _allow_existing=True)
        self.assertEqual(reconciled["state"], "completed")

    async def test_replay_conflict_and_original_bytes(self):
        first = await self.ingest()
        self.assertEqual(first["state"], "completed")
        self.assertTrue(first["document"]["accepted"])
        self.assertEqual(first["document"]["category_id"], "inbox")
        self.assertEqual(first["document"]["stages"]["wiki"]["state"], "pending")
        self.assertEqual(first["document"]["stages"]["projection"]["state"], "pending")
        calls = len(self.provider.calls)
        replay = await self.ingest()
        self.assertEqual(replay["operation_id"], first["operation_id"])
        self.assertEqual(len(self.provider.calls), calls)
        with self.assertRaises(OperationConflict):
            await self.ingest(b"different")
        sid = first["document"]["source_id"]
        self.assertEqual(
            self.custody.resolve_exact(sid, 1).original_bytes,
            b"hello world\nsecond line",
        )
        self.assertTrue(all(len(c[1].encode()) <= 8 for c in self.provider.calls))
        chunks = first["document"]["chunks"]
        self.assertEqual(chunks[0]["passage_start"], 0)
        self.assertEqual(
            chunks[-1]["passage_end"],
            first["document"]["extractions"][0]["passage_count"],
        )
        self.assertTrue(
            all(a["passage_end"] == b["passage_start"] for a, b in pairwise(chunks))
        )

    async def test_crash_after_custody_before_registration_restart(self):
        original = self.catalog.register_manifest

        def crash(*args, **kwargs):
            raise KeyboardInterrupt()

        self.catalog.register_manifest = crash
        with self.assertRaises(KeyboardInterrupt):
            await self.ingest()
        self.catalog.register_manifest = original
        pending = self.catalog.list_recoverable_operations()[0]
        sid = pending["result"]["recovery_intent"]["source_id"]
        self.assertEqual(self.custody.list_inventory(), [(sid, 1)])
        self.catalog.close()
        self.catalog = LifeContextCatalog(self.paths)
        self.service = DocumentService(
            self.catalog, self.custody, self.derived, self.provider, max_chunk_bytes=8
        )
        recovered = await self.service.recover(pending["operation_id"])
        self.assertEqual(recovered["state"], "completed")
        replay = await self.ingest()
        self.assertEqual(replay["operation_id"], pending["operation_id"])
        self.assertEqual(self.custody.list_inventory(), [(sid, 1)])

    async def test_partial_failure_retries_only_unacknowledged(self):
        self.provider.fail_at = 2
        first = await self.ingest()
        self.assertEqual(first["state"], "retryable_failed")
        self.assertTrue(first["document"]["accepted"])
        self.assertNotIn("secret", str(first))
        first_id = self.provider.calls[0][0]
        self.provider.fail_at = None
        recovered = await self.service.retry(first["operation_id"])
        self.assertEqual(recovered["state"], "completed")
        self.assertEqual(sum(c[0] == first_id for c in self.provider.calls), 1)

    async def test_wiki_waits_for_indexing_but_original_projection_does_not(self):
        wiki_calls, projection_calls = [], []

        def wiki(document):
            wiki_calls.append(document["source_id"])
            return {"state": "completed"}

        def project(document):
            projection_calls.append(document["source_id"])
            return {"state": "completed"}

        self.service.wiki_hook = wiki
        self.service.projection_hook = project
        self.provider.fail_at = 1
        first = await self.ingest(b"short")
        self.assertEqual(first["state"], "retryable_failed")
        self.assertEqual(wiki_calls, [])
        self.assertEqual(len(projection_calls), 1)
        self.assertEqual(first["document"]["stages"]["wiki"]["state"], "pending")
        self.provider.fail_at = None
        recovered = await self.service.retry(first["operation_id"])
        self.assertEqual(recovered["state"], "completed")
        self.assertEqual(len(wiki_calls), 1)
        self.assertEqual(len(projection_calls), 1)

    async def test_async_pending_is_not_completed(self):
        self.provider.async_ack = True
        first = await self.ingest(b"short")
        self.assertEqual(first["state"], "retryable_failed")
        pending = await self.service.retry(first["operation_id"])
        self.assertEqual(pending["state"], "retryable_failed")
        self.assertEqual(len(self.provider.calls), 1)
        for document in self.provider.documents.values():
            document["memory_unit_count"] = 1
        recovered = await self.service.retry(first["operation_id"])
        self.assertEqual(recovered["state"], "completed")
        self.assertEqual(len(self.provider.calls), 1)

    async def test_binary_failed_extraction_preserves_original(self):
        data = b"\x00\xff\xfe\x80private binary"
        result = await self.ingest(data, filename="bad.pdf")
        self.assertEqual(result["state"], "retryable_failed")
        sid = result["document"]["source_id"]
        self.assertEqual(self.custody.resolve_exact(sid, 1).original_bytes, data)
        self.assertEqual(self.provider.calls, [])
        record = result["document"]["extractions"][0]
        self.assertIsNotNone(record["artifact_path"])
        self.assertNotIn("private binary", str(result["result"]))

    async def test_rerun_allocates_only_derived_revision(self):
        first = await self.ingest(b"short")
        rerun = await self.service.retry(first["operation_id"], rerun_extraction=True)
        self.assertEqual(rerun["state"], "completed")
        self.assertEqual(
            [r["extraction_revision"] for r in rerun["document"]["extractions"]], [1, 2]
        )
        self.assertEqual(len(self.custody.list_inventory()), 1)

    async def test_repeated_rerun_recovers_same_intent_after_custody_becomes_available(self):
        first = await self.ingest(b"short")
        sid = first["document"]["source_id"]
        original = self.custody.original_path(sid, 1)
        inventory = self.custody.list_inventory()
        manifest = self.custody.resolve_exact(sid, 1).manifest.to_dict()
        original.chmod(0o644)
        try:
            failed = await self.service.retry(first["operation_id"], rerun_extraction=True)
            self.assertEqual(failed["state"], "retryable_failed")
            self.assertEqual(failed["error_code"], "evidence_unavailable")
            self.assertEqual(len(failed["document"]["extractions"]), 1)
            repeated = await self.service.retry(first["operation_id"], rerun_extraction=True)
            self.assertEqual(repeated["state"], "retryable_failed")
            self.assertEqual(repeated["operation_id"], failed["operation_id"])
        finally:
            original.chmod(0o600)
        recovered = await self.service.retry(first["operation_id"], rerun_extraction=True)
        self.assertEqual(recovered["state"], "completed")
        self.assertEqual(recovered["operation_id"], failed["operation_id"])
        self.assertNotIn("error_code", recovered["result"])
        self.assertEqual([row["extraction_revision"] for row in recovered["document"]["extractions"]], [1, 2])
        self.assertEqual(self.custody.list_inventory(), inventory)
        evidence = self.custody.resolve_exact(sid, 1)
        self.assertEqual(evidence.manifest.to_dict(), manifest)
        self.assertEqual(evidence.original_bytes, b"short")
        calls = len(self.provider.calls)
        replay = await self.service.retry(recovered["operation_id"])
        self.assertEqual(replay["state"], "completed")
        self.assertEqual(len(self.provider.calls), calls)

    async def test_historical_original_rerun_is_exact_and_only_adds_derived_revision(self):
        originals = {}
        for text in ("first", "second", "third"):
            document = Document()
            document.add_paragraph(text)
            output = io.BytesIO()
            document.save(output)
            originals[text] = output.getvalue()
        first = await self.ingest(originals["first"], key="first", filename="sample.docx")
        sid = first["document"]["source_id"]
        second = await self.service.import_document(originals["second"], "sample.docx", None, {"source": "second"}, "second", source_id=sid, source_version=2)
        third = await self.service.import_document(originals["third"], "sample.docx", None, {"source": "third"}, "third", source_id=sid, source_version=3)
        self.assertTrue(all(outcome["state"] == "completed" for outcome in (first, second, third)))
        inventory = self.custody.list_inventory()
        manifests = [self.custody.resolve_exact(sid, version).manifest.to_dict() for version in (1, 2, 3)]
        rerun = await self.service.retry(second["operation_id"], rerun_extraction=True)
        self.assertEqual(rerun["state"], "completed")
        self.assertEqual([row["extraction_revision"] for row in rerun["document"]["extractions"]], [1, 2])
        self.assertEqual(self.catalog.get_current_manifest(sid).source_version, 3)
        self.assertEqual(self.custody.list_inventory(), inventory)
        self.assertEqual([self.custody.resolve_exact(sid, version).manifest.to_dict() for version in (1, 2, 3)], manifests)
        self.assertEqual(self.custody.resolve_exact(sid, 2).original_bytes, originals["second"])
        self.assertEqual(self.derived.read(sid, 2, 2)["original_checksum"], manifests[1]["sha256"])
        self.assertEqual([row["extraction_revision"] for row in self.catalog.list_extractions(sid, 3)], [1])
        calls = len(self.provider.calls)
        replay = await self.service.retry(rerun["operation_id"])
        self.assertEqual(replay["state"], "completed")
        self.assertEqual(len(self.provider.calls), calls)

    async def test_historical_rerun_requires_registered_catalog_identity(self):
        for kind in ("missing", "checksum", "provenance"):
            first = await self.ingest(b"short", key=kind)
            manifest = self.catalog.get_manifest(first["document"]["source_id"], 1)
            registered = {"missing": None, "checksum": {**manifest, "sha256": "f" * 64},
                          "provenance": {**manifest, "provenance": {"source": "changed"}}}[kind]
            with self.subTest(kind=kind), patch.object(self.catalog, "get_manifest", return_value=registered):
                calls = len(self.provider.calls)
                failed = await self.service.retry(first["operation_id"], rerun_extraction=True)
                self.assertEqual(failed["state"], "retryable_failed")
                self.assertEqual(len(self.provider.calls), calls)
                self.assertEqual(len(self.catalog.list_extractions(first["document"]["source_id"], 1)), 1)

    async def test_reextraction_preserves_foreign_provider_identity(self):
        first = await self.ingest(b"short")
        sid = first["document"]["source_id"]
        pid = first["document"]["chunks"][0]["provider_id"]
        original = dict(self.provider.documents[pid])
        for foreign in (
            {**original, "document_metadata": {"source_id": "foreign"}},
            {**original, "id": "foreign"},
        ):
            with self.subTest(foreign=foreign):
                self.provider.documents[pid] = foreign
                failed = await self.service.retry(first["operation_id"], rerun_extraction=True)
                self.assertEqual(failed["state"], "retryable_failed")
                self.assertEqual(self.provider.documents[pid], foreign)
                self.assertEqual(self.provider.delete_calls, [])
                self.assertEqual(failed["document"]["stages"]["indexing"]["state"], "failed")
                old = self.catalog.list_provider_chunks(sid, 1)[0]
                self.assertEqual((old["submission_state"], old["completion_state"]), ("retired", "pending"))
        self.provider.documents[pid] = original
        recovered = await self.service.retry(failed["operation_id"])
        self.assertEqual(recovered["state"], "completed")
        self.assertNotIn(pid, self.provider.documents)
        self.assertEqual(self.provider.delete_calls.count(pid), 1)

    async def test_reextraction_retirement_requires_acknowledgement_and_retries(self):
        first = await self.ingest(b"short")
        sid = first["document"]["source_id"]
        pid = first["document"]["chunks"][0]["provider_id"]
        self.provider.delete_success = False
        failed = await self.service.retry(first["operation_id"], rerun_extraction=True)
        self.assertEqual(failed["state"], "retryable_failed")
        self.assertIn(pid, self.provider.documents)
        old = self.catalog.list_provider_chunks(sid, 1)[0]
        self.assertEqual((old["submission_state"], old["completion_state"]), ("retired", "pending"))
        retain_count = len(self.provider.calls)
        self.provider.delete_success = True
        recovered = await self.service.retry(failed["operation_id"])
        self.assertEqual(recovered["state"], "completed")
        self.assertNotIn(pid, self.provider.documents)
        self.assertEqual(len(self.provider.calls), retain_count)
        old = self.catalog.list_provider_chunks(sid, 1)[0]
        self.assertEqual((old["submission_state"], old["completion_state"]), ("retired", "completed"))

    async def test_tombstone_prevents_retry_resurrection(self):
        self.provider.fail_at = 1
        first = await self.ingest(b"short")
        sid = first["document"]["source_id"]
        digest = "a" * 64
        transfer = self.catalog.begin_operation("forget-transfer", digest, "import")
        self.catalog.initialize_transfer(
            transfer["operation_id"],
            digest,
            1,
            [
                {
                    "source_id": sid,
                    "source_version": 1,
                    "lifecycle_state": "forgotten",
                    "sha256": first["result"]["sources"][0]["sha256"],
                }
            ],
        )
        self.catalog.transition_operation(
            transfer["operation_id"], "pending", "running"
        )
        self.catalog.record_transfer_tombstone(
            transfer["operation_id"], sid, 1, "forgotten"
        )
        before = len(self.provider.calls)
        result = await self.service.retry(first["operation_id"])
        self.assertEqual(result["state"], "retryable_failed")
        self.assertEqual(len(self.provider.calls), before)

    async def test_ambiguous_submission_reconciles_without_repeat(self):
        original = self.provider.retain

        async def crash_after_effect(*args, **kwargs):
            await original(*args, **kwargs)
            raise KeyboardInterrupt()

        self.provider.retain = crash_after_effect
        with self.assertRaises(KeyboardInterrupt):
            await self.ingest(b"short")
        operation = self.catalog.list_recoverable_operations()[0]
        self.provider.retain = original
        result = await self.service.recover(operation)
        self.assertEqual(result["state"], "completed")
        self.assertEqual(len(self.provider.calls), 1)

    async def test_projection_pending_does_not_complete_wiki_during_outage(self):
        self.provider.fail_at = 1
        self.service.wiki_hook = lambda document: {"state": "completed"}
        self.service.projection_hook = lambda document: {"state": "pending"}
        result = await self.ingest(b"short")
        self.assertEqual(result["state"], "retryable_failed")
        self.assertEqual(result["document"]["stages"]["wiki"]["state"], "pending")
        self.assertNotEqual(
            result["document"]["stages"]["projection"]["state"], "completed"
        )

    async def test_configured_ocr_path_reaches_extractor(self):
        received = []
        original = self.service.extractor

        def extract(data, filename, **kwargs):
            received.append(kwargs.pop("tesseract_path"))
            return original(data, filename, **kwargs)

        self.service.tesseract_path = "/private/runtime/tesseract"
        self.service.extractor = extract
        outcome = await self.ingest(b"short")
        self.assertEqual(outcome["state"], "completed")
        self.assertEqual(received, ["/private/runtime/tesseract"])

    async def test_completion_failure_is_retryable_without_duplicate_indexing(self):
        completed = []

        def finalize(document):
            completed.append(document["source_id"])
            if len(completed) == 1:
                raise RuntimeError("private finalization failure")

        self.service.completion_hook = finalize
        first = await self.ingest(b"short")
        self.assertEqual(first["state"], "retryable_failed")
        self.assertNotIn("private", str(first))
        self.assertEqual(len(self.provider.calls), 1)
        recovered = await self.service.retry(first["operation_id"])
        self.assertEqual(recovered["state"], "completed")
        self.assertEqual(len(self.provider.calls), 1)
        self.assertEqual(len(completed), 2)

    async def test_initial_provider_collision_is_not_overwritten(self):
        first = await self.ingest(b"short")
        sid = first["document"]["source_id"]
        self.assertEqual(first["state"], "completed")
        self.provider.documents[f"doc:{sid}:v2:e1:p0-1"] = {
            "id": f"doc:{sid}:v2:e1:p0-1", "document_metadata": {"sha256": "a" * 64},
            "memory_unit_count": 1,
        }
        before = len(self.provider.calls)
        second = await self.service.import_document(b"short", "sample.txt", "text/plain", {"source": "test"}, "replacement",
                                                    source_id=sid, source_version=2)
        self.assertEqual(second["state"], "retryable_failed")
        self.assertEqual(len(self.provider.calls), before)

    async def test_missing_backend_has_explicit_blocked_reason(self):
        def blocked(data, filename, **kwargs):
            return ExtractionArtifact(
                source_id=kwargs["source_id"],
                source_version=kwargs["source_version"],
                extraction_revision=kwargs["extraction_revision"],
                original_checksum=hashlib.sha256(data).hexdigest(),
                config_identity=kwargs["limits"].identity,
                status="blocked",
                reason="ocr_backend_unavailable",
            )

        self.service.extractor = blocked
        result = await self.ingest(b"binary", filename="image.png")
        self.assertEqual(result["document"]["stages"]["extraction"]["state"], "blocked")
        self.assertEqual(
            result["document"]["extractions"][0]["reason"], "ocr_backend_unavailable"
        )
        self.assertTrue(result["document"]["accepted"])
        self.assertEqual(self.provider.calls, [])


if __name__ == "__main__":
    unittest.main()
