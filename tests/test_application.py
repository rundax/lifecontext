"""End-to-end application contracts over disposable V1 state."""

from __future__ import annotations

import asyncio
import hashlib
import os
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import yaml

from lifecontext.config import JournalLimits
from lifecontext.application import ApplicationError, JournalApplication
from lifecontext.catalog import CatalogError
from lifecontext.hindsight_adapter import HindsightAdapterError, LifeContextHindsightAdapter
from lifecontext.paths import LifeContextPaths
from lifecontext.relationships import RelationshipStore
from lifecontext.vault import ManagedVault, VaultError
from local_paths import scratch_root


class ControlledClient:
    def __init__(self) -> None:
        self.retain_calls: list[tuple[str, str, dict[str, str] | None, bool]] = []
        self.retain_tags: list[tuple[str, list[str] | None]] = []
        self.retain_response = SimpleNamespace(
            success=True, var_async=False, operation_id=None
        )
        self.error: Exception | None = None
        self.on_retain = None
        self.recall_calls: list[tuple[str, int, str]] = []
        self.recall_response: dict[str, object] = {"results": []}
        self.reflect_calls: list[tuple[str, int, str]] = []
        self.reflect_response: dict[str, object] = {
            "text": "",
            "based_on": {"memories": []},
        }
        self.documents = self
        self.memory = self
        self.memory_calls: list[str] = []
        self.memory_responses: dict[str, dict[str, object]] = {}
        self.document_metadata: dict[str, dict[str, str]] = {}

    async def aretain(
        self,
        bank_id: str,
        content: str,
        document_id: str,
        metadata: dict[str, str] | None = None,
        retain_async: bool = False,
        tags: list[str] | None = None,
    ):
        self.retain_calls.append((document_id, content, metadata, retain_async))
        self.retain_tags.append((document_id, tags))
        if metadata is not None:
            self.document_metadata[document_id] = dict(metadata)
        if self.on_retain is not None:
            self.on_retain(document_id, content, metadata)
        if self.error is not None:
            raise self.error
        return self.retain_response

    async def get_document(self, bank_id: str, document_id: str):
        return {
            "id": document_id,
            "document_metadata": self.document_metadata.get(document_id),
        }

    async def delete_document(self, bank_id: str, document_id: str):
        self.document_metadata.pop(document_id, None)
        return {"success": True, "document_id": document_id}

    async def arecall(
        self,
        bank_id: str,
        query: str,
        max_tokens: int = 4096,
        budget: str = "mid",
    ):
        self.recall_calls.append((query, max_tokens, budget))
        if self.error is not None:
            raise self.error
        return self.recall_response

    async def areflect(
        self,
        bank_id: str,
        query: str,
        budget: str = "low",
        max_tokens: int | None = None,
        include_facts: bool = False,
    ):
        self.reflect_calls.append((query, max_tokens or 4096, budget))
        if self.error is not None:
            raise self.error
        return self.reflect_response

    async def get_memory(self, bank_id: str, memory_id: str):
        self.memory_calls.append(memory_id)
        if self.error is not None:
            raise self.error
        return self.memory_responses.get(memory_id, {})


class JournalApplicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="lifeweave-application-test.", dir=scratch_root()
        )
        self.base = Path(self.temporary.name).resolve(strict=True)
        self.home = self.base / "home"
        self.home.mkdir(mode=0o700)
        self.home_patch = patch.dict(os.environ, {"HOME": str(self.home)})
        self.home_patch.start()
        self.paths = LifeContextPaths.current()
        self.paths.ensure_private_home()

        self.vault_root = self.base / "vault"
        self.journal = self.vault_root / "Journal"
        self.entries = self.journal / "entries"
        self.entries.mkdir(mode=0o700, parents=True)
        self.vault_root.chmod(0o700)
        self.journal.chmod(0o700)
        self.entries.chmod(0o700)

        self.client = ControlledClient()
        self.adapter = LifeContextHindsightAdapter(self.client, bank_id="lifeweave")
        self.vault = ManagedVault(self.vault_root, self.journal)
        self.app = JournalApplication(
            paths=self.paths,
            hindsight=self.adapter,
            vault=self.vault,
        )

    def tearDown(self) -> None:
        self.app.close()
        self.home_patch.stop()
        self.temporary.cleanup()

    def _recovery_operation(
        self, key: str, kind: str, intent: dict[str, object], *, running: bool = True
    ) -> dict[str, object]:
        operation = self.app.catalog.begin_operation(
            key, hashlib.sha256(key.encode()).hexdigest(), kind
        )
        operation = self.app.catalog.set_operation_intent(
            operation["operation_id"], {"recovery_intent": intent}
        )
        if running:
            operation = self.app.catalog.transition_operation(
                operation["operation_id"],
                "pending",
                "running",
                result=operation["result"],
            )
        return operation

    def _seed_relationship(self) -> dict[str, object]:
        notes = self.vault_root / "Notes"
        notes.mkdir(mode=0o700)
        (notes / "Guide.md").write_text(
            "---\nnote_id: guide\n---\nA disposable ordinary note.\n",
            encoding="utf-8",
        )
        self.app.relationships = RelationshipStore(
            self.vault, allowlist_roots=("Notes",)
        )
        source_id = "src_relationship_recovery"
        manifest = self.app.custody.write_version(
            source_id, 1, b"See [[Guide]] for details.\n", provenance={"source": "test"}
        )
        self.app.catalog.register_manifest(manifest)
        self.app.project(source_id, 1)
        result = self.app.relationships.scan_relationships(max_results=10)
        self.assertEqual(len(result["edges"]), 1)
        return result["edges"][0]

    def test_capture_commits_custody_before_provider_call_and_keeps_states_distinct(self) -> None:
        source_id = "src_ordered"
        content = "original source bytes"
        original = content.encode("utf-8")
        provenance = {"source": "test", "source_ref": "message-1"}

        def assert_custody_precedes_provider(document_id, provider_content, metadata):
            self.assertEqual(document_id, source_id)
            self.assertEqual(provider_content.encode("utf-8"), original)
            self.assertEqual(metadata["source_version"], "1")
            self.assertEqual(metadata["sha256"], hashlib.sha256(original).hexdigest())
            evidence = self.app.custody.resolve_exact(source_id, 1)
            self.assertEqual(evidence.original_bytes, original)
            self.assertEqual(dict(evidence.manifest.provenance), provenance)
            self.assertEqual(
                self.app.catalog.get_manifest(source_id, 1)["sha256"],
                evidence.manifest.sha256,
            )

        self.client.on_retain = assert_custody_precedes_provider
        manifest = asyncio.run(
            self.app.capture(
                source_id,
                1,
                original,
                provenance=provenance,
            )
        )

        self.assertEqual(manifest.sha256, hashlib.sha256(original).hexdigest())
        self.assertEqual(len(self.client.retain_calls), 1)
        provider = self.app.catalog.get_provider_state(source_id, 1)
        self.assertEqual(provider["submission_state"], "submitted")
        self.assertEqual(provider["completion_state"], "completed")
        self.assertEqual(provider["recall_visibility"], "unknown")
        self.assertEqual(
            self.app.catalog.get_projection_state(source_id, 1)["state"],
            "not_projected",
        )

    def test_retain_operation_replays_after_reopen_without_persisting_source_text(self) -> None:
        source_id = "src_durable_retain"
        original = b"application retain secret sentinel"
        provenance = {"source": "test", "source_ref": "private-source-reference"}
        key = "application-retain-key"

        first = asyncio.run(
            self.app.retain(source_id, 1, original, provenance=provenance, idempotency_key=key)
        )
        self.assertEqual(
            first,
            {
                "source_id": source_id,
                "source_version": 1,
                "sha256": hashlib.sha256(original).hexdigest(),
                "size_bytes": len(original),
                "submission_state": "submitted",
            },
        )
        self.assertEqual(len(self.client.retain_calls), 1)
        self.app.close()
        self.vault = ManagedVault(self.vault_root, self.journal)
        self.app = JournalApplication(
            paths=self.paths,
            hindsight=self.adapter,
            vault=self.vault,
        )

        replay = asyncio.run(
            self.app.retain(
                source_id,
                1,
                original,
                provenance={"source_ref": "private-source-reference", "source": "test"},
                idempotency_key=key,
            )
        )

        self.assertEqual(replay, first)
        self.assertEqual(len(self.client.retain_calls), 1)
        with sqlite3.connect(self.paths.catalog_path) as connection:
            row = connection.execute(
                "SELECT operation_id, idempotency_key_digest, request_digest, result_json "
                "FROM operations"
            ).fetchone()
        self.assertNotIn(original.decode(), repr(row))
        self.assertNotIn("private-source-reference", repr(row))
        self.assertNotIn(key, repr(row))
        status = self.app.operation_status(row[0])
        self.assertEqual(status["state"], "completed")
        self.assertNotIn(original.decode(), repr(status))

        with self.assertRaises(ApplicationError) as caught:
            asyncio.run(
                self.app.retain(
                    source_id,
                    1,
                    b"changed content",
                    provenance=provenance,
                    idempotency_key=key,
                )
            )
        self.assertEqual(caught.exception.code, "conflict")
        self.assertEqual(len(self.client.retain_calls), 1)

    def test_retain_automatically_projects_markdown_with_exact_provenance_and_distinct_states(self) -> None:
        source_id = "src_auto_project"
        content = "journal entry content for auto projection"
        original = content.encode("utf-8")
        provenance = {"source": "test", "source_ref": "auto-project-ref"}
        key = "auto-project-key"

        result = asyncio.run(
            self.app.retain(source_id, 1, original, provenance=provenance, idempotency_key=key)
        )
        self.assertEqual(result["submission_state"], "submitted")

        evidence = self.app.custody.resolve_exact(source_id, 1)
        self.assertEqual(evidence.original_bytes, original)

        provider = self.app.catalog.get_provider_state(source_id, 1)
        self.assertEqual(provider["submission_state"], "submitted")

        projection = self.app.catalog.get_projection_state(source_id, 1)
        self.assertEqual(projection["state"], "projected")
        self.assertEqual(Path(projection["projection_path"]).parent, Path("entries"))
        self.assertNotEqual(projection["projection_path"], f"entries/{source_id}-v1.md")

        entry_file = self.journal / projection["projection_path"]
        self.assertTrue(entry_file.is_file())
        file_text = entry_file.read_text(encoding="utf-8")
        self.assertIn("ownership_marker: lifeweave-journal", file_text)
        self.assertIn(f"source_id: {source_id}", file_text)
        self.assertIn("source_version: 1", file_text)
        self.assertIn(content, file_text)
        frontmatter = yaml.safe_load(file_text.split("---", 2)[1])
        self.assertEqual(frontmatter["title"], content)
        self.assertEqual(frontmatter["aliases"], [f"{source_id}-v1"])

    def test_retain_fails_durably_when_projection_fails(self) -> None:
        source_id = "src_proj_fail"
        original = b"content where projection fails"
        provenance = {"source": "test"}
        key = "proj-fail-key"

        with patch.object(self.app.vault, "safe_write_text", side_effect=VaultError("unsafe_permissions")):
            with self.assertRaises(ApplicationError) as caught:
                asyncio.run(
                    self.app.retain(source_id, 1, original, provenance=provenance, idempotency_key=key)
                )
            self.assertEqual(caught.exception.code, "unsafe_permissions")

        with sqlite3.connect(self.paths.catalog_path) as connection:
            row = connection.execute(
                "SELECT operation_id, state, error_code FROM operations WHERE idempotency_key_digest = ?",
                (hashlib.sha256(key.encode()).hexdigest(),),
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[1], "permanent_failed")
        self.assertEqual(row[2], "unsafe_permissions")

    def test_startup_reconciles_running_retain_only_from_exact_provider_metadata(self) -> None:
        source_id = "src_recovery_exact"
        original = b"recovery private source sentinel"
        manifest = self.app.custody.write_version(
            source_id, 1, original, provenance={"source": "test"}
        )
        self.app.catalog.register_manifest(manifest)
        operation = self.app.catalog.begin_operation(
            "recovery-exact-key", hashlib.sha256(b"request").hexdigest(), "retain"
        )
        self.app.catalog.transition_operation(
            operation["operation_id"],
            "pending",
            "running",
            result={
                "sources": [{
                    "source_id": source_id,
                    "source_version": 1,
                    "sha256": manifest.sha256,
                }],
                "size_bytes": len(original),
            },
        )
        self.client.document_metadata[source_id] = {
            "source_version": "1",
            "sha256": manifest.sha256,
        }

        self.app.close()
        self.vault = ManagedVault(self.vault_root, self.journal)
        self.app = JournalApplication(
            paths=self.paths, hindsight=self.adapter, vault=self.vault
        )
        report = asyncio.run(self.app.recover_startup())

        recovered = self.app.catalog.get_operation(operation["operation_id"])
        self.assertEqual(recovered["state"], "completed")
        self.assertEqual(recovered["result"]["sources"][0]["sha256"], manifest.sha256)
        self.assertEqual(len(self.client.retain_calls), 0)
        provider = self.app.catalog.get_provider_state(source_id, 1)
        self.assertEqual(provider["submission_state"], "submitted")
        self.assertEqual(provider["completion_state"], "completed")
        self.assertNotIn(original.decode(), repr(report))
        self.assertNotIn(original.decode(), repr(self.app.operation_status(operation["operation_id"])))

    def test_startup_keeps_unknown_retain_outcome_noncompleted_without_retry(self) -> None:
        source_id = "src_recovery_unknown"
        original = b"uncertain provider source sentinel"
        manifest = self.app.custody.write_version(
            source_id, 1, original, provenance={"source": "test"}
        )
        self.app.catalog.register_manifest(manifest)
        operation = self.app.catalog.begin_operation(
            "recovery-unknown-key", hashlib.sha256(b"request-unknown").hexdigest(), "retain"
        )
        self.app.catalog.transition_operation(
            operation["operation_id"],
            "pending",
            "running",
            result={
                "sources": [{
                    "source_id": source_id,
                    "source_version": 1,
                    "sha256": manifest.sha256,
                }],
                "size_bytes": len(original),
            },
        )
        self.client.document_metadata[source_id] = {
            "source_version": "2",
            "sha256": "f" * 64,
        }

        report = asyncio.run(self.app.recover_startup())

        recovered = self.app.catalog.get_operation(operation["operation_id"])
        self.assertNotEqual(recovered["state"], "completed")
        self.assertIn(recovered["error_code"], {"conflict", "provider_unavailable"})
        self.assertEqual(len(self.client.retain_calls), 0)
        self.assertNotIn(original.decode(), repr(report))
        self.assertNotIn(original.decode(), repr(self.app.operation_status(operation["operation_id"])))

    def test_provider_failure_keeps_exact_custody_and_records_no_projection_state_change(self) -> None:
        source_id = "src_provider_failure"
        original = b"custody survives provider failure"
        provenance = {"source": "test", "source_ref": "failed-retain"}
        self.client.error = RuntimeError("private provider text must stay hidden")

        with self.assertRaises(HindsightAdapterError):
            asyncio.run(
                self.app.capture(
                    source_id,
                    1,
                    original,
                    provenance=provenance,
                )
            )

        evidence = self.app.exact_evidence(source_id, 1)
        self.assertEqual(evidence.original_bytes, original)
        self.assertEqual(dict(evidence.manifest.provenance), provenance)
        self.assertEqual(
            self.app.catalog.get_manifest(source_id, 1)["sha256"],
            hashlib.sha256(original).hexdigest(),
        )
        provider = self.app.catalog.get_provider_state(source_id, 1)
        self.assertEqual(provider["submission_state"], "failed")
        self.assertEqual(provider["completion_state"], "unknown")
        self.assertEqual(provider["recall_visibility"], "unknown")
        projection = self.app.catalog.get_projection_state(source_id, 1)
        self.assertEqual(projection["state"], "not_projected")
        self.assertIsNone(projection["projection_path"])
        self.assertIsNone(projection["desired_hash"])
        self.assertIsNone(projection["last_projected_hash"])

    def test_projection_uses_exact_version_and_persists_separate_state_after_reopen(self) -> None:
        source_id = "src_versions"
        first = "version one bytes"
        second = "version two bytes"
        asyncio.run(
            self.app.capture(
                source_id,
                1,
                first.encode("utf-8"),
                provenance={"source": "test", "version": "one"},
            )
        )
        asyncio.run(
            self.app.capture(
                source_id,
                2,
                second.encode("utf-8"),
                provenance={"source": "test", "version": "two"},
            )
        )

        with patch.object(self.vault, "safe_write_text", wraps=self.vault.safe_write_text) as writer:
            result = self.app.project(source_id, 1)

        writer.assert_called_once()
        projection_path = result["projection_path"]
        self.assertEqual(Path(projection_path).parent, Path("entries"))
        self.assertNotEqual(projection_path, f"entries/{source_id}-v1.md")
        projection_file = self.journal / projection_path
        projected = projection_file.read_text(encoding="utf-8")
        self.assertIn("source_version: 1", projected)
        self.assertIn(hashlib.sha256(first.encode("utf-8")).hexdigest(), projected)
        self.assertIn(first, projected)
        self.assertNotIn(second, projected)
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(projection_file.lstat().st_mode), 0o600)

        state = self.app.catalog.get_projection_state(source_id, 1)
        self.assertEqual(state["state"], "projected")
        self.assertEqual(state["projection_path"], projection_path)
        self.assertEqual(state["desired_hash"], result["sha256"])
        self.assertEqual(state["last_projected_hash"], result["sha256"])
        self.assertEqual(
            self.app.catalog.get_projection_state(source_id, 2)["state"],
            "not_projected",
        )

        self.app.close()
        self.vault = ManagedVault(self.vault_root, self.journal)
        self.app = JournalApplication(
            paths=self.paths,
            hindsight=self.adapter,
            vault=self.vault,
        )
        reopened = self.app.catalog.get_projection_state(source_id, 1)
        self.assertEqual(reopened["state"], "projected")
        self.assertEqual(reopened["last_projected_hash"], result["sha256"])

    def test_recall_enforces_limits_and_resolves_each_result_to_exact_local_version(self) -> None:
        source_id = "src_recall_versions"
        first = b"first exact version"
        second = b"second exact version"
        asyncio.run(self.app.capture(source_id, 1, first, provenance={"source": "test"}))
        asyncio.run(self.app.capture(source_id, 2, second, provenance={"source": "test"}))
        self.client.recall_response = {
            "results": [
                {
                    "id": "fact-v1",
                    "text": "remembered first version",
                    "document_id": source_id,
                    "metadata": {"source_version": "1", "sha256": hashlib.sha256(first).hexdigest()},
                },
                {
                    "id": "fact-v2",
                    "text": "remembered second version",
                    "document_id": source_id,
                    "metadata": {"source_version": "2", "sha256": hashlib.sha256(second).hexdigest()},
                },
            ]
        }

        result = asyncio.run(
            self.app.recall(
                "which exact version?", max_results=2, max_tokens=512, budget="low"
            )
        )
        self.assertEqual(
            [item["evidence"]["source_version"] for item in result["results"]],
            [1, 2],
        )
        self.assertEqual(
            [item["evidence"]["sha256"] for item in result["results"]],
            [hashlib.sha256(first).hexdigest(), hashlib.sha256(second).hexdigest()],
        )
        self.assertFalse(result["truncated"])

        self.app.limits = JournalLimits(max_query_bytes=8, max_result_count=1)
        call_count = len(self.client.recall_calls)
        invalid = (
            {"query": "query is too long"},
            {"query": "valid", "max_results": 2},
            {"query": "valid", "max_tokens": 8_193},
        )
        for options in invalid:
            with self.subTest(options=options), self.assertRaises(ApplicationError) as caught:
                asyncio.run(self.app.recall(**options))
            self.assertEqual(caught.exception.code, "invalid_request")
        self.assertEqual(len(self.client.recall_calls), call_count)

    def test_recall_resolves_single_source_memory_chain_to_exact_local_version(self) -> None:
        source_id = "src_recall_memory_chain"
        original = b"source retained for memory-chain attribution"
        asyncio.run(self.app.capture(source_id, 1, original, provenance={"source": "test"}))
        checksum = hashlib.sha256(original).hexdigest()
        self.client.recall_response = {
            "results": [{"id": "memory-1", "text": "remembered source text"}],
        }
        self.client.memory_responses["memory-1"] = {
            "id": "memory-1",
            "state": "valid",
            "source_memory_ids": ["observation-1"],
        }
        self.client.memory_responses["observation-1"] = {
            "id": "observation-1",
            "state": "valid",
            "document_id": source_id,
            "metadata": {"source_version": "1", "sha256": checksum},
        }

        result = asyncio.run(self.app.recall("find the retained source"))

        self.assertEqual(result["results"][0]["evidence"], {
            "source_id": source_id,
            "source_version": 1,
            "sha256": checksum,
        })
        self.assertEqual(self.client.memory_calls, ["memory-1", "observation-1"])

    def test_recall_fails_closed_when_a_provider_reference_is_not_in_local_custody(self) -> None:
        self.client.recall_response = {
            "results": [
                {
                    "id": "unowned-fact",
                    "text": "provider controlled text",
                    "document_id": "src_not_captured",
                    "metadata": {"source_version": "1"},
                }
            ]
        }
        with self.assertRaises(ApplicationError) as caught:
            asyncio.run(self.app.recall("lookup"))
        self.assertEqual(caught.exception.code, "evidence_unavailable")
        self.assertNotIn("provider controlled text", str(caught.exception))

    def test_every_captured_version_is_retained_in_its_own_scope(self) -> None:
        # One shared scope lets Hindsight merge facts of different sources into a single
        # observation, which recall cannot attribute to exactly one local source version.
        asyncio.run(self.app.capture("src_scope_a", 1, b"entry a", provenance={"source": "test"}))
        asyncio.run(self.app.capture("src_scope_b", 1, b"entry b", provenance={"source": "test"}))
        asyncio.run(self.app.capture("src_scope_a", 2, b"entry a corrected", provenance={"source": "test"}))
        self.assertEqual(self.client.retain_tags, [
            ("src_scope_a", ["src:src_scope_a:v1"]),
            ("src_scope_b", ["src:src_scope_b:v1"]),
            ("src_scope_a", ["src:src_scope_a:v2"]),
        ])
        self.assertEqual(len({tag for _, tags in self.client.retain_tags for tag in tags}), 3)

    def test_recall_omits_results_without_exactly_one_local_source_but_keeps_the_rest(self) -> None:
        good_id, other_id = "src_recall_good", "src_recall_other"
        good, other = b"good entry", b"other entry"
        asyncio.run(self.app.capture(good_id, 1, good, provenance={"source": "test"}))
        asyncio.run(self.app.capture(other_id, 1, other, provenance={"source": "test"}))
        good_sha, other_sha = hashlib.sha256(good).hexdigest(), hashlib.sha256(other).hexdigest()
        self.client.recall_response = {"results": [
            {"id": "fact-good", "text": "citable text", "document_id": good_id,
             "metadata": {"source_version": "1", "sha256": good_sha}},
            {"id": "fact-unowned", "text": "unowned provider text", "document_id": "src_not_captured",
             "metadata": {"source_version": "1"}},
            {"id": "obs-multi", "text": "merged provider text", "type": "observation"},
        ]}
        self.client.memory_responses.update({
            "obs-multi": {"id": "obs-multi", "state": "valid", "source_memory_ids": ["fact-a", "fact-b"]},
            "fact-a": {"id": "fact-a", "state": "valid", "document_id": good_id,
                       "metadata": {"source_version": "1", "sha256": good_sha}},
            "fact-b": {"id": "fact-b", "state": "valid", "document_id": other_id,
                       "metadata": {"source_version": "1", "sha256": other_sha}},
        })

        result = asyncio.run(self.app.recall("anything"))

        self.assertEqual([item["text"] for item in result["results"]], ["citable text"])
        self.assertEqual(result["results"][0]["evidence"],
                         {"source_id": good_id, "source_version": 1, "sha256": good_sha})
        self.assertEqual(result["omitted_unresolvable"], 2)
        self.assertNotIn("unowned provider text", repr(result))
        self.assertNotIn("merged provider text", repr(result))

        # Nothing citable at all is still a failure, never an empty success.
        self.client.recall_response = {"results": [self.client.recall_response["results"][2]]}
        with self.assertRaises(ApplicationError) as caught:
            asyncio.run(self.app.recall("anything"))
        self.assertEqual(caught.exception.code, "evidence_unavailable")

        # Provider contract violations stay hard failures: only missing evidence is omitted.
        self.client.recall_response = {"results": [{"text": "no id"}]}
        with self.assertRaises(ApplicationError) as caught:
            asyncio.run(self.app.recall("anything"))
        self.assertEqual(caught.exception.code, "invalid_provider_response")

    def test_recall_item_without_version_metadata_resolves_through_its_memory_record(self) -> None:
        # Hindsight leaves the metadata of some older facts out of recall items although
        # the memory record has it. The same exact custody checks must still apply.
        source_id = "src_recall_no_item_metadata"
        original = b"older journal entry"
        asyncio.run(self.app.capture(source_id, 1, original, provenance={"source": "test"}))
        checksum = hashlib.sha256(original).hexdigest()
        self.client.recall_response = {"results": [
            {"id": "fact-bare", "text": "older fact", "document_id": source_id},
        ]}
        self.client.memory_responses["fact-bare"] = {
            "id": "fact-bare", "state": "valid", "document_id": source_id,
            "metadata": {"source_version": "1", "sha256": checksum},
        }

        result = asyncio.run(self.app.recall("older"))

        self.assertEqual(result["results"][0]["evidence"],
                         {"source_id": source_id, "source_version": 1, "sha256": checksum})
        self.assertEqual(result["omitted_unresolvable"], 0)
        self.assertEqual(self.client.memory_calls, ["fact-bare"])

        self.client.memory_responses["fact-bare"]["metadata"]["sha256"] = "0" * 64
        with self.assertRaises(ApplicationError) as caught:
            asyncio.run(self.app.recall("older"))
        self.assertEqual(caught.exception.code, "evidence_unavailable")

    def test_reflect_resolves_support_with_pinned_memory_lookup(self) -> None:
        source_id = "src_reflect"
        original = b"supported local evidence"
        asyncio.run(self.app.capture(source_id, 1, original, provenance={"source": "test"}))
        checksum = hashlib.sha256(original).hexdigest()
        self.client.reflect_response = {
            "text": "The source supports this answer.",
            "based_on": {"memories": [{"id": "fact-1", "text": "provider text sentinel"}]},
        }
        self.client.memory_responses["fact-1"] = {
            "id": "fact-1",
            "text": "provider text sentinel",
            "document_id": source_id,
            "metadata": {"source_version": "1", "sha256": checksum},
            "state": "valid",
        }

        result = asyncio.run(self.app.reflect("what does it support?", max_tokens=256))
        self.assertEqual(result["text"], "The source supports this answer.")
        self.assertEqual(
            result["evidence"],
            [{"source_id": source_id, "source_version": 1, "sha256": checksum}],
        )
        self.assertEqual(self.client.memory_calls, ["fact-1"])
        self.assertEqual(self.client.recall_calls, [])
        self.assertNotIn("fact-1", repr(result))
        self.assertNotIn("provider text sentinel", repr(result))

    def test_reflect_resolves_observation_through_its_source_memory_chain(self) -> None:
        source_id = "src_reflect_chain"
        original = b"local source behind observation"
        asyncio.run(self.app.capture(source_id, 1, original, provenance={"source": "test"}))
        checksum = hashlib.sha256(original).hexdigest()
        self.client.reflect_response = {
            "text": "The source supports this answer.",
            "based_on": {"memories": [{"id": "observation-1", "text": "provider text sentinel"}]},
        }
        self.client.memory_responses.update({
            "observation-1": {
                "id": "observation-1",
                "text": "provider text sentinel",
                "source_memory_ids": ["fact-1"],
                "state": "valid",
            },
            "fact-1": {
                "id": "fact-1",
                "text": "provider source text sentinel",
                "document_id": source_id,
                "metadata": {"source_version": "1", "sha256": checksum},
                "state": "valid",
            },
        })

        result = asyncio.run(self.app.reflect("what does it support?", max_tokens=256))
        self.assertEqual(
            result["evidence"],
            [{"source_id": source_id, "source_version": 1, "sha256": checksum}],
        )
        self.assertEqual(self.client.memory_calls, ["observation-1", "fact-1"])
        self.assertEqual(self.client.recall_calls, [])
        self.assertNotIn("observation-1", repr(result))
        self.assertNotIn("provider source text sentinel", repr(result))

    def test_reflect_rejects_provider_mapping_absent_from_local_custody(self) -> None:
        self.client.reflect_response = {
            "text": "answer",
            "based_on": {"memories": [{"id": "provider-memory-secret"}]},
        }
        self.client.memory_responses["provider-memory-secret"] = {
            "id": "provider-memory-secret",
            "text": "provider private content sentinel",
            "document_id": "src_not_in_local_custody",
            "metadata": {"source_version": "1", "sha256": "a" * 64},
            "state": "valid",
        }

        with self.assertRaises(ApplicationError) as caught:
            asyncio.run(self.app.reflect("answer"))
        self.assertEqual(caught.exception.code, "evidence_unavailable")
        self.assertEqual(str(caught.exception), "evidence_unavailable")
        self.assertNotIn("provider-memory-secret", str(caught.exception))
        self.assertNotIn("provider private content sentinel", str(caught.exception))

    def test_reflect_rejects_conflicting_mapping_without_leaking_provider_fields(self) -> None:
        source_id = "src_reflect_conflict"
        original = b"local source"
        asyncio.run(self.app.capture(source_id, 1, original, provenance={"source": "test"}))
        checksum = hashlib.sha256(original).hexdigest()
        self.client.reflect_response = {
            "text": "answer",
            "based_on": {"memories": [{"id": "provider-memory-secret"}]},
        }
        self.client.memory_responses["provider-memory-secret"] = {
            "id": "provider-memory-secret",
            "text": "provider private content sentinel",
            "document_id": source_id,
            "source_id": "src_other",
            "metadata": {"source_version": "1", "sha256": checksum},
        }

        with self.assertRaises(ApplicationError) as caught:
            asyncio.run(self.app.reflect("answer"))
        self.assertEqual(caught.exception.code, "evidence_unavailable")
        self.assertEqual(str(caught.exception), "evidence_unavailable")
        self.assertNotIn("provider-memory-secret", str(caught.exception))
        self.assertNotIn("provider private content sentinel", str(caught.exception))

    def test_reflect_fails_closed_without_locally_attributable_support(self) -> None:
        self.client.reflect_response = {
            "text": "unsupported factual answer",
            "based_on": {"memories": [{"id": "provider-memory-secret", "text": "provider text"}]},
        }

        with self.assertRaises(ApplicationError) as caught:
            asyncio.run(self.app.reflect("answer the question"))
        self.assertEqual(caught.exception.code, "evidence_unavailable")
        self.assertEqual(str(caught.exception), "evidence_unavailable")
        self.assertNotIn("provider-memory-secret", str(caught.exception))
        self.assertNotIn("provider text", str(caught.exception))

    def test_readiness_reports_safe_dependency_and_reconciliation_facts(self) -> None:
        source_id = "src_private_reconciliation_id"
        asyncio.run(
            self.app.capture(source_id, 1, b"local source", provenance={"source": "test"})
        )
        self.app.catalog.record_reconciliation_fact(source_id, 1, "stale_projection")

        status = self.app.readiness()
        self.assertTrue(status["ready"])
        self.assertTrue(status["dependencies"]["hindsight"]["ready"])
        self.assertTrue(status["dependencies"]["vault"]["permissions"])
        self.assertEqual(status["reconciliation"]["fact_count"], 1)
        self.assertEqual(status["reconciliation"]["facts"]["stale_projection"], 1)
        self.assertNotIn(source_id, repr(status))
        self.assertNotIn(str(self.home), repr(status))

        self.app.hindsight.capabilities["arecall"] = False
        degraded = self.app.readiness()
        self.assertFalse(degraded["ready"])
        self.assertFalse(degraded["dependencies"]["hindsight"]["ready"])
        self.assertTrue(degraded["dependencies"]["vault"]["ready"])

    def test_operation_status_has_stable_interim_error_until_phase_five_persistence(self) -> None:
        with self.assertRaises(ApplicationError) as caught:
            self.app.operation_status("operation-1")
        self.assertEqual(caught.exception.code, "operation_status_unavailable")
        self.assertEqual(str(caught.exception), "operation_status_unavailable")

    def test_correct_is_versioned_idempotent_and_projects_only_after_provider_submission(self) -> None:
        source_id = "src_correct_versioned"
        original = b"original curation sentinel"
        replacement = "replacement curation sentinel"
        first = asyncio.run(
            self.app.capture(source_id, 1, original, provenance={"source": "test"})
        )
        self.app.project(source_id, 1)
        prior_projection = self.app.catalog.get_projection_state(source_id, 1)
        before_calls = len(self.client.retain_calls)

        result = asyncio.run(self.app.curate(
            {
                "action": "correct",
                "source_id": source_id,
                "expected_version": 1,
                "content": replacement,
            },
            idempotency_key="correct-versioned-key",
        ))

        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["summary"]["sources"][0]["source_version"], 2)
        self.assertEqual(len(self.client.retain_calls), before_calls + 1)
        self.assertEqual(self.client.retain_calls[-1][1], replacement)
        self.assertEqual(self.client.retain_calls[-1][2]["source_version"], "2")
        self.assertEqual(self.app.custody.resolve_exact(source_id, 1).original_bytes, original)
        self.assertEqual(
            self.app.custody.resolve_exact(source_id, 2).original_bytes,
            replacement.encode("utf-8"),
        )
        self.assertEqual(self.app.catalog.get_projection_state(source_id, 1), prior_projection)
        self.assertEqual(
            self.app.catalog.get_projection_state(source_id, 2)["state"], "projected"
        )
        self.assertEqual(
            asyncio.run(self.app.curate(
                {
                    "action": "correct",
                    "source_id": source_id,
                    "expected_version": 1,
                    "content": replacement,
                },
                idempotency_key="correct-versioned-key",
            )),
            result,
        )
        with self.assertRaises(ApplicationError) as caught:
            asyncio.run(self.app.curate(
                {
                    "action": "correct",
                    "source_id": source_id,
                    "expected_version": 1,
                    "content": "changed replacement",
                },
                idempotency_key="correct-versioned-key",
            ))
        self.assertEqual(caught.exception.code, "conflict")
        self.assertEqual(len(self.client.retain_calls), before_calls + 1)
        self.assertEqual(first.source_version, 1)

    def test_stale_correction_and_reindex_use_only_current_verified_version(self) -> None:
        source_id = "src_stale_correction"
        original = b"current source sentinel"
        asyncio.run(self.app.capture(source_id, 1, original, provenance={"source": "test"}))
        provider_before = self.app.catalog.get_provider_state(source_id, 1)
        projection_before = self.app.catalog.get_projection_state(source_id, 1)
        calls_before = len(self.client.retain_calls)
        with self.assertRaises(ApplicationError) as caught:
            asyncio.run(self.app.curate(
                {
                    "action": "correct",
                    "source_id": source_id,
                    "expected_version": 2,
                    "content": "must not be written",
                },
                idempotency_key="stale-correction-key",
            ))
        self.assertEqual(caught.exception.code, "conflict")
        self.assertEqual(self.app.catalog.get_manifest(source_id, 1)["sha256"], hashlib.sha256(original).hexdigest())
        self.assertIsNone(self.app.catalog.get_manifest(source_id, 2))
        self.assertEqual(self.app.catalog.get_provider_state(source_id, 1), provider_before)
        self.assertEqual(self.app.catalog.get_projection_state(source_id, 1), projection_before)
        self.assertEqual(len(self.client.retain_calls), calls_before)

        result = asyncio.run(self.app.curate(
            {"action": "reindex", "source_id": source_id},
            idempotency_key="reindex-current-key",
        ))
        self.assertEqual(result["state"], "completed")
        self.assertEqual(self.client.retain_calls[-1][1], original.decode("utf-8"))
        self.assertEqual(self.client.retain_calls[-1][2], {
            "source_version": "1",
            "sha256": hashlib.sha256(original).hexdigest(),
        })
        self.assertEqual(self.client.retain_tags[-1], (source_id, [f"src:{source_id}:v1"]))
        self.assertEqual(self.app.catalog.get_projection_state(source_id, 1), projection_before)
        calls_after_reindex = len(self.client.retain_calls)
        self.assertEqual(
            asyncio.run(self.app.curate(
                {"action": "reindex", "source_id": source_id},
                idempotency_key="reindex-current-key",
            )),
            result,
        )
        self.assertEqual(len(self.client.retain_calls), calls_after_reindex)

    def test_provider_failure_keeps_reindex_and_projection_facts_separate(self) -> None:
        source_id = "src_reindex_provider_failure"
        original = b"reindex failure sentinel"
        asyncio.run(self.app.capture(source_id, 1, original, provenance={"source": "test"}))
        projection_before = self.app.catalog.get_projection_state(source_id, 1)
        self.client.error = RuntimeError("provider source text must not escape")

        with self.assertRaises(ApplicationError) as caught:
            asyncio.run(self.app.curate(
                {"action": "reindex", "source_id": source_id, "expected_version": 1},
                idempotency_key="reindex-provider-fail-key",
            ))
        self.assertEqual(caught.exception.code, "retain_failed")
        self.assertEqual(
            self.app.catalog.get_provider_state(source_id, 1)["submission_state"], "failed"
        )
        self.assertEqual(self.app.catalog.get_projection_state(source_id, 1), projection_before)
        with sqlite3.connect(self.paths.catalog_path) as connection:
            operation_id = connection.execute(
                "SELECT operation_id FROM operations"
            ).fetchone()[0]
        status = self.app.operation_status(operation_id)
        self.assertEqual(status["state"], "retryable_failed")
        self.assertEqual(status["summary"]["error_code"], "retain_failed")
        self.assertNotIn(original.decode(), repr(status))

    def test_prepared_forget_is_bound_replayable_and_preserves_local_custody(self) -> None:
        source_id = "src_prepared_forget"
        original = b"forget custody sentinel"
        asyncio.run(self.app.capture(source_id, 1, original, provenance={"source": "test"}))
        projection = self.app.project(source_id, 1)
        prepared_payload = {
            "action": "prepare_forget", "source_id": source_id, "expected_version": 1,
        }
        prepared = asyncio.run(self.app.curate(
            prepared_payload, idempotency_key="prepare-forget-key"
        ))
        self.assertEqual(prepared["state"], "completed")
        replayed_prepare = asyncio.run(self.app.curate(
            prepared_payload, idempotency_key="prepare-forget-key"
        ))
        self.assertEqual(replayed_prepare, prepared)

        execution_payload = {
            "action": "forget",
            "source_id": source_id,
            "expected_version": 1,
            "prepared_operation_id": prepared["operation_id"],
        }
        with patch.object(
            self.app.hindsight,
            "delete_document",
            new=AsyncMock(return_value={"success": True, "document_id": source_id}),
        ) as delete_document:
            result = asyncio.run(self.app.curate(
                execution_payload, idempotency_key="forget-execute-key"
            ))
            self.assertEqual(result["state"], "completed")
            self.assertEqual(delete_document.await_count, 1)
            self.assertEqual(self.app.custody.resolve_exact(source_id, 1).original_bytes, original)
            self.assertTrue((self.journal / projection["projection_path"]).exists())
            self.assertEqual(
                self.app.catalog.get_provider_state(source_id, 1)["recall_visibility"],
                "not_visible",
            )
            self.assertIsNone(self.app.catalog.get_tombstone(source_id, 1))
            self.assertEqual(
                asyncio.run(self.app.curate(
                    execution_payload, idempotency_key="forget-execute-key"
                )),
                result,
            )
            self.assertEqual(delete_document.await_count, 1)
            with self.assertRaises(ApplicationError) as caught:
                asyncio.run(self.app.curate(
                    execution_payload, idempotency_key="forget-execute-key-2"
                ))
            self.assertEqual(caught.exception.code, "conflict")

    def test_expired_prepared_delete_fails_before_tombstone_or_provider_effect(self) -> None:
        source_id = "src_expired_delete"
        asyncio.run(self.app.capture(source_id, 1, b"expiry sentinel", provenance={"source": "test"}))
        prepared = asyncio.run(self.app.curate(
            {"action": "prepare_delete", "source_id": source_id, "expected_version": 1},
            idempotency_key="prepare-expired-delete",
        ))
        with sqlite3.connect(self.paths.catalog_path) as connection:
            connection.execute(
                "UPDATE operations SET created_at='2000-01-01 00:00:00' WHERE operation_id=?",
                (prepared["operation_id"],),
            )
        with patch.object(
            self.app.hindsight, "delete_document", new=AsyncMock()
        ) as delete_document:
            with self.assertRaises(ApplicationError) as caught:
                asyncio.run(self.app.curate(
                    {
                        "action": "delete", "source_id": source_id, "expected_version": 1,
                        "prepared_operation_id": prepared["operation_id"],
                    },
                    idempotency_key="expired-delete-execute",
                ))
            self.assertEqual(caught.exception.code, "conflict")
            delete_document.assert_not_awaited()
        self.assertIsNone(self.app.catalog.get_tombstone(source_id, 1))
        self.assertEqual(self.app.custody.resolve_exact(source_id, 1).original_bytes, b"expiry sentinel")

    def test_delete_requires_provider_document_metadata_to_match_current_custody(self) -> None:
        source_id = "src_provider_metadata_mismatch"
        original = b"provider metadata ownership sentinel"
        asyncio.run(self.app.capture(source_id, 1, original, provenance={"source": "test"}))
        prepared = asyncio.run(self.app.curate(
            {"action": "prepare_delete", "source_id": source_id, "expected_version": 1},
            idempotency_key="prepare-provider-mismatch",
        ))
        self.client.document_metadata[source_id]["sha256"] = "0" * 64

        with patch.object(
            self.app.hindsight, "delete_document", new=AsyncMock()
        ) as delete_document:
            with self.assertRaises(ApplicationError) as caught:
                asyncio.run(self.app.curate(
                    {
                        "action": "delete",
                        "source_id": source_id,
                        "expected_version": 1,
                        "prepared_operation_id": prepared["operation_id"],
                    },
                    idempotency_key="delete-provider-mismatch",
                ))
            self.assertEqual(caught.exception.code, "conflict")
            delete_document.assert_not_awaited()

        tombstone = self.app.catalog.get_tombstone(source_id, 1)
        self.assertEqual(tombstone["progress"]["provider_delete"], "pending")
        self.assertEqual(self.app.custody.resolve_exact(source_id, 1).original_bytes, original)

    def test_delete_tombstones_before_effect_and_resumes_partial_cleanup_without_redeleting(self) -> None:
        source_id = "src_partial_delete"
        original_v1 = b"first version to delete"
        original_v2 = b"current version to delete"
        asyncio.run(self.app.capture(source_id, 1, original_v1, provenance={"source": "test"}))
        asyncio.run(self.app.capture(source_id, 2, original_v2, provenance={"source": "test"}))
        projection_v1 = self.app.project(source_id, 1)
        projection_v2 = self.app.project(source_id, 2)
        ordinary_note = self.vault_root / "plan.md"
        ordinary_note.write_text("leave this alone", encoding="utf-8")
        prepared = asyncio.run(self.app.curate(
            {"action": "prepare_delete", "source_id": source_id, "expected_version": 2},
            idempotency_key="prepare-partial-delete",
        ))
        execution_payload = {
            "action": "delete", "source_id": source_id, "expected_version": 2,
            "prepared_operation_id": prepared["operation_id"],
        }
        delete_calls: list[str] = []

        async def delete_provider_document(requested_source_id: str) -> dict[str, object]:
            delete_calls.append(requested_source_id)
            self.assertEqual(
                [item["tombstone_type"] for item in self.app.catalog.list_tombstones(source_id)],
                ["deleted", "deleted"],
            )
            self.assertTrue(self.app.custody.original_path(source_id, 1).exists())
            self.assertTrue(self.app.custody.original_path(source_id, 2).exists())
            return {"success": True, "document_id": requested_source_id}

        remove_projection = self.vault.safe_remove_projection
        injected = True

        def fail_once(*args, **kwargs):
            nonlocal injected
            if injected:
                injected = False
                remove_projection(*args, **kwargs)
                raise VaultError("vault_unavailable", retryable=True)
            return remove_projection(*args, **kwargs)

        with patch.object(
            self.app.hindsight, "delete_document", new=delete_provider_document
        ), patch.object(self.vault, "safe_remove_projection", side_effect=fail_once):
            with self.assertRaises(ApplicationError) as caught:
                asyncio.run(self.app.curate(
                    execution_payload, idempotency_key="delete-partial-first-attempt"
                ))
            self.assertEqual(caught.exception.code, "vault_unavailable")
            self.assertEqual(delete_calls, [source_id])
            self.assertEqual(self.app.catalog.get_tombstone(source_id, 1)["progress"]["provider_delete"], "completed")
            self.assertTrue(self.app.custody.original_path(source_id, 1).exists())
            self.assertFalse((self.journal / projection_v1["projection_path"]).exists())

            self.app.close()
            self.vault = ManagedVault(self.vault_root, self.journal)
            self.app = JournalApplication(paths=self.paths, hindsight=self.adapter, vault=self.vault)
            resumed = asyncio.run(self.app.curate(
                execution_payload, idempotency_key="delete-partial-resume"
            ))
            self.assertEqual(resumed["state"], "completed")
            self.assertEqual(delete_calls, [source_id])

        self.assertFalse(self.app.custody.original_path(source_id, 1).exists())
        self.assertFalse(self.app.custody.original_path(source_id, 2).exists())
        self.assertFalse((self.journal / projection_v1["projection_path"]).exists())
        self.assertFalse((self.journal / projection_v2["projection_path"]).exists())
        self.assertEqual(ordinary_note.read_text(encoding="utf-8"), "leave this alone")
        with self.assertRaises(ApplicationError) as caught:
            self.app.exact_evidence(source_id, 1)
        self.assertEqual(caught.exception.code, "evidence_unavailable")

    def test_tombstone_blocks_new_retain_and_old_replay_does_not_restore_custody(self) -> None:
        source_id = "src_no_resurrection"
        original = b"must remain deleted"
        retain_payload = {
            "source_id": source_id,
            "source_version": 1,
            "provenance": {"source": "test"},
        }
        retained = asyncio.run(self.app.retain(
            source_id,
            1,
            original,
            provenance=retain_payload["provenance"],
            idempotency_key="retain-before-delete",
        ))
        prepared = asyncio.run(self.app.curate(
            {"action": "prepare_delete", "source_id": source_id, "expected_version": 1},
            idempotency_key="prepare-no-resurrection",
        ))
        with patch.object(
            self.app.hindsight,
            "delete_document",
            new=AsyncMock(return_value={"success": True, "document_id": source_id}),
        ):
            asyncio.run(self.app.curate(
                {
                    "action": "delete",
                    "source_id": source_id,
                    "expected_version": 1,
                    "prepared_operation_id": prepared["operation_id"],
                },
                idempotency_key="delete-no-resurrection",
            ))

        original_path = self.app.custody.original_path(source_id, 1)
        self.assertFalse(original_path.exists())
        self.assertEqual(
            asyncio.run(self.app.retain(
                source_id,
                1,
                original,
                provenance=retain_payload["provenance"],
                idempotency_key="retain-before-delete",
            )),
            retained,
        )
        with self.assertRaises(ApplicationError) as caught:
            asyncio.run(self.app.retain(
                source_id,
                1,
                original,
                provenance=retain_payload["provenance"],
                idempotency_key="retain-after-delete",
            ))
        self.assertEqual(caught.exception.code, "source_conflict")
        self.assertFalse(original_path.exists())

    def test_startup_completes_prepared_curation_from_exact_history(self) -> None:
        source_id = "src_prepare_recovery"
        original = b"private preparation source sentinel"
        manifest = self.app.custody.write_version(
            source_id, 1, original, provenance={"source": "test"}
        )
        self.app.catalog.register_manifest(manifest)
        intent = {
            "kind": "curation",
            "action": "prepare_forget",
            "source_id": source_id,
            "source_version": 1,
            "sha256": manifest.sha256,
            "size_bytes": len(original),
        }
        operation = self._recovery_operation("recovery-prepare", "curation", intent)
        self.app.catalog.record_curation_history(
            operation["operation_id"], source_id, 1, "prepare_forget"
        )

        report = asyncio.run(self.app.recover_startup())

        recovered = self.app.catalog.get_operation(operation["operation_id"])
        self.assertEqual(recovered["state"], "completed")
        self.assertEqual(recovered["result"]["sources"][0]["sha256"], manifest.sha256)
        self.assertEqual(report["completed"], 1)
        self.assertNotIn(original.decode(), repr(report))
        self.assertNotIn(original.decode(), repr(self.app.operation_status(operation["operation_id"])))

    def test_startup_reconciles_running_delete_from_tombstones_without_second_delete(self) -> None:
        source_id = "src_delete_recovery"
        original = b"delete recovery source sentinel"
        manifest = asyncio.run(
            self.app.capture(source_id, 1, original, provenance={"source": "test"})
        )
        prepared = asyncio.run(self.app.curate(
            {"action": "prepare_delete", "source_id": source_id, "expected_version": 1},
            idempotency_key="prepare-delete-recovery",
        ))
        intent = {
            "kind": "curation",
            "action": "delete",
            "source_id": source_id,
            "source_version": 1,
            "sha256": manifest.sha256,
            "size_bytes": manifest.size_bytes,
            "prepared_operation_id": prepared["operation_id"],
        }
        operation = self._recovery_operation("delete-recovery", "curation", intent)
        self.app.catalog.begin_confirmed_curation(
            operation["operation_id"], prepared["operation_id"], source_id, 1, "delete",
            confirmation_ttl_seconds=600,
        )
        self.app.catalog.update_tombstone_progress(
            source_id, 1, "provider_delete", "running", whole_source=True
        )
        self.app.catalog.set_provider_state(source_id, 1, recall_visibility="not_visible")
        delete = AsyncMock()

        with patch.object(self.app.hindsight, "delete_document", new=delete):
            report = asyncio.run(self.app.recover_startup())

        recovered = self.app.catalog.get_operation(operation["operation_id"])
        tombstone = self.app.catalog.get_tombstone(source_id, 1)
        self.assertEqual(recovered["state"], "completed")
        self.assertEqual(tombstone["progress"], {
            "provider_delete": "completed",
            "projection_delete": "completed",
            "custody_delete": "completed",
        })
        self.assertFalse(self.app.custody.original_path(source_id, 1).exists())
        delete.assert_not_awaited()
        self.assertEqual(report["completed"], 1)
        self.assertNotIn(original.decode(), repr(report))

    def test_startup_reconciles_pending_projection_from_exact_managed_hash(self) -> None:
        source_id = "src_projection_recovery"
        original = b"projection recovery source sentinel\n"
        manifest = self.app.custody.write_version(
            source_id, 1, original, provenance={"source": "test"}
        )
        self.app.catalog.register_manifest(manifest)
        projected = self.app.project(source_id, 1)
        path = projected["projection_path"]
        self.app.catalog.set_projection_state(
            source_id, 1, state="pending", projection_path=path,
            desired_hash=projected["sha256"], last_projected_hash=None,
        )

        report = asyncio.run(self.app.recover_startup())

        state = self.app.catalog.get_projection_state(source_id, 1)
        self.assertEqual(state["state"], "projected")
        self.assertEqual(state["last_projected_hash"], projected["sha256"])
        self.assertEqual(report["projections_reconciled"], 1)
        self.assertNotIn(original.decode(), repr(report))

    def test_startup_does_not_overwrite_an_edited_pending_projection(self) -> None:
        source_id = "src_projection_conflict"
        manifest = self.app.custody.write_version(
            source_id, 1, b"original projected body\n", provenance={"source": "test"}
        )
        self.app.catalog.register_manifest(manifest)
        projected = self.app.project(source_id, 1)
        path = self.journal / projected["projection_path"]
        edited = path.read_bytes().replace(b"original projected body", b"human edit")
        path.write_bytes(edited)
        self.app.catalog.set_projection_state(
            source_id, 1, state="pending", projection_path=projected["projection_path"],
            desired_hash=projected["sha256"], last_projected_hash=projected["sha256"],
        )

        asyncio.run(self.app.recover_startup())

        self.assertEqual(self.app.catalog.get_projection_state(source_id, 1)["state"], "conflict")
        self.assertEqual(path.read_bytes(), edited)

    def test_startup_does_not_project_when_tombstone_lookup_fails(self) -> None:
        source_id = "src_projection_tombstone_error"
        original = b"pending projection with unknown tombstone state\n"
        manifest = self.app.custody.write_version(
            source_id, 1, original, provenance={"source": "test"}
        )
        self.app.catalog.register_manifest(manifest)
        content = self.app._render_projection(manifest, original, legacy=True)
        desired_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        path = self.entries / (source_id + "-v1.md")
        self.app.catalog.set_projection_state(
            source_id, 1, state="pending", projection_path="entries/" + path.name,
            desired_hash=desired_hash,
        )

        with patch.object(
            self.app.catalog, "get_tombstone", side_effect=CatalogError("catalog unavailable")
        ):
            report = asyncio.run(self.app.recover_startup())

        self.assertFalse(path.exists())
        self.assertEqual(self.app.catalog.get_projection_state(source_id, 1)["state"], "pending")
        self.assertEqual(report["projections_unresolved"], 1)

    def test_startup_reconciles_running_relationship_decision_from_current_ledger(self) -> None:
        edge = self._seed_relationship()
        principal = "recovery-test"
        intent = {
            "kind": "relationship_decision",
            "edge_id": edge["edge_id"],
            "decision": "approve",
            "expected_revision": edge["revision"],
            "principal": principal,
        }
        operation = self._recovery_operation("relationship-decision-recovery", "relationship_decision", intent)
        decided = self.app.relationships.decide(
            edge["edge_id"], "approve", edge["revision"], principal=principal
        )

        report = asyncio.run(self.app.recover_startup())

        recovered = self.app.catalog.get_operation(operation["operation_id"])
        self.assertEqual(recovered["state"], "completed")
        self.assertEqual(recovered["result"]["relationship_decision"], decided)
        self.assertEqual(report["completed"], 1)

    def test_startup_resumes_pending_relationship_scan_from_content_free_intent(self) -> None:
        self._seed_relationship()
        intent = {"kind": "relationship_scan", "source_id": None, "max_results": 10}
        operation = self._recovery_operation(
            "relationship-scan-pending", "relationship_scan", intent, running=False
        )

        report = asyncio.run(self.app.recover_startup())

        recovered = self.app.catalog.get_operation(operation["operation_id"])
        self.assertEqual(recovered["state"], "completed")
        self.assertEqual(len(recovered["result"]["relationship_scan"]["edges"]), 1)
        self.assertEqual(report["resumed"], 1)
        self.assertNotIn("See [[Guide]]", repr(report))

    def test_startup_reconciles_running_relationship_scan_only_with_saved_result_and_ledger(self) -> None:
        edge = self._seed_relationship()
        intent = {"kind": "relationship_scan", "source_id": None, "max_results": 10}
        operation = self._recovery_operation(
            "relationship-scan-running", "relationship_scan", intent, running=False
        )
        result = self.app.relationships.scan_relationships(max_results=10)
        self.app.catalog.transition_operation(
            operation["operation_id"], "pending", "running",
            result={"recovery_intent": intent, "relationship_scan": result},
        )

        report = asyncio.run(self.app.recover_startup())

        recovered = self.app.catalog.get_operation(operation["operation_id"])
        self.assertEqual(recovered["state"], "completed")
        self.assertEqual(recovered["result"]["relationship_scan"]["edges"][0]["edge_id"], edge["edge_id"])
        self.assertEqual(report["completed"], 1)

    def test_startup_keeps_running_relationship_scan_without_result_unresolved(self) -> None:
        self._seed_relationship()
        intent = {"kind": "relationship_scan", "source_id": None, "max_results": 10}
        operation = self._recovery_operation("relationship-scan-unknown", "relationship_scan", intent)
        scan = self.app.relationships.scan_relationships
        with patch.object(self.app.relationships, "scan_relationships", wraps=scan) as repeated:
            report = asyncio.run(self.app.recover_startup())

        recovered = self.app.catalog.get_operation(operation["operation_id"])
        self.assertNotEqual(recovered["state"], "completed")
        self.assertEqual(repeated.call_count, 0)
        self.assertEqual(report["unresolved"], 1)


    def test_reconcile_projections_projects_all_unreconciled_active_versions(self) -> None:
        provenance = {"source": "test"}
        source_ids = ["src_reconcile_a", "src_reconcile_b", "src_reconcile_c"]
        for idx, source_id in enumerate(source_ids, start=1):
            content = f"Journal entry {idx}: reconcile test content.\n".encode()
            manifest = self.app.custody.write_version(
                source_id, 1, content, provenance=provenance
            )
            self.app.catalog.register_manifest(manifest)

        report = self.app.reconcile_projections()

        self.assertEqual(report, {
            "examined": 3,
            "already_projected": 0,
            "projected": 3,
            "skipped": 0,
            "failed": 0,
        })
        for source_id in source_ids:
            projection = self.app.catalog.get_projection_state(source_id, 1)
            self.assertEqual(projection["state"], "projected")
            entry = self.journal / projection["projection_path"]
            self.assertTrue(entry.exists(), f"Missing projection for {source_id}")
            text = entry.read_text(encoding="utf-8")
            self.assertIn("ownership_marker: lifeweave-journal", text)
            self.assertIn(source_id, text)

    def test_reconcile_projections_skips_already_projected_and_non_active_versions(self) -> None:
        provenance = {"source": "test"}
        # v1: already projected
        manifest_a = self.app.custody.write_version(
            "src_recon_already", 1, b"projected content\n", provenance=provenance
        )
        self.app.catalog.register_manifest(manifest_a)
        self.app.project("src_recon_already", 1)

        # v2: active but not projected
        manifest_b = self.app.custody.write_version(
            "src_recon_new", 1, b"unreconciled content\n", provenance=provenance
        )
        self.app.catalog.register_manifest(manifest_b)

        # v3: lifecycle=forgotten (recall_visibility=not_visible, no tombstone) → skipped
        manifest_c = self.app.custody.write_version(
            "src_recon_forgotten", 1, b"forgotten content\n", provenance=provenance
        )
        self.app.catalog.register_manifest(manifest_c)
        self.app.catalog.set_provider_state(
            "src_recon_forgotten", 1, recall_visibility="not_visible"
        )

        report = self.app.reconcile_projections()

        self.assertEqual(report, {
            "examined": 3,
            "already_projected": 1,
            "projected": 1,
            "skipped": 1,
            "failed": 0,
        })
        projection = self.app.catalog.get_projection_state("src_recon_new", 1)
        self.assertEqual(projection["state"], "projected")
        new_entry = self.journal / projection["projection_path"]
        self.assertTrue(new_entry.exists())
        forgotten = self.app.catalog.get_projection_state("src_recon_forgotten", 1)
        self.assertEqual(forgotten["state"], "not_projected")
        self.assertIsNone(forgotten["projection_path"])
        self.assertEqual(set(self.entries.glob("*.md")), {
            self.journal / self.app.catalog.get_projection_state("src_recon_already", 1)["projection_path"],
            new_entry,
        })

    def test_reconcile_checks_projected_files_and_restores_only_missing_output(self) -> None:
        for source_id in ("src_missing", "src_edited"):
            manifest = self.app.custody.write_version(
                source_id, 1, b"exact custody content\n", provenance={"source": "test"}
            )
            self.app.catalog.register_manifest(manifest)
            self.app.project(source_id, 1)
        missing = self.journal / self.app.catalog.get_projection_state("src_missing", 1)["projection_path"]
        expected = missing.read_bytes()
        missing.unlink()
        edited = self.journal / self.app.catalog.get_projection_state("src_edited", 1)["projection_path"]
        edited.write_bytes(edited.read_bytes() + b"user edit\n")
        changed = edited.read_bytes()
        report = self.app.reconcile_projections()
        self.assertEqual(report["already_projected"], 0)
        self.assertEqual(report["projected"], 1)
        self.assertEqual(report["failed"], 1)
        self.assertEqual(missing.read_bytes(), expected)
        self.assertEqual(edited.read_bytes(), changed)
        self.assertEqual(self.app.catalog.get_projection_state("src_edited", 1)["state"], "conflict")
        self.assertEqual(self.app.reconcile_projections()["failed"], 1)
        self.assertEqual(edited.read_bytes(), changed)

    def test_reconcile_projections_continues_on_per_item_failure_and_reports_failed(
        self,
    ) -> None:
        provenance = {"source": "test"}
        source_ids = ["src_recon_ok1", "src_recon_fail", "src_recon_ok2"]
        for source_id in source_ids:
            manifest = self.app.custody.write_version(
                source_id, 1, f"{source_id} content\n".encode(), provenance=provenance
            )
            self.app.catalog.register_manifest(manifest)

        original_project = self.app.project

        def project_with_middle_failure(source_id, source_version):
            if source_id == "src_recon_fail":
                raise ApplicationError("projection_test_failure")
            return original_project(source_id, source_version)

        with patch.object(self.app, "project", side_effect=project_with_middle_failure):
            report = self.app.reconcile_projections()

        self.assertEqual(report["examined"], 3)
        self.assertEqual(report["projected"], 2)
        self.assertEqual(report["failed"], 1)
        self.assertEqual(report["skipped"], 0)
        self.assertEqual(report["already_projected"], 0)


if __name__ == "__main__":
    unittest.main()
