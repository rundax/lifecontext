"""Version-two portable document snapshots. Decoding and preflight have no effects."""
from __future__ import annotations

import hashlib
import io
import os
import re
import stat
import time
import zipfile
from dataclasses import dataclass, replace
from typing import Any

from lifecontext import bundle as legacy
from lifecontext.bundle import BundleError, BundleRecord
from lifecontext.config import JournalLimits
from lifecontext.extraction import (
    ExtractionArtifact,
    ExtractionLimits,
    Locator,
    TextSegment,
    _Rejected,
    _zip_directory_preflight,
)
from lifecontext.wiki import WikiPatch, patch_record

DOCUMENT_BUNDLE_VERSION = 2
_TABLES = ("sources", "metadata", "categories", "category_proposals", "extractions", "wiki_pages",
           "wiki_revisions", "wiki_reviews", "stage_intents", "extraction_payloads", "wiki_payloads")


@dataclass(frozen=True)
class DocumentBundleSnapshot:
    records: tuple[BundleRecord, ...] = ()
    bundle_version: int = DOCUMENT_BUNDLE_VERSION
    sources: tuple[dict[str, Any], ...] = ()
    metadata: tuple[dict[str, Any], ...] = ()
    categories: tuple[dict[str, Any], ...] = ()
    category_proposals: tuple[dict[str, Any], ...] = ()
    extractions: tuple[dict[str, Any], ...] = ()
    wiki_pages: tuple[dict[str, Any], ...] = ()
    wiki_revisions: tuple[dict[str, Any], ...] = ()
    wiki_reviews: tuple[dict[str, Any], ...] = ()
    stage_intents: tuple[dict[str, Any], ...] = ()
    extraction_payloads: tuple[dict[str, Any], ...] = ()
    wiki_payloads: tuple[dict[str, Any], ...] = ()

    def catalog_data(self) -> dict[str, list[dict[str, Any]]]:
        data = {name: list(getattr(self, name)) for name in _TABLES[:-2]}
        if not data["sources"]:
            documents = {(row["source_id"], row["source_version"]) for row in self.metadata}
            data["sources"] = [{"source_id": record.source_id, "source_version": record.source_version,
                                "layer": "document" if (record.source_id, record.source_version) in documents else "journal",
                                "lifecycle_state": record.lifecycle_state,
                                "sha256": record.manifest.sha256 if record.manifest else None}
                               for record in self.records]
        return legacy._parse_json(legacy._canonical_json(data))


def _key(row: dict, names: tuple[str, ...]) -> tuple:
    try:
        value = tuple(row[name] for name in names)
        for name, item in zip(names, value):
            if name.endswith("id"):
                legacy._validate_identity(item, 1)
            elif type(item) is not int or not 1 <= item <= 2_147_483_647:
                raise BundleError("invalid bundle")
        return value
    except (KeyError, TypeError):
        raise BundleError("invalid bundle") from None


def _index(rows: tuple[dict, ...], names: tuple[str, ...]) -> dict:
    result = {}
    if not isinstance(rows, tuple) or len(rows) > legacy.MAX_BUNDLE_ITEMS:
        raise BundleError("bundle limit exceeded")
    for row in rows:
        if not isinstance(row, dict) or legacy._has_provider_identifier(row):
            raise BundleError("invalid bundle")
        key = _key(row, names)
        if key in result:
            raise BundleError("invalid bundle")
        result[key] = row
    return result


def _validate(snapshot: DocumentBundleSnapshot, limits: JournalLimits) -> DocumentBundleSnapshot:
    try:
        return _validate_snapshot(snapshot, limits)
    except BundleError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, UnicodeError, RecursionError):
        raise BundleError("invalid bundle") from None


