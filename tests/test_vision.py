"""Opt-in vision tier: configuration, labelled merging, fault semantics and adapters."""

import asyncio
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch

from lifecontext.catalog import LifeContextCatalog
from lifecontext.credentials import _parse_credentials
from lifecontext.custody import LifeContextCustody
from lifecontext.derived import DerivedStore
from lifecontext.documents import VISION_OPT_OUT, DocumentService
from lifecontext.extraction import coverage_summary, extract_document
from lifecontext.hindsight import HindsightStartupError
from lifecontext.paths import LifeContextPaths
from lifecontext.vision import (
    PROBE_PROMPT,
    PROMPTS,
    VisionBlocked,
    VisionCache,
    VisionClient,
    VisionConfig,
    VisionReader,
    VisionReply,
    VisionUnavailable,
    _ItemRejected,
    load_vision_config,
    parse_reading,
    render_reading,
    validated_vision_settings,
)

READING = json.dumps(
    {
        "transcription": "Dream Outcome → loving relationship",
        "elements": [{"type": "handwriting", "description": "Notes on lined paper", "markdown": ""}],
    }
)


def pdf_page(*, text=False, drawing=False, picture=False):
    from PIL import Image
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    if picture:
        buffer = BytesIO()
        Image.new("RGB", (100, 100), "gray").save(buffer, format="PDF")
        writer = PdfWriter(clone_from=BytesIO(buffer.getvalue()))
        if text:
            writer.pages[0].merge_page(_text_page())
    else:
        writer = PdfWriter()
        page = writer.add_blank_page(width=100, height=100)
        stream = DecodedStreamObject()
        if drawing:
            # Distinct ink per page, so pages never share a cache entry by accident.
            stream.set_data(f"0 g 10 10 {10 + int(drawing) * 7} 80 re f".encode())
            page[NameObject("/Contents")] = writer._add_object(stream)
        elif text:
            font = DictionaryObject({
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            })
            page[NameObject("/Resources")] = DictionaryObject(
                {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
            )
            stream.set_data(b"BT /F1 12 Tf 10 50 Td (Hello PDF) Tj ET")
            page[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def _text_page():
    from pypdf import PdfReader

    return PdfReader(BytesIO(pdf_page(text=True))).pages[0]


def pdf(*pages):
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    for page in pages:
        writer.add_page(PdfReader(BytesIO(page)).pages[0])
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


class FakeClient:
    """Answers the probe from the code embedded by the patched probe image."""

    def __init__(self, *replies, probe_answer=None):
        self.replies = list(replies)
        self.calls = []
        self.probe_answer = probe_answer

    async def read(self, jpeg, prompt, *, timeout):
        if prompt == PROBE_PROMPT:
            code = jpeg[3:].decode()
            return VisionReply(json.dumps({"code": self.probe_answer or code}))
        self.calls.append((prompt, jpeg))
        reply = self.replies.pop(0) if self.replies else READING
        if isinstance(reply, Exception):
            raise reply
        return reply if isinstance(reply, VisionReply) else VisionReply(reply)


def config(**overrides):
    values = {"provider": "openai_compatible", "model": "fake-model", "base_url": "http://127.0.0.1:9/v1", "max_pages": 10}
    values.update(overrides)
    return VisionConfig(**values)


class VisionConfigTests(unittest.TestCase):
    def test_block_is_optional_and_strict(self):
        self.assertIsNone(validated_vision_settings({"llm": {}}))
        valid = {"provider": "anthropic", "model": "claude-sonnet-5-5"}
        self.assertEqual(
            validated_vision_settings({"vision": valid}),
            {"provider": "anthropic", "model": "claude-sonnet-5-5", "max_pages_per_document": 200},
        )
        for block in (
            {**valid, "temperature": "0"},
            {**valid, "provider": "groq"},
            {**valid, "model": " padded "},
            {**valid, "model": "a#b"},
            {**valid, "base_url": "http://example.com/v1"},
            {**valid, "base_url": "https://user:pw@example.com"},
            {"provider": "openai_compatible", "model": "qwen"},
            {**valid, "max_pages_per_document": 0},
            {**valid, "max_pages_per_document": True},
            "not a mapping",
        ):
            with self.subTest(block=block), self.assertRaises(HindsightStartupError):
                validated_vision_settings({"vision": block})

    def test_credentials_are_separate_reused_only_for_the_same_endpoint(self):
        llm = {"provider": "openai_compatible", "model": "m", "base_url": "http://127.0.0.1:18317/v1"}
        same = {"llm": llm, "vision": {"provider": "openai_compatible", "model": "v", "base_url": "http://127.0.0.1:18317/v1"}}
        self.assertEqual(load_vision_config(same, {"LIFEWEAVE_LLM_API_KEY": "llm"}).api_key, "llm")
        self.assertEqual(
            load_vision_config(same, {"LIFEWEAVE_LLM_API_KEY": "llm", "LIFEWEAVE_VISION_API_KEY": "vision"}).api_key,
            "vision",
        )
        remote = {"llm": llm, "vision": {"provider": "anthropic", "model": "claude-haiku-5-5"}}
        with self.assertRaises(HindsightStartupError) as caught:
            load_vision_config(remote, {"LIFEWEAVE_LLM_API_KEY": "llm"})
        self.assertEqual(caught.exception.code, "missing_credentials")
        local = {"llm": llm, "vision": {"provider": "openai_compatible", "model": "qwen3.5:4b", "base_url": "http://localhost:11434/v1"}}
        self.assertIsNone(load_vision_config(local, {"LIFEWEAVE_LLM_API_KEY": "llm"}).api_key)
        self.assertNotIn("llm", repr(load_vision_config(same, {"LIFEWEAVE_LLM_API_KEY": "llm"})))
        self.assertEqual(
            _parse_credentials("LIFEWEAVE_LLM_API_KEY=a\nLIFEWEAVE_VISION_API_KEY=b\n"),
            {"LIFEWEAVE_LLM_API_KEY": "a", "LIFEWEAVE_VISION_API_KEY": "b"},
        )

    def test_config_identity_tracks_model_and_prompt_not_endpoint(self):
        self.assertEqual(config().identity, config(base_url="http://localhost:1/v1").identity)
        self.assertNotEqual(config().identity, config(model="other").identity)
        self.assertNotEqual(config().identity, config(max_pages=5).identity)

    def test_settings_file_accepts_only_known_vision_keys(self):
        import os

        from lifecontext.config import ConfigError, load_settings

        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {"HOME": home}):
            directory = Path(home) / ".lifeweave"
            directory.mkdir(mode=0o700)
            path = directory / "config.yaml"
            for body, ok in (
                ("vision:\n  provider: anthropic\n  model: m\n  max_pages_per_document: 5\n", True),
                ("vision:\n  provider: anthropic\n  model: m\n  detail: high\n", False),
                ("vision:\n  provider: anthropic\n  model: m\n  max_pages_per_document: '5'\n", False),
            ):
                path.write_text(body)
                path.chmod(0o600)
                with self.subTest(body=body):
                    if ok:
                        self.assertEqual(load_settings()["vision"]["max_pages_per_document"], 5)
                    else:
                        with self.assertRaises(ConfigError):
                            load_settings()


class ReadingTests(unittest.TestCase):
    def test_parse_validates_shape_and_size(self):
        self.assertEqual(
            parse_reading('```json\n{"transcription": " a ", "elements": [{"type": "map", "description": "d"}]}\n```'),
            {"transcription": "a", "elements": [{"type": "other", "description": "d", "markdown": ""}]},
        )
        for raw in ("not json", "[]", '{"transcription": 3}', '{"elements": [1]}',
                    json.dumps({"transcription": "x" * 300_000})):
            with self.subTest(raw=raw[:20]):
                self.assertIsNone(parse_reading(raw))

    def test_render_puts_a_label_first(self):
        text = render_reading(
            {"transcription": "Hello", "elements": [{"type": "table", "description": "Costs", "markdown": "| a |\n| --- |"}]},
            what="page 90", model="m",
        )
        self.assertEqual(
            text,
            "[Model-read page 90 by m; verify against the original]\nHello\n\n[Table] Costs\n| a |\n| --- |\n",
        )


class VisionReaderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cache = VisionCache(Path(self.temp.name))
        patcher = patch("lifecontext.vision._probe_image", side_effect=lambda code: b"\xff\xd8\xff" + code.encode())
        patcher.start()
        self.addCleanup(patcher.stop)

    def reader(self, client, **overrides):
        return VisionReader(config(**overrides), self.cache, client=client)

    async def test_unreadable_page_is_read_labelled_and_accounted(self):
        client = FakeClient()
        artifact = await self.reader(client).extract(
            pdf(pdf_page(text=True), pdf_page(drawing=2)), "a.pdf", source_id="doc_1"
        )
        self.assertEqual(artifact.status, "completed", artifact.reason)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0][0], PROMPTS["page"])
        first, second = artifact.segments
        self.assertEqual(first.text, "Hello PDF\n")
        self.assertTrue(second.text.startswith("[Model-read page 2 by fake-model; verify against the original]\n"))
        self.assertEqual(second.locator.container, "vision:fake-model:page:page:2")
        self.assertEqual(artifact.warnings, ("vision_page:2; read by fake-model; verify against the original",))
        coverage = coverage_summary(artifact.to_dict())
        self.assertEqual((coverage["total"], coverage["resolved"], coverage["complete"]), (2, 2, True))
        self.assertNotEqual(artifact.config_identity, extract_document(b"%PDF-", "a.pdf", source_id="doc_1").config_identity)

    async def test_wholly_unreadable_pdf_completes_through_vision(self):
        artifact = await self.reader(FakeClient()).extract(pdf(pdf_page(drawing=3)), "a.pdf", source_id="doc_1")
        self.assertEqual((artifact.status, artifact.reason), ("completed", None))
        self.assertEqual([s.locator.start for s in artifact.segments], [1])

    async def test_figures_on_text_pages_are_added_after_the_text(self):
        client = FakeClient(json.dumps({"transcription": "", "elements": [
            {"type": "table", "description": "Fitness versus liposuction", "markdown": "| a | b |\n| --- | --- |"}]}))
        artifact = await self.reader(client).extract(pdf(pdf_page(text=True, picture=True)), "a.pdf", source_id="doc_1")
        self.assertEqual(client.calls[0][0], PROMPTS["figure"])
        native, figure = artifact.segments
        self.assertEqual(native.text, "Hello PDF\n")
        self.assertIn("[Table] Fitness versus liposuction", figure.text)
        self.assertEqual(artifact.warnings, ("vision_figure:1; read by fake-model; verify against the original",))

    async def test_probe_failure_blocks_vision_and_keeps_the_plain_ledger(self):
        client = FakeClient(probe_answer="WRONG1")
        reader = self.reader(client)
        artifact = await reader.extract(pdf(pdf_page(text=True), pdf_page(drawing=4)), "a.pdf", source_id="doc_1")
        self.assertEqual(client.calls, [])
        self.assertEqual(
            artifact.warnings,
            ("ocr_unavailable_page:2; page content unresolved", "vision_blocked; vision_model_unusable"),
        )
        self.assertEqual(reader.public_status()["probe"], "vision_model_unusable")

    async def test_refusal_malformed_and_rejected_items_stay_unresolved(self):
        client = FakeClient(VisionReply("", refused=True), "nonsense", "still nonsense", _ItemRejected("rejected_request"))
        document = pdf(pdf_page(drawing=5), pdf_page(drawing=6), pdf_page(text=True))
        artifact = await self.reader(client).extract(document, "a.pdf", source_id="doc_1")
        self.assertEqual(artifact.status, "completed")
        self.assertEqual(
            artifact.warnings,
            (
                "ocr_unavailable_page:1; page content unresolved",
                "ocr_unavailable_page:2; page content unresolved",
                "vision_refused:page:1; the vision model declined this item",
                "vision_failed:page:2; malformed_response",
            ),
        )
        # A refusal is cached; a malformed answer is asked again on the next run.
        retry_client = FakeClient()
        await self.reader(retry_client).extract(document, "a.pdf", source_id="doc_1")
        self.assertEqual(len(retry_client.calls), 1)

    async def test_outage_is_retryable_and_resumes_from_the_cache(self):
        document = pdf(pdf_page(drawing=7), pdf_page(drawing=8), pdf_page(drawing=9))
        failing = FakeClient(READING, VisionUnavailable("vision_unreachable"))
        with self.assertRaises(VisionUnavailable):
            await self.reader(failing).extract(document, "a.pdf", source_id="doc_1")
        self.assertEqual(len(failing.calls), 2)
        resumed = FakeClient()
        artifact = await self.reader(resumed).extract(document, "a.pdf", source_id="doc_1")
        self.assertEqual(len(resumed.calls), 2)
        self.assertEqual(len(artifact.segments), 3)

    async def test_deadline_exhaustion_is_retryable(self):
        ticks = iter([0.0, 0.0, 10_000.0, 10_000.0])
        reader = VisionReader(config(), self.cache, client=FakeClient(), clock=lambda: next(ticks))
        with self.assertRaises(VisionUnavailable) as caught:
            await reader.extract(pdf(pdf_page(drawing=1), pdf_page(drawing=2)), "a.pdf", source_id="doc_1")
        self.assertEqual(str(caught.exception), "vision_deadline")

    async def test_budget_and_model_changes(self):
        document = pdf(pdf_page(drawing=3), pdf_page(drawing=4))
        client = FakeClient()
        artifact = await self.reader(client, max_pages=1).extract(document, "a.pdf", source_id="doc_1")
        self.assertEqual(len(client.calls), 1)
        self.assertIn("vision_skipped:page:2; vision page budget reached", artifact.warnings)
        self.assertIn("ocr_unavailable_page:2; page content unresolved", artifact.warnings)
        other = FakeClient()
        await self.reader(other, model="other-model").extract(document, "a.pdf", source_id="doc_1")
        self.assertEqual(len(other.calls), 2)

    async def test_merge_settles_items_inside_a_compacted_ledger(self):
        from lifecontext.extraction import (
            ExtractionArtifact,
            Locator,
            RoutedImage,
            TextSegment,
            compact_ledger,
        )

        figures = [f"figure_not_read:{n}; images or drawings on this page were not read" for n in range(1, 1902, 2)]
        artifact = ExtractionArtifact(
            source_id="doc_1", source_version=1, original_checksum="a" * 64, extraction_revision=1,
            config_identity="b" * 64, status="completed", format="pdf",
            segments=(TextSegment("Text\n", 0, 5, Locator("page", 1, 1)),),
            warnings=compact_ledger(figures),
        )
        image = RoutedImage(item="page:1", mode="figure", kind="page", index=1, after=1,
                            data=b"\xff\xd8\xffx", resolves=(figures[0],))
        reader = self.reader(FakeClient())
        merged = reader._merge(artifact, [(image, reader_outcome())], extraction_limits())
        coverage = coverage_summary(merged.to_dict())
        self.assertNotIn("figure_not_read:1", coverage["partial"])
        self.assertIn("figure_not_read:3", coverage["partial"])
        self.assertEqual(coverage["vision"], 1)

    async def test_a_suspect_text_layer_stays_partial_after_a_model_reading(self):
        from lifecontext.extraction import (
            ExtractionArtifact,
            Locator,
            RoutedImage,
            TextSegment,
        )

        suspect = "text_layer_suspect:1; the text layer may be garbled"
        artifact = ExtractionArtifact(
            source_id="doc_1", source_version=1, original_checksum="a" * 64, extraction_revision=1,
            config_identity="b" * 64, status="completed", format="pdf",
            segments=(TextSegment("Пpивiт\n", 0, 7, Locator("page", 1, 1)),), warnings=(suspect,),
        )
        image = RoutedImage(item="page:1", mode="page", kind="page", index=1, after=1, data=b"\xff\xd8\xffx")
        reader = self.reader(FakeClient())
        merged = reader._merge(artifact, [(image, reader_outcome())], extraction_limits())
        self.assertIn(suspect, merged.warnings)
        self.assertFalse(coverage_summary(merged.to_dict())["complete"])

    def test_endpoints_are_explicit_and_unknown_errors_stay_per_item(self):
        import os

        from lifecontext.vision import _classify

        with patch.dict(os.environ, {"ANTHROPIC_BASE_URL": "https://attacker.invalid",
                                     "OPENAI_BASE_URL": "https://attacker.invalid"}):
            self.assertEqual(VisionClient(config(provider="anthropic", base_url=None)).base_url,
                             "https://api.anthropic.com")
            self.assertEqual(VisionClient(config(provider="openai", base_url=None)).base_url,
                             "https://api.openai.com/v1")
        self.assertIsInstance(_classify(ValueError("unexpected response shape")), _ItemRejected)
        self.assertIsInstance(_classify(ConnectionResetError()), VisionUnavailable)

    async def test_cache_is_removed_with_the_source(self):
        await self.reader(FakeClient()).extract(pdf(pdf_page(drawing=5)), "a.pdf", source_id="doc_1")
        directory = Path(self.temp.name) / "vision-cache" / "doc_1"
        self.assertEqual(len(list(directory.iterdir())), 1)
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        self.cache.remove("doc_1")
        self.assertFalse(directory.exists())
        self.cache.remove("doc_1")
        with self.assertRaises(ValueError):
            self.cache.remove("../escape")


