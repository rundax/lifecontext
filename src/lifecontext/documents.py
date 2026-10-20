"""Restart-safe original custody, extraction and acknowledged document indexing."""

from __future__ import annotations

import hashlib
import inspect
import json
import mimetypes
import re
import threading
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

from lifecontext.catalog import CatalogConflict, CatalogError
from lifecontext.custody import CustodyError, SourceManifest
from lifecontext.extraction import (
    ExtractionArtifact,
    ExtractionLimits,
    Locator,
    TextSegment,
    compact_ledger,
    coverage_summary,
    extract_document,
)
from lifecontext.hindsight_adapter import source_scope_tag
from lifecontext.vision import VisionUnavailable

# Written into every extraction a document opted out of; any later rerun path reads it.
VISION_OPT_OUT = "vision_opted_out; this document is never sent to a vision model"


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
    ).hexdigest()


class DocumentService:
    """Keep originals independent of all downstream, retryable stages."""

    def __init__(
        self,
        catalog,
        custody,
        derived,
        hindsight,
        *,
        limits=None,
        extractor=extract_document,
        wiki_hook=None,
        projection_hook=None,
        completion_hook=None,
        tesseract_path=None,
        max_chunk_bytes=1_048_576,
        vision=None,
        vision_cache=None,
    ):
        if (
            isinstance(max_chunk_bytes, bool)
            or not isinstance(max_chunk_bytes, int)
            or not 4 <= max_chunk_bytes <= 1_048_576
        ):
            raise CatalogError("invalid document chunk limit")
        self.catalog, self.custody, self.derived, self.hindsight = (
            catalog,
            custody,
            derived,
            hindsight,
        )
        self.limits = limits or ExtractionLimits()
        self.extractor = extractor
        self.wiki_hook, self.projection_hook = wiki_hook, projection_hook
        self.completion_hook = completion_hook
        self.tesseract_path = tesseract_path
        self.max_chunk_bytes = max_chunk_bytes
        self.vision, self.vision_cache = vision, vision_cache
        self._coverage_cache: dict[str, dict] = {}
        # Reports read coverage off the event loop; the cache is shared.
        self._coverage_lock = threading.Lock()

    async def import_document(
        self,
        original_bytes,
        filename,
        declared_mime,
        provenance,
        idempotency_key,
        *,
        source_id=None,
        source_version=None,
        _allow_existing=False,
        vision=True,
    ):
        if not isinstance(original_bytes, bytes) or len(original_bytes) > min(
            self.limits.max_original_bytes, self.custody.max_bytes
        ):
            raise CatalogError("invalid document bytes or size")
        mime = (
            declared_mime
            or mimetypes.guess_type(filename)[0]
            or "application/octet-stream"
        )
        if (
            not isinstance(filename, str)
            or not filename
            or filename in {".", ".."}
            or any(c in filename for c in "/\\")
            or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in filename)
            or len(filename.encode()) > 255
        ):
            raise CatalogError("invalid document filename")
        if (
            not isinstance(mime, str)
            or len(mime.encode()) > 255
            or not re.fullmatch(r"[a-z0-9][a-z0-9.+_-]*/[a-z0-9][a-z0-9.+_-]*", mime)
        ):
            raise CatalogError("invalid document MIME")
        if (source_id is None) != (source_version is None):
            raise CatalogError("explicit document revision required")
        if not isinstance(vision, bool):
            raise CatalogError("invalid vision choice")
        title = Path(filename).stem or filename
        checksum = hashlib.sha256(original_bytes).hexdigest()
        # Validate before admitting an operation; no body or extracted text enters its JSON.
        identity = source_id or "doc_" + uuid.uuid4().hex
        version = source_version if source_version is not None else 1
        manifest = SourceManifest(
            identity, version, checksum, len(original_bytes), provenance
        )
        request = {
            "sha256": checksum,
            "size_bytes": len(original_bytes),
            "filename": filename,
            "mime_type": mime,
            "title": title,
            "provenance": manifest.to_dict()["provenance"],
            "source_id": source_id,
            "source_version": source_version,
        }
        if not vision:
            # Only an opt-out joins the digest, so existing import keys keep their identity.
            request["vision"] = False
        digest = _digest(request)
        prior = self.catalog.lookup_operation(idempotency_key, digest, "document_import")
        if source_id is not None:
            source = self.catalog.get_source(source_id)
            current = self.catalog.get_current_manifest(source_id)
            existing_matches = False
            if _allow_existing and current is not None and current.to_dict() == manifest.to_dict():
                evidence = self.custody.resolve_exact(source_id, source_version)
                existing_matches = evidence.manifest.to_dict() == manifest.to_dict() and evidence.original_bytes == original_bytes
            # A forgotten or deleted document takes no new version: its wiki page and
            # file names keep the title it had, which must stay withdrawn.
            if (
                source is None
                or source["layer"] != "document"
                or current is None
                or self.catalog.source_withdrawn(source_id, current.source_version)
                or (prior is None and source_version != current.source_version + 1 and not existing_matches)
                or (_allow_existing and not existing_matches)
            ):
                raise CatalogConflict("invalid document replacement")
        operation = self.catalog.begin_operation(
            idempotency_key, digest, "document_import"
        )
        if not operation["result"]:
            intent = {
                "kind": "document_import",
                "source_id": identity,
                "source_version": version,
                **{
                    k: request[k]
                    for k in (
                        "sha256",
                        "size_bytes",
                        "filename",
                        "mime_type",
                        "title",
                        "provenance",
                    )
                },
                **({"vision": False} if not vision else {}),
                "request_digest": digest,
            }
            operation = self.catalog.set_operation_intent(
                operation["operation_id"], self._envelope(intent)
            )
        if operation["state"] in {"completed", "permanent_failed", "cancelled"}:
            return self._response(operation)
        return await self._run(operation, original_bytes=original_bytes)

    @staticmethod
    def _envelope(intent):
        return {
            "sources": [
                {k: intent[k] for k in ("source_id", "source_version", "sha256")}
            ],
            "size_bytes": intent["size_bytes"],
            "recovery_intent": intent,
        }

    async def recover(self, operation):
        if isinstance(operation, str):
            operation = self.catalog.get_operation(operation)
        if not operation or operation["operation_kind"] not in {
            "document_import",
            "document_retry",
        }:
            raise CatalogError("document operation unavailable")
        if operation["state"] in {"completed", "permanent_failed", "cancelled"}:
            return self._response(operation)
        return await self._run(operation)

    async def retry(self, operation_id, rerun_extraction=False, vision=None):
        operation = self.catalog.get_operation(operation_id)
        if (
            not operation
            or operation["operation_kind"] not in {"document_import", "document_retry"}
            or not isinstance(rerun_extraction, bool)
            or (vision is not None and not isinstance(vision, bool))
            or (vision is not None and not rerun_extraction)
        ):
            raise CatalogError("document operation unavailable")
        if not rerun_extraction:
            return await self.recover(operation)
        old = operation["result"].get("recovery_intent")
        if not old:
            raise CatalogError("document intent unavailable")
        sid, version = old["source_id"], old["source_version"]
        self._active(sid, version)
        extractions = self.catalog.list_extractions(sid, version)
        revision = extractions[-1]["extraction_revision"] + 1 if extractions else 1
        intent = {
            k: old[k] for k in ("source_id", "source_version", "sha256", "size_bytes")
        }
        intent.update(
            kind="document_retry", rerun_extraction=True, extraction_revision=revision
        )
        if vision is None and (self._opted_out(sid, version) or old.get("vision") is False):
            vision = False
        if vision is not None:
            # An explicit choice is durable for this revision; otherwise the ledger decides.
            intent["vision"] = vision
        digest = _digest(intent)
        intent["request_digest"] = digest
        # The choice is part of the key, so --no-vision can follow a rerun that is
        # stuck on a vision outage instead of conflicting with it.
        choice = "" if vision is None else (":vision" if vision else ":no-vision")
        retry = self.catalog.begin_operation(
            f"document-rerun:{sid}:{version}:{revision}{choice}", digest, "document_retry"
        )
        if not retry["result"]:
            retry = self.catalog.set_operation_intent(
                retry["operation_id"], self._envelope(intent)
            )
        elif retry["result"].get("recovery_intent") != intent:
            raise CatalogConflict("document retry identity conflict")
        return await self._run(retry)

    def _active(self, sid, version):
        # A forgotten version is never indexed again, though a forget writes no tombstone.
        if self.catalog.source_withdrawn(sid, version):
            raise CatalogConflict("document evidence unavailable")

    def _opted_out(self, sid, version, before=None):
        """The document's latest earlier extraction, of any version, carries the choice.

        An opt-out therefore covers later versions of the same document, and an
        explicit ``vision=True`` rerun lifts it for every later rerun.
        """
        latest = None
        for manifest in self.catalog.list_source_manifests(sid, limit=1000):
            if manifest.source_version > version:
                continue
            for record in self.catalog.list_extractions(sid, manifest.source_version):
                if (
                    manifest.source_version == version
                    and before is not None
                    and record["extraction_revision"] >= before
                ):
                    continue
                key = (manifest.source_version, record["extraction_revision"])
                if latest is None or key > latest[0]:
                    latest = (key, record)
        return latest is not None and VISION_OPT_OUT in latest[1]["warnings"]

    def forget_vision_cache(self, source_id):
        if self.vision_cache is not None:
            self.vision_cache.remove(source_id)

    def _stage(self, sid, version, stage, state, error=None):
        current = self.catalog.get_document_stage(sid, version, stage)
        if current["state"] == state:
            return current
        if current["state"] == "completed" and state == "running":
            current = self.catalog.transition_document_stage(
                sid,
                version,
                stage,
                expected_state="completed",
                expected_revision=current["revision"],
                state="pending",
            )
        return self.catalog.transition_document_stage(
            sid,
            version,
            stage,
            expected_state=current["state"],
            expected_revision=current["revision"],
            state=state,
            error_code=error,
        )

    async def _run(self, operation, *, original_bytes=None):
        intent = operation["result"].get("recovery_intent")
        if not intent:
            raise CatalogError("document intent unavailable")
        sid, version = intent["source_id"], intent["source_version"]
        result = self._envelope(intent)
        if operation["state"] != "running":
            operation = self.catalog.transition_operation(
                operation["operation_id"], operation["state"], "running", result=result
            )
        try:
            self._active(sid, version)
            source = self.catalog.get_source(sid)
            if source is not None and source["layer"] != "document":
                raise CatalogConflict("document layer conflict")
            registered = self.catalog.get_manifest(sid, version)
            if intent["kind"] == "document_retry":
                if source is None or registered is None or (
                    registered["sha256"], registered["size_bytes"]
                ) != (intent["sha256"], intent["size_bytes"]):
                    raise CatalogConflict("document retry identity conflict")
            elif version > 1:
                current = self.catalog.get_current_manifest(sid)
                if current is None or version not in {current.source_version, current.source_version + 1}:
                    raise CatalogConflict("document version conflict")
            try:
                evidence = self.custody.resolve_exact(sid, version)
            except CustodyError:
                # A replay may supply the bytes only if this exact version is absent.
                if (
                    original_bytes is None
                    or (sid, version) in self.custody.list_inventory()
                ):
                    raise CatalogError("document custody unavailable") from None
                self.custody.write_version(
                    sid, version, original_bytes, provenance=intent["provenance"]
                )
                evidence = self.custody.resolve_exact(sid, version)
            manifest = evidence.manifest
            if (
                manifest.sha256 != intent["sha256"]
                or manifest.size_bytes != intent["size_bytes"]
                or (
                    intent["kind"] == "document_import"
                    and manifest.to_dict()["provenance"] != intent["provenance"]
                )
                or (
                    intent["kind"] == "document_retry"
                    and manifest.to_dict()["provenance"] != registered["provenance"]
                )
            ):
                raise CatalogConflict("document custody conflict")
            self.catalog.register_manifest(manifest, layer="document")
            if intent["kind"] == "document_import":
                self.catalog.register_document_metadata(
                    sid,
                    version,
                    **{k: intent[k] for k in ("filename", "title", "mime_type")},
                )
            metadata = self.catalog.get_document_metadata(sid, version)
            extractions = self.catalog.list_extractions(sid, version)
            revision = intent.get(
                "extraction_revision",
                extractions[-1]["extraction_revision"] if extractions else 1,
            )
            record = self.catalog.get_extraction(sid, version, revision)
            if record is None:
                if intent.get("rerun_extraction"):
                    for stage in ("wiki", "projection"):
                        if (
                            self.catalog.get_document_stage(sid, version, stage)[
                                "state"
                            ]
                            == "completed"
                        ):
                            self._stage(sid, version, stage, "pending")
                    self.catalog.mark_dependent_wiki_stale(sid, version)
                self._stage(sid, version, "extraction", "running")
                try:
                    payload = self.derived.read(sid, version, revision)
                except ValueError:
                    allowed = intent.get("vision")
                    if allowed is None:
                        allowed = not self._opted_out(sid, version, before=revision)
                    extractor = (
                        self.vision.extract
                        if allowed and self.vision is not None
                        else self.extractor
                    )
                    try:
                        artifact = extractor(
                            evidence.original_bytes,
                            metadata["filename"],
                            source_id=sid,
                            source_version=version,
                            extraction_revision=revision,
                            limits=self.limits,
                            **({"tesseract_path": self.tesseract_path} if self.tesseract_path is not None else {}),
                        )
                        if inspect.isawaitable(artifact):
                            artifact = await artifact
                        artifact = self._bound_segments(artifact)
                    except VisionUnavailable:
                        # Retryable: nothing is written, so a plain retry re-enters this
                        # revision and resumes from the per-page cache.
                        raise
                    except Exception:  # noqa: BLE001 -- persist redacted extractor failure
                        artifact = ExtractionArtifact(
                            source_id=sid,
                            source_version=version,
                            extraction_revision=revision,
                            original_checksum=manifest.sha256,
                            config_identity=self.limits.identity,
                            status="failed",
                            reason="extractor_failed",
                        )
                    if not allowed:
                        # First, so ledger compaction can never push it out.
                        artifact = replace(
                            artifact, warnings=compact_ledger((VISION_OPT_OUT, *artifact.warnings))
                        )
                    if self.catalog.list_tombstones(sid):
                        # Forgotten or deleted while a model was reading it.
                        self.forget_vision_cache(sid)
                        raise CatalogConflict("document evidence unavailable")
                    payload = json.loads(
                        json.dumps(artifact.to_dict(), allow_nan=False)
                    )
                self._validate_artifact(payload, manifest, revision)
                stored = self.derived.write(sid, version, revision, payload)
                record = self.catalog.record_extraction(
                    sid,
                    version,
                    revision,
                    original_sha256=manifest.sha256,
                    extractor=payload["extractor"],
                    extractor_version=payload["extractor_version"],
                    config_sha256=payload["config_identity"],
                    artifact_path=str(
                        stored.path.relative_to(self.derived.paths.lifeweave_home)
                    ),
                    artifact_sha256=stored.sha256,
                    status=payload["status"],
                    passage_count=len(payload["segments"]),
                    warnings=list(payload["warnings"]),
                )
            else:
                payload = self.derived.read(
                    sid, version, revision, expected_sha256=record["artifact_sha256"]
                )
                self._validate_artifact(payload, manifest, revision)
            if record["status"] != "completed":
                self._stage(sid, version, "extraction", "running")
                self._stage(
                    sid,
                    version,
                    "extraction",
                    "blocked"
                    if record["status"] in {"blocked", "unsupported", "locked"}
                    else "failed",
                    "evidence_unavailable",
                )
                return self._fail(operation, result, "evidence_unavailable")
            if (
                self.catalog.get_document_stage(sid, version, "extraction")["state"]
                != "completed"
            ):
                self._stage(sid, version, "extraction", "running")
            self._stage(sid, version, "extraction", "completed")
            indexing_failed = False
            try:
                await self._index(manifest, revision, payload)
                if self.completion_hook is not None:
                    completion = self.completion_hook(self.show(sid, version))
                    if inspect.isawaitable(completion):
                        await completion
            except Exception:  # noqa: BLE001 -- durable boundary redacts dependency failures
                indexing_failed = True
            # Hooks own their effects; only an explicit completed response is acknowledgement.
            for stage, hook in (
                ("wiki", self.wiki_hook),
                ("projection", self.projection_hook),
            ):
                if (
                    hook is None
                    or (stage == "wiki" and indexing_failed)
                    or self.catalog.get_document_stage(sid, version, stage)["state"]
                    == "completed"
                ):
                    continue
                self._active(sid, version)
                self._stage(sid, version, stage, "running")
                try:
                    outcome = hook(self.show(sid, version))
                    if inspect.isawaitable(outcome):
                        outcome = await outcome
                    if (
                        isinstance(outcome, dict)
                        and outcome.get("state") == "completed"
                    ):
                        self._active(sid, version)
                        self._stage(sid, version, stage, "completed")
                    elif (
                        isinstance(outcome, dict) and outcome.get("state") == "conflict"
                    ):
                        self._stage(sid, version, stage, "conflict", "conflict")
                    elif not isinstance(outcome, dict) or outcome.get("state") not in {
                        "pending",
                        "running",
                    }:
                        self._stage(
                            sid, version, stage, "failed", "evidence_unavailable"
                        )
                except Exception:  # noqa: BLE001 -- durable boundary redacts dependency failures
                    self._stage(sid, version, stage, "failed", "internal_error")
            if indexing_failed:
                return self._fail(operation, result, "provider_unavailable")
            operation = self.catalog.transition_operation(
                operation["operation_id"], "running", "completed", result=result
            )
            return self._response(operation)
        except VisionUnavailable:
            if self.catalog.list_tombstones(sid):
                # Forgotten while a model was reading it: drop what this run cached.
                self.forget_vision_cache(sid)
            self._stage(sid, version, "extraction", "failed", "provider_unavailable")
            return self._fail(operation, result, "provider_unavailable")
        except Exception:  # noqa: BLE001 -- durable boundary redacts dependency failures
            code = "evidence_unavailable"
            if self.catalog.get_document_metadata(sid, version):
                for stage in ("extraction", "indexing"):
                    if (
                        self.catalog.get_document_stage(sid, version, stage)["state"]
                        == "running"
                    ):
                        code = (
                            "provider_unavailable"
                            if stage == "indexing"
                            else "internal_error"
                        )
                        self._stage(sid, version, stage, "failed", code)
            return self._fail(operation, result, code)

    def _fail(self, operation, result, code):
        operation = self.catalog.transition_operation(
            operation["operation_id"],
            "running",
            "retryable_failed",
            result=result,
            error_code=code,
        )
        return self._response(operation)

    def _bound_segments(self, artifact):
        segments, offset = [], 0
        for segment in artifact.segments:
            remaining = segment.text
            while remaining:
                text = remaining.encode()[: self.max_chunk_bytes].decode(
                    "utf-8", errors="ignore"
                )
                segments.append(
                    TextSegment(text, offset, offset + len(text), segment.locator)
                )
                if len(segments) > self.limits.max_segments:
                    return replace(
                        artifact,
                        status="limit_exceeded",
                        segments=(),
                        reason="segment_limit",
                    )
                offset += len(text)
                remaining = remaining[len(text) :]
        return replace(artifact, segments=tuple(segments))

    @staticmethod
    def _validate_artifact(payload, manifest, revision):
        values = dict(payload)
        values["segments"] = tuple(
            TextSegment(
                segment["text"],
                segment["start"],
                segment["end"],
                Locator(**segment["locator"]),
            )
            for segment in payload["segments"]
        )
        values["warnings"] = tuple(payload["warnings"])
        ExtractionArtifact(**values)
        if (
            payload.get("source_id") != manifest.source_id
            or payload.get("source_version") != manifest.source_version
            or payload.get("original_checksum") != manifest.sha256
            or payload.get("extraction_revision") != revision
        ):
            raise CatalogConflict("document extraction identity conflict")

    async def _index(self, manifest, revision, payload):
        sid, version = manifest.source_id, manifest.source_version
        self._active(sid, version)
        self._stage(sid, version, "indexing", "running")
        chunks = []
        start, texts, size, method = 0, [], 0, None
        for index, segment in enumerate(payload["segments"]):
            text = segment["text"]
            length = len(text.encode())
            if length > self.max_chunk_bytes:
                raise CatalogError("document passage exceeds chunk limit")
            # Model-read passages never share a chunk with text-layer or OCR passages.
            current = str(segment["locator"].get("container") or "").startswith("vision:")
            if texts and (size + length > self.max_chunk_bytes or current != method):
                chunks.append((start, index, "".join(texts)))
                start, texts, size = index, [], 0
            texts.append(text)
            size += length
            method = current
        if texts:
            chunks.append((start, len(payload["segments"]), "".join(texts)))
        prepared = []
        for start, end, text in chunks:
            pid = f"doc:{sid}:v{version}:e{revision}:p{start}-{end}"
            checksum = hashlib.sha256(text.encode()).hexdigest()
            chunk = self.catalog.record_provider_chunk(
                pid,
                sid,
                version,
                revision,
                passage_start=start,
                passage_end=end,
                content_sha256=checksum,
            )
            prepared.append((chunk, text))
        for chunk, text in prepared:
            if chunk["completion_state"] == "completed":
                continue
            self._active(sid, version)
            metadata = {
                "source_id": sid,
                "source_version": str(version),
                "sha256": manifest.sha256,
                "layer": "document",
                "extraction_revision": str(revision),
                "passage_start": str(chunk["passage_start"]),
                "passage_end": str(chunk["passage_end"]),
                "content_sha256": chunk["content_sha256"],
            }
            try:
                document = await self.hindsight.get_document(chunk["provider_id"])
                if document:
                    if (
                        document.get("id") != chunk["provider_id"]
                        or document.get("document_metadata") != metadata
                    ):
                        raise CatalogConflict("document provider conflict")
                    count = document.get("memory_unit_count")
                    if (
                        isinstance(count, int)
                        and not isinstance(count, bool)
                        and count > 0
                    ):
                        self.catalog.update_provider_chunk(
                            chunk["provider_id"],
                            expected_revision=chunk["revision"],
                            submission_state="submitted",
                            completion_state="completed",
                        )
                        continue
                    raise CatalogError("document acknowledgement pending")
                # Persist ambiguous submission before the external effect.
                chunk = self.catalog.update_provider_chunk(
                    chunk["provider_id"],
                    expected_revision=chunk["revision"],
                    submission_state="submitted",
                    completion_state="pending",
                )
                self._active(sid, version)
                ack = await self.hindsight.retain(
                    chunk["provider_id"], text, scope_tag=source_scope_tag(sid, version),
                    metadata=metadata, retain_async=False,
                )
                complete = ack.get("success") is True and ack.get("async") is False
                self.catalog.update_provider_chunk(
                    chunk["provider_id"],
                    expected_revision=chunk["revision"],
                    submission_state="submitted",
                    completion_state="completed" if complete else "pending",
                )
                if not complete:
                    raise CatalogError("document acknowledgement pending")
            except Exception:
                self._stage(sid, version, "indexing", "failed", "provider_unavailable")
                raise
        for old in self.catalog.list_provider_chunks(sid, version):
            if old["extraction_revision"] >= revision or (
                old["submission_state"] == "retired"
                and old["completion_state"] == "completed"
            ):
                continue
            self._active(sid, version)
            if old["submission_state"] == "pending":
                self.catalog.update_provider_chunk(
                    old["provider_id"], expected_revision=old["revision"],
                    submission_state="retired", completion_state="completed",
                )
                continue
            try:
                old = self.catalog.update_provider_chunk(
                    old["provider_id"], expected_revision=old["revision"],
                    submission_state="retired", completion_state="pending",
                )
                document = await self.hindsight.get_document(old["provider_id"])
                if document:
                    metadata = {
                        "source_id": sid,
                        "source_version": str(version),
                        "sha256": manifest.sha256,
                        "layer": "document",
                        "extraction_revision": str(old["extraction_revision"]),
                        "passage_start": str(old["passage_start"]),
                        "passage_end": str(old["passage_end"]),
                        "content_sha256": old["content_sha256"],
                    }
                    if (
                        document.get("id") != old["provider_id"]
                        or document.get("document_metadata") != metadata
                    ):
                        raise CatalogConflict("document provider conflict")
                    self._active(sid, version)
                    deleted = await self.hindsight.delete_document(old["provider_id"])
                    if deleted.get("success") is not True:
                        raise CatalogError("document chunk retirement pending")
                self.catalog.update_provider_chunk(
                    old["provider_id"], expected_revision=old["revision"],
                    submission_state="retired", completion_state="completed",
                )
            except Exception:
                self._stage(sid, version, "indexing", "failed", "provider_unavailable")
                raise
        self._stage(sid, version, "indexing", "completed")

    def list(self, *, limit=100):
        return [
            self.show(row["source_id"], row["source_version"])
            for row in self.catalog.list_documents(limit=limit, deleted=False)
        ]

    def show(self, source_id, source_version=None):
        if source_version is None:
            manifest = self.catalog.get_current_manifest(source_id)
            if manifest is None:
                raise CatalogError("document unavailable")
            source_version = manifest.source_version
        metadata = self.catalog.get_document_metadata(source_id, source_version)
        if metadata is None:
            raise CatalogError("document unavailable")
        stages = {
            stage: self.catalog.get_document_stage(source_id, source_version, stage)
            for stage in ("custody", "extraction", "indexing", "wiki", "projection")
        }
        extractions = self.catalog.list_extractions(source_id, source_version)
        for record in extractions:
            if record["status"] != "completed" and record["artifact_sha256"]:
                try:
                    artifact = self.derived.read(
                        source_id,
                        source_version,
                        record["extraction_revision"],
                        expected_sha256=record["artifact_sha256"],
                    )
                    reason = artifact.get("reason")
                    record["reason"] = (
                        reason
                        if isinstance(reason, str)
                        and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", reason)
                        else "evidence_unavailable"
                    )
                except ValueError:
                    record["reason"] = "evidence_unavailable"
        return {
            **metadata,
            "accepted": stages["custody"]["state"] == "accepted",
            "stages": stages,
            "extractions": extractions,
            "coverage": self._coverage(source_id, source_version, extractions),
            "chunks": self.catalog.list_provider_chunks(source_id, source_version),
        }

    def _coverage(self, source_id, source_version, extractions):
        """Summarize the latest extraction's ledger; artifacts are immutable per checksum."""
        if not extractions or not extractions[-1]["artifact_sha256"]:
            return None
        record = extractions[-1]
        checksum = record["artifact_sha256"]
        with self._coverage_lock:
            summary = self._coverage_cache.get(checksum)
        if summary is None:
            try:
                payload = self.derived.read(
                    source_id,
                    source_version,
                    record["extraction_revision"],
                    expected_sha256=checksum,
                )
            except ValueError:
                return None
            summary = coverage_summary(payload)
            with self._coverage_lock:
                if len(self._coverage_cache) >= 256:
                    self._coverage_cache.pop(next(iter(self._coverage_cache)))
                self._coverage_cache[checksum] = summary
        return {key: list(value) if isinstance(value, list) else value
                for key, value in summary.items()}

    def coverage(self, source_id, source_version, extraction):
        """Coverage of one extraction record (``extraction_revision``, ``artifact_sha256``)."""
        return self._coverage(source_id, source_version, [extraction])

    def _response(self, operation):
        result = dict(operation)
        intent = operation["result"].get("recovery_intent")
        if intent and self.catalog.get_document_metadata(
            intent["source_id"], intent["source_version"]
        ):
            result["document"] = self.show(
                intent["source_id"], intent["source_version"]
            )
        return result
