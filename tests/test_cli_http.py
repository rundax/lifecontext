"""Disposable real-loopback checks for the HTTP-only LifeContext CLI."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import uvicorn
import yaml
from local_paths import V1_ROOT, scratch_root

from lifecontext.api.app import create_app
from lifecontext.application import JournalApplication
from lifecontext.bundle import BundleRecord, decode_bundle, encode_bundle
from lifecontext.config import JournalLimits
from lifecontext.custody import SourceManifest
from lifecontext.hindsight_adapter import LifeContextHindsightAdapter
from lifecontext.paths import LifeContextPaths
from lifecontext.vault import ManagedVault


class ControlledDocuments:
    def __init__(self) -> None:
        self.items: dict[str, dict[str, object]] = {}

    async def get_document(self, *, bank_id: str, document_id: str) -> dict[str, object]:
        return self.items.get(document_id, {})

    async def delete_document(self, *, bank_id: str, document_id: str) -> dict[str, object]:
        self.items.pop(document_id, None)
        return {"success": True, "document_id": document_id}


class ControlledMemory:
    def __init__(self) -> None:
        self.response: dict[str, object] = {}

    async def get_memory(self, *, bank_id: str, memory_id: str) -> dict[str, object]:
        if self.response.get("id") == memory_id:
            return self.response
        return {}


class ControlledClient:
    def __init__(self) -> None:
        self.documents = ControlledDocuments()
        self.memory = ControlledMemory()
        self.retain_calls: list[tuple[str, str]] = []
        self.recall_response: dict[str, object] = {"results": []}
        self.reflect_response: dict[str, object] = {"text": "", "based_on": {"memories": []}}

    async def aretain(
        self, *, bank_id: str, content: str, document_id: str,
        metadata: dict[str, str] | None = None, retain_async: bool = False,
        tags: list[str] | None = None,
    ) -> SimpleNamespace:
        self.retain_calls.append((document_id, content))
        self.documents.items[document_id] = {
            "id": document_id,
            "bank_id": bank_id,
            "document_metadata": metadata or {},
            "memory_unit_count": 1,
        }
        return SimpleNamespace(success=True, var_async=False, operation_id=None)

    async def arecall(
        self, *, bank_id: str, query: str, max_tokens: int = 4096, budget: str = "mid"
    ) -> dict[str, object]:
        return self.recall_response

    async def areflect(
        self, *, bank_id: str, query: str, budget: str = "low",
        max_tokens: int = 4096, include_facts: bool = False,
    ) -> dict[str, object]:
        return self.reflect_response


class CliHTTPTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="lifeweave-cli-http.", dir=scratch_root()
        )
        self.base = Path(self.temporary.name).resolve(strict=True)
        self.api_home = self.base / "api-home"
        self.api_home.mkdir(mode=0o700)
        self.client_home = self.base / "client-home"
        self.client_home.mkdir(mode=0o700)
        self.home_patch = unittest.mock.patch.dict(os.environ, {"HOME": str(self.api_home)})
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
            yaml.safe_dump({
                "vault": {
                    "root": str(self.vault_root),
                    "journal_path": str(self.journal),
                    "relationship_roots": ["Notes"],
                },
                "limits": {
                    "max_text_bytes": 4096,
                    "max_file_bytes": 4096,
                    "max_query_bytes": 256,
                    "max_result_count": 5,
                    "max_response_bytes": 65536,
                    "max_relationship_files": 100,
                    "max_relationship_file_bytes": 16384,
                    "max_relationship_bytes": 65536,
                    "max_relationship_scan_ms": 2000,
                },
            }),
            encoding="utf-8",
        )
        self.paths.config_path.chmod(0o600)

        self.client = ControlledClient()
        self.application: JournalApplication | None = None
        api = create_app(application_factory=self._make_application)
        self.server = uvicorn.Server(
            uvicorn.Config(api, host="127.0.0.1", port=0, log_level="error", access_log=False)
        )
        self.server_thread = threading.Thread(target=self.server.run, daemon=True)
        self.server_thread.start()
        deadline = time.monotonic() + 5
        while not self.server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(self.server.started, "temporary loopback API failed to start")
        self.port = self.server.servers[0].sockets[0].getsockname()[1]

    def tearDown(self) -> None:
        if getattr(self, "server", None) is not None and self.server.started:
            self.server.should_exit = True
            self.server_thread.join(timeout=5)
        self.temporary.cleanup()

    def _make_application(self) -> JournalApplication:
        self.api_loop = asyncio.get_running_loop()
        application = JournalApplication(
            paths=self.paths,
            hindsight=LifeContextHindsightAdapter(self.client, bank_id="lifeweave"),
            vault=ManagedVault(self.vault_root, self.journal),
            limits=JournalLimits.from_config(yaml.safe_load(self.paths.config_path.read_text())),
        )
        # The CLI exercises typed route and operation behavior. Provider identity
        # verification itself is covered by the application contract suite.
        application._verify_provider_document = AsyncMock()
        self.application = application
        return application

    def _run(
        self,
        arguments: list[str],
        *,
        input_bytes: bytes | None = None,
        port: int | None = None,
        home: Path | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        selected_home = home or self.api_home
        environment = {
            "HOME": str(selected_home),
            "PATH": os.defpath,
            "PYTHONPATH": str(V1_ROOT / "src"),
            "TMPDIR": str(scratch_root()),
            "TEMP": str(scratch_root()),
            "TMP": str(scratch_root()),
        }
        if port is None:
            port = self.port
        return subprocess.run(
            [sys.executable, "-m", "lifecontext.cli", *arguments[:1], "--port", str(port), *arguments[1:]],
            cwd=selected_home,
            env=environment,
            input=input_bytes,
            capture_output=True,
            timeout=20,
            check=False,
        )

    @staticmethod
    def _json_result(result: subprocess.CompletedProcess[bytes], *, success: bool = True) -> dict[str, object]:
        if success:
            assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
        else:
            assert result.returncode != 0, (result.returncode, result.stdout, result.stderr)
        return json.loads(result.stdout.decode("utf-8"))

    def test_documents_categories_and_wiki_use_authenticated_http(self) -> None:
        source = self.base / "résumé.txt"
        source.write_bytes(b"Non-sensitive document fixture.")
        imported = self._json_result(self._run([
            "documents", "import", str(source), "--json",
            "--idempotency-key", "document-cli-import",
        ], home=self.api_home))
        self.assertEqual(imported["state"], "completed")
        sid = imported["document"]["source_id"]
        for path, method in (("/v1/exports", "POST"), (f"/v1/evidence/{sid}?version=1", "GET"), ("/v1/recall", "POST"), ("/v1/status", "GET")):
            with self.subTest(unauthenticated_path=path):
                request = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method)
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(request, timeout=5)
                self.assertEqual(error.exception.code, 401)
        shown = self._json_result(self._run(["documents", "show", sid, "--version", "1", "--json"], home=self.api_home))
        self.assertEqual(shown["filename"], "résumé.txt")
        self.assertEqual(shown["stages"]["indexing"]["state"], "completed")
        listed = self._json_result(self._run(["documents", "list", "--json"], home=self.api_home))
        self.assertEqual(listed["documents"][0]["source_id"], sid)
        destination = self.base / "downloaded.bin"
        downloaded = self._json_result(self._run(["documents", "download", sid, str(destination), "--json"], home=self.api_home))
        self.assertEqual(destination.read_bytes(), source.read_bytes())
        self.assertEqual(downloaded["sha256"], hashlib.sha256(source.read_bytes()).hexdigest())
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
        source.write_bytes(b"Explicit updated original.")
        replacement = self._json_result(self._run([
            "documents", "import", str(source), "--source-id", sid,
            "--source-version", "2", "--json",
        ], home=self.api_home))
        self.assertEqual(replacement["document"]["source_version"], 2)
        self.assertEqual(self.application.custody.resolve_exact(sid, 1).original_bytes, b"Non-sensitive document fixture.")
        self.assertEqual(self.application.custody.resolve_exact(sid, 2).original_bytes, source.read_bytes())
        categories = self._json_result(self._run(["categories", "list", "--json"], home=self.api_home))
        self.assertEqual(categories["categories"][0]["category_id"], "inbox")
        proposal_file = self.base / "classification.json"
        proposal_file.write_text(json.dumps({
            "classification": {"name": "Research", "description": "Research documents", "rationale": "A broad recurring subject", "confidence": 0.95, "broad": True},
            "documents": [{"source_id": sid, "source_version": 2}],
        }))
        proposed = self._json_result(self._run(["categories", "propose", "--input", str(proposal_file), "--json"], home=self.api_home))
        proposal = proposed["proposal"]
        approved = self._json_result(self._run([
            "categories", "approve", proposal["proposal_id"], "--category-id", "research",
            "--expected-revision", str(proposal["revision"]), "--json",
        ], home=self.api_home))
        self.assertEqual(approved["state"], "completed")
        self._json_result(self._run(["wiki", "list", "--json"], home=self.api_home))
        self._json_result(self._run(["wiki", "status", "--json"], home=self.api_home))
        for arguments in (["documents", "list", "--json"], ["status", "--json"]):
            unauthorized = self._json_result(self._run(arguments, home=self.client_home), success=False)
            self.assertEqual(unauthorized["error"], "token_unavailable")
        self.assertFalse((self.client_home / ".lifeweave").exists())

    def test_document_reconcile_moves_categories_and_materializes_wiki_over_http(self) -> None:
        from lifecontext.document_projection import USER_START
        from lifecontext.wiki import WikiPatch

        source = self.base / "fixture.txt"
        source.write_bytes(b"The sky is blue.")
        imported = self._json_result(self._run(["documents", "import", str(source), "--json"], home=self.api_home))
        sid = imported["document"]["source_id"]
        proposal_file = self.base / "classification.json"
        proposal_file.write_text(json.dumps({
            "classification": {"name": "Research", "description": "Broad topic", "rationale": "Fixture",
                               "confidence": .95, "broad": True},
            "documents": [{"source_id": sid, "source_version": 1}],
        }))
        proposal = self._json_result(self._run([
            "categories", "propose", "--input", str(proposal_file), "--json",
        ], home=self.api_home))["proposal"]
        self._json_result(self._run([
            "categories", "approve", proposal["proposal_id"], "--category-id", "research",
            "--expected-revision", str(proposal["revision"]), "--json",
        ], home=self.api_home))
        async def seed_wiki():
            extraction = self.application.catalog.list_extractions(sid, 1)[-1]
            citation = {"source_id": sid, "source_version": 1, "extraction_revision": 1,
                        "passage_start": 0, "passage_end": 1,
                        "original_sha256": extraction["original_sha256"], "quote": "The sky is blue."}
            self.application.catalog.create_wiki_page("index", title="Index")
            patch_value = WikiPatch.parse(json.dumps({"page_id": "index", "title": "Index", "expected_revision": 0,
                "claims": [{"text": "The sky is blue.", "citations": [citation]}], "contradictions": [], "links": []}))
            stored = self.application.wiki.store.write(patch_value)
            self.application.catalog.commit_wiki_revision("index", expected_revision=0,
                artifact_path=str(stored.path.relative_to(self.paths.lifeweave_home)),
                artifact_sha256=stored.sha256, dependencies=[patch_value.claims[0].citations[0].dependency()])

        asyncio.run_coroutine_threadsafe(seed_wiki(), self.api_loop).result(timeout=10)
        first = self._json_result(self._run(["reconcile", "--json"], home=self.api_home))
        self.assertEqual(first["projected"], 2)
        self.assertEqual(first["failed"], 0)
        shown = self._json_result(self._run(["documents", "show", sid, "--json"], home=self.api_home))
        self.assertEqual(shown["stages"]["projection"]["state"], "completed")
        attachment = self.journal / "documents" / "research" / "attachments" / f"{sid}-v1--fixture.txt"
        self.assertEqual(attachment.read_bytes(), source.read_bytes())
        self.assertFalse((self.journal / "documents" / "inbox" / "attachments" / attachment.name).exists())
        self.assertFalse((self.entries / f"{sid}-v1.md").exists())
        wiki = self.journal / "wiki" / "Index.md"
        history = self.journal / "wiki" / "history" / "Index - r1.md"
        self.assertNotIn("index.md", {path.name for path in wiki.parent.iterdir()})
        self.assertNotIn("index-r1.md", {path.name for path in history.parent.iterdir()})
        immutable = history.read_bytes()
        wiki.write_text(wiki.read_text().replace(USER_START + "\n", USER_START + "\nManual note.\n"))
        second = self._json_result(self._run(["reconcile", "--json"], home=self.api_home))
        self.assertEqual(second["projected"], 0)
        self.assertEqual(second["already_projected"], 2)
        self.assertEqual(second["failed"], 0)
        self.assertIn("Manual note.", wiki.read_text())
        self.assertEqual(history.read_bytes(), immutable)
        wiki.write_text(wiki.read_text().replace("The sky is blue.", "Managed content changed."))
        conflict = self._json_result(self._run(["reconcile", "--json"], home=self.api_home))
        self.assertEqual(conflict["failed"], 1)
        self.assertIn("Managed content changed.", wiki.read_text())

    def test_document_replacement_headers_validate_before_import(self) -> None:
        from lifecontext.api.security import local_token

        headers = {
            "Authorization": "Bearer " + local_token(self.paths),
            "Content-Type": "application/octet-stream",
            "Idempotency-Key": "invalid-replacement",
            "X-Document-Filename": "fixture.txt",
        }
        cases = [
            {"X-Document-Source-ID": "doc_fixture"},
            {"X-Document-Source-Version": "2"},
            {"X-Document-Source-ID": "../fixture", "X-Document-Source-Version": "2"},
            {"X-Document-Source-ID": "doc_fixture", "X-Document-Source-Version": "0"},
            {"X-Document-Source-ID": "doc_fixture", "X-Document-Source-Version": "2147483648"},
            {"X-Document-Vision": "on"},
            {"X-Document-Vision": ""},
        ]
        for extra in cases:
            with self.subTest(extra=extra), patch.object(self.application, "import_document", new_callable=AsyncMock) as upload:
                request = urllib.request.Request(f"http://127.0.0.1:{self.port}/v1/documents", data=b"fixture", headers={**headers, **extra}, method="POST")
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(request, timeout=5)
                self.assertEqual(error.exception.code, 422)
                upload.assert_not_awaited()

    def test_document_vision_choice_reaches_the_application(self) -> None:
        from lifecontext.api.security import local_token

        headers = {
            "Authorization": "Bearer " + local_token(self.paths),
            "Content-Type": "application/octet-stream",
            "Idempotency-Key": "vision-choice",
            "X-Document-Filename": "fixture.txt",
        }
        for extra, expected in (({}, True), ({"X-Document-Vision": "off"}, False)):
            with self.subTest(extra=extra), patch.object(self.application, "import_document", new_callable=AsyncMock) as upload:
                upload.side_effect = RuntimeError("stop after the call")
                request = urllib.request.Request(f"http://127.0.0.1:{self.port}/v1/documents", data=b"fixture", headers={**headers, **extra}, method="POST")
                with self.assertRaises(urllib.error.HTTPError):
                    urllib.request.urlopen(request, timeout=5)
                self.assertIs(upload.await_args.kwargs["vision"], expected)
        retry = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/v1/documents/operations/op_{'a' * 32}/retry",
            data=json.dumps({"vision": False}).encode(),
            headers={"Authorization": headers["Authorization"], "Content-Type": "application/json"},
            method="POST",
        )
        with patch.object(self.application, "retry_document", new_callable=AsyncMock) as retried:
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(retry, timeout=5)
            self.assertEqual(error.exception.code, 422)
            retried.assert_not_awaited()

    def test_reconcile_repairs_missing_files_and_reports_conflicts_over_http(self) -> None:
        source = self.base / "source.md"
        source.write_bytes(b"synthetic reconcile source")
        self._json_result(self._run(["retain", "src_reconcile_cli", "1", "--input", str(source), "--json"]))
        (entry,) = self.entries.glob("????-??-?? - synthetic reconcile source.md")
        expected = entry.read_bytes()
        entry.unlink()
        repaired = self._json_result(self._run(["reconcile", "--json"]))
        self.assertEqual(repaired["projected"], 1)
        self.assertEqual(entry.read_bytes(), expected)
        entry.write_bytes(expected + b"user edit")
        result = self._json_result(self._run(["reconcile", "--json"]))
        self.assertEqual(result["failed"], 1)
        self.assertEqual(entry.read_bytes(), expected + b"user edit")
        invalid = self._json_result(self._run(["reconcile", "--limit", "4097", "--json"]), success=False)
        self.assertEqual(invalid["error"], "invalid_request")

    def test_command_surface_uses_http_and_returns_operation_ids(self) -> None:
        status = self._json_result(self._run(["status", "--json"]))
        self.assertIn("dependencies", status)
        self.assertEqual(status["max_file_bytes"], 4096)

        original = b"source text must stay out of argv and default output. See [[plan-note]]."
        source_file = self.base / "source.md"
        source_file.write_bytes(original)
        retain_args = [
            "retain", "src_cli", "1", "--input", str(source_file),
            "--idempotency-key", "cli-retain-repeat", "--json",
        ]
        retained = self._json_result(self._run(retain_args))
        replayed = self._json_result(self._run(retain_args))
        checksum = hashlib.sha256(original).hexdigest()
        operation_id = retained["operation_id"]
        self.assertEqual(replayed["operation_id"], operation_id)
        self.assertEqual(retained["sha256"], checksum)
        self.assertNotIn(original.decode(), json.dumps(retained))
        self.assertEqual(self.client.retain_calls.count(("src_cli", original.decode())), 1)

        operation = self._json_result(self._run(["operations", "get", operation_id, "--json"]))
        self.assertEqual(operation["state"], "completed")

        self.client.recall_response = {
            "results": [{
                "id": "fact-1", "text": "remembered result", "document_id": "src_cli",
                "metadata": {"source_version": "1", "sha256": checksum},
            }]
        }
        self.client.reflect_response = {
            "text": "reflected answer",
            "based_on": {"memories": [{"id": "fact-1"}]},
        }
        self.client.memory.response = {
            "id": "fact-1",
            "document_id": "src_cli",
            "metadata": {"source_version": "1", "sha256": checksum},
            "state": "valid",
        }
        recalled = self._json_result(self._run(["recall", "--query", "find it", "--json"]))
        self.assertEqual(recalled["results"][0]["evidence"]["source_id"], "src_cli")
        reflected = self._json_result(self._run(["reflect", "--query", "explain it", "--json"]))
        self.assertEqual(reflected["text"], "reflected answer")

        exact = self._json_result(
            self._run(["evidence", "src_cli", "--version", "1", "--json"])
        )
        self.assertNotIn("content", exact)
        self.assertNotIn(original.decode(), json.dumps(exact))
        exact_with_content = self._json_result(
            self._run([
                "evidence", "src_cli", "--version", "1", "--include-content", "--json"
            ])
        )
        self.assertEqual(exact_with_content["content"], original.decode())

        note = self.notes / "plan.md"
        note_bytes = b"---\nnote_id: plan-note\ntitle: Plan\n---\nordinary note body\n"
        note.write_bytes(note_bytes)
        note.chmod(0o600)
        scanned = self._json_result(self._run([
            "relationships", "scan", "--source-id", "src_cli", "--json"
        ]))
        self.assertTrue(scanned["edges"])
        self.assertIn("operation_id", scanned)
        edge_id = scanned["edges"][0]["edge_id"]
        decided = self._json_result(self._run([
            "relationships", "decide", edge_id, "approve", "--expected-revision", "1", "--json"
        ]))
        self.assertEqual(decided["status"], "approved")
        self.assertIn("operation_id", decided)
        listed = self._json_result(self._run([
            "relationships", "list", "--source-id", "src_cli", "--status", "approved", "--json"
        ]))
        self.assertEqual(listed["relationships"][0]["edge_id"], edge_id)

        correction = self.base / "correction.md"
        correction.write_text("corrected source", encoding="utf-8")
        corrected = self._json_result(self._run([
            "curate", "correct", "src_cli", "--expected-version", "1",
            "--input", str(correction), "--json",
        ]))
        self.assertIn("operation_id", corrected)
        reindexed = self._json_result(self._run([
            "curate", "reindex", "src_cli", "--expected-version", "2", "--json"
        ]))
        self.assertIn("operation_id", reindexed)
        prepared_forget = self._json_result(self._run([
            "curate", "prepare_forget", "src_cli", "--expected-version", "2", "--json"
        ]))
        forgotten = self._json_result(self._run([
            "curate", "forget", "src_cli", "--expected-version", "2",
            "--prepared-operation-id", prepared_forget["operation_id"], "--json",
        ]))
        prepared_delete = self._json_result(self._run([
            "curate", "prepare_delete", "src_cli", "--expected-version", "2", "--json"
        ]))
        deleted = self._json_result(self._run([
            "curate", "delete", "src_cli", "--expected-version", "2",
            "--prepared-operation-id", prepared_delete["operation_id"], "--json",
        ]))
        for mutation in (corrected, reindexed, prepared_forget, forgotten, prepared_delete, deleted):
            self.assertIn("operation_id", mutation)
        self.assertEqual(note.read_bytes(), note_bytes)

        bad_bundle = self.base / "bad-bundle.lwb"
        bad_bundle.write_bytes(b"not a bundle")
        invalid_import = self._json_result(
            self._run(["import", str(bad_bundle), "--json"]), success=False
        )
        self.assertEqual(invalid_import["error"], "invalid_request")
        export_path = self.base / "deleted-source.lwb"
        exported = self._json_result(self._run([
            "export", str(export_path), "--source-id", "src_cli", "--json"
        ]))
        self.assertEqual(exported["size_bytes"], export_path.stat().st_size)
        self.assertEqual(stat.S_IMODE(export_path.stat().st_mode), 0o600)

        human_status = self._run(["status"])
        self.assertEqual(human_status.returncode, 0)
        self.assertIn(b"ready", human_status.stdout.lower())
        human_retain = self._run(["retain", "src_human", "1", "--input", str(source_file)])
        self.assertEqual(human_retain.returncode, 0, human_retain.stderr)
        self.assertIn(b"operation ID", human_retain.stdout)

    def test_bundle_commands_bound_inputs_and_publish_private_no_clobber_exports(self) -> None:
        def bundle_for(source_id: str, content: bytes) -> bytes:
            manifest = SourceManifest(
                source_id,
                1,
                hashlib.sha256(content).hexdigest(),
                len(content),
                {"source": "cli-test"},
            )
            return encode_bundle([
                BundleRecord(source_id, 1, "active", manifest, content)
            ])

        original_import = self.application.import_bundle
        self.application.import_bundle = AsyncMock(wraps=original_import)
        source_file = self.base / "opaque-bundle.lwb"
        source_content = b"opaque bytes from the caller's bundle"
        source_file.write_bytes(bundle_for("src_cli_file", source_content))
        imported = self._json_result(self._run([
            "import", str(source_file), "--idempotency-key", "cli-import-file", "--json"
        ]))
        self.assertRegex(imported["operation_id"], r"^op_[0-9a-f]{32}$")
        self.assertEqual(imported["summary"]["item_count"], 1)
        self.assertNotIn(source_content.decode(), json.dumps(imported))
        self.assertEqual(self.client.retain_calls[-1], ("src_cli_file", source_content.decode()))

        stdin_content = b"stdin bundle content"
        stdin_result = self._json_result(self._run(
            ["import", "-", "--idempotency-key", "cli-import-stdin", "--json"],
            input_bytes=bundle_for("src_cli_stdin", stdin_content),
        ))
        self.assertRegex(stdin_result["operation_id"], r"^op_[0-9a-f]{32}$")
        self.assertEqual(self.client.retain_calls[-1], ("src_cli_stdin", stdin_content.decode()))

        export_path = self.base / "exported.lwb"
        exported = self._json_result(self._run([
            "export", str(export_path), "--source-id", "src_cli_file", "--json"
        ]))
        self.assertEqual(stat.S_IMODE(export_path.stat().st_mode), 0o600)
        self.assertEqual(exported["size_bytes"], export_path.stat().st_size)
        self.assertEqual(exported["bundle_sha256"], hashlib.sha256(export_path.read_bytes()).hexdigest())
        record = decode_bundle(export_path.read_bytes())[0]
        self.assertEqual((record.source_id, record.original_bytes), ("src_cli_file", source_content))

        existing = self.base / "existing.lwb"
        existing.write_bytes(b"keep this file")
        with patch.object(self.application, "export_bundle", wraps=self.application.export_bundle) as export_spy:
            failed = self._json_result(self._run([
                "export", str(existing), "--source-id", "src_cli_file", "--json"
            ]), success=False)
            self.assertEqual(export_spy.call_count, 0)
        self.assertEqual(failed["error"], "destination_exists")
        self.assertEqual(existing.read_bytes(), b"keep this file")

        symlink_target = self.base / "symlink-target.lwb"
        symlink_target.write_bytes(b"keep symlink target")
        symlink_destination = self.base / "symlink-destination.lwb"
        symlink_destination.symlink_to(symlink_target)
        failed = self._json_result(self._run([
            "export", str(symlink_destination), "--source-id", "src_cli_file", "--json"
        ]), success=False)
        self.assertEqual(failed["error"], "destination_exists")
        self.assertTrue(symlink_destination.is_symlink())
        self.assertEqual(symlink_target.read_bytes(), b"keep symlink target")

        import_calls_before = self.application.import_bundle.await_count
        oversized = self.base / "over-limit.lwb"
        oversized.write_bytes(b"x" * 4097)
        failed = self._json_result(self._run(["import", str(oversized), "--json"]), success=False)
        self.assertEqual(failed["error"], "body_too_large")
        self.assertEqual(self.application.import_bundle.await_count, import_calls_before)

        class OversizedExportHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                body = json.dumps({"max_file_bytes": 64}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:
                request_size = int(self.headers.get("Content-Length", "0"))
                self.rfile.read(request_size)
                body = b"x" * 65
                self.send_response(200)
                self.send_header("Content-Type", "application/vnd.lifeweave.bundle")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        oversized_server = ThreadingHTTPServer(("127.0.0.1", 0), OversizedExportHandler)
        oversized_thread = threading.Thread(target=oversized_server.serve_forever, daemon=True)
        oversized_thread.start()
        try:
            overflow_path = self.base / "overflow.lwb"
            failed = self._json_result(self._run([
                "export", str(overflow_path), "--json"
            ], port=oversized_server.server_port), success=False)
            self.assertEqual(failed["error"], "response_too_large")
            self.assertFalse(overflow_path.exists())
            self.assertFalse(any(self.base.glob(".lifeweave-export-*")))
        finally:
            oversized_server.shutdown()
            oversized_server.server_close()
            oversized_thread.join(timeout=5)

    def test_disconnected_server_input_bounds_and_preflight(self) -> None:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            unused_port = sock.getsockname()[1]
        disconnected = self._run(["status", "--json"], port=unused_port)
        failure = self._json_result(disconnected, success=False)
        self.assertEqual(failure["error"], "api_unavailable")
        self.assertNotIn(b"Traceback", disconnected.stderr)
        self.assertNotIn(str(self.base).encode(), disconnected.stdout + disconnected.stderr)

        before = len(self.client.retain_calls)
        too_large = self.base / "too-large.md"
        too_large.write_bytes(b"x" * (1_048_576 + 1))
        oversized_file = self._json_result(self._run([
            "retain", "src_large_file", "1", "--input", str(too_large), "--json"
        ]), success=False)
        self.assertEqual(oversized_file["error"], "body_too_large")
        oversized_stdin = self._json_result(self._run([
            "retain", "src_large_stdin", "1", "--input", "-", "--json"
        ], input_bytes=b"x" * (1_048_576 + 1)), success=False)
        self.assertEqual(oversized_stdin["error"], "body_too_large")
        rejected_argv = self._run([
            "retain", "src_argv", "1", "private-source-text", "--json"
        ])
        self.assertNotEqual(rejected_argv.returncode, 0)
        self.assertEqual(len(self.client.retain_calls), before)

        app_home = self.client_home / ".lifeweave"
        app_home.mkdir(mode=0o700, exist_ok=True)
        config = app_home / "config.yaml"
        config.write_text("vault: [malformed\n", encoding="utf-8")
        config.chmod(0o600)
        preflight = self._json_result(self._run(["preflight"], home=self.client_home), success=False)
        self.assertEqual(preflight, {"error": "invalid_config", "ready": False})

    def test_serve_starts_loopback_api_in_a_subprocess(self) -> None:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        home = self.base / "serve-home"
        home.mkdir(mode=0o700)
        environment = {
            "HOME": str(home),
            "PATH": os.defpath,
            "PYTHONPATH": str(V1_ROOT / "src"),
            "TMPDIR": str(scratch_root()),
            "TEMP": str(scratch_root()),
            "TMP": str(scratch_root()),
        }
        bootstrap = "\n".join([
            "import sys",
            "from lifecontext.paths import LifeContextPaths",
            "import lifecontext.cli as cli",
            "class DisposableApplication:",
            "    paths = LifeContextPaths.current()",
            "    def close(self): return None",
            "cli._build_application = lambda: DisposableApplication()",
            "raise SystemExit(cli.main(['serve', '--port', sys.argv[1]]))",
        ])
        process = subprocess.Popen(
            [sys.executable, "-c", bootstrap, str(port)],
            cwd=home,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            deadline = time.monotonic() + 10
            status = None
            while time.monotonic() < deadline and process.poll() is None:
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=0.5) as response:
                        status = response.status
                    break
                except (OSError, urllib.error.URLError):
                    time.sleep(0.05)
            self.assertEqual(status, 200, "serve did not expose the API on its selected loopback port")
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
            stdout, stderr = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 0, (stdout, stderr))


if __name__ == "__main__":
    unittest.main()