def _extraction(payload: Any, row: dict[str, Any]) -> ExtractionArtifact:
    if not isinstance(payload, dict) or set(payload) != set(ExtractionArtifact.__dataclass_fields__):
        raise BundleError("invalid bundle")
    values = dict(payload)
    if not isinstance(values["segments"], list) or not isinstance(values["warnings"], list):
        raise BundleError("invalid bundle")
    values["segments"] = tuple(
        TextSegment(segment["text"], segment["start"], segment["end"], Locator(**segment["locator"]))
        for segment in values["segments"]
        if isinstance(segment, dict) and set(segment) == {"text", "start", "end", "locator"}
    )
    if len(values["segments"]) != len(payload["segments"]):
        raise BundleError("invalid bundle")
    values["warnings"] = tuple(values["warnings"])
    artifact = ExtractionArtifact(**values)
    expected = (row["source_id"], row["source_version"], row["extraction_revision"],
                row["original_sha256"], row["config_sha256"], row["extractor"],
                row["extractor_version"], row["status"], tuple(row["warnings"]), row["passage_count"])
    actual = (artifact.source_id, artifact.source_version, artifact.extraction_revision,
              artifact.original_checksum, artifact.config_identity, artifact.extractor,
              artifact.extractor_version, artifact.status, artifact.warnings, len(artifact.segments))
    if actual != expected or type(row["passage_count"]) is not int:
        raise BundleError("invalid bundle")
    offset = 0
    for segment in artifact.segments:
        if segment.start != offset:
            raise BundleError("invalid bundle")
        offset = segment.end
    return artifact


def _name_key(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.encode()) > 256:
        raise BundleError("invalid bundle")
    return " ".join(value.split()).casefold()


