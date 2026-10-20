from __future__ import annotations

import hashlib
import io
import json
import struct
import tempfile
import unittest
import zipfile
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch

from lifecontext.bundle import BundleError, BundleRecord, encode_bundle
from lifecontext.custody import SourceManifest
from lifecontext.derived import DerivedStore
from lifecontext.document_bundle import (
    DocumentBundleService,
    DocumentBundleSnapshot,
    decode_document_bundle,
    encode_document_bundle,
)
from lifecontext.extraction import ExtractionArtifact, Locator, TextSegment
from lifecontext.paths import LifeContextPaths
from lifecontext.wiki import WikiRevisionStore


def _json(value):
    return json.loads(json.dumps(value))


def _sha(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _snapshot(state="active"):
    original = b"Alice lives in Paris."
    manifest = SourceManifest("doc_fixture", 1, hashlib.sha256(original).hexdigest(), len(original), {"source": "test"})
    artifact = ExtractionArtifact("doc_fixture", 1, manifest.sha256, 1, "a" * 64, "completed", segments=(TextSegment(original.decode(), 0, len(original), Locator("lines", 1, 1)),), format="text")
    payload = _json(asdict(artifact))
    citation = {"source_id": "doc_fixture", "source_version": 1, "extraction_revision": 1, "passage_start": 0, "passage_end": 1, "original_sha256": manifest.sha256, "quote": original.decode()}
    wiki = {"page_id": "topic_home", "title": "Home", "expected_revision": 0, "claims": [{"text": original.decode(), "citations": [citation]}], "contradictions": [], "links": []}
    dependency = {key: value for key, value in citation.items() if key != "quote"}
    return DocumentBundleSnapshot(
        records=(BundleRecord("doc_fixture", 1, state, manifest, original),),
        sources=({"source_id": "doc_fixture", "source_version": 1, "layer": "document", "lifecycle_state": state, "sha256": manifest.sha256},),
        metadata=({"source_id": "doc_fixture", "source_version": 1, "filename": "home.txt", "title": "Home", "mime_type": "text/plain", "category_id": "inbox", "category_revision": 1},),
        categories=({"category_id": "inbox", "name": "Inbox", "name_key": "inbox", "description": "", "aliases": [], "revision": 1},),
        extractions=({"source_id": "doc_fixture", "source_version": 1, "extraction_revision": 1, "original_sha256": manifest.sha256, "extractor": artifact.extractor, "extractor_version": artifact.extractor_version, "config_sha256": artifact.config_identity, "artifact_sha256": _sha(payload), "status": "completed", "warnings": [], "passage_count": 1},),
        extraction_payloads=({"source_id": "doc_fixture", "source_version": 1, "extraction_revision": 1, "payload": payload},),
        wiki_pages=({"page_id": "topic_home", "title": "Home", "current_revision": 1, "state": "current"},),
        wiki_revisions=({"page_id": "topic_home", "revision": 1, "parent_revision": None, "artifact_sha256": _sha(wiki), "dependencies": [dependency]},),
        wiki_payloads=({"page_id": "topic_home", "revision": 1, "payload": wiki},),
        stage_intents=({"source_id": "doc_fixture", "source_version": 1, "stage": "indexing", "state": "completed", "revision": 2, "attempt": 1, "error_code": None},),
    )


def _changed_payload(snapshot, field, mutation):
    rows = _json(getattr(snapshot, field))
    mutation(rows[0]["payload"])
    revisions = _json(snapshot.wiki_revisions if field == "wiki_payloads" else snapshot.extractions)
    revisions[0]["artifact_sha256"] = _sha(rows[0]["payload"])
    return replace(snapshot, **{field: tuple(rows), "wiki_revisions" if field == "wiki_payloads" else "extractions": tuple(revisions)})


class DocumentBundleTests(unittest.TestCase):
    def test_roundtrip_preserves_document_layers_payloads_and_forgotten_history(self):
        for state in ("active", "forgotten"):
            with self.subTest(state=state):
                snapshot = _snapshot(state)
                raw = encode_document_bundle(snapshot)
                self.assertEqual(raw, encode_document_bundle(snapshot))
                self.assertEqual(decode_document_bundle(raw), snapshot)

    def test_review_resolution_is_an_optional_bundle_field(self):
        review = {"review_id": "review_home", "page_id": "topic_home", "revision": 1, "code": "manual_edit",
                  "details": {"expected_hash": "a" * 64, "actual_hash": "b" * 64}, "state": "pending"}
        resolved = {**review, "state": "resolved",
                    "resolution": {"note": "Restored the managed text.", "resolved_at": "2026-10-17T08:00:00Z"}}
        for rows in ((review,), (resolved,), ({**review, "state": "resolved"},)):
            with self.subTest(rows=rows):
                snapshot = replace(_snapshot(), wiki_reviews=rows)
                self.assertEqual(decode_document_bundle(encode_document_bundle(snapshot)), snapshot)
        with self.assertRaises(BundleError):
            encode_document_bundle(replace(_snapshot(), wiki_reviews=({**resolved, "resolver_id": "x"},)))

    def test_legacy_bundle_version_dispatch(self):
        snapshot = _snapshot()
        restored = decode_document_bundle(encode_bundle(snapshot.records))
        self.assertEqual(restored.bundle_version, 1)
        self.assertEqual(restored.records, snapshot.records)
        self.assertEqual(restored.metadata, ())

    def test_rehashed_malformed_extraction_is_rejected(self):
        mutations = [
            lambda payload: payload.update(source_id="doc_other"),
            lambda payload: payload.update(config_identity="b" * 64),
            lambda payload: payload.update(extractor_version="different"),
            lambda payload: payload["segments"][0].update(end=99),
            lambda payload: payload["segments"][0].update(start=1, end=len(payload["segments"][0]["text"]) + 1),
            lambda payload: payload["segments"][0]["locator"].update(start=0),
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation), self.assertRaises(BundleError):
                encode_document_bundle(_changed_payload(_snapshot(), "extraction_payloads", mutation))

    def test_rehashed_unsupported_claim_and_missing_quote_are_rejected(self):
        mutations = [
            lambda payload: payload["claims"][0].update(text="Alice lives in Rome."),
            lambda payload: payload["claims"][0]["citations"][0].update(quote="Alice lives in Rome."),
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation), self.assertRaises(BundleError):
                encode_document_bundle(_changed_payload(_snapshot(), "wiki_payloads", mutation))

    def test_wiki_summary_roundtrips_and_invalid_summaries_are_rejected(self):
        summary = [{"text": "Alice lives in Paris, it says.", "claims": [1]}]
        snapshot = _changed_payload(_snapshot(), "wiki_payloads", lambda payload: payload.update(summary=summary))
        self.assertEqual(decode_document_bundle(encode_document_bundle(snapshot)), snapshot)
        for invalid in ([], [{"text": "Alice lives in Rome.", "claims": [2]}], [{"text": "See www.example.test", "claims": [1]}]):
            with self.subTest(invalid=invalid), self.assertRaises(BundleError):
                encode_document_bundle(_changed_payload(_snapshot(), "wiki_payloads", lambda payload, invalid=invalid: payload.update(summary=invalid)))

    def test_deleted_original_cannot_be_reintroduced_as_history(self):
        snapshot = _snapshot()
        sources = ({**snapshot.sources[0], "lifecycle_state": "deleted", "sha256": None},)
        with self.assertRaises(BundleError):
            encode_document_bundle(replace(snapshot, records=(BundleRecord("doc_fixture", 1, "deleted"),), sources=sources))

    def test_taxonomy_alias_collision_is_rejected(self):
        snapshot = _snapshot()
        other = {"category_id": "cat_other", "name": "Other", "name_key": "other", "description": "", "aliases": ["  INBOX  "], "revision": 1}
        with self.assertRaises(BundleError):
            encode_document_bundle(replace(snapshot, categories=(*snapshot.categories, other)))

    def test_unknown_proposal_document_and_category_are_rejected(self):
        snapshot = _snapshot()
        proposal = {"proposal_id": "proposal_home", "name": "Home", "name_key": "home", "description": "", "rationale": "broad", "closest_category_id": "inbox", "category_id": None, "state": "pending", "revision": 1, "documents": [{"source_id": "doc_missing", "source_version": 1}]}
        with self.assertRaises(BundleError):
            encode_document_bundle(replace(snapshot, category_proposals=(proposal,)))
        proposal = {**proposal, "documents": [], "closest_category_id": "cat_missing"}
        with self.assertRaises(BundleError):
            encode_document_bundle(replace(snapshot, category_proposals=(proposal,)))

    def test_inventory_checksum_and_layer_must_match_records(self):
        snapshot = _snapshot()
        for field, value in (("sha256", "f" * 64), ("layer", "journal")):
            with self.subTest(field=field), self.assertRaises(BundleError):
                encode_document_bundle(replace(snapshot, sources=({**snapshot.sources[0], field: value},)))

    def test_forged_central_directory_count_rejected_before_zipfile(self):
        raw = bytearray(encode_document_bundle(_snapshot()))
        end = raw.rfind(b"PK\x05\x06")
        struct.pack_into("<H", raw, end + 8, 0)
        struct.pack_into("<H", raw, end + 10, 0)
        with patch("lifecontext.document_bundle.zipfile.ZipFile", side_effect=AssertionError("must not allocate")), self.assertRaises(BundleError):
            decode_document_bundle(bytes(raw))

    def test_prior_v2_without_sources_inventory_still_decodes(self):
        raw = encode_document_bundle(_snapshot())
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            members = {name: archive.read(name) for name in archive.namelist()}
        data = json.loads(members["snapshot.json"])
        data.pop("sources")
        members["snapshot.json"] = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
        root = json.loads(members["bundle.json"])
        root["snapshot"]["sha256"] = hashlib.sha256(members["snapshot.json"]).hexdigest()
        members["bundle.json"] = json.dumps(root).encode()
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            for name, value in members.items():
                archive.writestr(name, value)
        decoded = decode_document_bundle(output.getvalue())
        self.assertEqual(decoded.catalog_data()["sources"][0]["layer"], "document")

    def test_immutable_artifacts_restore_idempotently_and_preflight_collisions(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = LifeContextPaths(Path(temporary))
            paths.ensure_private_home()
            derived, wiki = DerivedStore(paths), WikiRevisionStore(paths)
            snapshot = _snapshot()

            class Catalog:
                def get_manifest(self, sid, version):
                    return snapshot.records[0].manifest

                def get_tombstone(self, sid, version):
                    return None

                def preflight_document_snapshot(self, data):
                    return data

            service = DocumentBundleService(Catalog(), None, derived, wiki)
            data = service.restore_artifacts(snapshot)
            self.assertTrue(data["extractions"][0]["artifact_path"].startswith("derived/"))
            self.assertTrue(data["wiki_revisions"][0]["artifact_path"].startswith("wiki_revisions/"))
            self.assertEqual(service.restore_artifacts(snapshot), data)
            self.assertEqual(data["stage_intents"][0]["state"], "pending")
            self.assertNotIn("artifact_path", snapshot.extractions[0])
            conflict = _changed_payload(snapshot, "extraction_payloads", lambda payload: payload.update(warnings=["different"]))
            conflict = replace(conflict, extractions=({**conflict.extractions[0], "warnings": ["different"]},))
            encode_document_bundle(conflict)
            with patch.object(derived, "write", side_effect=AssertionError("preflight must not write")), self.assertRaises(BundleError):
                service.preflight_restore(conflict)
