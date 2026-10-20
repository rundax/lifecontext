"""Fake reflection provenance tests; semantic entailment is deliberately not claimed."""
import asyncio
import hashlib
import json
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import unquote

from local_paths import scratch_root

from lifecontext.application import ApplicationError, JournalApplication
from lifecontext.catalog import LifeContextCatalog
from lifecontext.custody import SourceManifest
from lifecontext.derived import DerivedStore
from lifecontext.paths import LifeContextPaths
from lifecontext.wiki import (
    MAX_PAGE_CLAIMS,
    MAX_PATCH_BYTES,
    MAX_SOURCE_CLAIMS,
    Citation,
    Claim,
    WikiError,
    WikiPatch,
    WikiRevisionStore,
    WikiService,
    _patch_json,
    parse_patches,
    render_managed,
    shared_page_id,
    source_page_id,
)

PAGE = source_page_id("doc_1")


class WikiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=scratch_root())
        self.paths = LifeContextPaths(Path(self.temp.name))
        self.catalog = LifeContextCatalog(self.paths)
        self.digest = hashlib.sha256(b"original").hexdigest()
        manifest = SourceManifest("doc_1", 1, self.digest, 8, {"source": "test"})
        self.catalog.register_manifest(manifest, layer="document")
        self.catalog.register_document_metadata("doc_1", 1, filename="original.txt", title="Original", mime_type="text/plain")
        self.derived = DerivedStore(self.paths)
        self.payload = {"source_id": "doc_1", "source_version": 1, "extraction_revision": 1, "original_checksum": self.digest, "status": "completed", "segments": [{"text": "The sky is blue.", "start": 0, "end": 16, "locator": {"kind": "lines", "start": 1, "end": 1, "container": None, "schema_version": 1}}]}
        stored = self.derived.write("doc_1", 1, 1, self.payload)
        self.catalog.record_extraction("doc_1", 1, 1, original_sha256=self.digest, extractor="text", extractor_version="1", config_sha256="c"*64, artifact_path=str(stored.path.relative_to(self.paths.lifeweave_home)), artifact_sha256=stored.sha256, status="completed", passage_count=1)
        self.catalog.record_provider_chunk("chunk_1", "doc_1", 1, 1, passage_start=0, passage_end=1, content_sha256="b"*64)
        self.catalog.update_provider_chunk("chunk_1", expected_revision=1, submission_state="submitted", completion_state="completed")
        for state in ("running", "completed"):
            stage = self.catalog.get_document_stage("doc_1", 1, "indexing")
            self.catalog.transition_document_stage("doc_1", 1, "indexing", expected_state=stage["state"], expected_revision=stage["revision"], state=state)
        self.custody = Mock()
        self.manifests = {("doc_1", 1): manifest}
        self.custody.resolve_exact.side_effect = lambda sid, version: SimpleNamespace(manifest=self.manifests[(sid, version)])
        self.citation = {"source_id": "doc_1", "source_version": 1, "extraction_revision": 1, "passage_start": 0, "passage_end": 1, "original_sha256": self.digest, "quote": "The sky is blue."}
        self.value = {"page_id": PAGE, "title": "Original", "expected_revision": 0, "claims": [{"text": "The sky is blue.", "citations": [self.citation]}], "contradictions": [], "links": []}
        # Evidence-window requests go to self.queries and answer with self.value; the
        # routing request answers with self.routes; the summary request with
        # self.summary, or an invalid response while it is None.
        self.queries, self.summary_queries, self.routing_queries = [], [], []
        self.summary, self.routes = None, []
        async def reflect(query, **kwargs):
            if self.is_routing(query):
                self.routing_queries.append(query)
                return {"text": json.dumps({"routes": self.routes})}
            if not self.is_window(query):
                self.summary_queries.append(query)
                return {"text": json.dumps({"summary": self.summary}) if self.summary else "{}"}
            self.queries.append(query)
            return {"text": json.dumps(self.value)}
        self.service = WikiService(self.catalog, self.custody, self.derived, SimpleNamespace(reflect=reflect), self.paths)

    @staticmethod
    def data(query):
        return json.loads(query.split("UNTRUSTED_DATA=", 1)[1])

    @classmethod
    def is_window(cls, query):
        return "evidence" in cls.data(query)

    @classmethod
    def is_routing(cls, query):
        return "pages" in cls.data(query)

    def tearDown(self):
        self.catalog.close()
        self.temp.cleanup()

    def update(self):
        return asyncio.run(self.service.update_for_document("doc_1", 1))

    def application(self):
        app = JournalApplication.__new__(JournalApplication)
        app.catalog = self.catalog
        app.wiki = self.service
        app.documents = Mock()
        def show(sid, version=None):
            manifest = self.catalog.get_current_manifest(sid)
            if manifest is None:
                raise ApplicationError("source_not_found")
            return {"source_id": sid, "source_version": version or manifest.source_version}
        app.documents.show.side_effect = show
        app.document_projections = Mock()
        app.document_projections.project_wiki.return_value = {"state": "completed"}
        app.document_projections.project_wiki_pages.side_effect = lambda pages: [
            app.document_projections.project_wiki(page_id) for page_id in pages]
        return app

    def test_application_rebuild_publishes_projects_and_replays(self):
        app = self.application()
        result = asyncio.run(app.rebuild_wiki(source_id="doc_1", idempotency_key="rebuild-one"))
        self.assertEqual(result["state"], "completed")
        # The document's page and the model-free index.
        self.assertEqual(result["summary"]["item_count"], 2)
        self.assertEqual({call.args[0] for call in app.document_projections.project_wiki.call_args_list}, {PAGE, "index"})
        replay = asyncio.run(app.rebuild_wiki(source_id="doc_1", idempotency_key="rebuild-one"))
        self.assertEqual(replay, result)
        self.assertEqual(len(self.queries), 1)
        with self.assertRaises(ApplicationError) as error:
            asyncio.run(app.rebuild_wiki(source_id="doc_1", version=1, idempotency_key="rebuild-one"))
        self.assertEqual(error.exception.code, "conflict")

    def test_application_rebuild_failure_is_durable_and_not_republished(self):
        app = self.application()
        self.value["claims"][0]["text"] = "unsupported invented claim"
        result = asyncio.run(app.rebuild_wiki(source_id="doc_1", version=1, idempotency_key="bad-rebuild"))
        self.assertEqual(result["state"], "retryable_failed")
        self.assertEqual(result["summary"]["error_code"], "provider_unavailable")
        replay = asyncio.run(app.rebuild_wiki(source_id="doc_1", version=1, idempotency_key="bad-rebuild"))
        self.assertEqual(replay, result)
        self.assertEqual(len(self.queries), 1)
        app.document_projections.project_wiki.assert_not_called()
        self.assertEqual(self.service.history(PAGE), [])

    def test_application_bulk_rebuild_skips_old_and_unindexed_versions(self):
        app = self.application()
        citation = self.add_evidence("doc_1", "New evidence", version=2)
        self.value["claims"] = [{"text": "New evidence", "citations": [citation]}]
        raw = b"Unindexed"
        self.catalog.register_manifest(SourceManifest("doc_pending", 1, hashlib.sha256(raw).hexdigest(), len(raw), {}), layer="document")
        self.catalog.register_document_metadata("doc_pending", 1, filename="pending.txt", title="Pending", mime_type="text/plain")
        result = asyncio.run(app.rebuild_wiki(idempotency_key="empty-rebuild"))
        self.assertEqual([row["source_version"] for row in result["summary"]["sources"]], [2])
        self.assertEqual(len(self.queries), 1)
        with self.assertRaises(ApplicationError):
            asyncio.run(app.rebuild_wiki(source_id="doc_1", version=1, idempotency_key="old-rebuild"))

    def test_application_projection_conflict_preserves_published_revision(self):
        app = self.application()
        app.document_projections.project_wiki.return_value = {"state": "conflict"}
        result = asyncio.run(app.rebuild_wiki(source_id="doc_1", version=1, idempotency_key="projection-conflict"))
        self.assertEqual(result["state"], "permanent_failed")
        self.assertEqual(result["summary"]["error_code"], "conflict")
        self.assertEqual(len(self.service.history(PAGE)), 1)
        operation = self.catalog.begin_operation("projection-conflict", app._request_digest("POST /v1/wiki/rebuild", {"source_id": "doc_1", "version": 1}), "wiki_rebuild")
        self.assertEqual(app.operation_status(operation["operation_id"])["summary"]["item_count"], 2)

    def test_publish_history_and_separate_inventory(self):
        self.assertEqual(self.update()[0]["revision"], 1)
        self.value["expected_revision"] = 1
        self.update()
        self.assertEqual(len(self.service.history(PAGE)), 2)
        self.assertIn("UNTRUSTED_DATA", self.queries[0])
        self.assertEqual(self.derived.list_inventory(), [("doc_1", 1, 1)])
        managed = self.service.show(PAGE)["managed"]
        self.assertIn("managed:start", managed)
        self.assertNotIn("user:start", managed)
        self.assertEqual(self.catalog.get_document_stage("doc_1", 1, "wiki")["state"], "completed")

    def add_evidence(self, sid, text, version=1, revision=1, provenance=None):
        raw = text.encode()
        manifest = self.manifests.get((sid, version)) or SourceManifest(sid, version, hashlib.sha256(raw).hexdigest(), len(raw),
                                                                        provenance or {"source": "test"})
        self.manifests[(sid, version)] = manifest
        self.catalog.register_manifest(manifest, layer="document")
        self.catalog.register_document_metadata(sid, version, filename="original.txt", title="Original", mime_type="text/plain")
        payload = {**self.payload, "source_id": sid, "source_version": version, "extraction_revision": revision,
                   "original_checksum": manifest.sha256, "segments": [{**self.payload["segments"][0], "text": text, "end": len(text)}]}
        stored = self.derived.write(sid, version, revision, payload)
        self.catalog.record_extraction(sid, version, revision, original_sha256=manifest.sha256, extractor="text",
                                      extractor_version="1", config_sha256="c" * 64,
                                      artifact_path=str(stored.path.relative_to(self.paths.lifeweave_home)),
                                      artifact_sha256=stored.sha256, status="completed", passage_count=1)
        pid = f"chunk_{sid}_{version}_{revision}"
        self.catalog.record_provider_chunk(pid, sid, version, revision, passage_start=0, passage_end=1, content_sha256="b" * 64)
        self.catalog.update_provider_chunk(pid, expected_revision=1, submission_state="submitted", completion_state="completed")
        stage = self.catalog.get_document_stage(sid, version, "indexing")
        if stage["state"] != "completed":
            for state in ("running", "completed"):
                stage = self.catalog.get_document_stage(sid, version, "indexing")
                self.catalog.transition_document_stage(sid, version, "indexing", expected_state=stage["state"], expected_revision=stage["revision"], state=state)
        return {**self.citation, "source_id": sid, "source_version": version, "extraction_revision": revision,
                "original_sha256": manifest.sha256, "quote": text}

    def index(self):
        page = self.catalog.get_wiki_page("index")
        row = self.catalog.get_wiki_revision("index", page["current_revision"])
        return self.service.store.read("index", page["current_revision"], row["artifact_sha256"]), row

    def test_each_document_has_its_own_page_and_the_index_lists_them(self):
        self.value["links"] = ["topic_sky"]
        self.update()
        citation = self.add_evidence("doc_2", "Grass is green.")
        second = source_page_id("doc_2")
        self.value = {**self.value, "page_id": second, "claims": [{"text": "Grass is green.", "citations": [citation]}], "links": ["topic_grass"]}
        asyncio.run(self.service.update_for_document("doc_2", 1))
        row = self.catalog.get_wiki_revision(second, 1)
        patch = self.service.store.read(second, 1, row["artifact_sha256"])
        self.assertEqual([claim.text for claim in patch.claims], ["Grass is green."])
        self.assertEqual({dependency["source_id"] for dependency in row["dependencies"]}, {"doc_2"})
        first = self.service.store.read(PAGE, 1, self.catalog.get_wiki_revision(PAGE, 1)["artifact_sha256"])
        self.assertEqual([claim.text for claim in first.claims], ["The sky is blue."])
        index, row = self.index()
        self.assertEqual(sorted(claim.text for claim in index.claims), ["Grass is green.", "The sky is blue."])
        self.assertEqual(set(index.links), {PAGE, second})
        self.assertEqual({dependency["source_id"] for dependency in row["dependencies"]}, {"doc_1", "doc_2"})
        # Recompiling a document replaces its claims; nothing accumulates.
        self.value["expected_revision"] = 1
        asyncio.run(self.service.update_for_document("doc_2", 1))
        row = self.catalog.get_wiki_revision(second, 2)
        self.assertEqual(len(self.service.store.read(second, 2, row["artifact_sha256"]).claims), 1)
        self.assertEqual(len(self.queries), 3)  # the index never calls the model

    def assert_superseded_claim_removed(self, *, version=1, revision=1):
        self.update()
        citation = self.add_evidence("doc_1", "The sky is cloudy.", version=version, revision=revision)
        self.value = {**self.value, "expected_revision": 1,
                      "claims": [{"text": "The sky is cloudy.", "citations": [citation]}]}
        asyncio.run(self.service.update_for_document("doc_1", version))
        row = self.catalog.get_wiki_revision(PAGE, 2)
        patch = self.service.store.read(PAGE, 2, row["artifact_sha256"])
        self.assertEqual([claim.text for claim in patch.claims], ["The sky is cloudy."])
        self.assertEqual({(dependency["source_version"], dependency["extraction_revision"]) for dependency in row["dependencies"]}, {(version, revision)})
        self.assertIn("The sky is blue.", self.service.show(PAGE, 1)["managed"])

    def test_additive_update_excludes_superseded_original(self):
        self.assert_superseded_claim_removed(version=2)

    def test_additive_update_excludes_superseded_extraction(self):
        self.assert_superseded_claim_removed(revision=2)

    def test_index_excludes_tombstoned_documents(self):
        self.update()
        citation = self.add_evidence("doc_2", "Grass is green.")
        self.value = {**self.value, "page_id": source_page_id("doc_2"), "claims": [{"text": "Grass is green.", "citations": [citation]}]}
        original = self.catalog.list_tombstones
        for tombstone in ("forgotten", "deleted"):
            with self.subTest(tombstone=tombstone), unittest.mock.patch.object(
                self.catalog, "list_tombstones", side_effect=lambda sid, tombstone=tombstone: [{"tombstone_type": tombstone}] if sid == "doc_1" else original(sid)
            ):
                asyncio.run(self.service.update_for_document("doc_2", 1))
                index, row = self.index()
                self.assertEqual([claim.text for claim in index.claims], ["Grass is green."])
                self.assertEqual({dependency["source_id"] for dependency in row["dependencies"]}, {"doc_2"})
                self.value["expected_revision"] += 1

    def test_invalid_output_preserves_revision_and_retry(self):
        self.update()
        self.value["expected_revision"] = 1
        self.citation["quote"] = "absent"
        with self.assertRaises(WikiError):
            self.update()
        self.assertEqual(self.catalog.get_wiki_page(PAGE)["current_revision"], 1)
        self.assertEqual(self.catalog.get_document_stage("doc_1", 1, "wiki")["state"], "failed")
        self.citation["quote"] = "The sky is blue."
        self.update()
        self.assertEqual(self.catalog.get_wiki_page(PAGE)["current_revision"], 2)

    def test_invalid_refs(self):
        for key, value in (("original_sha256", "f"*64), ("extraction_revision", 2), ("passage_end", 2), ("source_id", "missing")):
            original = self.citation[key]
            self.citation[key] = value
            with self.assertRaises(WikiError):
                self.update()
            self.assertIsNone(self.catalog.get_wiki_page(PAGE))
            self.citation[key] = original

    def test_ancestry_rejects_stale_reflection(self):
        self.update()
        with self.assertRaises(WikiError):
            self.update()
        self.assertEqual(len(self.service.history(PAGE)), 1)

    def test_contradictions_keep_both_sides_dates(self):
        claim = self.value["claims"][0]
        self.value["contradictions"] = [{"left": claim, "right": {"text": "sky is blue", "citations": [self.citation]}, "left_date": "2026-01-01", "right_date": "2026-02-01"}]
        self.update()
        review = self.catalog.list_wiki_reviews(PAGE)[0]
        self.assertEqual(review["details"]["sources"]["right_date"], "2026-02-01")
        self.assertIn("sky is blue", self.service.show(PAGE)["managed"])
        self.assertIn("2026-02-01", self.service.show(PAGE)["managed"])

    def test_pending_contradiction_survives_empty_subsequent_output(self):
        claim = self.value["claims"][0]
        contradiction = {"left": claim, "right": {"text": "sky is blue", "citations": [self.citation]},
                         "left_date": "2026-01-01", "right_date": "2026-02-01"}
        self.value["contradictions"] = [contradiction]
        self.update()
        self.value["expected_revision"] = 1
        self.value["contradictions"] = []
        self.update()
        current = self.catalog.get_wiki_revision(PAGE, 2)
        patch = self.service.store.read(PAGE, 2, current["artifact_sha256"])
        self.assertEqual(len(patch.contradictions), 1)
        self.assertIn("2026-02-01", self.service.show(PAGE)["managed"])
        reviews = self.catalog.list_wiki_reviews(PAGE)
        self.assertEqual(len(reviews), 1)
        self.catalog.resolve_wiki_review(reviews[0]["review_id"], note="Owner checked both sources.")
        self.value["expected_revision"] = 2
        self.update()
        self.assertNotIn("Contradiction", self.service.show(PAGE)["managed"])

    def test_own_page_failure_rolls_back_shared_pages_and_reviews(self):
        claim = self.value["claims"][0]
        self.value["contradictions"] = [{"left": claim, "right": claim,
                                         "left_date": "2026-01-01", "right_date": "2026-02-01"}]
        self.routes = [{"page": {"kind": "topic", "title": "Sky"}, "claims": [1], "contradicts": []}]
        commit = self.catalog.commit_wiki_revision
        calls = []

        def conflict(page_id, **kwargs):
            calls.append(page_id)
            if len(calls) == 2:
                raise ValueError("second page conflict")
            return commit(page_id, **kwargs)

        with unittest.mock.patch.object(self.catalog, "commit_wiki_revision", side_effect=conflict), self.assertRaises(WikiError):
            self.update()
        self.assertEqual(calls, ["topic_sky", PAGE])
        self.assertEqual(self.service.list(), [])
        self.assertEqual(self.catalog.list_wiki_reviews(PAGE), [])
        self.assertEqual(self.catalog.get_document_stage("doc_1", 1, "wiki")["state"], "failed")
        self.update()
        self.assertEqual({page["page_id"] for page in self.service.list()}, {PAGE, "topic_sky", "index"})

    def test_new_topic_and_entity_pages_come_from_routing(self):
        # A window response that proposes pages is rejected: pages are routed, not compiled.
        self.value = {"patches": [self.value, {**self.value, "page_id": "topic_sky", "title": "Sky"}]}
        with self.assertRaises(WikiError):
            self.update()
        self.value = self.value["patches"][0]
        self.routes = [{"page": {"kind": "topic", "title": "Sky"}, "claims": [1], "contradicts": []},
                       {"page": {"kind": "entity", "title": "Earth"}, "claims": [1], "contradicts": []}]
        rows = self.update()
        self.assertEqual({row["page_id"] for row in rows}, {PAGE, "topic_sky", "entity_earth", "index"})
        self.assertTrue(all(row["revision"] == 1 for row in rows))
        self.assertIn("The sky is blue.", self.service.show("topic_sky")["managed"])
        self.assertEqual(self.current().links, ("topic_sky", "entity_earth"))
        self.assertEqual(self.current("topic_sky").links, (PAGE,))
        self.assertEqual(self.current("topic_sky").claims, self.current().claims)

    def test_large_evidence_uses_bounded_windows_without_omitting_passages(self):
        segments = []
        offset = 0
        for index in range(4):
            text = f"Passage {index}. " + "x" * 25_000
            segments.append({"text": text, "start": offset, "end": offset + len(text),
                             "locator": {"kind": "lines", "start": index + 1, "end": index + 1,
                                         "container": None, "schema_version": 1}})
            offset += len(text)
        payload = {**self.payload, "extraction_revision": 2, "segments": segments}
        stored = self.derived.write("doc_1", 1, 2, payload)
        self.catalog.record_extraction("doc_1", 1, 2, original_sha256=self.digest, extractor="text",
                                      extractor_version="1", config_sha256="c" * 64,
                                      artifact_path=str(stored.path.relative_to(self.paths.lifeweave_home)),
                                      artifact_sha256=stored.sha256, status="completed", passage_count=4)
        self.catalog.record_provider_chunk("chunk_2", "doc_1", 1, 2, passage_start=0, passage_end=4, content_sha256="b" * 64)
        self.catalog.update_provider_chunk("chunk_2", expected_revision=1, submission_state="submitted", completion_state="completed")
        seen = {index: "" for index in range(4)}
        queries = []

        async def reflect(query, **kwargs):
            queries.append(query)
            data = json.loads(query.split("UNTRUSTED_DATA=", 1)[1])
            window = data["evidence"]["segments"]
            for fragment in window:
                seen[fragment["passage_index"]] += fragment["text"]
            fragment = window[0]
            quote = fragment["text"][:16]
            citation = {**self.citation, "extraction_revision": 2,
                        "passage_start": fragment["passage_index"], "passage_end": fragment["passage_index"] + 1,
                        "quote": quote}
            return {"text": json.dumps({**self.value, "claims": [{"text": quote, "citations": [citation]}]})}

        self.service.hindsight = SimpleNamespace(reflect=reflect)
        self.update()
        self.assertGreater(len(queries), 1)
        self.assertTrue(all(len(query.encode()) <= 60_000 for query in queries))
        self.assertEqual(seen, {index: segment["text"] for index, segment in enumerate(segments)})
        self.assertEqual(self.catalog.get_document_stage("doc_1", 1, "wiki")["state"], "completed")

    def test_semantically_unsupported_fixture_demonstrates_gate(self):
        self.value["claims"][0]["text"] = "The moon is made of cheese."
        with self.assertRaises(WikiError):
            self.update()
        self.assertFalse(self.service.status()["semantic_support_verified"])
        self.assertIsNone(self.service.show(PAGE))

    def test_strict_json_and_manual_boundary_policy(self):
        raw = json.dumps(self.value)
        for invalid in (raw.replace(f'"page_id": "{PAGE}"', f'"page_id": "{PAGE}", "page_id": "{PAGE}"'), raw[:-1] + ', "user_section": "notes"}', raw.replace("The sky is blue.", "<!-- life-context:user:start -->"), raw.replace('"expected_revision": 0', '"expected_revision": true')):
            with self.assertRaises(WikiError):
                WikiPatch.parse(invalid)
        self.assertNotIn("user:start", render_managed(WikiPatch.parse(raw)))

    def test_compiler_repairs_one_invalid_response_without_weakening_quotes(self):
        responses = iter([json.dumps({**self.value, "claims": [{"text": "invented", "citations": [self.citation]}]}), json.dumps(self.value)])
        calls = []
        async def compile_document(query, **kwargs):
            if not self.is_window(query):
                return {"text": "{}"}
            calls.append(query)
            return {"text": next(responses)}
        self.service.compiler = compile_document
        self.update()
        self.assertEqual(len(calls), 2)
        self.assertIn("Correct the rejected JSON response", calls[1])
        self.assertIn("The sky is blue.", self.service.show(PAGE)["managed"])
        self.assertNotIn("invented", self.service.show(PAGE)["managed"])

    def test_compiler_stops_after_two_rejected_responses(self):
        calls = []
        async def compile_document(query, **kwargs):
            calls.append(query)
            return {"text": "{}"}
        self.service.compiler = compile_document
        with self.assertRaises(WikiError):
            self.update()
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.service.list(), [])

    def test_compiler_does_not_retry_after_source_is_forgotten(self):
        calls = []
        forgotten = False
        async def compile_document(query, **kwargs):
            nonlocal forgotten
            calls.append(query)
            forgotten = True
            return {"text": "{}"}
        self.service.compiler = compile_document
        with unittest.mock.patch.object(self.catalog, "list_tombstones", side_effect=lambda sid: [{"tombstone_type": "forgotten"}] if forgotten else []), self.assertRaises(WikiError):
            self.update()
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.service.list(), [])

    def test_single_json_fence_preserves_strict_patch_validation(self):
        raw = json.dumps(self.value)
        self.assertEqual(parse_patches("```json\n" + raw + "\n```"), [WikiPatch.parse(raw)])
        for invalid in ("prose\n```json\n" + raw + "\n```", "```python\n" + raw + "\n```",
                        "```json\n" + raw + "\n```\n```json\n" + raw + "\n```",
                        "```json\n" + raw.replace(f'"page_id": "{PAGE}"', f'"page_id": "{PAGE}", "page_id": "{PAGE}"') + "\n```"):
            with self.assertRaises(WikiError):
                parse_patches(invalid)

    def test_book_windows_merge_more_than_32_validated_claims(self):
        segments, offset = [], 0
        for index in range(40):
            text = f"Claim {index}. " + "x" * 22_000
            segments.append({"text": text, "start": offset, "end": offset + len(text),
                             "locator": {"kind": "lines", "start": index + 1, "end": index + 1,
                                         "container": None, "schema_version": 1}})
            offset += len(text)
        payload = {**self.payload, "extraction_revision": 2, "segments": segments}
        stored = self.derived.write("doc_1", 1, 2, payload)
        self.catalog.record_extraction("doc_1", 1, 2, original_sha256=self.digest, extractor="text",
                                      extractor_version="1", config_sha256="c" * 64,
                                      artifact_path=str(stored.path.relative_to(self.paths.lifeweave_home)),
                                      artifact_sha256=stored.sha256, status="completed", passage_count=40)
        self.catalog.record_provider_chunk("chunk_book", "doc_1", 1, 2, passage_start=0, passage_end=40, content_sha256="b" * 64)
        self.catalog.update_provider_chunk("chunk_book", expected_revision=1, submission_state="submitted", completion_state="completed")
        keys = []
        async def reflect(query, **kwargs):
            data = json.loads(query.split("UNTRUSTED_DATA=", 1)[1])
            keys.append(set(data))
            segment = data["evidence"]["segments"][0]
            quote = segment["text"][:20]
            citation = {**self.citation, "extraction_revision": 2, "passage_start": segment["passage_index"],
                        "passage_end": segment["passage_index"] + 1, "quote": quote}
            return {"text": "```json\n" + json.dumps({**self.value, "claims": [{"text": quote, "citations": [citation]}]}) + "\n```"}
        self.service.hindsight = SimpleNamespace(reflect=reflect)
        self.update()
        revision = self.catalog.get_wiki_revision(PAGE, 1)
        patch = self.service.store.read(PAGE, 1, revision["artifact_sha256"])
        self.assertEqual(len(patch.claims), 40)
        # Every window asks for the own page only; the summary and routing requests carry no evidence.
        self.assertEqual(keys, [{"target", "example_patch", "evidence"}] * 40 + [{"document", "claims"}, {"document", "claims", "pages", "existing_claims"}])
        self.assertEqual({c.citations[0].passage_start for c in patch.claims}, set(range(40)))

    def book(self, passages, size=22_000):
        segments, offset = [], 0
        for index in range(passages):
            text = f"Claim {index}. " + "x" * size
            segments.append({"text": text, "start": offset, "end": offset + len(text),
                             "locator": {"kind": "lines", "start": index + 1, "end": index + 1,
                                         "container": None, "schema_version": 1}})
            offset += len(text)
        payload = {**self.payload, "extraction_revision": 2, "segments": segments}
        stored = self.derived.write("doc_1", 1, 2, payload)
        self.catalog.record_extraction("doc_1", 1, 2, original_sha256=self.digest, extractor="text",
                                      extractor_version="1", config_sha256="c" * 64,
                                      artifact_path=str(stored.path.relative_to(self.paths.lifeweave_home)),
                                      artifact_sha256=stored.sha256, status="completed", passage_count=passages)
        self.catalog.record_provider_chunk("chunk_book", "doc_1", 1, 2, passage_start=0, passage_end=passages, content_sha256="b" * 64)
        self.catalog.update_provider_chunk("chunk_book", expected_revision=1, submission_state="submitted", completion_state="completed")

    def test_long_books_sample_windows_evenly_within_the_page_budget(self):
        self.book(120)
        seen = []

        async def reflect(query, **kwargs):
            data = json.loads(query.split("UNTRUSTED_DATA=", 1)[1])
            self.assertNotIn("previous_managed_patch", data)
            segment = data["evidence"]["segments"][0]
            seen.append(segment["passage_index"])
            quote = segment["text"][:20]
            citation = {**self.citation, "extraction_revision": 2, "passage_start": segment["passage_index"],
                        "passage_end": segment["passage_index"] + 1, "quote": quote}
            return {"text": json.dumps({**self.value, "claims": [{"text": quote, "citations": [citation]}]})}

        self.service.hindsight = SimpleNamespace(reflect=reflect)
        self.update()
        self.assertEqual(len(seen), MAX_SOURCE_CLAIMS)
        self.assertEqual((seen[0], seen[-1]), (0, 119))
        patch = self.service.store.read(PAGE, 1, self.catalog.get_wiki_revision(PAGE, 1)["artifact_sha256"])
        self.assertEqual(len(patch.claims), MAX_SOURCE_CLAIMS)
        self.assertEqual(self.catalog.get_document_stage("doc_1", 1, "wiki")["state"], "completed")

    def test_a_full_page_stops_asking_instead_of_failing(self):
        self.book(40)
        calls = []

        async def reflect(query, **kwargs):
            data = json.loads(query.split("UNTRUSTED_DATA=", 1)[1])
            segment = data["evidence"]["segments"][0]
            calls.append(segment["passage_index"])
            quote = segment["text"][:1900]
            citation = {**self.citation, "extraction_revision": 2, "passage_start": segment["passage_index"],
                        "passage_end": segment["passage_index"] + 1, "quote": quote}
            return {"text": json.dumps({**self.value, "claims": [{"text": quote, "citations": [citation]}]})}

        self.service.hindsight = SimpleNamespace(reflect=reflect)
        self.update()
        self.assertLess(len(calls), 40)
        row = self.catalog.get_wiki_revision(PAGE, 1)
        patch = self.service.store.read(PAGE, 1, row["artifact_sha256"])
        self.assertLessEqual(len(json.dumps({"claims": [c.text for c in patch.claims]}).encode()), 48_000)
        self.assertEqual(self.catalog.get_document_stage("doc_1", 1, "wiki")["state"], "completed")

    def compile_doc(self, sid, text, *, quote=None, revision=0):
        citation = self.add_evidence(sid, text)
        if quote is not None:
            citation["quote"] = quote
        self.value = {"page_id": source_page_id(sid), "title": "Original", "expected_revision": revision,
                      "claims": [{"text": citation["quote"], "citations": [citation]}], "contradictions": [], "links": []}
        return asyncio.run(self.service.update_for_document(sid, 1))

    def test_index_skips_another_documents_superseded_version(self):
        self.compile_doc("doc_2", "Grass is green.")
        raw = b"Second version"
        self.catalog.register_manifest(SourceManifest("doc_2", 2, hashlib.sha256(raw).hexdigest(), len(raw), {}), layer="document")
        self.value = {**self.value, "page_id": PAGE, "expected_revision": 0,
                      "claims": [{"text": "The sky is blue.", "citations": [self.citation]}]}
        self.update()
        self.assertEqual(self.catalog.get_document_stage("doc_1", 1, "wiki")["state"], "completed")
        index, _ = self.index()
        self.assertEqual(index.links, (PAGE,))

    def test_index_stays_within_its_size_bound(self):
        for n in range(16):
            text = f"Document {n} " + "y" * 1900
            self.compile_doc(f"doc_big_{n}", text, quote=text[:1900])
        index, _ = self.index()
        self.assertLess(len(index.links), 16)
        self.assertTrue(all(self.catalog.get_document_stage(f"doc_big_{n}", 1, "wiki")["state"] == "completed" for n in range(16)))

    def test_index_problems_never_fail_a_document(self):
        self.update()
        row = self.catalog.get_wiki_revision("index", 1)
        self.catalog.record_wiki_review("review_manual_index", "index", 1, code="manual_edit",
                                        details={"expected_hash": "a" * 64, "actual_hash": "b" * 64})
        self.compile_doc("doc_2", "Grass is green.")
        self.assertEqual(self.catalog.get_wiki_page("index")["current_revision"], 1)
        self.assertEqual(self.catalog.get_wiki_revision("index", 1), row)
        self.assertEqual(self.catalog.get_document_stage("doc_2", 1, "wiki")["state"], "completed")
        with unittest.mock.patch.object(self.service, "_publish_index", side_effect=WikiError("index broken")):
            self.compile_doc("doc_3", "Water is wet.")
        self.assertEqual(self.catalog.get_document_stage("doc_3", 1, "wiki")["state"], "completed")

    def test_a_legacy_model_written_index_becomes_the_table_of_contents(self):
        legacy = WikiPatch.parse(json.dumps({**self.value, "page_id": "index", "title": "Index", "links": ["topic_old"]}))
        stored = self.service.store.write(legacy)
        self.catalog.create_wiki_page("index", title="Index")
        self.catalog.commit_wiki_revision("index", expected_revision=0,
                                          artifact_path=str(stored.path.relative_to(self.paths.lifeweave_home)),
                                          artifact_sha256=stored.sha256, dependencies=self.service._validate(legacy))
        self.update()
        index, _ = self.index()
        self.assertEqual(self.catalog.get_wiki_page("index")["current_revision"], 2)
        self.assertEqual(index.links, (PAGE,))
        # Recompiling with nothing new leaves the index alone.
        self.value["expected_revision"] = 1
        self.update()
        self.assertEqual(self.catalog.get_wiki_page("index")["current_revision"], 2)

    def test_shared_pages_replace_this_documents_claims_and_keep_others(self):
        self.routes = [{"page": {"kind": "topic", "title": "Sky"}, "claims": [1], "contradicts": []}]
        own = self.value
        self.update()
        self.compile("doc_2", "Grass is green.", routes=[{"page": "topic_sky", "claims": [1], "contradicts": []}])
        cloudy = self.add_evidence("doc_1", "The sky is cloudy.", revision=2)
        self.value = {**own, "expected_revision": 1, "claims": [{"text": "The sky is cloudy.", "citations": [cloudy]}]}
        self.routes = [{"page": "topic_sky", "claims": [1], "contradicts": []}]
        self.update()
        patch = self.current("topic_sky")
        self.assertEqual(self.catalog.get_wiki_page("topic_sky")["current_revision"], 3)
        # doc_1's claim is replaced where it stood; doc_2's stays.
        self.assertEqual([claim.text for claim in patch.claims], ["The sky is cloudy.", "Grass is green."])
        self.assertEqual(patch.links, (PAGE, source_page_id("doc_2")))

    def test_six_routes_to_existing_and_new_pages(self):
        self.commit("topic_old", "Old", self.value["claims"], links=())
        self.routes = [{"page": "topic_old", "claims": [1], "contradicts": []}] + [
            {"page": {"kind": "topic", "title": f"New {n}"}, "claims": [1], "contradicts": []} for n in range(5)]
        rows = self.update()
        self.assertEqual({row["page_id"] for row in rows} - {"index"},
                         {PAGE, "topic_old", "topic_new_0", "topic_new_1", "topic_new_2", "topic_new_3", "topic_new_4"})
        # The old page already held exactly this claim; it gains only the link back.
        self.assertEqual(self.catalog.get_wiki_page("topic_old")["current_revision"], 2)
        self.assertEqual(self.current().links, ("topic_old", *(f"topic_new_{n}" for n in range(5))))

    def test_count_overflow_is_a_full_page_and_titles_are_ours(self):
        self.book(60)
        calls = []

        async def reflect(query, **kwargs):
            data = json.loads(query.split("UNTRUSTED_DATA=", 1)[1])
            fragment = data["evidence"]["segments"][0]
            calls.append(fragment["passage_index"])
            claims = []
            for start in range(3):
                quote = fragment["text"][start:start + 12]
                citation = {**self.citation, "extraction_revision": 2, "passage_start": fragment["passage_index"],
                            "passage_end": fragment["passage_index"] + 1, "quote": quote}
                claims.append({"text": quote, "citations": [citation]})
            return {"text": json.dumps({**self.value, "title": "A mistyped title", "claims": claims})}

        self.service.hindsight = SimpleNamespace(reflect=reflect)
        self.update()
        self.assertLess(len(calls), MAX_SOURCE_CLAIMS)
        page = self.catalog.get_wiki_page(PAGE)
        patch = self.service.store.read(PAGE, 1, self.catalog.get_wiki_revision(PAGE, 1)["artifact_sha256"])
        self.assertEqual(page["title"], "Original")
        self.assertEqual(len(patch.claims), MAX_SOURCE_CLAIMS)
        self.assertEqual(self.catalog.get_document_stage("doc_1", 1, "wiki")["state"], "completed")

    def test_links_and_contradictions_are_trimmed_not_a_reason_to_stop(self):
        self.book(40)
        calls = []

        async def reflect(query, **kwargs):
            data = json.loads(query.split("UNTRUSTED_DATA=", 1)[1])
            fragment = data["evidence"]["segments"][0]
            calls.append(fragment["passage_index"])
            quote = fragment["text"][:12]
            citation = {**self.citation, "extraction_revision": 2, "passage_start": fragment["passage_index"],
                        "passage_end": fragment["passage_index"] + 1, "quote": quote}
            claim = {"text": quote, "citations": [citation]}
            contradiction = {"left": claim, "right": claim, "left_date": "2026-01-01",
                             "right_date": f"2026-02-{fragment['passage_index'] % 28 + 1:02d}"}
            return {"text": json.dumps({**self.value, "claims": [claim], "contradictions": [contradiction],
                                        "links": [f"topic_{fragment['passage_index']}"]})}

        self.service.hindsight = SimpleNamespace(reflect=reflect)
        self.update()
        self.assertEqual(len(calls), 40)
        patch = self.service.store.read(PAGE, 1, self.catalog.get_wiki_revision(PAGE, 1)["artifact_sha256"])
        # Links proposed by the model are ignored; they come from routing.
        self.assertEqual((len(patch.links), len(patch.contradictions)), (0, 8))
        self.assertEqual(self.catalog.get_document_stage("doc_1", 1, "wiki")["state"], "completed")

    def test_a_damaged_document_is_left_out_of_the_index(self):
        self.update()
        self.compile_doc("doc_2", "Grass is green.")
        self.derived.artifact_path("doc_2", 1, 1).write_bytes(b"corrupt")
        self.compile_doc("doc_3", "Water is wet.")
        index, _ = self.index()
        self.assertEqual(set(index.links), {PAGE, source_page_id("doc_3")})

    def test_superseded_contradictions_are_not_republished(self):
        claim = self.value["claims"][0]
        self.value["contradictions"] = [{"left": claim, "right": claim, "left_date": "2026-01-01", "right_date": "2026-02-01"}]
        self.update()
        cloudy = self.add_evidence("doc_1", "The sky is cloudy.", revision=2)
        self.value = {**self.value, "expected_revision": 1, "contradictions": [],
                      "claims": [{"text": "The sky is cloudy.", "citations": [cloudy]}]}
        self.update()
        patch = self.service.store.read(PAGE, 2, self.catalog.get_wiki_revision(PAGE, 2)["artifact_sha256"])
        self.assertEqual(patch.contradictions, ())

    def test_sampling_and_page_identity_edges(self):
        from lifecontext.wiki import _sample

        self.assertEqual(_sample(list(range(5)), 10), list(range(5)))
        self.assertEqual(_sample(list(range(5)), 1), [0])
        self.assertEqual(_sample(list(range(11)), 10)[::9], [0, 10])
        self.assertEqual(len(_sample(list(range(1000)), 48)), 48)
        long_id = "d" * 128
        self.assertLessEqual(len(source_page_id(long_id)), 128)
        self.assertNotEqual(source_page_id(long_id), source_page_id("d" * 127))

    def test_bounded_patch_and_no_claim_without_citations(self):
        self.value["claims"][0]["citations"] = []
        with self.assertRaises(WikiError):
            WikiPatch.parse(json.dumps(self.value))
        self.value["claims"][0]["citations"] = [self.citation]
        self.value["claims"][0]["text"] = "x" * 2049
        with self.assertRaises(WikiError):
            WikiPatch.parse(json.dumps(self.value))
        with self.assertRaises(WikiError):
            WikiPatch.parse(" " * 48001)

    def test_rebuild_only_reflects_no_reindex(self):
        before = self.catalog.list_provider_chunks("doc_1", 1)
        asyncio.run(self.service.rebuild("doc_1", 1))
        self.assertEqual(self.catalog.list_provider_chunks("doc_1", 1), before)

    def test_checksum_and_symlink_storage(self):
        patch = WikiPatch.parse(json.dumps(self.value))
        store = WikiRevisionStore(self.paths)
        stored = store.write(patch)
        stored.path.write_bytes(b"corrupt")
        with self.assertRaises(WikiError):
            store.read(PAGE, 1, stored.sha256)
        stored.path.unlink()
        stored.path.symlink_to(self.paths.catalog_path)
        with self.assertRaises(ValueError):
            store.write(patch)

    def test_concurrent_ancestry_change_rejects_output(self):
        self.update()
        self.value["expected_revision"] = 1
        async def reflect(query, **kwargs):
            self.catalog.mark_dependent_wiki_stale("doc_1", 1)
            row = self.catalog.get_wiki_revision(PAGE, 1)
            self.catalog.commit_wiki_revision(PAGE, expected_revision=1, artifact_path=row["artifact_path"], artifact_sha256=row["artifact_sha256"], dependencies=row["dependencies"])
            return {"text": json.dumps(self.value)}
        self.service.hindsight = SimpleNamespace(reflect=reflect)
        with self.assertRaises(WikiError):
            self.update()
        self.assertEqual(self.catalog.get_wiki_page(PAGE)["current_revision"], 2)

    def test_unindexed_and_tombstoned_fail_before_reflection(self):
        original = self.catalog.get_document_stage
        def stage(source_id, version, name):
            if name == "indexing":
                return {"state": "pending"}
            return original(source_id, version, name)
        with unittest.mock.patch.object(self.catalog, "get_document_stage", side_effect=stage), self.assertRaises(WikiError):
            self.update()
        with unittest.mock.patch.object(self.catalog, "list_tombstones", return_value=[{"source_id": "doc_1"}]), self.assertRaises(WikiError):
            self.update()
        self.assertEqual(self.queries, [])

    def test_corrupt_extraction_not_published(self):
        self.derived.artifact_path("doc_1", 1, 1).write_bytes(b"corrupt")
        with self.assertRaises(WikiError):
            self.update()
        self.assertIsNone(self.catalog.get_wiki_page(PAGE))

    def current(self, page_id=PAGE):
        page = self.catalog.get_wiki_page(page_id)
        return self.service.store.read(page_id, page["current_revision"],
                                       self.catalog.get_wiki_revision(page_id, page["current_revision"])["artifact_sha256"])

    def commit(self, page_id, title, claims, *, links=(), contradictions=()):
        """Publish a revision directly, as another compile (or an older version) would have."""
        page = self.catalog.get_wiki_page(page_id)
        revision = page["current_revision"] if page else 0
        patch = WikiPatch.parse(json.dumps({"page_id": page_id, "title": title, "expected_revision": revision,
                                            "claims": list(claims), "contradictions": list(contradictions), "links": list(links)}))
        stored = self.service.store.write(patch)
        self.catalog.create_wiki_page(page_id, title=title)
        return self.catalog.commit_wiki_revision(page_id, expected_revision=revision,
                                                 artifact_path=str(stored.path.relative_to(self.paths.lifeweave_home)),
                                                 artifact_sha256=stored.sha256, dependencies=self.service._validate(patch))

    def lint_codes(self, sid="doc_1"):
        stages = {name: {"state": "accepted" if name == "custody" else "completed"}
                  for name in ("custody", "extraction", "indexing", "wiki", "projection")}
        return [item["code"] for item in self.service.lint([{"source_id": sid, "source_version": 1, "stages": stages}])["findings"]]

    def test_summary_is_labelled_above_numbered_claims_in_the_same_revision(self):
        self.summary = [{"text": "The page says the sky is blue.", "claims": [1]}]
        rows = self.update()
        self.assertEqual([row["revision"] for row in rows if row["page_id"] == PAGE], [1])
        patch = self.current()
        self.assertEqual([(s.text, s.claims) for s in patch.summary], [("The page says the sky is blue.", (1,))])
        self.assertEqual(json.loads(self.summary_queries[0].split("UNTRUSTED_DATA=", 1)[1])["claims"],
                         [{"n": 1, "text": "The sky is blue."}])
        managed = self.service.show(PAGE)["managed"]
        label = "> Model summary, generated from the numbered claims below; verify before relying on it."
        self.assertIn(label + "\n>\n> The page says the sky is blue. [1]\n\n1. The sky is blue. [doc_1 v1/e1 passages 0:1]", managed)
        self.assertEqual(self.service.status()["summary_model_generated"], True)
        self.assertFalse(self.service.status()["semantic_support_verified"])
        self.assertNotIn("summary_missing", self.lint_codes())

    def test_a_failed_summary_leaves_the_page_published(self):
        invalid = (
            [{"text": "Out of range.", "claims": [2]}],
            [{"text": "See https://example.test for more.", "claims": [1]}],
            [{"text": "Two\nlines.", "claims": [1]}],
            [{"text": "x" * 301, "claims": [1]}],
            [{"text": "Too many references.", "claims": [1, 1]}],
            [{"text": f"Sentence {n}.", "claims": [1]} for n in range(6)],
        )
        for revision, summary in enumerate(invalid):
            with self.subTest(summary=summary):
                self.summary = summary
                self.value["expected_revision"] = revision
                self.update()
                self.assertEqual(self.catalog.get_document_stage("doc_1", 1, "wiki")["state"], "completed")
                self.assertEqual(self.catalog.get_wiki_page(PAGE)["current_revision"], revision + 1)
                self.assertEqual(self.current().summary, ())
                self.assertIn("summary_missing", self.lint_codes())
        self.service.hindsight = SimpleNamespace(reflect=unittest.mock.AsyncMock(side_effect=[
            {"text": json.dumps(self.value | {"expected_revision": len(invalid)})}, RuntimeError("provider down")]))
        self.update()
        self.assertEqual(self.catalog.get_wiki_page(PAGE)["current_revision"], len(invalid) + 1)

    def test_compiler_repairs_one_rejected_summary(self):
        responses = iter([json.dumps({"summary": [{"text": "Bad <b>markup</b>.", "claims": [1]}]}),
                          json.dumps({"summary": [{"text": "The sky is blue, it says.", "claims": [1]}]})])
        summaries = []

        async def compile_document(query, **kwargs):
            if self.is_window(query):
                return {"text": json.dumps(self.value)}
            if self.is_routing(query):
                return {"text": json.dumps({"routes": []})}
            summaries.append(query)
            return {"text": next(responses)}

        self.service.compiler = compile_document
        self.update()
        self.assertEqual(len(summaries), 2)
        self.assertIn("Correct the rejected JSON response", summaries[1])
        self.assertEqual(self.current().summary[0].text, "The sky is blue, it says.")

    def test_patch_parsing_stays_backward_compatible_and_strict(self):
        legacy = json.dumps(self.value)
        patch = WikiPatch.parse(legacy)
        self.assertEqual(patch.summary, ())
        stored = WikiRevisionStore(self.paths).write(patch)
        self.assertNotIn(b"summary", stored.path.read_bytes())
        self.assertEqual(stored.path.read_bytes(), json.dumps(self.value, sort_keys=True, separators=(",", ":")).encode())
        summarized = json.dumps({**self.value, "summary": [{"text": "Blue sky.", "claims": [1]}]})
        self.assertEqual(WikiPatch.parse(summarized).summary[0].claims, (1,))
        for invalid in ({**self.value, "summary": []}, {**self.value, "summary": None}, {**self.value, "abstract": []},
                        {**self.value, "summary": [{"text": "Blue sky.", "claims": [0]}]},
                        {**self.value, "summary": [{"text": "Blue sky.", "claims": [1], "extra": 1}]},
                        {**self.value, "summary": [{"text": "Blue sky.", "claims": [1, 1, 1, 1, 1]}]},
                        {**self.value, "summary": [{"text": "<!-- hidden -->", "claims": [1]}]},
                        {**self.value, "summary": [{"text": "Blue [sky](javascript:x).", "claims": [1]}]}):
            with self.subTest(invalid=invalid), self.assertRaises(WikiError):
                WikiPatch.parse(json.dumps(invalid))
        sentences = [{"text": f"Entry {n}.", "claims": [1]} for n in range(6)]
        with self.assertRaises(WikiError):
            WikiPatch.parse(json.dumps({**self.value, "summary": sentences}))
        index = {**self.value, "page_id": "index", "title": "Index", "summary": sentences}
        self.assertEqual(len(WikiPatch.parse(json.dumps(index)).summary), 6)

    def test_boilerplate_lines_in_english_ukrainian_and_russian(self):
        from lifecontext.wiki import _boilerplate

        for text in ("© 2019 Penguin Random House", "Copyright 2020 by the author", "ISBN 978-0-14-044913-6",
                     "All rights reserved.", "Printed in Great Britain", "Published by Vintage Books",
                     "© Видавництво «Наш формат», 2021", "Усі права захищені.", "Віддруковано в Україні",
                     "Издательство АСТ, 2018", "Все права защищены.", "Отпечатано в типографии", "Тираж 3000 экз.",
                     "УДК 821.161.2"):
            with self.subTest(text=text):
                self.assertTrue(_boilerplate(text))
        for text in ("The sky is blue.", "Небо синє, а трава зелена.", "Небо голубое.", "He published nothing."):
            with self.subTest(text=text):
                self.assertFalse(_boilerplate(text))

    def test_index_entry_uses_the_first_summary_sentence_or_skips_boilerplate(self):
        text = "© 2021 Видавництво «Знання». Усі права захищені. Небо синє."
        citation = self.add_evidence("doc_2", text)
        second = source_page_id("doc_2")
        self.value = {"page_id": second, "title": "Книга", "expected_revision": 0, "contradictions": [], "links": [],
                      "claims": [{"text": "© 2021 Видавництво «Знання».", "citations": [citation]},
                                 {"text": "Небо синє.", "citations": [citation]}]}
        asyncio.run(self.service.update_for_document("doc_2", 1))
        index = self.current("index")
        self.assertEqual([claim.text for claim in index.claims], ["Небо синє."])
        self.assertEqual(index.summary, ())
        managed = self.service.show("index")["managed"]
        self.assertIn(f"1. Original ({second}) — Небо синє. [doc_2 v1/e1 passages 0:1]", managed)
        self.summary = [{"text": "Книга про колір неба.", "claims": [2]}, {"text": "Друге речення.", "claims": [1]}]
        self.value["expected_revision"] = 1
        asyncio.run(self.service.update_for_document("doc_2", 1))
        index = self.current("index")
        self.assertEqual([claim.text for claim in index.claims], ["Небо синє."])
        self.assertEqual([(s.text, s.claims) for s in index.summary], [("Книга про колір неба.", (1,))])
        managed = self.service.show("index")["managed"]
        self.assertIn("Lines marked \"Model summary\" are generated", managed)
        self.assertIn(f"1. Original ({second}) — Model summary: Книга про колір неба.", managed)
        self.assertNotIn("Друге речення", managed)
        # Nothing changed: the index keeps its revision.
        revision = self.catalog.get_wiki_page("index")["current_revision"]
        self.value["expected_revision"] = 2
        asyncio.run(self.service.update_for_document("doc_2", 1))
        self.assertEqual(self.catalog.get_wiki_page("index")["current_revision"], revision)

    # Cross-source compilation: routing the own page's checked claims onto shared pages.

    def compile(self, sid, text, *, revision=0, routes=(), provenance=None):
        citation = self.add_evidence(sid, text, provenance=provenance)
        self.value = {"page_id": source_page_id(sid), "title": "Original", "expected_revision": revision,
                      "claims": [{"text": text, "citations": [citation]}], "contradictions": [], "links": []}
        self.routes = list(routes)
        asyncio.run(self.service.update_for_document(sid, 1))
        return citation

    @staticmethod
    def new(title, *claims, kind="topic", contradicts=()):
        return {"page": {"kind": kind, "title": title}, "claims": list(claims or [1]), "contradicts": list(contradicts)}

    @staticmethod
    def to(page_id, *claims, contradicts=()):
        return {"page": page_id, "claims": list(claims or [1]), "contradicts": list(contradicts)}

    def test_two_documents_routed_to_one_topic_share_a_page_with_links_both_ways(self):
        self.routes = [self.new("Sky")]
        self.update()
        self.compile("doc_2", "The sky is wide.", routes=[self.to("topic_sky")])
        patch = self.current("topic_sky")
        self.assertEqual([claim.text for claim in patch.claims], ["The sky is blue.", "The sky is wide."])
        self.assertEqual(patch.links, (PAGE, source_page_id("doc_2")))
        self.assertEqual(self.current().links, ("topic_sky",))
        self.assertEqual(self.current(source_page_id("doc_2")).links, ("topic_sky",))
        row = self.catalog.get_wiki_revision("topic_sky", 2)
        self.assertEqual({d["source_id"] for d in row["dependencies"]}, {"doc_1", "doc_2"})
        # The routing request offered the existing page, its claims and the new document's claims.
        data = self.data(self.routing_queries[-1])
        self.assertEqual(data["pages"], [{"page_id": "topic_sky", "title": "Sky"}])
        self.assertEqual(data["existing_claims"], {"topic_sky": [{"n": 1, "text": "The sky is blue."}]})
        self.assertEqual(data["claims"], [{"n": 1, "text": "The sky is wide."}])
        self.assertNotIn("evidence", data)
        self.assertEqual(self.lint_codes("doc_2").count("source_unrouted"), 0)

    def test_a_new_title_whose_page_exists_adds_to_it(self):
        self.routes = [self.new("Sky")]
        self.update()
        self.compile("doc_2", "The sky is wide.", routes=[self.new("  SKY "), self.new("sky", kind="entity")])
        self.assertEqual([c.text for c in self.current("topic_sky").claims], ["The sky is blue.", "The sky is wide."])
        self.assertEqual(self.catalog.get_wiki_page("topic_sky")["title"], "Sky")
        self.assertEqual([c.text for c in self.current("entity_sky").claims], ["The sky is wide."])
        self.assertEqual(shared_page_id("topic", "Copy-writing: the Café way"), "topic_copy_writing_the_cafe_way")
        self.assertEqual(shared_page_id("topic", "Копірайтинг"), shared_page_id("topic", "КОПІРАЙТИНГ"))
        self.assertRegex(shared_page_id("topic", "Копірайтинг"), r"^topic_[0-9a-f]{12}$")
        self.assertRegex(shared_page_id("entity", "GPT-4 модель"), r"^entity_[0-9a-f]{12}$")
        self.assertNotEqual(shared_page_id("topic", "Кава"), shared_page_id("topic", "Чай"))
        self.assertRegex(shared_page_id("topic", "!!!"), r"^topic_[0-9a-f]{12}$")
        self.assertLessEqual(len(shared_page_id("entity", "word " * 60)), 128)
        # Symbols name different things; separators and case do not.
        ids = {shared_page_id("topic", title) for title in ("C++", "C#", "C", "F#", "F")}
        self.assertEqual(len(ids), 5)
        self.assertEqual(shared_page_id("topic", "C"), "topic_c")
        self.assertRegex(shared_page_id("topic", "C++"), r"^topic_c_[0-9a-f]{8}$")
        self.assertEqual(shared_page_id("topic", "c ++"), shared_page_id("topic", "C++"))
        self.assertEqual(shared_page_id("topic", "Machine-learning"), shared_page_id("topic", "machine learning"))
        self.assertEqual(shared_page_id("topic", "Копірайтинг!"), shared_page_id("topic", "«Копірайтинг»"))

    def test_cross_document_contradiction_dates_come_from_source_metadata(self):
        self.compile("doc_1b", "Coffee is a stimulant.", routes=[self.new("Coffee")],
                     provenance={"source": "test", "captured_at": "2024-03-01T23:30:00+02:00"})
        self.compile("doc_2", "Coffee is a sedative.",
                     routes=[self.to("topic_coffee", contradicts=[{"claim": 1, "page_id": "topic_coffee", "existing_claim": 1}])])
        patch = self.current("topic_coffee")
        self.assertEqual(len(patch.contradictions), 1)
        contradiction = patch.contradictions[0]
        retained = self.catalog.get_source("doc_2")["created_at"][:10]
        self.assertEqual((contradiction.left.text, contradiction.right.text), ("Coffee is a stimulant.", "Coffee is a sedative."))
        self.assertEqual((contradiction.left_date, contradiction.right_date), ("2024-03-01", retained))
        reviews = [r for r in self.catalog.list_wiki_reviews("topic_coffee") if r["code"] == "contradiction"]
        self.assertEqual([(r["state"], r["details"]["sources"]["left_date"]) for r in reviews], [("pending", "2024-03-01")])
        self.assertIn("> [!note] Contradiction (2024-03-01 / " + retained + ")", self.service.show("topic_coffee")["managed"])
        # A contradiction must name a claim the request showed for that page.
        for item in ({"claim": 1, "page_id": "topic_coffee", "existing_claim": 9},
                     {"claim": 2, "page_id": "topic_coffee", "existing_claim": 1},
                     {"claim": 1, "page_id": "topic_other", "existing_claim": 1}):
            with self.subTest(item=item):
                self.value["expected_revision"] += 1
                self.routes = [self.to("topic_coffee", contradicts=[item])]
                asyncio.run(self.service.update_for_document("doc_2", 1))
                # Routing failed: shared pages and the own page's links stay as they were.
                self.assertEqual(self.catalog.get_wiki_page("topic_coffee")["current_revision"], 2)
                self.assertEqual(self.current(source_page_id("doc_2")).links, ("topic_coffee",))

    def test_a_contradiction_names_one_of_its_routes_claims(self):
        self.compile("doc_1b", "Coffee is a stimulant.", routes=[self.new("Coffee")])
        citation = self.add_evidence("doc_2", "Coffee is a sedative.")
        self.value = {"page_id": source_page_id("doc_2"), "title": "Original", "expected_revision": 0,
                      "claims": [{"text": "Coffee is a sedative.", "citations": [citation]},
                                 {"text": "a sedative", "citations": [citation]}], "contradictions": [], "links": []}
        # Claim 2 is the document's, but this route places only claim 1 on the page.
        self.routes = [self.to("topic_coffee", 1, contradicts=[{"claim": 2, "page_id": "topic_coffee", "existing_claim": 1}])]
        asyncio.run(self.service.update_for_document("doc_2", 1))
        self.assertEqual(self.catalog.get_wiki_page("topic_coffee")["current_revision"], 1)
        self.assertEqual(self.current(source_page_id("doc_2")).links, ())
        self.assertEqual(self.catalog.list_wiki_reviews("topic_coffee"), [])
        # Two routes to one page keep its first 8 claims; a contradiction must name one of those.
        own = {number: Claim(f"claim {number}", ()) for number in range(1, 10)}
        shown = {"topic_coffee": {1: Claim("existing", ())}}
        routes = [{"page": "topic_coffee", "claims": list(range(1, 9)), "contradicts": []},
                  {"page": "topic_coffee", "claims": [9], "contradicts": [{"claim": 9, "page_id": "topic_coffee", "existing_claim": 1}]}]
        with self.assertRaises(WikiError):
            self.service._parse_routes(json.dumps({"routes": routes}), own, shown)
        routes[0]["claims"] = list(range(1, 8))
        routes[1].update(claims=[8], contradicts=[{"claim": 8, "page_id": "topic_coffee", "existing_claim": 1}])
        parsed = self.service._parse_routes(json.dumps({"routes": routes}), own, shown)
        self.assertEqual(parsed["topic_coffee"]["claims"], [own[number] for number in range(1, 9)])
        self.assertEqual(parsed["topic_coffee"]["contradicts"], [(shown["topic_coffee"][1], own[8])])

    @staticmethod
    def bounded_page(own_claims, others, *, text_bytes=40):
        """A shared page: the compiling document doc_1's routed claims first, then other documents' claims."""
        def claim(source_id, number):
            text = f"{source_id} claim {number} ".ljust(text_bytes, "x")
            return Claim(text, (Citation(source_id, 1, 1, number, number + 1, "a" * 64, text),))
        claims = [claim("doc_1", n) for n in range(own_claims)]
        claims += [claim(source_id, n) for source_id, count in others for n in range(count)]
        return WikiPatch("topic_bounded", "Bounded", 1, tuple(claims), (), ())

    @staticmethod
    def per_source(patch):
        counts = {}
        for claim in patch.claims:
            counts[claim.citations[0].source_id] = counts.get(claim.citations[0].source_id, 0) + 1
        return counts

    def test_a_full_page_leaves_out_the_compiling_documents_claims_first(self):
        # 8 routed claims of doc_1, then 15 documents with 8 claims and doc_17 with 4: 132 claims.
        others = [(f"doc_{number}", 8) for number in range(2, 17)] + [("doc_17", 4)]
        fitted = self.service._fit(self.bounded_page(8, others), "doc_1", 8)
        self.assertEqual(len(fitted.claims), MAX_PAGE_CLAIMS)
        self.assertEqual(self.per_source(fitted), {"doc_1": 4, **dict(others)})
        self.assertEqual([claim.text for claim in fitted.claims[:4]],
                         [claim.text for claim in self.bounded_page(8, others).claims[:4]])
        # Without a compiling document (a model-free refresh) the last claims go.
        fitted = self.service._fit(self.bounded_page(8, others), None, 8)
        self.assertEqual(self.per_source(fitted), {"doc_1": 8, **dict(others[:-1])})

    def test_a_page_over_48_kb_leaves_out_the_compiling_documents_claims_first(self):
        # Five documents with 4 claims of about 2.2 KB each (claim text and quote) fill about 44 KB.
        others = [(f"doc_{number}", 4) for number in range(2, 7)]
        patch = self.bounded_page(8, others, text_bytes=1000)
        self.assertGreater(len(_patch_json(patch).encode()), MAX_PATCH_BYTES)
        fitted = self.service._fit(patch, "doc_1", 8)
        self.assertLessEqual(len(_patch_json(fitted).encode()), MAX_PATCH_BYTES)
        counts = self.per_source(fitted)
        self.assertEqual({source_id: counts[source_id] for source_id, _ in others}, dict(others))
        self.assertLess(counts.get("doc_1", 0), 8)

    def test_a_resolved_contradiction_never_comes_back(self):
        claim = self.value["claims"][0]
        contradiction = {"left": claim, "right": {"text": "sky is blue", "citations": [self.citation]},
                         "left_date": "2026-01-01", "right_date": "2026-02-01"}
        own = {**self.value, "contradictions": [contradiction]}
        self.value = own
        self.update()
        pending = [r for r in self.catalog.list_wiki_reviews(PAGE) if r["state"] == "pending"]
        self.assertEqual(len(pending), 1)
        self.catalog.resolve_wiki_review(pending[0]["review_id"], note="Both readings stand.")
        # The model returns the same contradiction again on a rebuild.
        self.value = {**own, "expected_revision": 1}
        self.update()
        self.assertEqual(self.current().contradictions, ())
        self.assertEqual([r["state"] for r in self.catalog.list_wiki_reviews(PAGE)], ["resolved"])
        self.assertNotIn("Contradiction", self.service.show(PAGE)["managed"])
        # The same holds for a contradiction found by routing.
        self.compile("doc_2", "The sky is grey.", routes=[self.new("Sky")])
        self.routes = [self.to("topic_sky", contradicts=[{"claim": 1, "page_id": "topic_sky", "existing_claim": 1}])]
        self.value = {**own, "expected_revision": 2, "contradictions": []}
        self.update()
        reviews = [r for r in self.catalog.list_wiki_reviews("topic_sky") if r["code"] == "contradiction"]
        self.assertEqual([r["state"] for r in reviews], ["pending"])
        self.assertEqual(reviews[0]["details"]["sources"]["left"]["text"], "The sky is grey.")
        self.catalog.resolve_wiki_review(reviews[0]["review_id"], note="Different skies.")
        self.value = {**self.value, "expected_revision": 3}
        self.update()
        self.assertEqual(self.current("topic_sky").contradictions, ())
        self.assertEqual([r["state"] for r in self.catalog.list_wiki_reviews("topic_sky")], ["resolved"])

    def test_a_concurrent_revision_is_reloaded_and_merged_once(self):
        import contextlib
        self.routes = [self.new("Sky")]
        self.update()
        grass = self.add_evidence("doc_2", "Grass is green.")
        # doc_2's own page routes to the shared page, as its compile would leave it.
        self.commit(source_page_id("doc_2"), "Original", [{"text": "Grass is green.", "citations": [grass]}], links=["topic_sky"])
        cloudy = self.add_evidence("doc_1", "The sky is cloudy.", revision=2)
        self.value = {**self.value, "expected_revision": 1, "claims": [{"text": "The sky is cloudy.", "citations": [cloudy]}]}
        self.routes = [self.to("topic_sky")]
        publication = self.catalog.wiki_publication
        raced = []

        @contextlib.contextmanager
        def racing():
            # Another process publishes the shared page after this compile read it.
            if not raced:
                raced.append(self.commit("topic_sky", "Sky", [{"text": "Grass is green.", "citations": [grass]}]))
            with publication():
                yield

        with unittest.mock.patch.object(self.catalog, "wiki_publication", racing):
            rows = self.update()
        self.assertEqual(raced[0]["revision"], 2)
        self.assertIn(("topic_sky", 3), [(row["page_id"], row["revision"]) for row in rows])
        self.assertEqual([c.text for c in self.current("topic_sky").claims], ["Grass is green.", "The sky is cloudy."])
        # A page that conflicts again (a hand-edited page) is left for the next compile.
        self.catalog.record_wiki_review("projection_topic_sky", "topic_sky", 3, code="manual_edit",
                                        details={"expected_hash": "a" * 64, "actual_hash": "b" * 64})
        self.value["expected_revision"] = 2
        self.value["claims"] = [{"text": "sky is cloudy", "citations": [cloudy]}]
        rows = self.update()
        self.assertNotIn("topic_sky", [row["page_id"] for row in rows])
        self.assertEqual(self.catalog.get_wiki_page("topic_sky")["current_revision"], 3)
        # The page still holds the document's earlier claims, so the link stays.
        self.assertEqual(self.current().links, ("topic_sky",))
        self.assertEqual(self.catalog.get_document_stage("doc_1", 1, "wiki")["state"], "completed")

    def test_a_rebuild_drops_routes_the_document_no_longer_has(self):
        self.routes = [self.new("Sky"), self.new("Weather")]
        self.update()
        self.compile("doc_2", "The sky is wide.", routes=[self.to("topic_sky")])
        self.value = {"page_id": PAGE, "title": "Original", "expected_revision": 1,
                      "claims": [{"text": "The sky is blue.", "citations": [self.citation]}], "contradictions": [], "links": []}
        self.routes = [self.to("topic_weather")]
        rows = asyncio.run(self.service.rebuild("doc_1", 1))
        self.assertEqual([c.text for c in self.current("topic_sky").claims], ["The sky is wide."])
        self.assertEqual(self.current("topic_sky").links, (source_page_id("doc_2"),))
        self.assertEqual(self.current().links, ("topic_weather",))
        # The weather page did not change, so it keeps its revision.
        self.assertNotIn("topic_weather", [row["page_id"] for row in rows])
        # A page that would be left without claims keeps its revision but shows as retired.
        self.routes = []
        self.value["expected_revision"] = 2
        asyncio.run(self.service.rebuild("doc_1", 1))
        self.assertEqual(self.catalog.get_wiki_page("topic_weather")["current_revision"], 1)
        self.assertEqual(self.current().links, ())
        shown = self.service.show("topic_weather")
        self.assertEqual(shown["withdrawn"], "retired")
        self.assertNotIn("The sky is blue.", shown["managed"])
        self.assertIn("Retired: no document routes claims here anymore.", shown["managed"])
        self.assertIn("# Weather", shown["managed"])

    def test_forget_removes_the_sources_claims_from_shared_pages(self):
        self.routes = [self.new("Sky")]
        self.update()
        self.compile("doc_2", "The sky is wide.", routes=[self.to("topic_sky")])
        original = self.catalog.list_tombstones
        with unittest.mock.patch.object(self.catalog, "list_tombstones",
                                        side_effect=lambda sid: [{"tombstone_type": "forgotten"}] if sid == "doc_1" else original(sid)):
            self.catalog.mark_dependent_wiki_stale("doc_1", 1)
            self.assertEqual(self.catalog.get_wiki_page("topic_sky")["state"], "stale")
            rows = self.service.refresh_shared_pages()
            self.assertEqual([(row["page_id"], row["revision"]) for row in rows], [("topic_sky", 3)])
            patch = self.current("topic_sky")
            self.assertEqual([c.text for c in patch.claims], ["The sky is wide."])
            self.assertEqual(patch.links, (source_page_id("doc_2"),))
            self.assertEqual(self.catalog.get_wiki_page("topic_sky")["state"], "current")
            self.assertEqual(self.service.refresh_shared_pages(), [])

    def test_routing_bounds_and_one_repair(self):
        # Too many routes, too many claims, unknown pages and claims are rejected.
        invalid = ([self.new(f"Topic {n}") for n in range(7)], [self.new("Sky", *range(1, 10))],
                   [self.to("topic_missing")], [self.to(PAGE)], [self.new("Sky", 2)], [self.new("Sky", 1, 1)],
                   [self.new("See https://example.test")], [{"page": {"kind": "idea", "title": "Sky"}, "claims": [1], "contradicts": []}],
                   [{**self.new("Sky"), "quote": "invented"}])
        for revision, routes in enumerate(invalid):
            with self.subTest(routes=routes):
                self.value["expected_revision"] = revision
                self.routes = routes
                self.update()
                self.assertEqual(self.catalog.get_document_stage("doc_1", 1, "wiki")["state"], "completed")
                self.assertEqual([page["page_id"] for page in self.service.list()], ["index", PAGE])
                self.assertIn("source_unrouted", self.lint_codes())
        responses = iter([json.dumps({"routes": invalid[0]}), json.dumps({"routes": [self.new("Sky")]})])
        routing = []

        async def compile_document(query, **kwargs):
            if self.is_routing(query):
                routing.append(query)
                return {"text": next(responses)}
            return {"text": json.dumps(self.value) if self.is_window(query) else "{}"}

        self.service.compiler = compile_document
        self.value["expected_revision"] = len(invalid)
        self.update()
        self.assertEqual(len(routing), 2)
        self.assertIn("Correct the rejected JSON response", routing[1])
        self.assertEqual(self.current().links, ("topic_sky",))
        self.assertNotIn("source_unrouted", self.lint_codes())

    def test_routing_target_and_request_size_limits(self):
        for n in range(15):
            self.commit(f"topic_t{n:02}", f"T{n}", self.value["claims"])
        self.routes = [self.new("Sky")]
        self.update()
        # Pages that only lose this document's claims do not count toward the target limit.
        self.assertEqual(self.catalog.get_wiki_page("topic_sky")["current_revision"], 1)
        self.assertEqual(self.catalog.get_wiki_page("topic_t00")["current_revision"], 1)
        self.assertEqual(self.service.show("topic_t00")["withdrawn"], "retired")
        self.assertEqual(self.current().links, ("topic_sky",))
        # A long catalog and long claims stay within the request bounds.
        pages = [{"page_id": f"topic_filler_{n:03}", "title": f"Filler title number {n} " + "x" * 40,
                  "current_revision": 1, "state": "current"} for n in range(400)]
        real_list, real_revision = self.service.list, self.catalog.get_wiki_revision
        from lifecontext.wiki import Citation, Claim
        long = "Небо синє. " + "ю" * 1000
        # Built directly: the request bound must hold even beyond what one page can store.
        claims = tuple(Claim(long[:1000 - n], (Citation(**{**self.citation, "quote": long[:1000 - n]}),)) for n in range(48))
        patch = WikiPatch(PAGE, "Original", 1, claims, (), ())
        with unittest.mock.patch.object(self.service, "list", side_effect=lambda: real_list() + pages), \
             unittest.mock.patch.object(self.catalog, "get_wiki_revision", side_effect=lambda page_id, revision: (
                 {"dependencies": []} if page_id.startswith("topic_filler") else real_revision(page_id, revision))):
            query, own, shown = self.service._routing_request(patch, "doc_1")
        data = self.data(query)
        self.assertLessEqual(len(query.encode()), 60_000)
        self.assertLessEqual(len(data["pages"]), 300)
        self.assertLessEqual(len(json.dumps(data["pages"], ensure_ascii=False, separators=(",", ":")).encode()), 12_000)
        self.assertLessEqual(len(json.dumps(data["claims"], ensure_ascii=False, separators=(",", ":")).encode()), 24_000)
        self.assertEqual([item["n"] for item in data["claims"]], list(range(1, 49)))
        self.assertEqual(len(own), 48)
        # Pages that already cite the document come first.
        self.assertEqual({entry["page_id"] for entry in data["pages"][:16]},
                         {"topic_sky", *(f"topic_t{n:02}" for n in range(15))})
        self.assertEqual(set(shown), set(data["existing_claims"]))

    def test_fake_compiler_pipeline_in_english_and_ukrainian(self):
        from wiki_fake_compiler import FakeCompiler

        from lifecontext.document_projection import DocumentProjectionService
        from lifecontext.vault import ManagedVault
        topics = {"coffee": ("topic", "Coffee"), "copywriting": ("topic", "Copywriting"), "france": ("entity", "France"),
                  "кава": ("topic", "Кава"), "копірайтинг": ("topic", "Копірайтинг")}
        compiler = FakeCompiler(topics)
        self.service.compiler = compiler
        documents = (
            ("doc_en_a", "Coffee is a stimulant. Copywriting is persuasive writing. The capital of France is Paris.", "2024-03-01"),
            ("doc_en_b", "Copywriting is persuasive writing. Coffee is a sedative.", "2025-06-30"),
            ("doc_uk_a", "Копірайтинг — це переконливе письмо. Кава — це стимулятор.", "2024-05-01"),
            ("doc_uk_b", "Кава — це депресант.", "2025-01-15"),
        )
        for count, (sid, text, captured) in enumerate(documents, 1):
            self.add_evidence(sid, text, provenance={"source": "test", "captured_at": captured})
            asyncio.run(self.service.update_for_document(sid, 1))
            self.assertEqual(self.catalog.get_document_stage(sid, 1, "wiki")["state"], "completed")
            shared = sorted(page["page_id"] for page in self.service.list() if page["page_id"].startswith(("topic_", "entity_")))
            if count == 2:
                # Two English documents: shared pages, links both ways, one contradiction review.
                self.assertEqual(shared, ["entity_france", "topic_coffee", "topic_copywriting"])
                self.assertEqual(self.current("topic_copywriting").links, (source_page_id("doc_en_a"), source_page_id("doc_en_b")))
                self.assertEqual(self.current(source_page_id("doc_en_b")).links, ("topic_copywriting", "topic_coffee"))
                self.assertEqual(len(self.catalog.find_wiki_reviews(state="pending", limit=10)[0]), 1)
            if count == 3:
                # A third, Ukrainian document gets pages with deterministic non-Latin IDs.
                self.assertEqual(shared, sorted(["entity_france", "topic_coffee", "topic_copywriting",
                                                 shared_page_id("topic", "Кава"), shared_page_id("topic", "Копірайтинг")]))
                self.assertEqual(self.current(source_page_id("doc_uk_a")).links,
                                 (shared_page_id("topic", "Копірайтинг"), shared_page_id("topic", "Кава")))
                self.assertEqual(self.catalog.get_wiki_page(shared_page_id("topic", "Кава"))["title"], "Кава")
        coffee, kava = self.current("topic_coffee"), self.current(shared_page_id("topic", "Кава"))
        self.assertEqual([c.text for c in coffee.claims], ["Coffee is a stimulant.", "Coffee is a sedative."])
        self.assertEqual(coffee.links, (source_page_id("doc_en_a"), source_page_id("doc_en_b")))
        self.assertEqual([(c.left.text, c.right.text, c.left_date, c.right_date) for c in coffee.contradictions],
                         [("Coffee is a stimulant.", "Coffee is a sedative.", "2024-03-01", "2025-06-30")])
        self.assertEqual([(c.left.text, c.right.text, c.left_date, c.right_date) for c in kava.contradictions],
                         [("Кава — це стимулятор.", "Кава — це депресант.", "2024-05-01", "2025-01-15")])
        self.assertEqual(len(self.current("topic_copywriting").claims), 2)
        self.assertEqual(self.current("entity_france").links, (source_page_id("doc_en_a"),))
        self.assertEqual(self.current(source_page_id("doc_en_a")).links, ("topic_coffee", "topic_copywriting", "entity_france"))
        self.assertEqual(self.current(source_page_id("doc_uk_b")).links, (shared_page_id("topic", "Кава"),))
        pending = [r for r in self.catalog.find_wiki_reviews(state="pending", limit=100)[0] if r["code"] == "contradiction"]
        self.assertEqual(sorted(r["page_id"] for r in pending), sorted(["topic_coffee", shared_page_id("topic", "Кава")]))
        self.assertTrue(all(self.current(source_page_id(sid)).summary for sid, _, _ in documents))
        self.assertEqual({len(compiler.queries[kind]) for kind in ("summary", "routing")}, {len(documents)})
        self.assertTrue(all(len(query.encode()) <= 60_000 for queries in compiler.queries.values() for query in queries))
        # The shared pages and their links are visible in the vault.
        root = Path(self.temp.name) / "vault"
        (root / "Journal").mkdir(parents=True, mode=0o700)
        root.chmod(0o700)
        vault = ManagedVault(root, root / "Journal")
        self.addCleanup(vault.close)
        projection = DocumentProjectionService(self.catalog, self.custody, self.derived, vault, self.service)
        pages = [page["page_id"] for page in self.service.list()]
        self.assertEqual({result["state"] for result in projection.project_wiki_pages(pages)}, {"completed"})
        receipt = self.catalog.get_artifact_projection("wiki", "topic_coffee", self.catalog.get_wiki_page("topic_coffee")["current_revision"])
        text = (root / "Journal" / receipt["projection_path"]).read_text()
        self.assertIn("> [!note] Contradiction (2024-03-01 / 2025-06-30)", text)
        related = [line for line in text.splitlines() if line.startswith("Related: ")]
        self.assertEqual(len(related), 1)
        self.assertEqual(related[0].count("]("), 2)
        for target in re.findall(r"\]\(([^)#]+)\)", text):
            self.assertTrue((root / "Journal" / "wiki" / unquote(target)).is_file(), target)

    # Review fixes: forgotten sources, contradiction identity, retired routes, isolation.

    def forget(self, *sources):
        original = self.catalog.list_tombstones
        return unittest.mock.patch.object(self.catalog, "list_tombstones", side_effect=lambda sid: (
            [{"tombstone_type": "forgotten"}] if sid in sources else original(sid)))

    def test_forgotten_text_is_never_shown_nor_sent_to_the_model(self):
        self.routes = [self.new("Sky"), self.new("Weather")]
        self.update()
        self.compile("doc_2", "The sky is wide.", routes=[self.to("topic_sky")])
        with self.forget("doc_1"):
            for page_id, revision in ((PAGE, None), ("topic_weather", None), ("topic_sky", 1)):
                with self.subTest(page_id=page_id, revision=revision):
                    shown = self.service.show(page_id, revision)
                    self.assertEqual((shown["withdrawn"], shown["title"]), ("forgotten", "Withdrawn page"))
                    self.assertEqual(shown["managed"], "<!-- life-context:managed:start -->\n# Withdrawn page\n\n"
                                     "Withdrawn: this page's sources were forgotten.\n<!-- life-context:managed:end -->\n")
                    self.assertTrue(self.service.forgotten(page_id, revision))
            sky = self.service.show("topic_sky")
            self.assertIsNone(sky["withdrawn"])
            self.assertNotIn("The sky is blue.", sky["managed"])
            self.assertIn("The sky is wide.", sky["managed"])
            self.assertIn(f"Related: Original ({source_page_id('doc_2')})", sky["managed"])
            self.assertNotIn(PAGE, sky["managed"])
            index = self.service.show("index")["managed"]
            self.assertNotIn("The sky is blue.", index)
            self.assertNotIn(PAGE, index)
            self.compile("doc_3", "The sky is high.", routes=[self.to("topic_sky")])
            query = self.routing_queries[-1]
            self.assertNotIn("The sky is blue.", query)
            self.assertNotIn("topic_weather", query)
            self.assertIn("The sky is wide.", query)
            self.assertEqual([c.text for c in self.current("topic_sky").claims], ["The sky is wide.", "The sky is high."])

    def test_a_forgotten_document_is_neither_compiled_nor_rebuilt(self):
        self.update()
        # A document forget leaves no tombstone: its versions can no longer be recalled.
        self.catalog.set_provider_state("doc_1", 1, recall_visibility="not_visible")
        self.assertTrue(self.service.source_withdrawn("doc_1"))
        before = len(self.queries)
        self.value["expected_revision"] = 1
        with self.assertRaises(WikiError):
            self.update()
        self.assertEqual(len(self.queries), before)
        result = asyncio.run(self.application().rebuild_wiki(idempotency_key="bulk-after-forget"))
        self.assertEqual((result["state"], result["summary"]["sources"]), ("completed", []))
        self.assertEqual(self.service.show(PAGE)["withdrawn"], "forgotten")

    def test_a_contradiction_found_again_in_the_other_order_is_the_same_one(self):
        from wiki_fake_compiler import FakeCompiler
        self.service.compiler = FakeCompiler({"coffee": ("topic", "Coffee")})
        for sid, text in (("doc_en_a", "Coffee is a stimulant."), ("doc_en_b", "Coffee is a sedative.")):
            self.add_evidence(sid, text)
            asyncio.run(self.service.update_for_document(sid, 1))

        def contradictions():
            reviews = [r for r in self.catalog.list_wiki_reviews("topic_coffee") if r["code"] == "contradiction"]
            return [r["state"] for r in reviews], self.service.show("topic_coffee")["managed"].count("[!note] Contradiction")

        self.assertEqual(contradictions(), (["pending"], 1))
        # Rebuilding the first document finds the pair again, reversed and with swapped dates.
        asyncio.run(self.service.update_for_document("doc_en_a", 1))
        self.assertEqual(contradictions(), (["pending"], 1))
        review = self.catalog.list_wiki_reviews("topic_coffee")[0]["review_id"]
        self.catalog.resolve_wiki_review(review, note="Both are true in their context.")
        for _ in range(2):
            asyncio.run(self.service.update_for_document("doc_en_a", 1))
            self.assertEqual(contradictions()[0], ["resolved"])
            self.assertLessEqual(contradictions()[1], 1)
        self.assertEqual(self.current("topic_coffee").contradictions, ())

    def test_rebuilding_with_new_titles_retires_the_old_pages(self):
        titles = ("Sky", "Weather", "Climate", "Sky")
        for revision, title in enumerate(titles):
            with self.subTest(title=title):
                self.value["expected_revision"] = revision
                self.routes = [self.new(title)]
                self.update()
                current = shared_page_id("topic", title)
                self.assertEqual(self.current().links, (current,))
                self.assertIsNone(self.service.show(current)["withdrawn"])
                self.assertIn("The sky is blue.", self.service.show(current)["managed"])
                for other in {shared_page_id("topic", t) for t in titles} - {current}:
                    if self.catalog.get_wiki_page(other):
                        self.assertEqual(self.service.show(other)["withdrawn"], "retired")
                self.assertNotIn("source_unrouted", self.lint_codes())
        # Retired pages are not counted as targets; a document can route six times over.
        self.assertEqual(self.catalog.get_document_stage("doc_1", 1, "wiki")["state"], "completed")

    def test_a_damaged_other_contributor_never_blocks_a_document(self):
        self.routes = [self.new("Sky")]
        self.update()
        self.compile("doc_2", "The sky is wide.", routes=[self.to("topic_sky")])
        self.derived.artifact_path("doc_2", 1, 1).write_bytes(b"corrupt")
        revision = self.catalog.get_wiki_page("topic_sky")["current_revision"]
        self.value = {"page_id": PAGE, "title": "Original", "expected_revision": 1,
                      "claims": [{"text": "sky is blue", "citations": [self.citation]}], "contradictions": [], "links": []}
        self.routes = [self.to("topic_sky")]
        rows = self.update()
        self.assertEqual(self.catalog.get_document_stage("doc_1", 1, "wiki")["state"], "completed")
        self.assertEqual(self.catalog.get_wiki_page("topic_sky")["current_revision"], revision)
        self.assertIn((PAGE, 2), [(row["page_id"], row["revision"]) for row in rows])
        self.assertEqual(self.current().links, ("topic_sky",))

    def test_rebuilding_with_the_same_routes_publishes_nothing_new(self):
        self.routes = [self.new("Sky")]
        self.update()
        self.compile("doc_2", "The sky is wide.", routes=[self.to("topic_sky")])
        revision = self.catalog.get_wiki_page("topic_sky")["current_revision"]
        self.value = {"page_id": PAGE, "title": "Original", "expected_revision": 1,
                      "claims": [{"text": "The sky is blue.", "citations": [self.citation]}], "contradictions": [], "links": []}
        self.routes = [self.to("topic_sky")]
        for _ in range(2):
            rows = self.update()
            self.value["expected_revision"] += 1
            self.assertNotIn("topic_sky", [row["page_id"] for row in rows])
            self.assertEqual(self.catalog.get_wiki_page("topic_sky")["current_revision"], revision)

    def test_model_prose_and_titles_stay_plain_text(self):
        for text in ("See [[Secret note]].", "An image ![x](y).", "Hidden %%comment%%."):
            with self.subTest(text=text), self.assertRaises(WikiError):
                WikiPatch.parse(json.dumps({**self.value, "summary": [{"text": text, "claims": [1]}]}))
        patch = WikiPatch.parse(json.dumps({**self.value, "title": "Two\nlines # title"}))
        self.assertIn("\n# Two lines # title\n", render_managed(patch))

    def test_contradiction_dates_are_days_in_the_top_level_timezone(self):
        from zoneinfo import ZoneInfo
        self.add_evidence("doc_2", "Late night.", provenance={"source": "test", "captured_at": "2024-03-01T23:30:00+00:00"})
        self.assertEqual(self.service._source_date("doc_2", 1), "2024-03-01")
        self.service.timezone = ZoneInfo("Europe/Kyiv")
        self.assertEqual(self.service._source_date("doc_2", 1), "2024-03-02")
        retained = self.catalog.get_source("doc_1")["created_at"]
        self.service.timezone = ZoneInfo("UTC")
        self.assertEqual(self.service._source_date("doc_1", 1), retained[:10])