def _validate_snapshot(snapshot: DocumentBundleSnapshot, limits: JournalLimits) -> DocumentBundleSnapshot:
    if not isinstance(snapshot, DocumentBundleSnapshot) or type(snapshot.bundle_version) is not int or snapshot.bundle_version not in {1, 2}:
        raise BundleError("invalid bundle")
    schemas = {
        "sources": ({"source_id", "source_version", "layer", "lifecycle_state", "sha256"}, set()),
        "metadata": ({"source_id", "source_version", "filename", "title", "mime_type", "category_id", "category_revision"}, set()),
        "categories": ({"category_id", "name", "name_key", "description", "aliases", "revision"}, {"created_at"}),
        "category_proposals": ({"proposal_id", "name", "name_key", "description", "rationale", "closest_category_id", "category_id", "state", "revision"}, {"created_at", "documents"}),
        "extractions": ({"source_id", "source_version", "extraction_revision", "original_sha256", "extractor", "extractor_version", "config_sha256", "artifact_sha256", "status", "warnings", "passage_count"}, {"created_at"}),
        "wiki_pages": ({"page_id", "title", "current_revision", "state"}, {"created_at"}),
        "wiki_revisions": ({"page_id", "revision", "parent_revision", "artifact_sha256", "dependencies"}, {"created_at"}),
        "wiki_reviews": ({"review_id", "page_id", "revision", "code", "details", "state"}, {"resolution"}),
        "stage_intents": ({"source_id", "source_version", "stage", "state"}, {"revision", "attempt", "error_code"}),
        "extraction_payloads": ({"source_id", "source_version", "extraction_revision", "payload"}, set()),
        "wiki_payloads": ({"page_id", "revision", "payload"}, set()),
    }
    for name, (required, optional) in schemas.items():
        rows = getattr(snapshot, name)
        if not isinstance(rows, tuple) or len(rows) > legacy.MAX_BUNDLE_ITEMS:
            raise BundleError("bundle limit exceeded")
        for row in rows:
            if not isinstance(row, dict) or not required <= set(row) or set(row) - required - optional or legacy._has_provider_identifier(row):
                raise BundleError("invalid bundle")
    records = legacy._validate_records(snapshot.records, limits)
    sources = {(r.source_id, r.source_version): r for r in records}
    categories = _index(snapshot.categories, ("category_id",))
    pages = _index(snapshot.wiki_pages, ("page_id",))
    metadata = _index(snapshot.metadata, ("source_id", "source_version"))
    extractions = _index(snapshot.extractions, ("source_id", "source_version", "extraction_revision"))
    payloads = _index(snapshot.extraction_payloads, ("source_id", "source_version", "extraction_revision"))
    revisions = _index(snapshot.wiki_revisions, ("page_id", "revision"))
    wiki_payloads = _index(snapshot.wiki_payloads, ("page_id", "revision"))
    inventory = _index(snapshot.sources, ("source_id", "source_version"))
    if inventory and set(inventory) != set(sources):
        raise BundleError("invalid bundle")
    for key, row in inventory.items():
        record = sources[key]
        if row["layer"] not in {"journal", "document"} or row["lifecycle_state"] != record.lifecycle_state or row["sha256"] != (record.manifest.sha256 if record.manifest else None):
            raise BundleError("invalid bundle")
        if key in metadata and row["layer"] != "document":
            raise BundleError("invalid bundle")
    taxonomy = {}
    for (category_id,), row in categories.items():
        if row["name_key"] != _name_key(row["name"]) or type(row["revision"]) is not int or row["revision"] < 1 or not isinstance(row["description"], str) or len(row["description"].encode()) > 4096 or not isinstance(row["aliases"], list) or len(row["aliases"]) > 32:
            raise BundleError("invalid bundle")
        for label in [row["name"], *row["aliases"]]:
            normalized = _name_key(label)
            if normalized in taxonomy:
                raise BundleError("invalid bundle")
            taxonomy[normalized] = category_id
    _index(snapshot.category_proposals, ("proposal_id",))
    _index(snapshot.wiki_reviews, ("review_id",))
    for row in snapshot.category_proposals:
        if row["name_key"] != _name_key(row["name"]) or type(row["revision"]) is not int or row["revision"] < 1 or row["state"] not in {"pending", "approved", "rejected", "redirected"}:
            raise BundleError("invalid bundle")
        for field in ("closest_category_id", "category_id"):
            if row[field] is not None and (row[field],) not in categories:
                raise BundleError("invalid bundle")
        if row["state"] in {"approved", "redirected"} and row["category_id"] is None:
            raise BundleError("invalid bundle")
        for field in ("description", "rationale"):
            if not isinstance(row[field], str) or len(row[field].encode()) > 4096:
                raise BundleError("invalid bundle")
        documents = row.get("documents", [])
        if not isinstance(documents, list) or len(documents) > legacy.MAX_BUNDLE_ITEMS:
            raise BundleError("invalid bundle")
        seen_documents = set()
        for document in documents:
            if not isinstance(document, dict) or set(document) != {"source_id", "source_version"}:
                raise BundleError("invalid bundle")
            key = _key(document, ("source_id", "source_version"))
            if key not in metadata or key in seen_documents:
                raise BundleError("invalid bundle")
            seen_documents.add(key)
    for key, row in metadata.items():
        if key not in sources or sources[key].lifecycle_state == "deleted" or (row.get("category_id"),) not in categories:
            raise BundleError("invalid bundle")
        if any(not isinstance(row.get(name), str) or not row[name] for name in ("filename", "title", "mime_type")):
            raise BundleError("invalid bundle")
    for key, row in extractions.items():
        original = sources.get(key[:2])
        if key[:2] not in metadata or original is None or original.manifest is None or row.get("original_sha256") != original.manifest.sha256:
            raise BundleError("invalid bundle")
        checksum = row.get("artifact_sha256")
        if checksum is not None:
            payload = payloads.get(key)
            if payload is None or set(payload) != {"source_id", "source_version", "extraction_revision", "payload"} or hashlib.sha256(legacy._canonical_json(payload["payload"])).hexdigest() != checksum:
                raise BundleError("invalid bundle")
            _extraction(payload["payload"], row)
        elif key in payloads or row.get("status") == "completed":
            raise BundleError("invalid bundle")
    if set(payloads) - set(extractions) or set(wiki_payloads) != set(revisions):
        raise BundleError("invalid bundle")
    for key, row in revisions.items():
        if (key[0],) not in pages or row.get("parent_revision") != (key[1] - 1 or None) or (key[1] > 1 and (key[0], key[1] - 1) not in revisions):
            raise BundleError("invalid bundle")
        payload = wiki_payloads[key]
        if set(payload) != {"page_id", "revision", "payload"}:
            raise BundleError("invalid bundle")
        raw = legacy._canonical_json(payload["payload"])
        patch = WikiPatch.parse(raw.decode())
        if patch.page_id != key[0] or patch.expected_revision + 1 != key[1] or hashlib.sha256(raw).hexdigest() != row.get("artifact_sha256"):
            raise BundleError("invalid bundle")
        dependencies = row.get("dependencies")
        if not isinstance(dependencies, list) or not dependencies or len(dependencies) > legacy.MAX_BUNDLE_ITEMS:
            raise BundleError("invalid bundle")
        citations = [citation for claim in patch.claims for citation in claim.citations]
        citations += [citation for contradiction in patch.contradictions for claim in (contradiction.left, contradiction.right) for citation in claim.citations]
        if {legacy._canonical_json(c.dependency()) for c in citations} != {legacy._canonical_json(d) for d in dependencies}:
            raise BundleError("invalid bundle")
        claims = [*patch.claims, *(side for contradiction in patch.contradictions for side in (contradiction.left, contradiction.right))]
        for claim in claims:
            supported = False
            for citation in claim.citations:
                depkey = (citation.source_id, citation.source_version, citation.extraction_revision)
                payload = payloads.get(depkey)
                if payload is None:
                    raise BundleError("invalid bundle")
                segments = payload["payload"]["segments"]
                if citation.passage_end > len(segments):
                    raise BundleError("invalid bundle")
                text = "".join(segment["text"] for segment in segments[citation.passage_start:citation.passage_end])
                if citation.quote not in text:
                    raise BundleError("invalid bundle")
                supported = supported or claim.text in citation.quote
            if not supported:
                raise BundleError("invalid bundle")
        for dependency in dependencies:
            depkey = _key(dependency, ("source_id", "source_version", "extraction_revision"))
            source = sources.get(depkey[:2])
            extraction = extractions.get(depkey)
            if source is None or source.lifecycle_state == "deleted" or source.manifest is None or source.manifest.sha256 != dependency.get("original_sha256") or extraction is None or extraction.get("status") != "completed":
                raise BundleError("invalid bundle")
            start, end = dependency.get("passage_start"), dependency.get("passage_end")
            if type(start) is not int or type(end) is not int or not 0 <= start < end <= extraction.get("passage_count", 0):
                raise BundleError("invalid bundle")
    for (page_id,), page in pages.items():
        current = page.get("current_revision")
        if type(current) is not int or current < 0 or (current and (page_id, current) not in revisions):
            raise BundleError("invalid bundle")
    for review in snapshot.wiki_reviews:
        if _key(review, ("page_id", "revision")) not in revisions:
            raise BundleError("invalid bundle")
    seen_stages = set()
    for intent in snapshot.stage_intents:
        if type(intent.get("revision", 1)) is not int or not 1 <= intent.get("revision", 1) <= 2_147_483_647 or type(intent.get("attempt", 0)) is not int or not 0 <= intent.get("attempt", 0) <= 2_147_483_647:
            raise BundleError("invalid bundle")
        error = intent.get("error_code")
        if error is not None and (not isinstance(error, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", error) or intent["state"] not in {"failed", "blocked", "conflict"}):
            raise BundleError("invalid bundle")
        key = _key(intent, ("source_id", "source_version"))
        stage = intent["stage"]
        if key not in metadata or stage not in {"custody", "extraction", "indexing", "wiki", "projection"} or not isinstance(intent["state"], str) or intent["state"] not in {"pending", "running", "accepted", "completed", "blocked", "failed", "conflict"} or (*key, stage) in seen_stages:
            raise BundleError("invalid bundle")
        seen_stages.add((*key, stage))
    # Round-trip JSON detaches caller-owned dictionaries and enforces JSON bounds.
    data = {name: list(getattr(snapshot, name)) for name in _TABLES}
    raw = legacy._canonical_json(data)
    if len(raw) > legacy._MAX_BUNDLE_MANIFEST_BYTES:
        raise BundleError("bundle limit exceeded")
    copied = legacy._parse_json(raw)
    return DocumentBundleSnapshot(records=records, bundle_version=snapshot.bundle_version, **{name: tuple(copied[name]) for name in _TABLES})


def encode_document_bundle(snapshot: DocumentBundleSnapshot, *, limits: JournalLimits | None = None) -> bytes:
    limits = limits or JournalLimits()
    snapshot = _validate(snapshot, limits)
    members = {}
    items = []
    for record in snapshot.records:
        item = {"source_id": record.source_id, "source_version": record.source_version, "lifecycle_state": record.lifecycle_state}
        if record.manifest is not None:
            for name, raw in (("manifest.json", record.manifest.to_bytes()), ("original.bin", record.original_bytes)):
                path = legacy._member_path(record.source_id, record.source_version, name)
                members[path] = raw
                item[name] = {"path": path, "sha256": hashlib.sha256(raw).hexdigest()}
        items.append(item)
    members["snapshot.json"] = legacy._canonical_json({name: list(getattr(snapshot, name)) for name in _TABLES})
    root = {"bundle_format": legacy.BUNDLE_FORMAT, "bundle_version": 2, "inventory_complete": True, "items": items,
            "snapshot": {"path": "snapshot.json", "sha256": hashlib.sha256(members["snapshot.json"]).hexdigest()}}
    members["bundle.json"] = legacy._canonical_json(root)
    if sum(map(len, members.values())) > legacy._max_uncompressed_bytes(limits):
        raise BundleError("bundle limit exceeded")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name in sorted(members):
            archive.writestr(legacy._zip_info(name), members[name])
    raw = output.getvalue()
    if len(raw) > legacy._max_bundle_bytes(limits):
        raise BundleError("bundle limit exceeded")
    return raw


def decode_document_bundle(raw: bytes, *, limits: JournalLimits | None = None) -> DocumentBundleSnapshot:
    limits = limits or JournalLimits()
    if not isinstance(raw, bytes) or not raw or len(raw) > legacy._max_bundle_bytes(limits):
        raise BundleError("bundle limit exceeded")
    try:
        _zip_directory_preflight(raw, ExtractionLimits(max_zip_entries=2 + legacy.MAX_BUNDLE_ITEMS * 2), time.monotonic() + 10)
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            infos = archive.infolist()
            if not infos or len(infos) > 2 + legacy.MAX_BUNDLE_ITEMS * 2:
                raise BundleError("bundle limit exceeded")
            members = {}
            total = 0
            for info in infos:
                legacy._check_zip_info(info)
                if info.filename in members:
                    raise BundleError("invalid bundle")
                budget = legacy._MAX_BUNDLE_MANIFEST_BYTES if info.filename.endswith(".json") else limits.max_file_bytes
                value = legacy._read_member(archive, info, max_member_bytes=budget, remaining_total_bytes=legacy._max_uncompressed_bytes(limits) - total)
                total += len(value)
                members[info.filename] = value
        root = legacy._parse_json(members.get("bundle.json", b""))
        if isinstance(root, dict) and type(root.get("bundle_version")) is int and root["bundle_version"] == 1:
            return DocumentBundleSnapshot(records=legacy.decode_bundle(raw, limits=limits), bundle_version=1)
        if not isinstance(root, dict) or set(root) != {"bundle_format", "bundle_version", "inventory_complete", "items", "snapshot"} or root["bundle_format"] != legacy.BUNDLE_FORMAT or type(root["bundle_version"]) is not int or root["bundle_version"] != 2 or root["inventory_complete"] is not True or not isinstance(root["items"], list) or len(root["items"]) > legacy.MAX_BUNDLE_ITEMS:
            raise BundleError("invalid bundle")
        used = {"bundle.json"}
        def reference(ref: Any, expected: str) -> bytes:
            if not isinstance(ref, dict) or set(ref) != {"path", "sha256"} or ref["path"] != expected or expected in used or expected not in members:
                raise BundleError("invalid bundle")
            value = members[expected]
            if hashlib.sha256(value).hexdigest() != ref["sha256"]:
                raise BundleError("invalid bundle")
            used.add(expected)
            return value
        data = legacy._parse_json(reference(root["snapshot"], "snapshot.json"))
        if not isinstance(data, dict) or set(data) not in (set(_TABLES), set(_TABLES) - {"sources"}):
            raise BundleError("invalid bundle")
        data.setdefault("sources", [])
        if any(not isinstance(data[name], list) for name in _TABLES):
            raise BundleError("invalid bundle")
        records = []
        for item in root["items"]:
            if not isinstance(item, dict):
                raise BundleError("invalid bundle")
            sid, version = legacy._validate_identity(item.get("source_id"), item.get("source_version"))
            state = item.get("lifecycle_state")
            expected = {"source_id", "source_version", "lifecycle_state"}
            if state != "deleted":
                expected |= {"manifest.json", "original.bin"}
            if set(item) != expected:
                raise BundleError("invalid bundle")
            manifest = original = None
            if state != "deleted":
                manifest = legacy._read_source_manifest(reference(item["manifest.json"], legacy._member_path(sid, version, "manifest.json")))
                original = reference(item["original.bin"], legacy._member_path(sid, version, "original.bin"))
            records.append(BundleRecord(sid, version, state, manifest, original))
        if used != set(members):
            raise BundleError("invalid bundle")
        return _validate(DocumentBundleSnapshot(records=tuple(records), **{name: tuple(data[name]) for name in _TABLES}), limits)
    except BundleError:
        raise
    except (_Rejected, ValueError, TypeError, KeyError, AttributeError, RecursionError, OSError, EOFError, zipfile.BadZipFile):
        raise BundleError("invalid bundle") from None


class DocumentBundleService:
    def __init__(self, catalog: Any, custody: Any, derived: Any, wiki_store: Any, *, limits: JournalLimits | None = None) -> None:
        self.catalog, self.custody, self.derived, self.wiki_store = catalog, custody, derived, wiki_store
        self.limits = limits or JournalLimits()

    def export_snapshot(self, source_ids: list[str] | None = None) -> DocumentBundleSnapshot:
        data = self.catalog.export_document_snapshot(source_ids=source_ids)
        identities = {(r["source_id"], r["source_version"]): r for r in data["sources"]}
        records = []
        for (sid, version), row in identities.items():
            if row["lifecycle_state"] == "deleted":
                records.append(BundleRecord(sid, version, "deleted"))
            else:
                evidence = self.custody.resolve_exact(sid, version)
                records.append(BundleRecord(sid, version, row["lifecycle_state"], evidence.manifest, evidence.original_bytes))
        extraction_payloads = []
        for row in data["extractions"]:
            row.pop("artifact_path", None)
            if row.get("artifact_sha256"):
                extraction_payloads.append({"source_id": row["source_id"], "source_version": row["source_version"], "extraction_revision": row["extraction_revision"], "payload": self.derived.read(row["source_id"], row["source_version"], row["extraction_revision"], expected_sha256=row["artifact_sha256"])})
        wiki_payloads = []
        for row in data["wiki_revisions"]:
            row.pop("artifact_path", None)
            patch = self.wiki_store.read(row["page_id"], row["revision"], row["artifact_sha256"])
            wiki_payloads.append({"page_id": row["page_id"], "revision": row["revision"], "payload": patch_record(patch)})
        snapshot = DocumentBundleSnapshot(records=tuple(records), extraction_payloads=tuple(extraction_payloads), wiki_payloads=tuple(wiki_payloads), **{name: tuple(data.get(name, [])) for name in _TABLES[:-2]})
        return _validate(snapshot, self.limits)

    def preflight_restore(self, value: bytes | DocumentBundleSnapshot) -> DocumentBundleSnapshot:
        snapshot = decode_document_bundle(value, limits=self.limits) if isinstance(value, bytes) else _validate(value, self.limits)
        for record in snapshot.records:
            existing = self.catalog.get_manifest(record.source_id, record.source_version)
            tombstone = self.catalog.get_tombstone(record.source_id, record.source_version)
            if tombstone and record.lifecycle_state == "active":
                raise BundleError("bundle identity conflict")
            if existing and record.manifest and (existing.get("sha256") if isinstance(existing, dict) else existing.sha256) != record.manifest.sha256:
                raise BundleError("bundle identity conflict")
        self.catalog.preflight_document_snapshot(snapshot.catalog_data())
        self._preflight_artifacts(snapshot)
        intents = tuple({**row, "state": "pending", "error_code": None} if row["stage"] in {"indexing", "wiki", "projection"} else row for row in snapshot.stage_intents)
        return replace(snapshot, stage_intents=intents)

    def _artifact_exists(self, directories: tuple[str, ...], filename: str, *, revision: int | None = None) -> bool:
        opened = []
        try:
            with self.derived._directories((), create=False) as fds:
                directory = fds[-1]
                for name in directories:
                    try:
                        child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
                    except FileNotFoundError:
                        return False
                    opened.append(child)
                    info = os.fstat(child)
                    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
                        raise BundleError("bundle identity conflict")
                    directory = child
                if revision is not None:
                    names = os.listdir(directory)
                    if len(names) > legacy.MAX_BUNDLE_ITEMS or any(name.startswith(f"r{revision}-") and name != filename for name in names):
                        raise BundleError("bundle identity conflict")
                try:
                    info = os.stat(filename, dir_fd=directory, follow_symlinks=False)
                except FileNotFoundError:
                    return False
                if not stat.S_ISREG(info.st_mode):
                    raise BundleError("bundle identity conflict")
                return True
        except (OSError, ValueError):
            raise BundleError("bundle identity conflict") from None
        finally:
            for fd in reversed(opened):
                os.close(fd)

    def _preflight_artifacts(self, snapshot: DocumentBundleSnapshot) -> None:
        try:
            for row in snapshot.extractions:
                if row["artifact_sha256"] is not None and self._artifact_exists(("derived", row["source_id"], f"v{row['source_version']}"), f"r{row['extraction_revision']}.json"):
                    self.derived.read(row["source_id"], row["source_version"], row["extraction_revision"], expected_sha256=row["artifact_sha256"])
            for row in snapshot.wiki_revisions:
                filename = f"r{row['revision']}-{row['artifact_sha256']}.json"
                if self._artifact_exists(("wiki_revisions", row["page_id"]), filename, revision=row["revision"]):
                    self.wiki_store.read(row["page_id"], row["revision"], row["artifact_sha256"])
        except (OSError, ValueError):
            raise BundleError("bundle identity conflict") from None

    def restore_artifacts(self, snapshot: DocumentBundleSnapshot) -> dict[str, list[dict[str, Any]]]:
        snapshot = self.preflight_restore(snapshot)
        data = snapshot.catalog_data()
        extractions = {(row["source_id"], row["source_version"], row["extraction_revision"]): row for row in data["extractions"]}
        for item in snapshot.extraction_payloads:
            key = (item["source_id"], item["source_version"], item["extraction_revision"])
            stored = self.derived.write(*key, item["payload"])
            if stored.sha256 != extractions[key]["artifact_sha256"]:
                raise BundleError("bundle identity conflict")
            extractions[key]["artifact_path"] = str(stored.path.relative_to(self.derived.paths.lifeweave_home))
        revisions = {(row["page_id"], row["revision"]): row for row in data["wiki_revisions"]}
        for item in snapshot.wiki_payloads:
            key = (item["page_id"], item["revision"])
            stored = self.wiki_store.write(WikiPatch.parse(legacy._canonical_json(item["payload"]).decode()))
            if stored.sha256 != revisions[key]["artifact_sha256"]:
                raise BundleError("bundle identity conflict")
            revisions[key]["artifact_path"] = str(stored.path.relative_to(self.derived.paths.lifeweave_home))
        return data
