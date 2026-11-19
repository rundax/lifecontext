"""Restart-safe original custody, extraction and acknowledged document indexing."""

from __future__ import annotations

import contextlib
import hashlib
import inspect
import json
import logging
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
from lifecontext.vault import default_journal_paths
from lifecontext.vision import VisionUnavailable
from lifecontext.wiki import source_page_id

# Written into every extraction a document opted out of; any later rerun path reads it.
VISION_OPT_OUT = "vision_opted_out; this document is never sent to a vision model"

_PROBLEM_STATES = {"failed", "blocked", "conflict"}
# Vault receipts are relative to the Journal folder; responses give the path from the vault
# root. The service passes the configured folder; this is the default one.
_JOURNAL_FOLDER = default_journal_paths("")[1].name
_NOT_LOOKED_UP = object()
_FORMATS = "PDF, DOCX, XLSX, PPTX, TXT, Markdown, PNG, JPEG or TIFF"
# What a person can do about an extraction that could not read the file.
_EXTRACTION_STEPS = {
    "unsupported_format": f"This file type is not supported; save it as {_FORMATS} and import it again.",
    "macro_enabled_office": f"Macro-enabled Office files are not supported; save it as {_FORMATS} and import it again.",
    "encrypted_or_legacy_office": "The file is password-protected or in an old Office format; remove the password or save it as DOCX, XLSX or PPTX and import it again.",
    "encrypted_pdf": "The PDF is password-protected; save an unlocked copy and import it again.",
    "encrypted_zip": "The archive is password-protected; import an unlocked copy.",
    "no_extractable_text": "No text could be read; install tesseract for OCR or turn on vision, then run: lifecontext documents retry {source_id} --rerun-extraction",
    "ocr_backend_not_configured": "OCR is not available; install tesseract, then run: lifecontext documents retry {source_id} --rerun-extraction",
    "ocr_executable_unavailable": "OCR is not available; install tesseract, then run: lifecontext documents retry {source_id} --rerun-extraction",
}
# What a person can do about a wiki stage that failed, by the reason it stored.
_WIKI_STEPS = {
    "wiki_provider_failed": "The model did not answer; check it with: lifecontext doctor, then run: lifecontext documents retry {source_id}",
    # A retry would fail the same way, so these two suggest none.
    "wiki_evidence_limit": "One passage is too large for a single model request and a retry does not change that; import a version with shorter passages.",
    "wiki_no_evidence": "The document has no readable text to summarise and a retry does not change that; check the extraction with: lifecontext documents show {source_id}",
    "wiki_update_rejected": "The page failed its checks; run: lifecontext documents retry {source_id}",
    "wiki_interrupted": "An earlier run stopped part-way; run: lifecontext documents retry {source_id}",
    # The stage completed; only the page's model summary is missing.
    "wiki_summary_missing": "The page has no model summary; run: lifecontext documents retry {source_id}",
    # The stage completed from the windows that validated; a retry asks only for the others.
    "wiki_windows_skipped": "The page leaves out parts of the document the model failed on; run: lifecontext documents retry {source_id}",
}


def document_outcome(stages, operation_state=None, awaited=(), summary_missing=False, in_flight=True):
    """What an import came to, from its stage states; catalog states never change for it.

    A finished operation whose wiki page or vault note failed, or whose page has no
    model summary or skipped evidence windows (``summary_missing``), is ``completed_with_errors``.
    One whose configured wiki or vault stage is still pending or running is ``running``
    while this process runs the document (``in_flight``), not ``completed``; with nothing
    running it, such a stage waits for a retry, so it is ``completed_with_errors``.
    ``awaited`` names the stages that have a hook to wait for. The values are the catalog
    operation states plus ``completed_with_errors``.
    """
    if operation_state not in (None, "completed"):
        return operation_state
    core = [stages[name]["state"] for name in ("extraction", "indexing")]
    if any(state in _PROBLEM_STATES for state in core):
        return "retryable_failed"
    if any(state != "completed" for state in core):
        return "running"
    if summary_missing or any(stages[name]["state"] in _PROBLEM_STATES for name in ("wiki", "projection")):
        return "completed_with_errors"
    if any(stages[name]["state"] in {"pending", "running"} for name in awaited):
        return "running" if in_flight else "completed_with_errors"
    return "completed"