def reader_outcome():
    from lifecontext.vision import _Outcome

    return _Outcome("read", {"transcription": "", "elements": [
        {"type": "diagram", "description": "A funnel", "markdown": ""}]})


def extraction_limits():
    from lifecontext.extraction import ExtractionLimits

    return ExtractionLimits()


class FakeProvider:
    def __init__(self):
        self.calls, self.documents = [], {}

    async def retain(self, source_id, content, *, scope_tag, metadata, retain_async=False):
        self.calls.append((source_id, content))
        self.documents[source_id] = {"id": source_id, "document_metadata": metadata, "memory_unit_count": 1}
        return {"success": True, "async": False}

    async def get_document(self, source_id):
        return self.documents.get(source_id, {})

    async def delete_document(self, source_id):
        self.documents.pop(source_id, None)
        return {"success": True}


class DocumentVisionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.paths = LifeContextPaths(Path(self.temp.name))
        self.catalog = LifeContextCatalog(self.paths)
        self.addCleanup(self.catalog.close)
        self.provider = FakeProvider()
        self.cache = VisionCache(self.paths.lifeweave_home)
        patcher = patch("lifecontext.vision._probe_image", side_effect=lambda code: b"\xff\xd8\xff" + code.encode())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.document = pdf(pdf_page(text=True), pdf_page(drawing=6), pdf_page(drawing=7))

    def service(self, client):
        reader = VisionReader(config(), self.cache, client=client) if client is not None else None
        return DocumentService(
            self.catalog, LifeContextCustody(self.paths, max_bytes=32 * 1024 * 1024),
            DerivedStore(self.paths), self.provider, vision=reader, vision_cache=self.cache,
        )

    async def test_outage_fails_retryably_and_a_plain_retry_finishes_the_same_revision(self):
        client = FakeClient(READING, VisionUnavailable("vision_unreachable"))
        service = self.service(client)
        first = await service.import_document(self.document, "a.pdf", "application/pdf", {}, "key")
        self.assertEqual((first["state"], first["error_code"]), ("retryable_failed", "provider_unavailable"))
        document = first["document"]
        self.assertEqual(document["stages"]["extraction"]["state"], "failed")
        self.assertEqual(document["extractions"], [])
        self.assertEqual(self.provider.calls, [])
        client.replies = []
        done = await service.retry(first["operation_id"])
        self.assertEqual(done["state"], "completed")
        self.assertEqual(len(client.calls), 3)  # two pages + the one that failed, never page 2 twice
        self.assertEqual([e["extraction_revision"] for e in done["document"]["extractions"]], [1])
        # Text layer and model-read passages are indexed as separate chunks.
        self.assertEqual(len(self.provider.calls), 2)
        self.assertTrue(self.provider.calls[1][1].startswith("[Model-read page 2"))
        self.assertTrue(done["document"]["coverage"]["complete"])

    async def test_no_vision_is_honoured_on_every_rerun_path(self):
        client = FakeClient()
        service = self.service(client)
        first = await service.import_document(self.document, "a.pdf", "application/pdf", {}, "key", vision=False)
        self.assertEqual(first["state"], "completed")
        self.assertIn(VISION_OPT_OUT, first["document"]["extractions"][-1]["warnings"])
        recovered = await service.recover(first["operation_id"])
        rerun = await service.retry(first["operation_id"], rerun_extraction=True)
        self.assertEqual((recovered["state"], rerun["state"]), ("completed", "completed"))
        self.assertEqual(client.calls, [])
        self.assertIn(VISION_OPT_OUT, rerun["document"]["extractions"][-1]["warnings"])
        lifted = await service.retry(rerun["operation_id"], rerun_extraction=True, vision=True)
        self.assertEqual(lifted["state"], "completed")
        self.assertEqual(len(client.calls), 2)
        self.assertNotIn(VISION_OPT_OUT, lifted["document"]["extractions"][-1]["warnings"])

    async def test_opt_out_covers_later_versions_of_the_document(self):
        client = FakeClient()
        service = self.service(client)
        first = await service.import_document(self.document, "a.pdf", "application/pdf", {}, "v1", vision=False)
        sid = first["document"]["source_id"]
        second = await service.import_document(
            self.document + b"\n", "a.pdf", "application/pdf", {}, "v2", source_id=sid, source_version=2
        )
        self.assertEqual(second["state"], "completed")
        self.assertEqual(client.calls, [])
        self.assertIn(VISION_OPT_OUT, second["document"]["extractions"][-1]["warnings"])

    async def test_no_vision_can_follow_a_rerun_stuck_on_an_outage(self):
        client = FakeClient()
        service = self.service(client)
        first = await service.import_document(self.document, "a.pdf", "application/pdf", {}, "key")
        client.replies = [VisionUnavailable("vision_unreachable")]
        client.calls.clear()
        self.cache.remove(first["document"]["source_id"])
        stuck = await service.retry(first["operation_id"], rerun_extraction=True)
        self.assertEqual(stuck["state"], "retryable_failed")
        client.calls.clear()
        quiet = await service.retry(first["operation_id"], rerun_extraction=True, vision=False)
        self.assertEqual(quiet["state"], "completed")
        self.assertEqual(client.calls, [])
        self.assertEqual([e["extraction_revision"] for e in quiet["document"]["extractions"]], [1, 2])
        # The stuck operation now finds revision 2 written and sends nothing.
        resumed = await service.recover(stuck["operation_id"])
        self.assertEqual(resumed["state"], "completed")
        self.assertEqual(client.calls, [])

    async def test_forget_during_reading_writes_nothing(self):
        service = self.service(None)
        tombstoned = {"now": False}

        class ForgettingClient(FakeClient):
            async def read(inner, jpeg, prompt, *, timeout):
                reply = await FakeClient.read(inner, jpeg, prompt, timeout=timeout)
                if prompt != PROBE_PROMPT:
                    tombstoned["now"] = True
                return reply

        service.vision = VisionReader(config(), self.cache, client=ForgettingClient())
        real = service.catalog.list_tombstones
        with patch.object(service.catalog, "list_tombstones",
                          side_effect=lambda sid: [{"tombstone_type": "forgotten"}] if tombstoned["now"] else real(sid)):
            outcome = await service.import_document(self.document, "a.pdf", "application/pdf", {}, "key")
        self.assertNotEqual(outcome["state"], "completed")
        self.assertFalse((self.paths.lifeweave_home / "vision-cache").exists() and any(
            (self.paths.lifeweave_home / "vision-cache").iterdir()))
        self.assertFalse((self.paths.lifeweave_home / "derived").exists() and any(
            (self.paths.lifeweave_home / "derived").rglob("r1.json")))

    async def test_vision_choice_requires_a_rerun_and_forget_clears_the_cache(self):
        service = self.service(FakeClient())
        first = await service.import_document(self.document, "a.pdf", "application/pdf", {}, "key")
        from lifecontext.catalog import CatalogError

        with self.assertRaises(CatalogError):
            await service.retry(first["operation_id"], vision=True)
        sid = first["document"]["source_id"]
        self.assertTrue((self.paths.lifeweave_home / "vision-cache" / sid).is_dir())
        service.forget_vision_cache(sid)
        self.assertFalse((self.paths.lifeweave_home / "vision-cache" / sid).exists())

    async def test_without_a_vision_block_nothing_is_sent(self):
        service = self.service(None)
        first = await service.import_document(self.document, "a.pdf", "application/pdf", {}, "key")
        self.assertEqual(first["state"], "completed")
        self.assertEqual(
            first["document"]["coverage"]["unresolved"], ["page:2", "page:3"]
        )
        self.assertFalse((self.paths.lifeweave_home / "vision-cache").exists())


