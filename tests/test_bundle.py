from __future__ import annotations

import hashlib
import io
import json
import stat
import unittest
import warnings
import zipfile
from unittest.mock import patch

from lifecontext.bundle import BundleError, BundleRecord, decode_bundle, encode_bundle
from lifecontext.config import JournalLimits
from lifecontext.custody import SourceManifest


def _record(
    source_id: str,
    version: int,
    content: bytes,
    *,
    lifecycle_state: str = "active",
) -> BundleRecord:
    manifest = SourceManifest(
        source_id=source_id,
        source_version=version,
        sha256=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
        provenance={"origin": "fixture", "version": version},
    )
    return BundleRecord(
        source_id=source_id,
        source_version=version,
        lifecycle_state=lifecycle_state,
        manifest=manifest,
        original_bytes=content,
    )


def _root_manifest(items: list[dict[str, object]] | None = None) -> dict[str, object]:
    return {
        "bundle_format": "lifeweave.bundle",
        "bundle_version": 1,
        "inventory_complete": True,
        "items": items or [],
    }


def _archive(
    manifest: dict[str, object],
    *,
    members: list[tuple[str, bytes, int | None]] | None = None,
) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("bundle.json", json.dumps(manifest).encode("utf-8"))
        for name, content, mode in members or []:
            info = zipfile.ZipInfo(name)
            if mode is not None:
                info.external_attr = mode << 16
            archive.writestr(info, content)
    return buffer.getvalue()


