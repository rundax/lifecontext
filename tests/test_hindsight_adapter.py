"""Contract tests for LifeContext's pinned Hindsight operation boundary."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
import unittest

from hindsight_client import Hindsight

from lifecontext.hindsight_adapter import (
    HindsightAdapterError,
    LifeContextHindsightAdapter,
    source_scope_tag,
)


class FakeDocuments:
    def __init__(self, calls):
        self.calls = calls
        self.get_response = SimpleNamespace(
            id="source-1",
            bank_id="journal",
            original_text="provider copy is not source custody",
            content_hash="sha256:abc",
            created_at="2026-09-23T00:00:00Z",
            updated_at="2026-09-23T00:00:00Z",
            memory_unit_count=1,
            document_metadata={"source_id": "source-1"},
        )
        self.delete_response = SimpleNamespace(
            success=True,
            document_id="source-1",
            memory_units_deleted=1,
            message="deleted",
        )
        self.error = None

    async def get_document(self, bank_id, document_id):
        self.calls.append(("get_document", bank_id, document_id))
        if self.error:
            raise self.error
        return self.get_response

    async def delete_document(self, bank_id, document_id):
        self.calls.append(("delete_document", bank_id, document_id))
        if self.error:
            raise self.error
        return self.delete_response


class FakeMemory:
    def __init__(self):
        self.calls = []
        self.response = SimpleNamespace(
            id="fact-1",
            document_id="source-1",
            metadata={"source_version": "1", "sha256": "a" * 64},
            state="valid",
            text="provider source text must not leave adapter",
        )
        self.error = None

    async def get_memory(self, bank_id, memory_id):
        self.calls.append((bank_id, memory_id))
        if self.error:
            raise self.error
        return self.response


class ControlledClient:
    def __init__(self):
        self.calls = []
        self.documents = FakeDocuments(self.calls)
        self.memory = FakeMemory()
        self.retain_response = SimpleNamespace(
            success=True, var_async=False, operation_id=None, secret="never returned"
        )
        self.recall_response = SimpleNamespace(
            results=[SimpleNamespace(id="fact-1", source_id="source-1", text="fact")]
        )
        self.reflect_response = SimpleNamespace(
            text="answer", based_on=SimpleNamespace(facts=["fact-1"])
        )
        self.error = None

    async def aretain(
        self, bank_id, content, document_id, metadata=None, retain_async=False,
        tags=None,
    ):
        self.calls.append(
            ("aretain", bank_id, content, document_id, metadata, retain_async, tags)
        )
        if self.error:
            raise self.error
        return self.retain_response

    async def arecall(self, bank_id, query, max_tokens=4096, budget="mid"):
        self.calls.append(("arecall", bank_id, query, max_tokens, budget))
        if self.error:
            raise self.error
        return self.recall_response

    async def areflect(
        self, bank_id, query, budget="low", max_tokens=None, include_facts=False
    ):
        self.calls.append(
            ("areflect", bank_id, query, budget, max_tokens, include_facts)
        )
        if self.error:
            raise self.error
        return self.reflect_response


class HindsightAdapterTests(unittest.TestCase):
    def test_installed_pinned_client_exposes_required_async_operations(self):
        client = Hindsight(base_url="http://127.0.0.1:9", timeout=0.1)
        try:
            adapter = LifeContextHindsightAdapter(client, bank_id="journal")
            self.assertTrue(adapter._memory_lookup_available)
            self.assertEqual(
                adapter.capabilities,
                {
                    "aretain": True,
                    "arecall": True,
                    "areflect": True,
                    "documents.get_document": True,
                    "documents.delete_document": True,
                    "attributed_reflect": True,
                },
            )
        finally:
            client.close()

    def test_memory_lookup_returns_only_bounded_attribution_fields(self):
        client = ControlledClient()
        adapter = LifeContextHindsightAdapter(client, bank_id="journal")

        result = asyncio.run(adapter.get_memory("fact-1"))

        self.assertEqual(client.memory.calls, [("journal", "fact-1")])
        self.assertEqual(result, {
            "id": "fact-1",
            "document_id": "source-1",
            "metadata": {"source_version": "1", "sha256": "a" * 64},
            "state": "valid",
        })
        self.assertNotIn("provider source text must not leave adapter", repr(result))

    def test_memory_lookup_normalizes_source_chain_without_provider_text(self):
        client = ControlledClient()
        client.memory.response = {
            "id": "fact-1",
            "source_memory_ids": ["source-fact-1"],
            "source_memories": [{"id": "source-fact-1", "text": "provider source text"}],
            "text": "provider observation text",
        }
        adapter = LifeContextHindsightAdapter(client, bank_id="journal")

        result = asyncio.run(adapter.get_memory("fact-1"))

        self.assertEqual(result, {
            "id": "fact-1",
            "source_memory_ids": ["source-fact-1"],
        })
        self.assertNotIn("provider source text", repr(result))
        self.assertNotIn("provider observation text", repr(result))

    def test_memory_lookup_rejects_mismatched_ids_and_malformed_chains(self):
        client = ControlledClient()
        adapter = LifeContextHindsightAdapter(client, bank_id="journal")
        client.memory.response = {"id": "different-provider-memory-id", "text": "private"}

        with self.assertRaises(HindsightAdapterError) as caught:
            asyncio.run(adapter.get_memory("fact-1"))
        self.assertEqual(caught.exception.code, "invalid_provider_response")
        self.assertNotIn("different-provider-memory-id", str(caught.exception))

        client.memory.response = {
            "id": "fact-1",
            "source_memory_ids": "malformed-provider-field",
        }
        with self.assertRaises(HindsightAdapterError) as caught:
            asyncio.run(adapter.get_memory("fact-1"))
        self.assertEqual(caught.exception.code, "invalid_provider_response")
        self.assertNotIn("malformed-provider-field", str(caught.exception))

    def test_memory_lookup_failures_are_stable_and_redacted(self):
        client = ControlledClient()
        adapter = LifeContextHindsightAdapter(client, bank_id="journal")
        client.memory.error = RuntimeError("provider memory secret and source text")

        with self.assertRaises(HindsightAdapterError) as caught:
            asyncio.run(adapter.get_memory("fact-secret"))

        self.assertEqual(caught.exception.code, "memory_lookup_failed")
        self.assertEqual(str(caught.exception), "memory_lookup_failed")
        self.assertNotIn("fact-secret", str(caught.exception))
        self.assertNotIn("source text", str(caught.exception))

    def test_unsupported_memory_lookup_fails_before_provider_call(self):
        class NoMemoryLookup(ControlledClient):
            def __init__(self):
                super().__init__()
                self.memory = object()

        client = NoMemoryLookup()
        adapter = LifeContextHindsightAdapter(client, bank_id="journal")

        with self.assertRaises(HindsightAdapterError) as caught:
            asyncio.run(adapter.get_memory("fact-1"))

        self.assertEqual(caught.exception.code, "capability_unavailable")

    def test_every_retain_is_confined_to_one_source_version_scope(self):
        # Hindsight consolidates observations per tag set, so a shared scope would merge
        # facts from different sources into one observation with no single local source.
        self.assertNotEqual(source_scope_tag("source-1", 1), source_scope_tag("source-1", 2))
        self.assertNotEqual(source_scope_tag("source-1", 1), source_scope_tag("source-2", 1))
        self.assertEqual(source_scope_tag("source-1", 1), source_scope_tag("source-1", 1))
        client = ControlledClient()
        adapter = LifeContextHindsightAdapter(client, bank_id="journal")
        with self.assertRaises(TypeError):
            asyncio.run(adapter.retain("source-1", "content"))
        for bad in ("", None):
            with self.subTest(scope_tag=bad), self.assertRaises(HindsightAdapterError) as caught:
                asyncio.run(adapter.retain("source-1", "content", scope_tag=bad))
            self.assertEqual(caught.exception.code, "invalid_request")
        self.assertEqual(client.calls, [])

    def test_retain_uses_source_identity_and_returns_only_actual_status(self):
        client = ControlledClient()
        adapter = LifeContextHindsightAdapter(client, bank_id="journal")

        result = asyncio.run(
            adapter.retain(
                "source-1",
                "exact retained bytes",
                scope_tag=source_scope_tag("source-1", 1),
                metadata={"source_version": "1"},
            )
        )

        self.assertEqual(
            client.calls[0],
            (
                "aretain",
                "journal",
                "exact retained bytes",
                "source-1",
                {"source_version": "1"},
                False,
                ["src:source-1:v1"],
            ),
        )
        self.assertEqual(result, {"success": True, "async": False, "operation_id": None})
        self.assertNotIn("secret", result)

        client.retain_response = SimpleNamespace(
            success=True, var_async=True, operation_id="operation-1"
        )
        accepted = asyncio.run(adapter.retain("source-1", "later version", scope_tag="src:source-1:v2", retain_async=True))
        self.assertEqual(accepted, {"success": True, "async": True, "operation_id": "operation-1"})

    def test_recall_and_reflect_use_bounded_options_and_return_plain_values(self):
        client = ControlledClient()
        adapter = LifeContextHindsightAdapter(client, bank_id="journal")

        recall = asyncio.run(adapter.recall("recent events", max_tokens=512, budget="low"))
        reflect = asyncio.run(
            adapter.reflect("summarize events", max_tokens=768, budget="mid")
        )

        self.assertEqual(client.calls[0], ("arecall", "journal", "recent events", 512, "low"))
        self.assertEqual(client.calls[1], ("areflect", "journal", "summarize events", "mid", 768, True))
        self.assertEqual(recall, {"results": [{"id": "fact-1", "source_id": "source-1", "text": "fact"}]})
        self.assertEqual(reflect, {"text": "answer", "based_on": {"facts": ["fact-1"]}})

    def test_document_methods_delegate_and_return_provider_state_only(self):
        client = ControlledClient()
        adapter = LifeContextHindsightAdapter(client, bank_id="journal")

        document = asyncio.run(adapter.get_document("source-1"))
        deleted = asyncio.run(adapter.delete_document("source-1"))

        self.assertEqual(
            client.calls,
            [
                ("get_document", "journal", "source-1"),
                ("delete_document", "journal", "source-1"),
            ],
        )
        self.assertEqual(
            document,
            {
                "id": "source-1",
                "bank_id": "journal",
                "content_hash": "sha256:abc",
                "created_at": "2026-09-23T00:00:00Z",
                "updated_at": "2026-09-23T00:00:00Z",
                "memory_unit_count": 1,
                "document_metadata": {"source_id": "source-1"},
            },
        )
        self.assertNotIn("original_text", document)
        self.assertEqual(
            deleted,
            {"success": True, "document_id": "source-1", "memory_units_deleted": 1},
        )

    def test_unsupported_attributed_reflect_fails_before_provider_call(self):
        class NoAttributedReflect(ControlledClient):
            async def areflect(self, bank_id, query, budget="low", max_tokens=None):
                self.calls.append(("areflect", bank_id, query, budget, max_tokens))
                return self.reflect_response

        client = NoAttributedReflect()
        adapter = LifeContextHindsightAdapter(client, bank_id="journal")

        with self.assertRaises(HindsightAdapterError) as caught:
            asyncio.run(adapter.reflect("query"))

        self.assertEqual(caught.exception.code, "capability_unavailable")
        self.assertEqual(client.calls, [])

    def test_provider_failures_are_stable_and_redacted(self):
        client = ControlledClient()
        client.error = RuntimeError("raw provider body includes api-key-secret")
        adapter = LifeContextHindsightAdapter(client, bank_id="journal")

        with self.assertRaises(HindsightAdapterError) as caught:
            asyncio.run(adapter.retain("source-1", "content", scope_tag="src:source-1:v1"))

        self.assertEqual(caught.exception.code, "retain_failed")
        self.assertNotIn("api-key-secret", str(caught.exception))
        self.assertEqual(str(caught.exception), "retain_failed")

        client.error = RuntimeError("raw provider body includes api-key-secret")
        with self.assertRaises(HindsightAdapterError) as caught:
            asyncio.run(adapter.recall("query"))
        self.assertEqual(caught.exception.code, "recall_failed")
        self.assertNotIn("api-key-secret", str(caught.exception))

        with self.assertRaises(HindsightAdapterError) as caught:
            asyncio.run(adapter.reflect("query"))
        self.assertEqual(caught.exception.code, "reflect_failed")

        client.documents.error = RuntimeError("raw provider body includes api-key-secret")
        with self.assertRaises(HindsightAdapterError) as caught:
            asyncio.run(adapter.get_document("source-1"))
        self.assertEqual(caught.exception.code, "document_lookup_failed")

        with self.assertRaises(HindsightAdapterError) as caught:
            asyncio.run(adapter.delete_document("source-1"))
        self.assertEqual(caught.exception.code, "document_delete_failed")

    def test_missing_provider_document_is_distinct_from_lookup_failure(self):
        client = ControlledClient()
        adapter = LifeContextHindsightAdapter(client, bank_id="journal")

        class MissingDocument(Exception):
            status = 404

        client.documents.error = MissingDocument("not found")
        self.assertEqual(asyncio.run(adapter.get_document("source-1")), {})

        client.documents.error = RuntimeError("provider unavailable")
        with self.assertRaises(HindsightAdapterError) as caught:
            asyncio.run(adapter.get_document("source-1"))
        self.assertEqual(caught.exception.code, "document_lookup_failed")

    def test_a_port_another_account_took_gets_no_request(self):
        import socket
        from unittest.mock import patch

        client = ControlledClient()
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            adapter = LifeContextHindsightAdapter(client, bank_id="journal", port=port)
            self.assertEqual(asyncio.run(adapter.recall("query"))["results"][0]["id"], "fact-1")
            # The same port, now held by a listener of another account.
            with patch("lifecontext.hindsight_adapter.listener_owner", return_value="foreign"):
                for call in (lambda: adapter.retain("source-1", "journal text", scope_tag="src:source-1:v1"),
                             lambda: adapter.recall("query"), lambda: adapter.get_document("source-1")):
                    with self.assertRaises(HindsightAdapterError) as caught:
                        asyncio.run(call())
                    self.assertEqual(caught.exception.code, "daemon_port_foreign")
        self.assertEqual([call[0] for call in client.calls], ["arecall"])
        with self.assertRaises(HindsightAdapterError) as caught:
            asyncio.run(adapter.recall("query"))  # Nobody listens any more.
        self.assertEqual(caught.exception.code, "daemon_port_foreign")

    def test_a_401_reads_the_renewed_token_once(self):
        class Unauthorized(Exception):
            status = 401

        old, new = ControlledClient(), ControlledClient()
        old.error = Unauthorized("token renewed by another start")
        made = []
        adapter = LifeContextHindsightAdapter(old, bank_id="journal",
                                              reconnect=lambda: made.append(new) or new)
        self.assertEqual(asyncio.run(adapter.recall("query"))["results"][0]["id"], "fact-1")
        self.assertEqual((len(made), len(new.calls)), (1, 1))
        new.error = Unauthorized("still refused")
        with self.assertRaises(HindsightAdapterError) as caught:
            asyncio.run(adapter.recall("query"))
        self.assertEqual(caught.exception.code, "recall_failed")
        self.assertEqual(len(made), 1)


if __name__ == "__main__":
    unittest.main()