class _Endpoint(BaseHTTPRequestHandler):
    status: ClassVar[int] = 200
    body: ClassVar[bytes] = b"{}"
    requests: ClassVar[list] = []

    def log_message(self, *_):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        headers = {name.lower(): value for name, value in self.headers.items()}
        type(self).requests.append((self.path, headers, json.loads(self.rfile.read(length) or b"{}")))
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(type(self).body)))
        self.end_headers()
        self.wfile.write(type(self).body)


class AdapterTests(unittest.TestCase):
    """Each SDK adapter against a loopback endpoint: request shape and failure mapping."""

    def setUp(self):
        _Endpoint.requests = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Endpoint)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.root = f"http://127.0.0.1:{self.server.server_address[1]}"

    def respond(self, status, body):
        _Endpoint.status, _Endpoint.body = status, json.dumps(body).encode()

    def read(self, provider, base_url):
        client = VisionClient(config(provider=provider, base_url=base_url, api_key="secret-key", model="model-x"))
        return asyncio.run(client.read(b"\xff\xd8\xffjpeg", "PROMPT", timeout=10))

    def test_openai_compatible_sends_an_image_url_part(self):
        self.respond(200, {"id": "c", "object": "chat.completion", "created": 0, "model": "model-x",
                           "choices": [{"index": 0, "finish_reason": "stop",
                                        "message": {"role": "assistant", "content": READING}}]})
        reply = self.read("openai_compatible", self.root + "/v1")
        self.assertEqual(reply, VisionReply(READING))
        path, headers, body = _Endpoint.requests[0]
        self.assertEqual(path, "/v1/chat/completions")
        self.assertEqual(headers["authorization"], "Bearer secret-key")
        parts = body["messages"][1]["content"]
        self.assertEqual(parts[0], {"type": "text", "text": "PROMPT"})
        self.assertTrue(parts[1]["image_url"]["url"].startswith("data:image/jpeg;base64,"))
        self.assertEqual((body["model"], body["max_tokens"]), ("model-x", 8192))

    def test_anthropic_sends_a_base64_image_block_and_reports_refusals(self):
        self.respond(200, {"id": "m", "type": "message", "role": "assistant", "model": "model-x",
                           "content": [], "stop_reason": "refusal", "stop_sequence": None,
                           "usage": {"input_tokens": 1, "output_tokens": 1}})
        reply = self.read("anthropic", self.root)
        self.assertTrue(reply.refused)
        path, headers, body = _Endpoint.requests[0]
        self.assertEqual(path, "/v1/messages")
        self.assertEqual(headers["x-api-key"], "secret-key")
        image = body["messages"][0]["content"][0]
        self.assertEqual((image["type"], image["source"]["media_type"]), ("image", "image/jpeg"))

    def test_gemini_sends_inline_data(self):
        self.respond(200, {"candidates": [{"content": {"role": "model", "parts": [{"text": READING}]},
                                           "finishReason": "STOP"}]})
        reply = self.read("gemini", self.root + "/")
        self.assertEqual(reply.text, READING)
        path, _, body = _Endpoint.requests[0]
        self.assertTrue(path.endswith("models/model-x:generateContent"), path)
        self.assertEqual(body["contents"][0]["parts"][0]["inlineData"]["mimeType"], "image/jpeg")

    def test_http_failures_map_to_retry_block_or_item(self):
        for status, expected in ((503, VisionUnavailable), (429, VisionUnavailable),
                                 (401, VisionBlocked), (404, VisionBlocked), (400, _ItemRejected)):
            for provider, base in (("openai_compatible", self.root + "/v1"), ("anthropic", self.root),
                                   ("gemini", self.root + "/")):
                with self.subTest(status=status, provider=provider):
                    self.respond(status, {"error": {"message": "secret detail", "code": status}})
                    with self.assertRaises(expected) as caught:
                        self.read(provider, base)
                    self.assertNotIn("secret", str(caught.exception))

    def test_unreachable_endpoint_is_retryable(self):
        self.server.shutdown()
        self.server.server_close()
        with self.assertRaises(VisionUnavailable):
            self.read("openai_compatible", self.root + "/v1")


if __name__ == "__main__":
    unittest.main()
