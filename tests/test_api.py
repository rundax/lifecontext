"""Strict schema checks for the isolated LifeContext HTTP contract."""

from __future__ import annotations

import asyncio
import hashlib
import http.client
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import uvicorn
import yaml
from pydantic import TypeAdapter, ValidationError

from lifecontext.api.app import LocalRequestGuard, PublicAPIError, create_app, run
from lifecontext.api.models import (
    CurationRequest,
    ExactEvidenceResponse,
    OperationResponse,
    RelationshipScanRequest,
    RetainRequest,
    omit_source_text,
)
from lifecontext.application import ApplicationError, JournalApplication
from lifecontext.bundle import BUNDLE_MEDIA_TYPE, BundleRecord, decode_bundle, encode_bundle
from lifecontext.catalog import LifeContextCatalog
from lifecontext.config import JournalLimits
from lifecontext.custody import SourceManifest
from lifecontext.hindsight_adapter import LifeContextHindsightAdapter
from lifecontext.paths import LifeContextPaths
from lifecontext.vault import ManagedVault, VaultError
from lifecontext.wiki import WikiError
from local_paths import scratch_root


class APIModelTests(unittest.TestCase):
    def test_document_response_exposes_bounded_latest_extraction_warnings(self):
        from lifecontext.api.models import DocumentResponse
        from lifecontext.api.routes import _document
        stage = {"state": "completed", "revision": 1, "attempt": 1, "error_code": None}
        value = {"source_id": "doc_book", "source_version": 1, "filename": "book.pdf", "title": "Book",
                 "mime_type": "application/pdf", "category_id": "inbox", "category_revision": 1,
                 "accepted": True, "stages": {name: stage for name in ("custody", "extraction", "indexing", "wiki", "projection")},
                 "extractions": [{"warnings": ["old"]}, {"warnings": ["ocr_no_text_page:2; page content unresolved"]}]}
        response = _document(value)
        self.assertEqual(response.extraction_warnings, ["ocr_no_text_page:2; page content unresolved"])
        with self.assertRaises(ValidationError):
            DocumentResponse.model_validate({**response.model_dump(), "extraction_warnings": ["warning"] * 1001})

    def test_retain_model_rejects_extra_fields_coercion_and_path_basename(self) -> None:
        base = {
            "source_id": "src_test",
            "source_version": 1,
            "content": "bounded text",
            "provenance": {"basename": "note.md"},
        }
        self.assertEqual(RetainRequest.model_validate(base).source_id, "src_test")
        with self.assertRaises(ValidationError):
            RetainRequest.model_validate({**base, "filesystem_path": "/tmp/note.md"})
        with self.assertRaises(ValidationError):
            RetainRequest.model_validate({**base, "source_version": "1"})
        with self.assertRaises(ValidationError):
            RetainRequest.model_validate({**base, "provenance": {"basename": "../note.md"}})

    def test_text_fields_enforce_utf8_byte_limits(self) -> None:
        too_many_bytes = "é" * 524_289
        with self.assertRaises(ValidationError):
            RetainRequest.model_validate(
                {"source_id": "src_large", "source_version": 1, "content": too_many_bytes}
            )

    def test_exact_evidence_recursively_suppresses_source_text_fields(self) -> None:
        source_text = {
            "content": "private source",
            "metadata": {
                "text": "nested source excerpt",
                "sha256": "a" * 64,
                "descriptors": [{"source_text": "nested quote", "source_version": 2}],
            },
            "citations": [{"source_id": "src_exact", "source_version": 2}],
        }
        safe = omit_source_text(source_text)
        self.assertNotIn("content", safe)
        self.assertNotIn("text", safe["metadata"])
        self.assertNotIn("source_text", safe["metadata"]["descriptors"][0])
        self.assertEqual(safe["citations"][0]["source_version"], 2)
        self.assertEqual(safe["metadata"]["sha256"], "a" * 64)

    def test_operation_summary_allows_only_bounded_content_free_facts(self) -> None:
        response = OperationResponse.model_validate(
            {
                "operation_id": "op_123",
                "state": "retryable_failed",
                "summary": {
                    "sources": [
                        {
                            "source_id": "src_safe",
                            "source_version": 2,
                            "sha256": "b" * 64,
                        }
                    ],
                    "item_count": 1,
                    "error_code": "provider_unavailable",
                },
            }
        )
        self.assertEqual(response.state, "retryable_failed")
        with self.assertRaises(ValidationError):
            OperationResponse.model_validate(
                {
                    "operation_id": "op_123",
                    "state": "completed",
                    "summary": {"result": {"text": "must not be modelled"}},
                }
            )

    def test_curation_contract_is_discriminated_and_unknown_actions_fail(self) -> None:
        adapter = TypeAdapter(CurationRequest)
        value = adapter.validate_python(
            {
                "action": "correct",
                "source_id": "src_correct",
                "expected_version": 3,
                "content": "replacement source",
            }
        )
        self.assertEqual(value.action, "correct")
        with self.assertRaises(ValidationError):
            adapter.validate_python({"action": "custom", "source_id": "src_correct"})

    def test_exact_evidence_model_keeps_provenance_and_citations_without_content(self) -> None:
        value = ExactEvidenceResponse.model_validate(
            {
                "source_id": "src_exact",
                "source_version": 4,
                "sha256": "c" * 64,
                "size_bytes": 20,
                "metadata": {"source": "journal", "basename": "record.md"},
                "citations": [
                    {"source_id": "src_exact", "source_version": 4, "sha256": "c" * 64}
                ],
            }
        )
        self.assertIsNone(value.content)
        self.assertEqual(value.citations[0].source_version, 4)


class ControlledMemory:
    def __init__(self) -> None:
        self.response: dict[str, object] = {}

    async def get_memory(self, bank_id: str, memory_id: str) -> dict[str, object]:
        if self.response.get("id") == memory_id:
            return self.response
        return {}


class ControlledClient:
    def __init__(self) -> None:
        self.error: Exception | None = None
        self.retain_calls: list[tuple[str, str, dict[str, str] | None, bool]] = []
        self.retain_response = SimpleNamespace(success=True, var_async=False, operation_id=None)
        self.recall_response: dict[str, object] = {"results": []}
        self.memory = ControlledMemory()
        self.reflect_response: dict[str, object] = {
            "text": "supported response",
            "based_on": {"memories": [{"id": "fact-1", "text": "provider-only text"}]},
        }

    async def aretain(self, bank_id, content, document_id, metadata=None, retain_async=False, tags=None):
        self.retain_calls.append((document_id, content, metadata, retain_async))
        if self.error:
            raise self.error
        return self.retain_response

    async def arecall(self, bank_id, query, max_tokens=4096, budget="mid"):
        if self.error:
            raise self.error
        return self.recall_response

    async def areflect(self, bank_id, query, budget="low", max_tokens=None, include_facts=False):
        if self.error:
            raise self.error
        return self.reflect_response


class APIHTTPTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="lifeweave-api-test.", dir=scratch_root())
        self.base = Path(self.temporary.name).resolve(strict=True)
        self.home = self.base / "home"
        self.home.mkdir(mode=0o700)
        self.home_patch = patch.dict(os.environ, {"HOME": str(self.home)})
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)
        self.paths = LifeContextPaths.current()
        self.paths.ensure_private_home()

        self.vault_root = self.base / "vault"
        self.journal = self.vault_root / "Journal"
        self.entries = self.journal / "entries"
        self.notes = self.vault_root / "Notes"
        for directory in (self.vault_root, self.journal, self.entries, self.notes):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            directory.chmod(0o700)
        self.paths.config_path.write_text(
            yaml.safe_dump(
                {
                    "vault": {
                        "root": str(self.vault_root),
                        "journal_path": str(self.journal),
                        "relationship_roots": ["Notes"],
                    },
                    "limits": {
                        "max_text_bytes": 4096,
                        "max_query_bytes": 256,
                        "max_result_count": 5,
                        "max_response_bytes": 65536,
                        "max_relationship_files": 100,
                        "max_relationship_file_bytes": 16384,
                        "max_relationship_bytes": 65536,
                        "max_relationship_scan_ms": 2000,
                    },
                }
            ),
            encoding="utf-8",
        )
        self.paths.config_path.chmod(0o600)

        self.client = ControlledClient()
        self._test_request_key = 0
        self.application = None
        self.api = create_app(application_factory=self._make_application)
        self.server = uvicorn.Server(
            uvicorn.Config(self.api, host="127.0.0.1", port=0, log_level="error", access_log=False)
        )
        self.server_thread = threading.Thread(target=self.server.run, daemon=True)
        self.server_thread.start()
        deadline = time.monotonic() + 5
        while not self.server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(self.server.started, "uvicorn failed to start on a temporary loopback port")
        self.base_url = f"http://127.0.0.1:{self.server.servers[0].sockets[0].getsockname()[1]}"

    def tearDown(self) -> None:
        if getattr(self, "server", None) is not None and self.server.started:
            self.server.should_exit = True
            self.server_thread.join(timeout=5)
        self.temporary.cleanup()

    def test_wiki_rebuild_http_empty_inventory_and_idempotent_replay(self):
        headers = {"Content-Type": "application/json", "Idempotency-Key": "wiki-http-rebuild",
                   "Authorization": "Bearer " + self.api.state.api_token}
        status, _, result = self._request("/v1/wiki/rebuild", method="POST", data=b"{}", headers=headers)
        self.assertEqual(status, 200)
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["summary"]["item_count"], 0)
        _, _, replay = self._request("/v1/wiki/rebuild", method="POST", data=b"{}", headers=headers)
        self.assertEqual(replay, result)
        status, _, _ = self._request("/v1/wiki/rebuild", method="POST", data=b'{"version":1}',
                                     headers={**headers, "Idempotency-Key": "wiki-invalid-rebuild"})
        self.assertEqual(status, 422)

    def test_wiki_rebuild_http_provider_failure_exposes_operation_id(self):
        manifest = SourceManifest("doc_rebuild", 1, hashlib.sha256(b"source").hexdigest(), 6, {})
        catalog = LifeContextCatalog(self.paths)
        self.addCleanup(catalog.close)
        catalog.register_manifest(manifest, layer="document")
        catalog.register_document_metadata("doc_rebuild", 1, filename="source.txt", title="Source", mime_type="text/plain")
        catalog.record_extraction("doc_rebuild", 1, 1, original_sha256=manifest.sha256,
                                  extractor="text", extractor_version="1", config_sha256="c" * 64,
                                  artifact_path="derived/doc_rebuild/v1/r1.json", artifact_sha256="a" * 64,
                                  status="completed", passage_count=1)
        catalog.record_provider_chunk("chunk_rebuild", "doc_rebuild", 1, 1,
                                      passage_start=0, passage_end=1, content_sha256="b" * 64)
        catalog.update_provider_chunk("chunk_rebuild", expected_revision=1,
                                      submission_state="submitted", completion_state="completed")
        for state in ("running", "completed"):
            stage = catalog.get_document_stage("doc_rebuild", 1, "indexing")
            catalog.transition_document_stage("doc_rebuild", 1, "indexing", expected_state=stage["state"],
                                              expected_revision=stage["revision"], state=state)
        with patch.object(self.application.wiki, "update_for_document", new=AsyncMock(side_effect=WikiError("unavailable"))) as update:
            headers = {"Content-Type": "application/json", "Idempotency-Key": "wiki-provider-failure",
                       "Authorization": "Bearer " + self.api.state.api_token}
            body = b'{"source_id":"doc_rebuild","version":1}'
            status, response_headers, result = self._request("/v1/wiki/rebuild", method="POST", data=body, headers=headers)
            self.assertEqual(status, 200)
            self.assertEqual(result["state"], "retryable_failed")
            self.assertEqual(result["summary"]["error_code"], "provider_unavailable")
            self.assertEqual(response_headers["x-lifeweave-operation-id"], result["operation_id"])
            _, _, replay = self._request("/v1/wiki/rebuild", method="POST", data=body, headers=headers)
            self.assertEqual(replay, result)
            update.assert_awaited_once()

    def test_wiki_lint_and_review_queue_over_http(self):
        status, _, wiki = self._request("/v1/wiki/status")
        self.assertEqual((status, wiki["semantic_support_verified"], wiki["summary_model_generated"]), (200, False, True))
        status, _, lint = self._request("/v1/wiki/lint")
        self.assertEqual((status, lint), (200, {"findings": [], "counts": {"error": 0, "warning": 0, "info": 0}, "omitted": 0,
                                                "documents_omitted": 0}))
        digest = hashlib.sha256(b"source").hexdigest()
        catalog = LifeContextCatalog(self.paths)
        self.addCleanup(catalog.close)
        catalog.register_manifest(SourceManifest("doc_review", 1, digest, 6, {}), layer="document")
        catalog.register_document_metadata("doc_review", 1, filename="source.txt", title="Source", mime_type="text/plain")
        catalog.record_extraction("doc_review", 1, 1, original_sha256=digest, extractor="text", extractor_version="1",
                                  config_sha256="c" * 64, artifact_path="derived/doc_review/v1/r1.json",
                                  artifact_sha256="a" * 64, status="completed", passage_count=1)
        catalog.record_provider_chunk("chunk_review", "doc_review", 1, 1, passage_start=0, passage_end=1, content_sha256="b" * 64)
        catalog.update_provider_chunk("chunk_review", expected_revision=1, submission_state="submitted", completion_state="completed")
        for state in ("running", "completed"):
            stage = catalog.get_document_stage("doc_review", 1, "indexing")
            catalog.transition_document_stage("doc_review", 1, "indexing", expected_state=stage["state"],
                                              expected_revision=stage["revision"], state=state)
        dependency = {"source_id": "doc_review", "source_version": 1, "extraction_revision": 1,
                      "passage_start": 0, "passage_end": 1, "original_sha256": digest}
        catalog.create_wiki_page("topic_review", title="Review")
        catalog.commit_wiki_revision("topic_review", expected_revision=0, artifact_path="wiki_revisions/topic_review/r1.json",
                                     artifact_sha256="d" * 64, dependencies=[dependency])
        claim = {"text": "source", "citations": [{**dependency, "quote": "source"}]}
        catalog.record_wiki_review("review_http", "topic_review", 1, code="contradiction", details={"sources": {
            "left": claim, "right": claim, "left_date": "2024-01-01", "right_date": "2025-01-01"}})

        _, _, lint = self._request("/v1/wiki/lint")
        codes = {item["code"] for item in lint["findings"]}
        self.assertTrue({"contradiction_pending", "revision_unreadable", "source_page_missing"} <= codes, codes)
        status, _, listed = self._request("/v1/wiki/reviews?page=topic_review")
        self.assertEqual(status, 200)
        self.assertEqual([item["review_id"] for item in listed["reviews"]], ["review_http"])
        self.assertIsNone(listed["reviews"][0]["resolution"])

        headers = {"Idempotency-Key": "resolve-http"}
        status, response_headers, resolved = self._json("/v1/wiki/reviews/review_http/resolve",
                                                        {"note": "Both dates stand."}, headers=headers)
        self.assertEqual(status, 200, resolved)
        self.assertEqual(resolved["state"], "completed")
        self.assertEqual(resolved["summary"]["item_count"], 1)
        self.assertEqual(response_headers["x-lifeweave-operation-id"], resolved["operation_id"])
        self.assertEqual(resolved["review"]["state"], "resolved")
        self.assertEqual(resolved["review"]["resolution"]["note"], "Both dates stand.")
        _, _, replay = self._json("/v1/wiki/reviews/review_http/resolve", {"note": "Both dates stand."}, headers=headers)
        self.assertEqual(replay, resolved)
        self._assert_error(self._json("/v1/wiki/reviews/review_http/resolve", {"note": "Both dates stand."},
                                      headers={"Idempotency-Key": "resolve-http-again"}), 409, "conflict")
        self._assert_error(self._json("/v1/wiki/reviews/review_missing/resolve", {"note": "No such review."},
                                      headers={"Idempotency-Key": "resolve-missing"}), 404, "source_not_found")
        for body in ({}, {"note": ""}, {"note": "x", "winner": "left"}):
            self._assert_error(self._json("/v1/wiki/reviews/review_http/resolve", body,
                                          headers={"Idempotency-Key": "resolve-invalid"}), 422, "invalid_request")
        self._assert_error(self._json("/v1/wiki/reviews/review_http/resolve", {"note": "Missing key."}), 422, "invalid_request")
        _, _, pending = self._request("/v1/wiki/reviews")
        self.assertEqual(pending, {"reviews": [], "omitted": 0})
        _, _, done = self._request("/v1/wiki/reviews?state=resolved")
        self.assertEqual(done["reviews"], [resolved["review"]])
        self.assertEqual(catalog.get_wiki_revision("topic_review", 1)["artifact_sha256"], "d" * 64)
        self._assert_error(self._request("/v1/wiki/reviews?state=open"), 422, "invalid_request")

    def test_brief_reads_and_projects_over_http(self):
        status, _, brief = self._request("/v1/brief?date=2026-10-16")
        self.assertEqual(status, 200, brief)
        self.assertEqual((brief["date"], brief["documents"], brief["journal_entries"], brief["projection"]),
                         ("2026-10-16", {"items": [], "omitted": 0}, 0, None))
        self.assertTrue(brief["since"].endswith("Z") and brief["until"].endswith("Z"))
        self._json("/v1/retain", {"source_id": "src_brief", "source_version": 1, "content": "brief entry"})
        _, _, today = self._request("/v1/brief")
        self.assertEqual(today["journal_entries"], 1)
        _, _, since = self._request("/v1/brief?since=2000-01-01T00:00:00Z")
        self.assertIsNone(since["date"])
        self.assertEqual(since["journal_entries"], 1)
        for query in ("date=2026-13-01", "date=2026-10-16&since=2026-10-16T00:00:00Z", "since=2999-01-01T00:00:00Z"):
            with self.subTest(query=query):
                self._assert_error(self._request("/v1/brief?" + query), 422, "invalid_request")
        status, _, projected = self._request("/v1/brief/project?date=2026-10-16", method="POST")
        self.assertEqual(status, 200, projected)
        self.assertEqual(projected["projection"], {"brief": "completed", "log": "completed"})
        brief_file = self.journal / "wiki" / "Briefs" / "2026-10-16 - Brief.md"
        self.assertTrue(brief_file.read_text().startswith("# Brief 2026-10-16\n"))
        self.assertTrue((self.journal / "wiki" / "Log.md").read_text().startswith("# Log\n"))
        self.assertNotIn(str(self.base), brief_file.read_text() + json.dumps(projected))
        self._assert_error(self._request("/v1/brief/project?date=2026-10-16", method="POST", authenticated=False),
                           401, "unauthorized")

    def _make_application(self) -> JournalApplication:
        vault = ManagedVault(self.vault_root, self.journal)
        self.application = JournalApplication(
            paths=self.paths,
            hindsight=LifeContextHindsightAdapter(self.client, bank_id="lifeweave"),
            vault=vault,
        )
        return self.application

    def _request(self, path: str, *, method: str = "GET", data: bytes | None = None,
                 headers: dict[str, str] | None = None,
                 auto_idempotency: bool = True,
                 authenticated: bool = True) -> tuple[int, dict[str, str], object]:
        selected = dict(headers or {})
        if authenticated and not any(name.casefold() == "authorization" for name in selected):
            selected["Authorization"] = "Bearer " + self.api.state.api_token
        mutation_paths = {
            "/v1/retain", "/v1/curation", "/v1/relationships/scan",
            "/v1/imports", "/v1/exports",
        }
        path_only = path.partition("?")[0]
        is_decision = path_only.startswith("/v1/relationships/") and path_only.endswith("/decision")
        has_key = any(name.casefold() == "idempotency-key" for name in selected)
        if auto_idempotency and method.upper() == "POST" and (path_only in mutation_paths or is_decision) and not has_key:
            self._test_request_key += 1
            selected["Idempotency-Key"] = f"test-request-{self._test_request_key}"
        request = urllib.request.Request(self.base_url + path, data=data, method=method, headers=selected)
        try:
            response = urllib.request.urlopen(request, timeout=3)
        except urllib.error.HTTPError as exc:
            response = exc
        body = response.read()
        try:
            value: object = json.loads(body.decode("utf-8"))
        except (UnicodeError, ValueError):
            value = body
        return response.status, {key.casefold(): val for key, val in response.headers.items()}, value

    def _json(self, path: str, value: object, *, method: str = "POST",
              headers: dict[str, str] | None = None,
              auto_idempotency: bool = True) -> tuple[int, dict[str, str], object]:
        selected = {"Content-Type": "application/json"}
        selected.update(headers or {})
        return self._request(
            path,
            method=method,
            data=json.dumps(value).encode("utf-8"),
            headers=selected,
            auto_idempotency=auto_idempotency,
        )

    def _duplicate_idempotency_request(self, path: str, value: object):
        from urllib.parse import urlsplit

        target = urlsplit(self.base_url)
        connection = http.client.HTTPConnection(target.hostname, target.port, timeout=3)
        body = json.dumps(value).encode("utf-8")
        request_target = path
        connection.putrequest("POST", request_target, skip_host=True)
        connection.putheader("Host", f"{target.hostname}:{target.port}")
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Authorization", "Bearer " + self.api.state.api_token)
        connection.putheader("Idempotency-Key", "duplicate-first")
        connection.putheader("Idempotency-Key", "duplicate-second")
        connection.putheader("Content-Length", str(len(body)))
        connection.endheaders(body)
        response = connection.getresponse()
        raw = response.read()
        connection.close()
        return response.status, json.loads(raw.decode("utf-8"))

    def _assert_error(self, result: tuple[int, dict[str, str], object], status: int, code: str) -> dict[str, object]:
        actual_status, headers, body = result
        self.assertEqual(actual_status, status)
        self.assertNotIn("x-lifeweave-operation-id", headers)
        self.assertIsInstance(body, dict)
        self.assertEqual(set(body), {"request_id", "error_code", "retryable"})
        self.assertEqual(body["error_code"], code)
        self.assertEqual(len(body["request_id"]), 32)
        self.assertEqual(headers.get("x-request-id"), body["request_id"])
        return body

    def test_openapi_lists_bounded_contract_and_redacted_errors(self) -> None:
        status, _, schema = self._request("/openapi.json")
        self.assertEqual(status, 200)
        self.assertEqual(
            set(schema["paths"]),
            {
                "/healthz", "/v1/status", "/v1/retain", "/v1/recall", "/v1/reflect",
                "/v1/projections/reconcile",
                "/v1/evidence/{source_id}", "/v1/curation", "/v1/relationships",
                "/v1/relationships/scan", "/v1/relationships/{edge_id}/decision",
                "/v1/imports", "/v1/exports", "/v1/operations/{operation_id}",
                "/v1/documents", "/v1/documents/{source_id}",
                "/v1/documents/{source_id}/original",
                "/v1/documents/operations/{operation_id}/retry",
                "/v1/categories", "/v1/categories/proposals",
                "/v1/categories/proposals/{proposal_id}/approve",
                "/v1/categories/proposals/{proposal_id}/reject",
                "/v1/categories/proposals/{proposal_id}/redirect",
                "/v1/categories/{category_id}/rename",
                "/v1/categories/{category_id}/alias",
                "/v1/categories/{category_id}/merge", "/v1/categories/reassign",
                "/v1/wiki", "/v1/wiki/{page_id}", "/v1/wiki/{page_id}/history",
                "/v1/wiki/rebuild", "/v1/wiki/status", "/v1/wiki/lint", "/v1/wiki/reviews",
                "/v1/wiki/reviews/{review_id}/resolve", "/v1/brief", "/v1/brief/project",
            },
        )
        parameters = schema["paths"]["/v1/projections/reconcile"]["post"]["parameters"]
        limit = next(item["schema"] for item in parameters if item["name"] == "limit")
        self.assertEqual((limit["minimum"], limit["maximum"]), (1, 4096))
        self.assertNotIn("HTTPValidationError", schema["components"]["schemas"])
        self.assertIn("RetainRequest", schema["components"]["schemas"])
        self.assertIn(
            "ProvenanceInput",
            schema["components"]["schemas"],
        )
        self.assertEqual(
            set(schema["components"]["schemas"]["OperationSummary"]["properties"]),
            {"sources", "item_count", "error_code"},
        )
        self.assertNotIn("path", repr(schema["components"]["schemas"]["ExactEvidenceResponse"]).casefold())
        max_results_schema = schema["components"]["schemas"]["RecallRequest"]["properties"]["max_results"]
        self.assertIn("maximum", repr(max_results_schema))

        def assert_local_refs_resolve(value: object, path: str = "#") -> None:
            if isinstance(value, dict):
                reference = value.get("$ref")
                if isinstance(reference, str) and reference.startswith("#/"):
                    target = schema
                    for segment in reference[2:].split("/"):
                        segment = segment.replace("~1", "/").replace("~0", "~")
                        self.assertIsInstance(target, dict, f"{path}: invalid $ref {reference}")
                        self.assertIn(segment, target, f"{path}: dangling $ref {reference}")
                        target = target[segment]
                for key, child in value.items():
                    assert_local_refs_resolve(child, f"{path}/{key}")
            elif isinstance(value, list):
                for index, child in enumerate(value):
                    assert_local_refs_resolve(child, f"{path}/{index}")

        assert_local_refs_resolve(schema)
        for operations in schema["paths"].values():
            for operation in operations.values():
                if not isinstance(operation, dict) or "responses" not in operation:
                    continue
                for response in operation["responses"].values():
                    ref = response.get("content", {}).get("application/json", {}).get("schema", {}).get("$ref", "")
                    self.assertNotIn("HTTPValidationError", ref)

        import_operation = schema["paths"]["/v1/imports"]["post"]
        self.assertEqual(
            set(import_operation["requestBody"]["content"]),
            {"application/octet-stream"},
        )
        self.assertEqual(
            import_operation["requestBody"]["content"]["application/octet-stream"]["schema"],
            {"type": "string", "format": "binary"},
        )
        export_response = schema["paths"]["/v1/exports"]["post"]["responses"]["200"]["content"]
        self.assertEqual(set(export_response), {BUNDLE_MEDIA_TYPE})
        self.assertEqual(
            export_response[BUNDLE_MEDIA_TYPE]["schema"],
            {"type": "string", "format": "binary"},
        )

        for path in (
            "/v1/retain", "/v1/curation", "/v1/relationships/scan",
            "/v1/relationships/{edge_id}/decision", "/v1/imports", "/v1/exports",
        ):
            operation = schema["paths"][path]["post"]
            parameter = next(
                parameter for parameter in operation["parameters"]
                if parameter["name"] == "Idempotency-Key"
            )
            self.assertTrue(parameter["required"], path)
        for path in ("/v1/recall", "/v1/reflect"):
            self.assertNotIn(
                "Idempotency-Key",
                {parameter["name"] for parameter in schema["paths"][path]["post"].get("parameters", [])},
            )

    def test_mutations_require_one_bounded_printable_idempotency_key(self) -> None:
        payload = {"source_id": "src_missing_idempotency", "source_version": 1, "content": "header gate"}
        calls_before = len(self.client.retain_calls)
        self._assert_error(
            self._json("/v1/retain", payload, auto_idempotency=False),
            422,
            "invalid_request",
        )
        self._assert_error(
            self._json(
                "/v1/retain", payload,
                headers={"Idempotency-Key": "has spaces"},
                auto_idempotency=False,
            ),
            422,
            "invalid_request",
        )
        self._assert_error(
            self._json(
                "/v1/retain", payload,
                headers={"Idempotency-Key": "k" * 256},
                auto_idempotency=False,
            ),
            422,
            "invalid_request",
        )
        status, error = self._duplicate_idempotency_request("/v1/retain", payload)
        self.assertEqual(status, 422)
        self.assertEqual(error["error_code"], "invalid_request")
        self.assertEqual(len(self.client.retain_calls), calls_before)
        with sqlite3.connect(self.paths.catalog_path) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM source_versions WHERE source_id=?",
                    (payload["source_id"],),
                ).fetchone()[0],
                0,
            )

        self._assert_error(
            self._json(
                "/v1/curation", {"action": "reindex", "source_id": "src_read_keyless"},
                auto_idempotency=False,
            ),
            422,
            "invalid_request",
        )
        self._assert_error(
            self._json(
                "/v1/imports",
                {"bundle_format": "lifeweave", "bundle_version": 1, "bundle_sha256": "f" * 64},
                auto_idempotency=False,
            ),
            422,
            "invalid_request",
        )
        self._assert_error(
            self._json("/v1/exports", {"source_ids": ["src_read_keyless"]}, auto_idempotency=False),
            422,
            "invalid_request",
        )
        status, _, recalled = self._json("/v1/recall", {"query": "keyless read"}, auto_idempotency=False)
        self.assertEqual(status, 200)
        self.assertEqual(recalled["results"], [])

    def test_retain_same_key_replays_safe_result_and_changed_request_conflicts(self) -> None:
        source_text = "retained-secret-sentinel"
        payload = {
            "source_id": "src_idempotent_retain",
            "source_version": 1,
            "content": source_text,
            "provenance": {"source": "test", "source_ref": "private-ref"},
        }
        headers = {"Idempotency-Key": "retain-idempotency-key"}
        calls_before = len(self.client.retain_calls)
        first_status, first_headers, first = self._json("/v1/retain", payload, headers=headers)
        second_status, second_headers, second = self._json("/v1/retain", payload, headers=headers)
        self.assertEqual(first_status, 200)
        self.assertEqual(second_status, 200)
        self.assertEqual(second, first)
        self.assertEqual(
            set(first),
            {"source_id", "source_version", "sha256", "size_bytes", "submission_state"},
        )
        operation_id = first_headers["x-lifeweave-operation-id"]
        self.assertEqual(second_headers["x-lifeweave-operation-id"], operation_id)
        self.assertNotIn("operation_id", first)
        self.assertEqual(len(self.client.retain_calls), calls_before + 1)

        changed_content = {**payload, "content": "changed-private-sentinel"}
        self._assert_error(
            self._json("/v1/retain", changed_content, headers=headers), 409, "conflict"
        )
        changed_metadata = {
            **payload,
            "provenance": {"source": "other", "source_ref": "private-ref"},
        }
        self._assert_error(
            self._json("/v1/retain", changed_metadata, headers=headers), 409, "conflict"
        )
        self.assertEqual(len(self.client.retain_calls), calls_before + 1)

        with sqlite3.connect(self.paths.catalog_path) as connection:
            row = connection.execute(
                "SELECT operation_id, idempotency_key_digest, request_digest, result_json, error_code "
                "FROM operations"
            ).fetchone()
        self.assertNotIn(source_text, repr(row))
        self.assertNotIn("private-ref", repr(row))
        self.assertNotIn(headers["Idempotency-Key"], repr(row))
        self.assertEqual(operation_id, row[0])
        status, _, operation = self._request(f"/v1/operations/{operation_id}")
        self.assertEqual(status, 200)
        self.assertEqual(operation["state"], "completed")
        self.assertNotIn(source_text, repr(operation))
        self.assertNotIn("private-ref", repr(operation))

    def test_each_curation_action_exposes_its_durable_operation_id(self) -> None:
        source_id = "src_api_curation_headers"
        status, _, _ = self._json(
            "/v1/retain",
            {"source_id": source_id, "source_version": 1, "content": "curation source"},
        )
        self.assertEqual(status, 200)

        def curate(action: str, key: str, **fields: object) -> dict[str, object]:
            status, headers, value = self._json(
                "/v1/curation",
                {"action": action, "source_id": source_id, **fields},
                headers={"Idempotency-Key": key},
            )
            self.assertEqual(status, 200, repr(value))
            self.assertIsInstance(value, dict)
            self.assertEqual(set(value), {"operation_id", "state", "summary"})
            self.assertEqual(headers["x-lifeweave-operation-id"], value["operation_id"])
            self.assertEqual(value["state"], "completed")
            return value

        corrected = curate("correct", "curation-correct", expected_version=1, content="corrected")
        self.assertIn("sources", corrected["summary"])
        reindexed = curate("reindex", "curation-reindex", expected_version=2)
        self.assertIn("sources", reindexed["summary"])
        prepared_forget = curate("prepare_forget", "curation-prepare-forget", expected_version=2)
        with patch.object(self.application, "_verify_provider_document", new=AsyncMock()), patch.object(
            self.application.hindsight,
            "delete_document",
            new=AsyncMock(return_value={"success": True}),
        ):
            forgotten = curate(
                "forget",
                "curation-forget",
                expected_version=2,
                prepared_operation_id=prepared_forget["operation_id"],
            )
            prepared_delete = curate(
                "prepare_delete", "curation-prepare-delete", expected_version=2
            )
            deleted = curate(
                "delete",
                "curation-delete",
                expected_version=2,
                prepared_operation_id=prepared_delete["operation_id"],
            )
        self.assertEqual(len({
            corrected["operation_id"], reindexed["operation_id"], prepared_forget["operation_id"],
            forgotten["operation_id"], prepared_delete["operation_id"], deleted["operation_id"],
        }), 6)

    def test_every_versioned_route_requires_the_token_and_liveness_stays_public(self) -> None:
        paths = self.api.openapi()["paths"]
        versioned = {path: operations for path, operations in paths.items() if path.startswith("/v1/")}
        self.assertGreater(len(versioned), 30)
        self.assertEqual(set(paths) - set(versioned), {"/healthz"})
        for path, operations in versioned.items():
            target = re.sub(r"\{[^}]+\}", "x", path)
            for method in sorted(operations):
                method = method.upper()
                data = b"{}" if method == "POST" else None
                with self.subTest(method=method, path=path):
                    self._assert_error(self._request(
                        target, method=method, data=data, authenticated=False,
                        headers={"Content-Type": "application/json"},
                    ), 401, "unauthorized")
                    self._assert_error(self._request(
                        target, method=method, data=data,
                        headers={"Content-Type": "application/json", "Authorization": "Bearer " + "0" * 64},
                    ), 401, "unauthorized")
        for target in ("/v1", "/v1/unknown"):
            self._assert_error(self._request(target, authenticated=False), 401, "unauthorized")

        self.application.custody.write_version("src_liveness", 1, b"private liveness sentinel", provenance={})
        status, headers, liveness = self._request("/healthz", authenticated=False)
        self.assertEqual((status, liveness), (200, {"status": "ok"}))
        self.assertEqual(set(headers) & {"x-lifeweave-operation-id", "set-cookie"}, set())
        self._assert_error(self._request("/openapi.json", authenticated=False), 401, "unauthorized")
        self._assert_error(self._request("/", authenticated=False), 401, "unauthorized")
        status, _, _ = self._request("/openapi.json")
        self.assertEqual(status, 200)

    def test_metadata_free_document_keeps_original_content_out_of_evidence(self) -> None:
        manifest = self.application.custody.write_version(
            "doc_partial", 1, b"private interrupted document", provenance={}
        )
        catalog = LifeContextCatalog(self.paths)
        self.addCleanup(catalog.close)
        catalog.register_manifest(manifest, layer="document")
        self.assertIsNone(catalog.get_document_metadata("doc_partial", 1))
        self._assert_error(self._request(
            "/v1/evidence/doc_partial?version=1&include_content=true", authenticated=False
        ), 401, "unauthorized")
        self._assert_error(self._request(
            "/v1/evidence/doc_partial?version=1&include_content=true"
        ), 422, "invalid_request")
        status, _, evidence = self._request("/v1/evidence/doc_partial?version=1")
        self.assertEqual(status, 200)
        self.assertNotIn("content", evidence)
        self.assertNotIn("private interrupted document", repr(evidence))

    def test_metadata_free_terminal_document_identity_keeps_authentication(self) -> None:
        catalog = LifeContextCatalog(self.paths)
        self.addCleanup(catalog.close)
        for lifecycle in ("forgotten", "deleted"):
            with self.subTest(lifecycle=lifecycle):
                with catalog.transaction() as connection:
                    connection.execute(
                        "INSERT INTO sources(source_id,layer) VALUES(?,?)",
                        ("doc_" + lifecycle, "document"),
                    )
                digest = "a" * 64
                operation = catalog.begin_operation(lifecycle, digest, "import")
                catalog.initialize_transfer(operation["operation_id"], digest, 1, [{
                    "source_id": "doc_" + lifecycle, "source_version": 1,
                    "lifecycle_state": lifecycle,
                    "sha256": digest if lifecycle == "forgotten" else None,
                }])
                catalog.transition_operation(operation["operation_id"], "pending", "running")
                catalog.record_transfer_tombstone(
                    operation["operation_id"], "doc_" + lifecycle, 1, lifecycle
                )
                self.assertEqual(catalog.list_documents(), [])
                self._assert_error(self._request("/v1/status", authenticated=False), 401, "unauthorized")
                self._assert_error(self._request(
                    "/v1/exports", method="POST", data=b"{}", authenticated=False
                ), 401, "unauthorized")

    def test_http_retain_exact_evidence_and_stream_body_limits(self) -> None:
        original = "historic source text"
        status, _, retained = self._json(
            "/v1/retain",
            {
                "source_id": "src_api_history",
                "source_version": 1,
                "content": original,
                "provenance": {"source": "test", "source_ref": "message-1", "basename": "history.md"},
            },
        )
        self.assertEqual(status, 200, repr(retained))
        checksum = hashlib.sha256(original.encode("utf-8")).hexdigest()
        self.assertEqual(retained["sha256"], checksum)

        status, _, uploaded = self._request(
            "/v1/retain?source_id=src_api_history&source_version=2",
            method="POST",
            data=b"second streamed version",
            headers={"Content-Type": "application/octet-stream", "X-Source-Basename": "current.md"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(uploaded["source_version"], 2)

        status, _, safe = self._request("/v1/evidence/src_api_history?version=1")
        self.assertEqual(status, 200)
        self.assertNotIn("content", safe)
        self.assertEqual(safe["source_version"], 1)
        self.assertEqual(safe["sha256"], checksum)
        self.assertEqual(safe["citations"][0]["source_version"], 1)
        self.assertEqual(safe["metadata"]["basename"], "history.md")
        self.assertNotIn("message-1", repr(safe))
        self.assertNotIn("second streamed version", repr(safe))

        status, _, visible = self._request("/v1/evidence/src_api_history?version=1&include_content=true")
        self.assertEqual(status, 200)
        self.assertEqual(visible["content"], original)
        self.assertEqual(visible["citations"][0]["sha256"], checksum)

        self._assert_error(
            self._request(
                "/v1/retain?source_id=src_too_large&source_version=1",
                method="POST",
                data=b"x" * 4097,
                headers={"Content-Type": "application/octet-stream"},
            ),
            413,
            "body_too_large",
        )
        self._assert_error(
            self._json("/v1/retain", {
                "source_id": "src_extra", "source_version": 1, "content": "secret",
                "unexpected": "/tmp/private-source",
            }),
            422,
            "invalid_request",
        )
        self._assert_error(
            self._request(
                "/v1/retain?source_id=src_name&source_version=1",
                method="POST",
                data=b"content",
                headers={"Content-Type": "application/octet-stream", "X-Source-Basename": "../secret.md"},
            ),
            422,
            "invalid_request",
        )

    def test_recall_reflect_readiness_operation_and_unavailable_routes(self) -> None:
        source_text = "locally held support"
        self._json("/v1/retain", {"source_id": "src_api_queries", "source_version": 1, "content": source_text})
        checksum = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
        self.client.recall_response = {
            "results": [{
                "id": "fact-1", "text": "remembered text", "document_id": "src_api_queries",
                "metadata": {"source_version": "1", "sha256": checksum},
            }]
        }
        self.client.memory.response = {
            "id": "fact-1",
            "document_id": "src_api_queries",
            "metadata": {"source_version": "1", "sha256": checksum},
            "state": "valid",
        }
        status, recall_headers, recalled = self._json("/v1/recall", {"query": "lookup", "max_results": 1})
        self.assertEqual(status, 200)
        self.assertNotIn("x-lifeweave-operation-id", recall_headers)
        self.assertEqual(recalled["results"][0]["evidence"]["source_version"], 1)
        self.assertEqual(recalled["results"][0]["evidence"]["sha256"], checksum)
        self.assertNotIn("id", recalled["results"][0])
        self.assertEqual(recalled["omitted_unresolvable"], 0)

        self.client.recall_response = {"results": [
            self.client.recall_response["results"][0],
            {"id": "fact-unowned", "text": "unowned provider text", "document_id": "src_not_held",
             "metadata": {"source_version": "1"}},
        ]}
        status, _, partial = self._json("/v1/recall", {"query": "lookup", "max_results": 2})
        self.assertEqual(status, 200)
        self.assertEqual(len(partial["results"]), 1)
        self.assertEqual(partial["omitted_unresolvable"], 1)
        self.assertNotIn("unowned provider text", json.dumps(partial))

        status, _, reflected = self._json("/v1/reflect", {"query": "explain", "max_results": 1})
        self.assertEqual(status, 200)
        self.assertEqual(reflected["evidence"][0]["source_id"], "src_api_queries")
        self.assertEqual(reflected["evidence"][0]["source_version"], 1)

        status, liveness_headers, liveness = self._request("/healthz")
        self.assertEqual((status, liveness), (200, {"status": "ok"}))
        self.assertNotIn("x-lifeweave-operation-id", liveness_headers)
        self.application.hindsight.capabilities["arecall"] = False
        status, _, readiness = self._request("/v1/status")
        self.assertEqual(status, 200)
        self.assertFalse(readiness["ready"])
        self.assertEqual(readiness["max_file_bytes"], JournalLimits().max_file_bytes)

        error = self._assert_error(self._request("/v1/operations/op_pending"), 503, "operation_status_unavailable")
        self.assertFalse(error["retryable"])
        curation_payload = {
            "action": "correct",
            "source_id": "src_api_queries",
            "expected_version": 1,
            "content": "corrected api source sentinel",
        }
        curation_key = "api-correct-key"
        status, _, curated = self._json(
            "/v1/curation",
            curation_payload,
            headers={"Idempotency-Key": curation_key},
        )
        self.assertEqual(status, 200, repr(curated))
        self.assertEqual(curated["state"], "completed")
        self.assertNotIn(curation_payload["content"], repr(curated))
        replay_status, _, replayed = self._json(
            "/v1/curation",
            curation_payload,
            headers={"Idempotency-Key": curation_key},
        )
        self.assertEqual(replay_status, 200)
        self.assertEqual(replayed, curated)
        prepare_payload = {
            "action": "prepare_forget",
            "source_id": "src_api_queries",
            "expected_version": 2,
        }
        prepare_key = "api-prepare-forget-key"
        prepare_status, _, prepared = self._json(
            "/v1/curation", prepare_payload,
            headers={"Idempotency-Key": prepare_key},
        )
        self.assertEqual(prepare_status, 200, repr(prepared))
        self.assertEqual(prepared["state"], "completed")
        replay_status, _, replayed_prepare = self._json(
            "/v1/curation", prepare_payload,
            headers={"Idempotency-Key": prepare_key},
        )
        self.assertEqual((replay_status, replayed_prepare), (200, prepared))
        self._assert_error(
            self._json(
                "/v1/curation", prepare_payload, auto_idempotency=False,
            ),
            422,
            "invalid_request",
        )
        self._assert_error(
            self._json(
                "/v1/curation",
                {**prepare_payload, "action": "prepare_delete"},
                headers={"Idempotency-Key": prepare_key},
            ),
            409,
            "conflict",
        )
        self._assert_error(
            self._json("/v1/curation", {"action": "custom", "source_id": "src_api_queries"}),
            422,
            "invalid_request",
        )
        self._assert_error(
            self._request(
                "/v1/imports",
                method="POST",
                data=b"not a bundle",
                headers={"Content-Type": "application/octet-stream"},
            ),
            422,
            "invalid_request",
        )
        status, export_headers, exported = self._json(
            "/v1/exports", {"source_ids": ["src_api_queries"]}
        )
        self.assertEqual(status, 200)
        self.assertEqual(export_headers["content-type"], BUNDLE_MEDIA_TYPE)
        self.assertEqual(decode_bundle(exported)[0].source_id, "src_api_queries")

        self.application.hindsight.capabilities["arecall"] = True
        self.client.error = RuntimeError("secret token at /tmp/provider-internal")
        status, _, failed = self._json("/v1/recall", {"query": "provider error"})
        self.assertEqual(status, 503, repr(failed))
        self.assertEqual(failed["error_code"], "recall_failed")
        self.assertNotIn("secret", repr(failed))
        self.assertNotIn("/tmp", repr(failed))
        self.assertNotIn("provider-internal", repr(failed))

    def test_import_and_export_transfer_binary_bundles_over_loopback(self) -> None:
        content = b"imported through bounded loopback bytes"
        manifest = SourceManifest(
            "src_api_import",
            1,
            hashlib.sha256(content).hexdigest(),
            len(content),
            {"source": "api-test", "basename": "import.md"},
        )
        bundle = encode_bundle(
            [BundleRecord("src_api_import", 1, "active", manifest, content)],
            limits=self.application.limits,
        )
        self.application.hindsight.get_document = AsyncMock(return_value={})
        key = "api-import-bundle-key"
        status, headers, imported = self._request(
            "/v1/imports",
            method="POST",
            data=bundle,
            headers={
                "Content-Type": "application/octet-stream",
                "Idempotency-Key": key,
            },
        )
        self.assertEqual(status, 200, repr(imported))
        self.assertEqual(imported["state"], "completed")
        self.assertEqual(imported["summary"]["item_count"], 1)
        operation_id = headers["x-lifeweave-operation-id"]
        self.assertEqual(imported["operation_id"], operation_id)
        self.assertEqual(len(self.client.retain_calls), 1)
        self.assertEqual(self.client.retain_calls[0][0], "src_api_import")
        self.assertEqual(self.client.retain_calls[0][1], content.decode("utf-8"))

        replay_status, replay_headers, replayed = self._request(
            "/v1/imports",
            method="POST",
            data=bundle,
            headers={
                "Content-Type": "application/octet-stream",
                "Idempotency-Key": key,
            },
        )
        self.assertEqual((replay_status, replayed), (200, imported))
        self.assertEqual(replay_headers["x-lifeweave-operation-id"], operation_id)
        self.assertEqual(len(self.client.retain_calls), 1)

        status, export_headers, exported = self._json(
            "/v1/exports", {"source_ids": ["src_api_import"]}
        )
        self.assertEqual(status, 200)
        self.assertEqual(export_headers["content-type"], BUNDLE_MEDIA_TYPE)
        exported_record = decode_bundle(exported)[0]
        self.assertEqual(exported_record.source_id, "src_api_import")
        self.assertEqual(exported_record.manifest.to_bytes(), manifest.to_bytes())
        self.assertEqual(exported_record.original_bytes, content)

        changed_content = b"changed request"
        changed_manifest = SourceManifest(
            "src_changed_import",
            1,
            hashlib.sha256(changed_content).hexdigest(),
            len(changed_content),
            {"source": "api-test"},
        )
        changed_bundle = encode_bundle([
            BundleRecord(
                "src_changed_import", 1, "active", changed_manifest, changed_content
            )
        ])
        self._assert_error(
            self._request(
                "/v1/imports",
                method="POST",
                data=changed_bundle,
                headers={
                    "Content-Type": "application/octet-stream",
                    "Idempotency-Key": key,
                },
            ),
            409,
            "conflict",
        )
        self.assertEqual(len(self.client.retain_calls), 1)

    def test_transfer_media_validation_and_byte_limits_precede_partial_responses(self) -> None:
        calls_before = len(self.client.retain_calls)
        inventory_before = self.application.custody.list_inventory()
        with sqlite3.connect(self.paths.catalog_path) as connection:
            import_operations_before = connection.execute(
                "SELECT COUNT(*) FROM operations WHERE operation_kind='import'"
            ).fetchone()[0]
        self._assert_error(
            self._request(
                "/v1/imports",
                method="POST",
                data=b"{}",
                headers={"Content-Type": "application/json"},
            ),
            415,
            "invalid_request",
        )
        self._assert_error(
            self._request(
                "/v1/imports",
                method="POST",
                data=b"not a zip bundle",
                headers={"Content-Type": "application/octet-stream"},
            ),
            422,
            "invalid_request",
        )
        self.assertEqual(len(self.client.retain_calls), calls_before)
        self.assertEqual(self.application.custody.list_inventory(), inventory_before)
        with sqlite3.connect(self.paths.catalog_path) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM operations WHERE operation_kind='import'"
                ).fetchone()[0],
                import_operations_before,
            )

        original_limits = self.application.limits
        try:
            self.application.limits = replace(
                original_limits, max_text_bytes=1, max_file_bytes=70_000
            )
            self._assert_error(
                self._request(
                    "/v1/imports",
                    method="POST",
                    data=b"x" * 66_000,
                    headers={"Content-Type": "application/octet-stream"},
                ),
                422,
                "invalid_request",
            )
            self.application.limits = replace(original_limits, max_file_bytes=64)
            self._assert_error(
                self._request(
                    "/v1/imports",
                    method="POST",
                    data=b"x" * 65,
                    headers={"Content-Type": "application/octet-stream"},
                ),
                413,
                "body_too_large",
            )
            self.application.export_bundle = lambda **_kwargs: b"x" * 65
            self._assert_error(
                self._json("/v1/exports", {"source_ids": None}),
                500,
                "response_too_large",
            )
        finally:
            self.application.limits = original_limits

    def test_relationship_decision_is_revision_guarded_and_note_bytes_are_unchanged(self) -> None:
        note = self.notes / "plan.md"
        note_bytes = b"---\nnote_id: plan-note\ntitle: Plan\n---\nordinary private note body\n"
        note.write_bytes(note_bytes)
        note.chmod(0o600)
        source_id = "src_api_relationship"
        source_text = "See [[plan-note]]."
        checksum = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
        journal_entry = self.entries / f"{source_id}-v1.md"
        journal_entry.write_text(
            "---\nownership_marker: lifeweave-journal\n"
            f"source_id: {source_id}\nsource_version: 1\nsource_checksum: {checksum}\n"
            "title: Test source\n---\n\n" + source_text + "\n",
            encoding="utf-8",
        )
        journal_entry.chmod(0o600)

        scan_payload = {"source_id": source_id, "max_results": 5}
        scan_key_headers = {"Idempotency-Key": "relationship-scan-replay"}
        status, scan_response_headers, scan = self._json(
            "/v1/relationships/scan", scan_payload, headers=scan_key_headers
        )
        self.assertEqual(status, 200)
        scan_operation_id = scan_response_headers["x-lifeweave-operation-id"]
        self.assertNotIn("operation_id", scan)
        self.assertEqual(len(scan["edges"]), 1)
        edge = scan["edges"][0]
        self.assertEqual(edge["status"], "proposed")
        self.assertEqual(edge["to"], "note:plan-note")
        self.assertNotIn("ordinary private note body", repr(scan))
        self.assertNotIn(str(self.vault_root), repr(scan))

        with patch.object(
            self.application.relationships,
            "scan_relationships",
            wraps=self.application.relationships.scan_relationships,
        ) as scan_call:
            replay_status, replay_headers, replay = self._json(
                "/v1/relationships/scan", scan_payload, headers=scan_key_headers
            )
            self.assertEqual((replay_status, replay), (200, scan))
            self.assertEqual(replay_headers["x-lifeweave-operation-id"], scan_operation_id)
            scan_call.assert_not_called()
            self._assert_error(
                self._json(
                    "/v1/relationships/scan",
                    {"source_id": source_id, "max_results": 4},
                    headers=scan_key_headers,
                ),
                409,
                "conflict",
            )
            scan_call.assert_not_called()

        with sqlite3.connect(self.paths.catalog_path) as connection:
            persisted_scan_operation_id = connection.execute(
                "SELECT operation_id FROM operations WHERE operation_kind='relationship_scan'"
            ).fetchone()[0]
        self.assertEqual(persisted_scan_operation_id, scan_operation_id)
        status, _, operation = self._request(f"/v1/operations/{scan_operation_id}")
        self.assertEqual(status, 200)
        self.assertEqual(operation["summary"]["item_count"], 1)
        self.assertNotIn("edges", repr(operation))
        self.assertNotIn("ordinary private note body", repr(operation))

        decision_key_headers = {"Idempotency-Key": "relationship-decision-replay"}
        status, decision_response_headers, approved = self._json(
            f"/v1/relationships/{edge['edge_id']}/decision",
            {"decision": "approve", "expected_revision": 1},
            headers=decision_key_headers,
        )
        self.assertEqual(status, 200)
        decision_operation_id = decision_response_headers["x-lifeweave-operation-id"]
        self.assertNotIn("operation_id", approved)
        self.assertEqual(approved["status"], "approved")
        self.assertTrue(approved["decision_principal"])
        self.assertEqual(note.read_bytes(), note_bytes)
        with patch.object(
            self.application.relationships,
            "decide",
            wraps=self.application.relationships.decide,
        ) as decide_call:
            replay_status, replay_decision_headers, replayed_approval = self._json(
                f"/v1/relationships/{edge['edge_id']}/decision",
                {"decision": "approve", "expected_revision": 1},
                headers=decision_key_headers,
            )
            self.assertEqual((replay_status, replayed_approval), (200, approved))
            self.assertEqual(
                replay_decision_headers["x-lifeweave-operation-id"], decision_operation_id
            )
            decide_call.assert_not_called()
            self._assert_error(
                self._json(
                    f"/v1/relationships/{edge['edge_id']}/decision",
                    {"decision": "reject", "expected_revision": 1},
                    headers=decision_key_headers,
                ),
                409,
                "conflict",
            )
            decide_call.assert_not_called()
        self._assert_error(
            self._json(
                f"/v1/relationships/{edge['edge_id']}/decision",
                {"decision": "reject", "expected_revision": 1},
            ),
            409,
            "conflict",
        )
        status, _, listing = self._request("/v1/relationships?source_id=src_api_relationship&status=approved")
        self.assertEqual(status, 200)
        self.assertEqual(listing["relationships"][0]["edge_id"], edge["edge_id"])
        self.assertEqual(note.read_bytes(), note_bytes)

    def test_host_origin_storage_validation_and_bind_rejection(self) -> None:
        bad_host = self._assert_error(self._request("/healthz", headers={"Host": "attacker.example"}), 403, "host_not_allowed")
        self.assertFalse(bad_host["retryable"])
        self._assert_error(self._request("/healthz", headers={"Origin": "http://localhost"}), 403, "origin_not_allowed")
        self._assert_error(
            self._request("/v1/retain?source_id=src_bad&source_version=nope", method="POST", data=b"{}",
                          headers={"Content-Type": "application/json"}),
            422,
            "invalid_request",
        )

        with patch("uvicorn.run") as uvicorn_run:
            with self.assertRaisesRegex(ValueError, "loopback"):
                run(self.application, host="0.0.0.0")
            with self.assertRaisesRegex(ValueError, "loopback"):
                run(self.application, host="localhost")
            uvicorn_run.assert_not_called()
        with patch.object(self.application, "exact_evidence", side_effect=VaultError("unsafe_path")):
            error = self._assert_error(self._request("/v1/evidence/src_safe?version=1"), 403, "unsafe_path")
        self.assertNotIn("unsafe path", repr(error))

    def test_guard_refuses_websockets_and_passes_lifespan(self) -> None:
        called = []

        async def application(scope, receive, send):
            called.append(scope["type"])

        sent = []

        async def collect(message):
            sent.append(message)

        async def nothing():
            return {"type": "websocket.connect"}

        guard = LocalRequestGuard(application, limits=JournalLimits(), app_state=SimpleNamespace(api_token="t" * 64))
        asyncio.run(guard({"type": "websocket", "path": "/v1/status", "headers": [(b"host", b"127.0.0.1")]}, nothing, collect))
        self.assertEqual((called, sent), ([], [{"type": "websocket.close", "code": 1008}]))
        asyncio.run(guard({"type": "lifespan"}, nothing, collect))
        self.assertEqual(called, ["lifespan"])

    def test_busy_catalog_is_a_retryable_service_error(self) -> None:
        from lifecontext.api.app import _error_details

        self.assertEqual(_error_details("catalog_busy"), (503, True, "catalog_busy"))

    def test_openapi_response_allowance_remains_bounded(self) -> None:
        async def no_body():
            return {"type": "http.request", "body": b"", "more_body": False}

        for size, expected_status in ((1_048_576, 200), (1_048_577, 500)):
            with self.subTest(size=size):
                async def response(scope, receive, send, size=size):
                    await send({"type": "http.response.start", "status": 200, "headers": []})
                    await send({"type": "http.response.body", "body": b"x" * size})

                output = []

                async def collect(message, output=output):
                    output.append(message)

                guard = LocalRequestGuard(response, limits=JournalLimits(max_response_bytes=16),
                                          app_state=SimpleNamespace(api_token="t" * 64))
                scope = {"type": "http", "method": "GET", "path": "/openapi.json",
                         "headers": [(b"host", b"127.0.0.1"), (b"authorization", b"Bearer " + b"t" * 64)]}
                asyncio.run(guard(scope, no_body, collect))
                self.assertEqual(output[0]["status"], expected_status)
                if expected_status == 500:
                    self.assertEqual(json.loads(output[-1]["body"])["error_code"], "response_too_large")

    def test_asgi_guard_caps_chunked_request_and_serialized_response(self) -> None:
        limits = JournalLimits(max_text_bytes=4, max_file_bytes=4, max_response_bytes=16)
        calls = 0

        async def oversized_response(scope, receive, send):
            nonlocal calls
            calls += 1
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"x" * 17, "more_body": False})

        guard = LocalRequestGuard(oversized_response, limits=limits, app_state=SimpleNamespace(api_token="t" * 64))
        scope = {"type": "http", "method": "GET", "path": "/",
                 "headers": [(b"host", b"127.0.0.1"), (b"authorization", b"Bearer " + b"t" * 64)]}

        async def no_body():
            return {"type": "http.request", "body": b"", "more_body": False}

        output: list[dict[str, object]] = []

        async def collect(message):
            output.append(message)

        asyncio.run(guard(scope, no_body, collect))
        self.assertEqual(json.loads(output[-1]["body"])["error_code"], "response_too_large")

        chunks = iter([
            {"type": "http.request", "body": b"123", "more_body": True},
            {"type": "http.request", "body": b"45", "more_body": True},
        ])

        async def incoming():
            return next(chunks)

        output.clear()
        calls = 0
        asyncio.run(guard(scope, incoming, collect))
        self.assertEqual(calls, 0)
        self.assertEqual(json.loads(output[-1]["body"])["error_code"], "body_too_large")


class APILifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_application_factory_closes_owned_resources_when_lifespan_exits_after_failure(self) -> None:
        class Owned:
            limits = JournalLimits()
            paths = None

            def __init__(self) -> None:
                self.closed = False

            def close(self) -> None:
                self.closed = True

        owned = Owned()
        with tempfile.TemporaryDirectory(
            prefix="lifeweave-api-lifecycle.", dir=scratch_root()
        ) as temporary:
            home = Path(temporary).resolve(strict=True) / "home"
            home.mkdir(mode=0o700)
            with patch.dict(os.environ, {"HOME": str(home)}):
                owned.paths = LifeContextPaths.current()
                app = create_app(application_factory=lambda: owned)
                with self.assertRaisesRegex(RuntimeError, "startup path failed"):
                    async with app.router.lifespan_context(app):
                        self.assertIs(app.state.application, owned)
                        raise RuntimeError("startup path failed")
                self.assertTrue(owned.closed)

    async def test_failed_application_construction_closes_the_preopened_vault(self) -> None:
        with tempfile.TemporaryDirectory(prefix="lifeweave-api-startup.", dir=scratch_root()) as root:
            base = Path(root).resolve(strict=True)
            home = base / "home"
            home.mkdir(mode=0o700)
            with patch.dict(os.environ, {"HOME": str(home)}):
                paths = LifeContextPaths.current()
                paths.ensure_private_home()
                vault_root = base / "vault"
                journal = vault_root / "Journal"
                journal.mkdir(mode=0o700, parents=True)
                vault_root.chmod(0o700)
                wrong_root = base / "wrong"
                paths.config_path.write_text(
                    yaml.safe_dump(
                        {
                            "vault": {
                                "root": str(wrong_root),
                                "journal_path": str(wrong_root / "Journal"),
                            }
                        }
                    ),
                    encoding="utf-8",
                )
                paths.config_path.chmod(0o600)

                vault = ManagedVault(vault_root, journal)
                app = create_app(
                    application_factory=lambda: JournalApplication(
                        paths=paths,
                        hindsight=SimpleNamespace(),
                        vault=vault,
                    )
                )
                with self.assertRaisesRegex(ApplicationError, "vault_configuration_mismatch"):
                    async with app.router.lifespan_context(app):
                        self.fail("invalid configuration should fail application startup")
                self.assertTrue(vault._closed)

    async def test_service_home_claim_is_exclusive_across_processes_and_released(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="lifeweave-home-claim.", dir=scratch_root()
        ) as temporary:
            base = Path(temporary).resolve(strict=True)
            home = base / "home"
            other_home = base / "other-home"
            home.mkdir(mode=0o700)
            other_home.mkdir(mode=0o700)
            ready = base / "holder-ready"
            code = """
import asyncio
import sys
from pathlib import Path
from lifecontext.api.app import create_app
from lifecontext.config import JournalLimits
from lifecontext.paths import LifeContextPaths

class Owned:
    limits = JournalLimits()
    paths = LifeContextPaths.current()
    def close(self):
        pass

async def main():
    app = create_app(application_factory=Owned)
    async with app.router.lifespan_context(app):
        if sys.argv[1] == "hold":
            Path(sys.argv[2]).write_text("ready", encoding="ascii")
            await asyncio.sleep(30)

asyncio.run(main())
"""
            environment = dict(os.environ)
            environment["PYTHONPATH"] = "src:tests"
            environment["HOME"] = str(home)
            holder = subprocess.Popen(
                [sys.executable, "-c", code, "hold", str(ready)],
                cwd=Path(__file__).resolve().parents[1],
                env=environment,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                deadline = time.monotonic() + 10
                while not ready.exists() and holder.poll() is None and time.monotonic() < deadline:
                    await asyncio.sleep(0.01)
                self.assertTrue(ready.exists(), "first independent process did not claim HOME")

                blocked = subprocess.run(
                    [sys.executable, "-c", code, "once", str(base / "blocked")],
                    cwd=Path(__file__).resolve().parents[1],
                    env=environment,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                    check=False,
                )
                self.assertNotEqual(blocked.returncode, 0)

                other_environment = dict(environment, HOME=str(other_home))
                independent = subprocess.run(
                    [sys.executable, "-c", code, "once", str(base / "independent")],
                    cwd=Path(__file__).resolve().parents[1],
                    env=other_environment,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                    check=False,
                )
                self.assertEqual(independent.returncode, 0)
            finally:
                holder.terminate()
                holder.wait(timeout=10)

            released = subprocess.run(
                [sys.executable, "-c", code, "once", str(base / "released")],
                cwd=Path(__file__).resolve().parents[1],
                env=environment,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
            self.assertEqual(released.returncode, 0)

    async def test_home_claim_is_released_after_application_factory_failure(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="lifeweave-home-claim-failure.", dir=scratch_root()
        ) as temporary:
            home = Path(temporary).resolve(strict=True) / "home"
            home.mkdir(mode=0o700)
            with patch.dict(os.environ, {"HOME": str(home)}):
                app = create_app(
                    application_factory=lambda: (_ for _ in ()).throw(
                        RuntimeError("construction failed")
                    )
                )
                with self.assertRaisesRegex(RuntimeError, "construction failed"):
                    async with app.router.lifespan_context(app):
                        self.fail("failed construction must abort startup")

                owned = SimpleNamespace(
                    paths=LifeContextPaths.current(),
                    limits=JournalLimits(),
                    close=lambda: None,
                )
                healthy = create_app(application=owned)
                async with healthy.router.lifespan_context(healthy):
                    self.assertIs(healthy.state.application, owned)

    async def test_home_claim_rejects_symlink_and_nonprivate_files(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="lifeweave-home-claim-file.", dir=scratch_root()
        ) as temporary:
            base = Path(temporary).resolve(strict=True)
            home = base / "home"
            home.mkdir(mode=0o700)
            paths = LifeContextPaths(home)
            paths.ensure_private_home()
            claim = paths.service_lock_path
            target = base / "target"
            target.write_text("untouched", encoding="ascii")
            target.chmod(0o600)
            claim.symlink_to(target)
            with patch.dict(os.environ, {"HOME": str(home)}):
                app = create_app(application_factory=lambda: SimpleNamespace(close=lambda: None))
                with self.assertRaises(PublicAPIError) as caught:
                    async with app.router.lifespan_context(app):
                        self.fail("symlink lock must be rejected")
                self.assertEqual(getattr(caught.exception, "code", None), "unsafe_path")
            self.assertEqual(target.read_text(encoding="ascii"), "untouched")
            claim.unlink()
            claim.write_text("", encoding="ascii")
            claim.chmod(0o644)
            with patch.dict(os.environ, {"HOME": str(home)}):
                app = create_app(application_factory=lambda: SimpleNamespace(close=lambda: None))
                with self.assertRaises(PublicAPIError) as caught:
                    async with app.router.lifespan_context(app):
                        self.fail("nonprivate lock must be rejected")
                self.assertEqual(getattr(caught.exception, "code", None), "unsafe_permissions")


if __name__ == "__main__":
    unittest.main()
