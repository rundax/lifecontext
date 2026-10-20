"""Small asynchronous boundary for the pinned Hindsight 0.9.2 client."""

from __future__ import annotations

from collections.abc import Callable, Mapping
import inspect
from typing import Any

from lifecontext.hindsight import _client_capabilities, _client_has_memory_lookup, listener_owner


_MAX_QUERY_BYTES = 65_536
_MAX_TEXT_BYTES = 1_048_576
_MAX_RESULT_TOKENS = 8_192


def source_scope_tag(source_id: str, source_version: int) -> str:
    """Tag that confines Hindsight observation consolidation to one source version.

    Observations are consolidated per tag set. Without a distinct tag every source
    shares one scope and Hindsight merges similar facts from different sources into a
    single observation, which cannot be attributed to exactly one local source version.
    """
    return f"src:{source_id}:v{source_version}"
_MAX_SOURCE_MEMORY_IDS = 256
_BUDGETS = {"low", "mid", "high"}
_DOCUMENT_STATE_FIELDS = (
    "id",
    "bank_id",
    "content_hash",
    "created_at",
    "updated_at",
    "memory_unit_count",
    "tags",
    "document_metadata",
)


class HindsightAdapterError(RuntimeError):
    """Stable domain error that never includes provider-controlled text."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _plain(value: Any) -> Any:
    """Convert common generated-client responses to JSON-shaped values."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError
        return {key: _plain(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(child) for child in value]

    for method_name, kwargs in (
        ("model_dump", {"mode": "json", "by_alias": True, "exclude_none": True}),
        ("to_dict", {}),
    ):
        method = getattr(value, method_name, None)
        if callable(method):
            try:
                data = method(**kwargs)
            except TypeError:
                data = method()
            if isinstance(data, Mapping):
                return _plain(data)

    attributes = getattr(value, "__dict__", None)
    if isinstance(attributes, dict):
        return _plain({key: child for key, child in attributes.items() if not key.startswith("_")})
    raise TypeError


def _provider_mapping(value: Any) -> dict[str, Any]:
    try:
        result = _plain(value)
    except Exception:
        raise HindsightAdapterError("invalid_provider_response") from None
    if not isinstance(result, dict):
        raise HindsightAdapterError("invalid_provider_response")
    return result


def _text(value: Any, *, max_bytes: int, code: str) -> str:
    if not isinstance(value, str) or not value:
        raise HindsightAdapterError(code)
    try:
        if len(value.encode("utf-8")) > max_bytes:
            raise HindsightAdapterError(code)
    except UnicodeError:
        raise HindsightAdapterError(code) from None
    return value


def _bounded_options(max_tokens: Any, budget: Any) -> tuple[int, str]:
    if (
        isinstance(max_tokens, bool)
        or not isinstance(max_tokens, int)
        or not 1 <= max_tokens <= _MAX_RESULT_TOKENS
        or not isinstance(budget, str)
        or budget not in _BUDGETS
    ):
        raise HindsightAdapterError("invalid_request")
    return max_tokens, budget


class LifeContextHindsightAdapter:
    """Call only the pinned client's async API and return plain domain values."""

    def __init__(self, client: Any, *, bank_id: str, port: int | None = None,
                 reconnect: Callable[[], Any] | None = None):
        """``port``: the daemon's loopback port, checked before every call; ``reconnect``
        makes a client with the current engine token, used once when the daemon answers 401."""
        if not isinstance(bank_id, str) or not bank_id.strip():
            raise HindsightAdapterError("invalid_request")
        self._bank_id = bank_id
        self._port = port
        self._reconnect = reconnect
        self._use(client)
        self.capabilities = _client_capabilities(client)
        self._memory_lookup_available = _client_has_memory_lookup(client)

    def _use(self, client: Any) -> None:
        self._client = client
        try:
            self._documents = getattr(client, "documents", None)
        except Exception:
            self._documents = None
        try:
            self._memory = getattr(client, "memory", None)
        except Exception:
            self._memory = None

    def _renew_client(self) -> bool:
        """After a 401: a client with the token a later start wrote (each start renews it)."""
        if self._reconnect is None:
            return False
        try:
            client = self._reconnect()
        except Exception:  # noqa: BLE001 -- no new client: report the original failure
            return False
        old, self._reconnect = self._client, None  # Once: a second 401 is a real refusal.
        self._use(client)
        try:
            old.close()
        except Exception:  # noqa: BLE001, S110 -- closing the replaced client is best effort
            pass
        return True

    async def _invoke(
        self,
        owner: Any,
        method_name: str,
        capability: str,
        failure_code: str,
        not_found_is_missing: bool = False,
        **kwargs: Any,
    ) -> Any:
        # The daemon may have died and someone else taken its port: send it nothing,
        # neither the token nor journal text, and believe no answer from it.
        if self._port is not None and listener_owner(self._port) != "ours":
            raise HindsightAdapterError("daemon_port_foreign")
        role = "client" if owner is self._client else "documents" if owner is self._documents else (
            "memory" if owner is self._memory else None)
        try:
            method = getattr(owner, method_name, None)
        except Exception:
            method = None
        if not self.capabilities.get(capability) or not callable(method) or not inspect.iscoroutinefunction(method):
            raise HindsightAdapterError("capability_unavailable")
        try:
            return await method(**kwargs)
        except Exception as exc:
            status = getattr(exc, "status", None)
            if status is None:
                status = getattr(getattr(exc, "response", None), "status_code", None)
            if not_found_is_missing and status in (404, "404"):
                return {}
            if status in (401, "401") and role is not None and self._renew_client():
                return await self._invoke(getattr(self, f"_{role}"), method_name, capability, failure_code,
                                          not_found_is_missing, **kwargs)
            raise HindsightAdapterError(failure_code) from None

    async def retain(
        self,
        source_id: str,
        content: str,
        *,
        scope_tag: str,
        metadata: Mapping[str, str] | None = None,
        retain_async: bool = False,
    ) -> dict[str, Any]:
        source_id = _text(source_id, max_bytes=4096, code="invalid_request")
        content = _text(content, max_bytes=_MAX_TEXT_BYTES, code="invalid_request")
        scope_tag = _text(scope_tag, max_bytes=4096, code="invalid_request")
        if not isinstance(retain_async, bool) or (
            metadata is not None
            and (
                not isinstance(metadata, Mapping)
                or any(not isinstance(key, str) or not isinstance(value, str) for key, value in metadata.items())
            )
        ):
            raise HindsightAdapterError("invalid_request")
        response = await self._invoke(
            self._client,
            "aretain",
            "aretain",
            "retain_failed",
            bank_id=self._bank_id,
            content=content,
            document_id=source_id,
            tags=[scope_tag],
            metadata=dict(metadata) if metadata is not None else None,
            retain_async=retain_async,
        )
        data = _provider_mapping(response)
        async_status = data.get("async", data.get("var_async", False))
        operation_id = data.get("operation_id")
        if data.get("success") is not True or not isinstance(async_status, bool):
            raise HindsightAdapterError("retain_failed")
        if operation_id is not None and not isinstance(operation_id, str):
            raise HindsightAdapterError("invalid_provider_response")
        return {
            "success": True,
            "async": async_status,
            "operation_id": operation_id,
        }

    async def recall(
        self,
        query: str,
        *,
        max_tokens: int = 4096,
        budget: str = "mid",
    ) -> dict[str, Any]:
        query = _text(query, max_bytes=_MAX_QUERY_BYTES, code="invalid_request")
        max_tokens, budget = _bounded_options(max_tokens, budget)
        response = await self._invoke(
            self._client,
            "arecall",
            "arecall",
            "recall_failed",
            bank_id=self._bank_id,
            query=query,
            max_tokens=max_tokens,
            budget=budget,
        )
        return _provider_mapping(response)

    async def reflect(
        self,
        query: str,
        *,
        max_tokens: int = 4096,
        budget: str = "low",
    ) -> dict[str, Any]:
        query = _text(query, max_bytes=_MAX_QUERY_BYTES, code="invalid_request")
        max_tokens, budget = _bounded_options(max_tokens, budget)
        if not self.capabilities.get("attributed_reflect"):
            raise HindsightAdapterError("capability_unavailable")
        response = await self._invoke(
            self._client,
            "areflect",
            "areflect",
            "reflect_failed",
            bank_id=self._bank_id,
            query=query,
            max_tokens=max_tokens,
            budget=budget,
            include_facts=True,
        )
        return _provider_mapping(response)

    async def get_document(self, source_id: str) -> dict[str, Any]:
        source_id = _text(source_id, max_bytes=4096, code="invalid_request")
        response = await self._invoke(
            self._documents,
            "get_document",
            "documents.get_document",
            "document_lookup_failed",
            not_found_is_missing=True,
            bank_id=self._bank_id,
            document_id=source_id,
        )
        data = _provider_mapping(response)
        return {field: data[field] for field in _DOCUMENT_STATE_FIELDS if field in data}

    async def get_memory(self, memory_id: str) -> dict[str, Any]:
        """Return only the pinned memory identity fields needed for local attribution."""
        memory_id = _text(memory_id, max_bytes=4096, code="invalid_request")
        if not self._memory_lookup_available:
            raise HindsightAdapterError("capability_unavailable")
        try:
            response = await self._memory.get_memory(
                bank_id=self._bank_id,
                memory_id=memory_id,
            )
        except Exception as exc:
            status = getattr(exc, "status", None)
            if status is None:
                status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in (404, "404"):
                return {}
            raise HindsightAdapterError("memory_lookup_failed") from None
        data = _provider_mapping(response)
        if not data:
            return {}
        if data.get("id") != memory_id:
            raise HindsightAdapterError("invalid_provider_response")

        result = {
            key: data[key]
            for key in ("id", "source_id", "document_id", "source_version", "state")
            if key in data
        }
        metadata = data.get("metadata")
        if metadata is not None:
            if not isinstance(metadata, Mapping):
                raise HindsightAdapterError("invalid_provider_response")
            result["metadata"] = {
                key: metadata[key]
                for key in ("source_id", "source_version", "sha256")
                if key in metadata
            }
        source_memory_ids = data.get("source_memory_ids")
        if source_memory_ids is not None:
            if (
                not isinstance(source_memory_ids, list)
                or len(source_memory_ids) > _MAX_SOURCE_MEMORY_IDS
                or any(not isinstance(item, str) or not item for item in source_memory_ids)
            ):
                raise HindsightAdapterError("invalid_provider_response")
            result["source_memory_ids"] = source_memory_ids
        return result

    async def delete_document(self, source_id: str) -> dict[str, Any]:
        source_id = _text(source_id, max_bytes=4096, code="invalid_request")
        response = await self._invoke(
            self._documents,
            "delete_document",
            "documents.delete_document",
            "document_delete_failed",
            bank_id=self._bank_id,
            document_id=source_id,
        )
        data = _provider_mapping(response)
        if data.get("success") is not True:
            raise HindsightAdapterError("document_delete_failed")
        return {
            key: data[key]
            for key in ("success", "document_id", "memory_units_deleted")
            if key in data
        }