class BundleTests(unittest.TestCase):
    def test_deterministic_round_trip_preserves_active_forgotten_and_deleted_records(self) -> None:
        records = (
            _record("src_mail_2", 2, b"forgotten bytes", lifecycle_state="forgotten"),
            BundleRecord("src_mail_3", 1, "deleted"),
            _record("src_mail_1", 1, b"active bytes"),
        )

        first = encode_bundle(records)
        second = encode_bundle(records)

        self.assertEqual(first, second)
        restored = decode_bundle(first)
        self.assertEqual(restored, tuple(sorted(records, key=lambda r: (r.source_id, r.source_version))))
        self.assertEqual(restored[0].original_bytes, b"active bytes")
        self.assertEqual(restored[1].lifecycle_state, "forgotten")
        self.assertEqual(restored[1].original_bytes, b"forgotten bytes")
        self.assertEqual(restored[2], BundleRecord("src_mail_3", 1, "deleted"))

        with zipfile.ZipFile(io.BytesIO(first)) as archive:
            names = archive.namelist()
            self.assertEqual(names[0], "bundle.json")
            self.assertFalse(any(name.endswith((".db", ".sqlite", ".sqlite3")) for name in names))
            root = json.loads(archive.read("bundle.json"))
            self.assertEqual(root["bundle_format"], "lifeweave.bundle")
            self.assertEqual(root["bundle_version"], 1)
            self.assertIs(root["inventory_complete"], True)
            deleted_item = next(item for item in root["items"] if item["source_id"] == "src_mail_3")
            self.assertEqual(deleted_item["lifecycle_state"], "deleted")
            self.assertNotIn("sources/src_mail_3/v1/original.bin", names)

    def test_empty_bundle_declares_a_complete_empty_inventory(self) -> None:
        encoded = encode_bundle(())
        self.assertEqual(decode_bundle(encoded), ())
        with zipfile.ZipFile(io.BytesIO(encoded)) as archive:
            manifest = json.loads(archive.read("bundle.json"))
        self.assertIs(manifest["inventory_complete"], True)
        self.assertEqual(manifest["items"], [])

    def test_rejects_missing_inventory_marker_and_unknown_root_fields(self) -> None:
        incomplete = _root_manifest()
        incomplete.pop("inventory_complete")
        unknown = {**_root_manifest(), "provider_memory_ids": []}

        for manifest in (incomplete, unknown):
            with self.subTest(manifest=manifest), self.assertRaises(BundleError):
                decode_bundle(_archive(manifest))

    def test_rejects_unsupported_bundle_version_and_unknown_item_fields(self) -> None:
        unsupported = {**_root_manifest(), "bundle_version": 2}
        unknown_item = _root_manifest(
            [{"source_id": "src_a", "source_version": 1, "lifecycle_state": "deleted", "note": "x"}]
        )

        for manifest in (unsupported, unknown_item):
            with self.subTest(manifest=manifest), self.assertRaises(BundleError):
                decode_bundle(_archive(manifest))

    def test_rejects_duplicate_archive_paths_and_unlisted_members(self) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            duplicate = _archive(_root_manifest(), members=[("bundle.json", b"{}", None)])
        extra = _archive(_root_manifest(), members=[("unlisted.txt", b"x", None)])

        for raw in (duplicate, extra):
            with self.subTest(raw=raw[:8]), self.assertRaises(BundleError):
                decode_bundle(raw)

    def test_rejects_traversal_and_symlink_members(self) -> None:
        traversal = _archive(_root_manifest(), members=[("../outside", b"x", None)])
        symlink = _archive(
            _root_manifest(),
            members=[("link", b"target", stat.S_IFLNK | 0o777)],
        )

        for raw in (traversal, symlink):
            with self.subTest(raw=raw[:8]), self.assertRaises(BundleError):
                decode_bundle(raw)

    def test_rejects_duplicate_source_versions_and_lifecycle_conflicts(self) -> None:
        item = {"source_id": "src_a", "source_version": 1, "lifecycle_state": "deleted"}
        duplicate = _root_manifest([item, item])
        conflict = _root_manifest(
            [item, {"source_id": "src_a", "source_version": 1, "lifecycle_state": "active"}]
        )

        for manifest in (duplicate, conflict):
            with self.subTest(manifest=manifest), self.assertRaises(BundleError):
                decode_bundle(_archive(manifest))

    def test_rejects_deleted_records_with_content_members(self) -> None:
        item = {"source_id": "src_a", "source_version": 1, "lifecycle_state": "deleted"}
        raw = _archive(
            _root_manifest([item]),
            members=[("sources/src_a/v1/original.bin", b"must not restore", None)],
        )
        with self.assertRaises(BundleError):
            decode_bundle(raw)

    def test_rejects_missing_source_members_and_checksum_mismatch(self) -> None:
        source = _record("src_a", 1, b"original")
        encoded = encode_bundle((source,))
        with zipfile.ZipFile(io.BytesIO(encoded)) as archive:
            manifest = json.loads(archive.read("bundle.json"))
            members = {
                name: archive.read(name)
                for name in archive.namelist()
                if name != "bundle.json"
            }

        missing = _archive(manifest)
        members["sources/src_a/v1/original.bin"] = b"tampered"
        mismatch = _archive(
            manifest,
            members=[(name, content, None) for name, content in members.items()],
        )
        for raw in (missing, mismatch):
            with self.subTest(raw=raw[:8]), self.assertRaises(BundleError):
                decode_bundle(raw)

    def test_rejects_source_identity_mismatch_and_invalid_deleted_identity(self) -> None:
        source = _record("src_a", 1, b"original")
        encoded = encode_bundle((source,))
        with zipfile.ZipFile(io.BytesIO(encoded)) as archive:
            root = json.loads(archive.read("bundle.json"))
            member_data = {
                name: archive.read(name)
                for name in archive.namelist()
                if name != "bundle.json"
            }
        root["items"][0]["source_id"] = "src_other"
        mismatch = _archive(root, members=[(name, data, None) for name, data in member_data.items()])
        invalid_identity = _archive(
            _root_manifest([{"source_id": "../escape", "source_version": 1, "lifecycle_state": "deleted"}])
        )

        for raw in (mismatch, invalid_identity):
            with self.subTest(raw=raw[:8]), self.assertRaises(BundleError):
                decode_bundle(raw)

    def test_rejects_per_source_and_bundle_size_overflow(self) -> None:
        raw = encode_bundle((_record("src_large", 1, b"12345"),))
        with self.assertRaises(BundleError):
            decode_bundle(raw, limits=JournalLimits(max_file_bytes=4))

        with self.assertRaises(BundleError):
            decode_bundle(raw, limits=JournalLimits(max_file_bytes=64))

    def test_rejects_aggregate_decompression_overflow_before_returning_records(self) -> None:
        raw = encode_bundle((_record("src_large", 1, b"123456789"),))
        with patch("lifecontext.bundle.MAX_BUNDLE_UNCOMPRESSED_BYTES", 8):
            with self.assertRaises(BundleError):
                decode_bundle(raw)

    def test_encoder_rejects_aggregate_uncompressed_overflow(self) -> None:
        with patch("lifecontext.bundle.MAX_BUNDLE_UNCOMPRESSED_BYTES", 8):
            with self.assertRaises(BundleError):
                encode_bundle((_record("src_large", 1, b"x"),))

    def test_validation_errors_are_content_free(self) -> None:
        secret_content = b"private-source-text"
        raw = _archive(
            _root_manifest(),
            members=[("unlisted.txt", secret_content, None)],
        )
        try:
            decode_bundle(raw)
        except BundleError as exc:
            self.assertNotIn(secret_content.decode(), str(exc))
            self.assertNotIn("unlisted.txt", str(exc))
        else:
            self.fail("invalid bundle unexpectedly accepted")

    def test_provider_specific_identifiers_in_provenance_are_rejected(self) -> None:
        content = b"source"
        manifest = SourceManifest(
            source_id="src_provider_id",
            source_version=1,
            sha256=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
            provenance={"capture": {"hindsight": {"memory_id": "provider-secret-id"}}},
        )
        record = BundleRecord("src_provider_id", 1, "active", manifest, content)

        with self.assertRaises(BundleError):
            encode_bundle((record,))


if __name__ == "__main__":
    unittest.main()