def document_problems(source_id, stages, extractions, summary_missing=False, stalled=(), windows_skipped=False):
    """One entry per failed, blocked or conflicting stage, with what to run next.

    ``stalled`` names pending or running stages that nothing runs; a retry runs them.
    A completed wiki stage that skipped evidence windows (``windows_skipped``) or whose
    page lacks its summary is listed too; the retry of skipped windows also asks for a summary.
    """
    problems = []
    retry = f"Run: lifecontext documents retry {source_id}"
    for name in ("extraction", "indexing", "wiki", "projection"):
        stage = stages[name]
        if name == "wiki" and (windows_skipped or summary_missing):
            code = "wiki_windows_skipped" if windows_skipped else "wiki_summary_missing"
            stage = {**stage, "state": "failed", "error_code": code}
        if stage["state"] not in _PROBLEM_STATES and name not in stalled:
            continue
        code = stage["error_code"] or stage["state"]
        reason = code
        if name == "extraction" and extractions and extractions[-1].get("reason"):
            reason = extractions[-1]["reason"]
        steps = {"extraction": _EXTRACTION_STEPS, "wiki": _WIKI_STEPS}.get(name, {})
        next_step = steps.get(reason, retry)
        if stage["state"] == "conflict":
            next_step = "The vault copy was edited; resolve the conflict, then " + retry[0].lower() + retry[1:]
        problems.append({"stage": name, "code": code, "reason": reason,
                         "next_step": next_step.format(source_id=source_id)})
    return problems


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
        wiki_summary_hook=None,
        wiki_summary_retry_hook=None,
        wiki_window_hook=None,
        projection_hook=None,
        completion_hook=None,
        tesseract_path=None,
        max_chunk_bytes=1_048_576,
        vision=None,
        vision_cache=None,
        journal_prefix=_JOURNAL_FOLDER,
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
        # source_id -> True when the document's wiki page was published without its summary.
        self.wiki_summary_hook = wiki_summary_hook
        # (source_id, version) -> revisions written: asks only for that summary, on the same page.
        self.wiki_summary_retry_hook = wiki_summary_retry_hook
        # (source_id, version) -> {"skipped": windows, "dropped": claims} of the last wiki compile.
        self.wiki_window_hook = wiki_window_hook
        self.completion_hook = completion_hook
        self.tesseract_path = tesseract_path
        self.max_chunk_bytes = max_chunk_bytes
        self.vision, self.vision_cache = vision, vision_cache
        # The Journal folder's path from the vault root, for ``vault_path``.
        self.journal_prefix = journal_prefix
        self._coverage_cache: dict[str, dict] = {}
        # Reports read coverage off the event loop; the cache is shared.
        self._coverage_lock = threading.Lock()
        # Operation IDs and source IDs this process is running right now, with counts. A
        # retry that finds its operation or source here reports the current state instead of
        # starting a second run; after a crash the set is empty and startup recovery resumes.
        # Every service of one application shares it.
        self._inflight: dict[str, int] = {}

    def in_flight(self, source_id):
        """This process is running an import, retry, reindex or rebuild of the source right now."""
        return source_id in self._inflight

    @contextlib.contextmanager
    def hold(self, *keys):
        """Mark operation or source IDs as running in this process until the block ends."""
        for key in keys:
            self._inflight[key] = self._inflight.get(key, 0) + 1
        try:
            yield
        finally:
            for key in keys:
                self._inflight[key] -= 1
                if not self._inflight[key]:
                    del self._inflight[key]

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
        guessed = mimetypes.guess_type(filename)[0] if isinstance(filename, str) else None
        # The request digest keeps the type earlier releases recorded, so an existing
        # import key or a bundle restore keeps its identity.
        declared = declared_mime or guessed or "application/octet-stream"
        # Clients send application/octet-stream when they do not know the type (the
        # CLI and API defaults); the filename says more.
        mime = guessed if declared == "application/octet-stream" and guessed else declared
        if (
            not isinstance(filename, str)
            or not filename
            or filename in {".", ".."}
            or any(c in filename for c in "/\\")
            or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in filename)
            or len(filename.encode()) > 255
        ):
            raise CatalogError("invalid document filename")
        if any(
            not isinstance(value, str)
            or len(value.encode()) > 255
            or not re.fullmatch(r"[a-z0-9][a-z0-9.+_-]*/[a-z0-9][a-z0-9.+_-]*", value)
            for value in (declared, mime)
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
            "mime_type": declared,
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
            # A version already on record (a bundle restore) keeps the type it has.
            existing = self.catalog.get_document_metadata(identity, version) if source_id is not None else None
            intent["mime_type"] = existing["mime_type"] if existing else mime
            operation = self.catalog.set_operation_intent(
                operation["operation_id"], self._envelope(intent)
            )
        if operation["state"] in {"completed", "permanent_failed", "cancelled"} or self._running(operation):
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
        if operation["state"] in {"completed", "permanent_failed", "cancelled"} or self._running(operation, by_source=True):
            return self._response(operation)
        return await self._run(operation)

    def _running(self, operation, *, by_source=False):
        """This process is already running the operation, or (by_source) any operation on its source."""
        intent = operation["result"].get("recovery_intent") or {}
        keys = [operation["operation_id"]] + ([intent["source_id"]] if by_source and "source_id" in intent else [])
        return any(key in self._inflight for key in keys)

    def _retry_target(self, target):
        """An operation ID, or a source ID whose current version's latest operation is used."""
        if not isinstance(target, str):
            raise CatalogError("document operation unavailable")
        if re.fullmatch(r"op_[0-9a-f]{32}", target):
            operation = self.catalog.get_operation(target)
            if operation is not None:
                return operation, False
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", target):
            raise CatalogError("document operation unavailable")
        current = self.catalog.get_current_manifest(target)
        if current is None:
            raise CatalogError("document operation unavailable")
        # A forgotten or deleted document is not retried by its source ID.
        self._active(target, current.source_version)
        if self.catalog.list_tombstones(target):
            raise CatalogConflict("document evidence unavailable")
        return self.catalog.latest_document_operation(target, current.source_version), True

    async def retry(self, operation_id, rerun_extraction=False, vision=None, idempotency_key=None):
        if (
            not isinstance(rerun_extraction, bool)
            or (vision is not None and not isinstance(vision, bool))
            or (vision is not None and not rerun_extraction)
        ):
            raise CatalogError("document operation unavailable")
        operation, by_source = self._retry_target(operation_id)
        if (
            not operation
            or operation["operation_kind"] not in {"document_import", "document_retry"}
        ):
            raise CatalogError("document operation unavailable")
        request_digest = _digest({"retry": operation_id, "rerun_extraction": rerun_extraction, "vision": vision})
        if idempotency_key is not None:
            # The same key and request replays (or resumes) the retry it started.
            prior = self.catalog.lookup_operation(idempotency_key, request_digest, "document_retry")
            if prior is not None:
                return await self.recover(prior)
        old = operation["result"].get("recovery_intent")
        if not old:
            raise CatalogError("document intent unavailable")
        if self._running(operation, by_source=True):
            return self._response(operation)
        sid, version = old["source_id"], old["source_version"]
        extractions = self.catalog.list_extractions(sid, version)
        if by_source and not rerun_extraction and extractions and extractions[-1]["status"] != "completed":
            # A recorded extraction that could not read the file is only retried by reading it again.
            rerun_extraction = True
        if not rerun_extraction:
            if operation["state"] == "completed" and self._unfinished_stages(sid, version):
                return await self._rerun_stages(old, extractions, idempotency_key, request_digest)
            return await self.recover(operation)
        self._active(sid, version)
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
        if idempotency_key is not None:
            retry = self.catalog.begin_operation(idempotency_key, request_digest, "document_retry")
        else:
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

    def _unfinished_stages(self, sid, version):
        """Stages a retry runs again: indexing, and for the current version the wiki and vault stages that have a hook."""
        names = ["indexing", *self._awaited(sid, version)]
        return [name for name in names
                if self.catalog.get_document_stage(sid, version, name)["state"] != "completed"
                or (name == "wiki" and (self._summary_missing(sid, version) or self._windows_skipped(sid, version)))]

    async def _rerun_stages(self, old, extractions, idempotency_key, request_digest):
        """A finished import whose wiki or vault stage did not complete runs those stages again."""
        sid, version = old["source_id"], old["source_version"]
        self._active(sid, version)
        if not extractions:
            raise CatalogError("document intent unavailable")
        intent = {k: old[k] for k in ("source_id", "source_version", "sha256", "size_bytes")}
        intent.update(kind="document_retry", extraction_revision=extractions[-1]["extraction_revision"])
        intent["request_digest"] = _digest(intent)
        if idempotency_key is not None:
            retry = self.catalog.begin_operation(idempotency_key, request_digest, "document_retry")
        else:
            # Each plain retry is a new attempt; a finished one is never replayed instead.
            retry = self.catalog.begin_operation(
                f"document-stages:{sid}:{version}:{uuid.uuid4().hex}", intent["request_digest"], "document_retry"
            )
        if not retry["result"]:
            retry = self.catalog.set_operation_intent(retry["operation_id"], self._envelope(intent))
        if retry["state"] in {"completed", "permanent_failed", "cancelled"}:
            return self._response(retry)
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
        with self.hold(operation["operation_id"], intent["source_id"]):
            operation = await self._run_stages(operation, intent, original_bytes)
        # Shown once this run has left the map, so its outcome is not ``running`` for it.
        return self._response(operation)

    async def _run_stages(self, operation, intent, original_bytes):
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
            wiki_ran = False
            # No run, an import's by its operation ID included, rebuilds the page of a
            # version a newer one replaced.
            hooks = (("wiki", self.wiki_hook), ("projection", self.projection_hook))
            if not self._is_current(sid, version):
                hooks = ()
            for stage, hook in hooks:
                if hook is None or (stage == "wiki" and indexing_failed):
                    continue
                # The vault note and page follow every wiki run, even after an
                # earlier projection completed without them.
                # A page that skipped evidence windows compiles again; the windows it has are not asked again.
                if (self.catalog.get_document_stage(sid, version, stage)["state"] == "completed"
                        and not (stage == "projection" and wiki_ran)
                        and not (stage == "wiki" and self._windows_skipped(sid, version))):
                    if (stage == "wiki" and self.wiki_summary_retry_hook is not None
                            and self._summary_missing(sid, version)):
                        # Only the summary is missing: ask for it alone, not the whole page.
                        self._active(sid, version)
                        try:
                            written = self.wiki_summary_retry_hook(sid, version)
                            if inspect.isawaitable(written):
                                written = await written
                            wiki_ran = bool(written)
                        except Exception as exc:  # noqa: BLE001 -- the page stays as it was; problems names the gap
                            logging.getLogger(__name__).warning(
                                "Wiki summary retry for %s failed: %s", sid, type(exc).__name__)
                    continue
                wiki_ran = wiki_ran or stage == "wiki"
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
            return self.catalog.transition_operation(
                operation["operation_id"], "running", "completed", result=result
            )
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
        return self.catalog.transition_operation(
            operation["operation_id"],
            "running",
            "retryable_failed",
            result=result,
            error_code=code,
        )

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
        # One query for every document's latest operation, instead of a scan for each.
        latest = self.catalog.latest_document_operations()
        return [
            self.show(row["source_id"], row["source_version"],
                      last=latest.get((row["source_id"], row["source_version"])))
            for row in self.catalog.list_documents(limit=limit, deleted=False)
        ]

    def _is_current(self, source_id, source_version):
        current = self.catalog.get_current_manifest(source_id)
        return current is not None and current.source_version == source_version

    def _awaited(self, source_id, source_version):
        """Stages whose hook a current version waits for; an old version never runs them again."""
        if not self._is_current(source_id, source_version):
            return ()
        return tuple(name for name, hook in (("wiki", self.wiki_hook), ("projection", self.projection_hook))
                     if hook is not None)

    def _summary_missing(self, source_id, source_version):
        """A current version's wiki stage completed but its page has no model summary."""
        return (self.wiki_summary_hook is not None and self._is_current(source_id, source_version)
                and self.catalog.get_document_stage(source_id, source_version, "wiki")["state"] == "completed"
                and bool(self.wiki_summary_hook(source_id)))

    def _window_report(self, source_id, source_version):
        report = self.wiki_window_hook(source_id, source_version) if self.wiki_window_hook is not None else None
        return report or {"skipped": 0, "dropped": 0}

    def _windows_skipped(self, source_id, source_version):
        """A current version's wiki stage completed without some of its evidence windows."""
        return (self.wiki_window_hook is not None and self._is_current(source_id, source_version)
                and self.catalog.get_document_stage(source_id, source_version, "wiki")["state"] == "completed"
                and self._window_report(source_id, source_version)["skipped"] > 0)

    def operation_outcome(self, operation):
        """What a document import or retry came to; ``None`` for any other operation."""
        return self.operation_outcomes([operation]).get(operation["operation_id"])

    def operation_outcomes(self, operations):
        """``operation_outcome`` by operation ID for the document imports and retries among
        ``operations``, from one catalog query for all of their stages."""
        wanted = {}
        for operation in operations:
            intent = operation["result"].get("recovery_intent") or {}
            if operation["operation_kind"] in {"document_import", "document_retry"} and "source_id" in intent:
                wanted[operation["operation_id"]] = (operation["state"], intent["source_id"], intent["source_version"])
        stages, current = {}, {}
        for row in self.catalog.list_document_stages({sid for state, sid, _ in wanted.values() if state == "completed"}):
            current[row["source_id"]] = row.pop("current_version")
            stages.setdefault((row["source_id"], row["source_version"]), {})[row["stage"]] = row
        hooked = tuple(name for name, hook in (("wiki", self.wiki_hook), ("projection", self.projection_hook))
                       if hook is not None)
        missing, outcomes = {}, {}
        for operation_id, (state, sid, version) in wanted.items():
            found = stages.get((sid, version), {})
            if state != "completed" or any(name not in found for name in ("extraction", "indexing", "wiki", "projection")):
                outcomes[operation_id] = state
                continue
            is_current = current.get(sid) == version
            summary = False
            # As _summary_missing and _windows_skipped; the page is read once per source.
            if is_current and found["wiki"]["state"] == "completed":
                if sid not in missing:
                    missing[sid] = ((self.wiki_summary_hook is not None and bool(self.wiki_summary_hook(sid)))
                                    or self._window_report(sid, version)["skipped"] > 0)
                summary = missing[sid]
            outcomes[operation_id] = document_outcome(found, state, hooked if is_current else (), summary,
                                                      self.in_flight(sid))
        return outcomes

    def outcome(self, document, operation_state=None):
        """``document_outcome`` for a shown document, given the state of the operation reported."""
        sid, version = document["source_id"], document["source_version"]
        return document_outcome(document["stages"], operation_state, self._awaited(sid, version),
                                self._summary_missing(sid, version) or self._windows_skipped(sid, version),
                                self.in_flight(sid))

    def show(self, source_id, source_version=None, *, last=_NOT_LOOKED_UP):
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
        if last is _NOT_LOOKED_UP:
            last = self.catalog.latest_document_operation(source_id, source_version)
        page_id = source_page_id(source_id)
        page = self.catalog.get_wiki_page(page_id)
        receipts = [row for row in self.catalog.list_artifact_projections(layer="wiki", artifact_id=page_id)
                    if row["state"] == "projected"]
        awaited, in_flight = self._awaited(source_id, source_version), self.in_flight(source_id)
        # As _summary_missing, from the stages and current version already read.
        summary_missing = ("wiki" in awaited and stages["wiki"]["state"] == "completed"
                           and self.wiki_summary_hook is not None and bool(self.wiki_summary_hook(source_id)))
        windows = self._window_report(source_id, source_version)
        windows_skipped = "wiki" in awaited and stages["wiki"]["state"] == "completed" and windows["skipped"] > 0
        outcome = document_outcome(stages, last["state"] if last else None, awaited,
                                   summary_missing or windows_skipped, in_flight)
        # A wiki or vault stage that nothing runs once the import finished waits for a retry.
        stalled = () if in_flight or outcome != "completed_with_errors" else [
            name for name in awaited if stages[name]["state"] in {"pending", "running"}]
        return {
            **metadata,
            "accepted": stages["custody"]["state"] == "accepted",
            "stages": stages,
            "extractions": extractions,
            "coverage": self._coverage(source_id, source_version, extractions),
            "chunks": self.catalog.list_provider_chunks(source_id, source_version),
            "last_operation_id": last["operation_id"] if last else None,
            "outcome": outcome,
            "problems": document_problems(source_id, stages, extractions, summary_missing, stalled, windows_skipped),
            # Claims the model wrote that failed their checks in the last wiki compile; kept, only counted here.
            "wiki_dropped_claims": windows["dropped"],
            # The page exists for the user once its note is in the vault.
            "wiki_page_id": page_id if page and page["current_revision"] and receipts else None,
            # Path of the page's newest projected note from the vault root; the receipt is
            # relative to the Journal folder.
            "vault_path": f"{self.journal_prefix}/{receipts[-1]['projection_path']}" if receipts else None,
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
