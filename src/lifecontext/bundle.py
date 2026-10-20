"""Deterministic, bounded LifeContext custody bundles."""

from __future__ import annotations

import hashlib
import io
import json
import re
import stat
import zipfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from lifecontext.config import LIMIT_CEILINGS, JournalLimits
from lifecontext.custody import CustodyError, SourceManifest


BUNDLE_FORMAT = "lifeweave.bundle"
BUNDLE_VERSION = 1
BUNDLE_MEDIA_TYPE = "application/vnd.lifeweave.bundle"
MAX_BUNDLE_ITEMS = 4_096
MAX_BUNDLE_UNCOMPRESSED_BYTES = LIMIT_CEILINGS["max_file_bytes"]
_MAX_BUNDLE_BYTES = LIMIT_CEILINGS["max_file_bytes"]
_MAX_BUNDLE_MANIFEST_BYTES = 4 * 1024 * 1024
_MAX_SOURCE_MANIFEST_BYTES = 64 * 1024
_MAX_SOURCE_VERSION = 2_147_483_647
_SOURCE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_ROOT_KEYS = {"bundle_format", "bundle_version", "inventory_complete", "items"}
_ITEM_KEYS = {"source_id", "source_version", "lifecycle_state"}
_LIFECYCLE_STATES = {"active", "forgotten", "deleted"}
_MANIFEST_KEYS = {
    "schema_version", "source_id", "source_version", "sha256", "size_bytes", "provenance"
}


class BundleError(ValueError):
    """Stable, content-free invalid bundle error."""


@dataclass(frozen=True)
class BundleRecord:
    """One source version, including content-free deleted lifecycle markers."""

    source_id: str
    source_version: int
    lifecycle_state: str
    manifest: SourceManifest | None = None
    original_bytes: bytes | None = None


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise BundleError("invalid bundle") from None


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> None:
    raise ValueError("invalid JSON constant")


def _parse_json(raw: bytes) -> Any:
    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_json_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeError, ValueError, TypeError):
        raise BundleError("invalid bundle") from None


def _validate_identity(source_id: Any, source_version: Any) -> tuple[str, int]:
    if not isinstance(source_id, str) or not _SOURCE_ID.fullmatch(source_id):
        raise BundleError("invalid bundle")
    if (
        isinstance(source_version, bool)
        or not isinstance(source_version, int)
        or not 1 <= source_version <= _MAX_SOURCE_VERSION
    ):
        raise BundleError("invalid bundle")
    return source_id, source_version


def _has_provider_identifier(value: Any) -> bool:
    pending: list[tuple[Any, bool]] = [(value, False)]
    while pending:
        current, provider_context = pending.pop()
        if isinstance(current, Mapping):
            for key, child in current.items():
                if not isinstance(key, str):
                    return True
                tokens = set(re.sub(r"[^a-z0-9]+", " ", key.casefold()).split())
                scoped = provider_context or bool(tokens & {"hindsight", "provider"})
                if scoped and tokens & {"id", "ids"}:
                    return True
                pending.append((child, scoped))
        elif isinstance(current, (list, tuple)):
            pending.extend((child, provider_context) for child in current)
    return False


def _member_path(source_id: str, source_version: int, name: str) -> str:
    return f"sources/{source_id}/v{source_version}/{name}"


def _validate_member_name(name: str) -> None:
    if (
        not isinstance(name, str)
        or not name
        or "\\" in name
        or name.startswith("/")
        or "\x00" in name
    ):
        raise BundleError("invalid bundle")
    path = PurePosixPath(name)
    if path.is_absolute() or path.as_posix() != name or any(part in {"", ".", ".."} for part in path.parts):
        raise BundleError("invalid bundle")


def _max_bundle_bytes(limits: JournalLimits) -> int:
    return min(_MAX_BUNDLE_BYTES, limits.max_file_bytes)


def _max_uncompressed_bytes(limits: JournalLimits) -> int:
    return min(
        MAX_BUNDLE_UNCOMPRESSED_BYTES,
        limits.max_file_bytes * MAX_BUNDLE_ITEMS + _MAX_BUNDLE_MANIFEST_BYTES,
    )


def _validate_record(record: BundleRecord, max_source_bytes: int) -> BundleRecord:
    if not isinstance(record, BundleRecord):
        raise BundleError("invalid bundle")
    source_id, source_version = _validate_identity(record.source_id, record.source_version)
    if record.lifecycle_state not in _LIFECYCLE_STATES:
        raise BundleError("invalid bundle")

    if record.lifecycle_state == "deleted":
        if record.manifest is not None or record.original_bytes is not None:
            raise BundleError("invalid bundle")
        return record

    manifest = record.manifest
    content = record.original_bytes
    if not isinstance(manifest, SourceManifest) or not isinstance(content, bytes):
        raise BundleError("invalid bundle")
    if (
        manifest.source_id != source_id
        or manifest.source_version != source_version
        or _has_provider_identifier(manifest.provenance)
        or len(content) > max_source_bytes
        or manifest.size_bytes != len(content)
        or hashlib.sha256(content).hexdigest() != manifest.sha256
    ):
        raise BundleError("invalid bundle")
    if len(manifest.to_bytes()) > _MAX_SOURCE_MANIFEST_BYTES:
        raise BundleError("invalid bundle")
    return record


def _validate_records(
    records: Iterable[BundleRecord], limits: JournalLimits
) -> tuple[BundleRecord, ...]:
    if isinstance(records, (bytes, bytearray, str)):
        raise BundleError("invalid bundle")
    validated: list[BundleRecord] = []
    identities: set[tuple[str, int]] = set()
    total_source_bytes = 0
    try:
        iterator = iter(records)
        for record in iterator:
            if len(validated) >= MAX_BUNDLE_ITEMS:
                raise BundleError("bundle limit exceeded")
            validated_record = _validate_record(record, limits.max_file_bytes)
            identity = (validated_record.source_id, validated_record.source_version)
            if identity in identities:
                raise BundleError("invalid bundle")
            identities.add(identity)
            if validated_record.original_bytes is not None:
                total_source_bytes += len(validated_record.original_bytes)
                if total_source_bytes > _max_uncompressed_bytes(limits):
                    raise BundleError("bundle limit exceeded")
            validated.append(validated_record)
    except BundleError:
        raise
    except (TypeError, ValueError):
        raise BundleError("invalid bundle") from None
    return tuple(sorted(validated, key=lambda item: (item.source_id, item.source_version)))


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.create_system = 3
    info.compress_type = zipfile.ZIP_STORED
    info.external_attr = (stat.S_IFREG | 0o600) << 16
    info.flag_bits = 0
    return info


def encode_bundle(
    records: Iterable[BundleRecord], *, limits: JournalLimits | None = None
) -> bytes:
    """Encode a complete record inventory into a stable bounded ZIP bundle."""
    limits = limits or JournalLimits()
    ordered = _validate_records(records, limits)
    manifest = {
        "bundle_format": BUNDLE_FORMAT,
        "bundle_version": BUNDLE_VERSION,
        "inventory_complete": True,
        "items": [
            {
                "source_id": record.source_id,
                "source_version": record.source_version,
                "lifecycle_state": record.lifecycle_state,
            }
            for record in ordered
        ],
    }
    root_raw = _canonical_json(manifest)
    total_uncompressed = len(root_raw)
    for record in ordered:
        if record.lifecycle_state != "deleted":
            assert record.manifest is not None and record.original_bytes is not None
            total_uncompressed += len(record.manifest.to_bytes()) + len(record.original_bytes)
    if total_uncompressed > _max_uncompressed_bytes(limits):
        raise BundleError("bundle limit exceeded")

    buffer = io.BytesIO()
    try:
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr(_zip_info("bundle.json"), root_raw)
            for record in ordered:
                if record.lifecycle_state == "deleted":
                    continue
                assert record.manifest is not None and record.original_bytes is not None
                archive.writestr(
                    _zip_info(_member_path(record.source_id, record.source_version, "manifest.json")),
                    record.manifest.to_bytes(),
                )
                archive.writestr(
                    _zip_info(_member_path(record.source_id, record.source_version, "original.bin")),
                    record.original_bytes,
                )
    except (OSError, ValueError, zipfile.BadZipFile):
        raise BundleError("invalid bundle") from None
    raw = buffer.getvalue()
    if len(raw) > _max_bundle_bytes(limits):
        raise BundleError("bundle limit exceeded")
    return raw


def _check_zip_info(info: zipfile.ZipInfo) -> None:
    _validate_member_name(info.filename)
    if info.is_dir() or info.flag_bits & 0x1:
        raise BundleError("invalid bundle")
    if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
        raise BundleError("invalid bundle")
    mode = info.external_attr >> 16
    kind = stat.S_IFMT(mode)
    if kind not in (0, stat.S_IFREG):
        raise BundleError("invalid bundle")
    if isinstance(info.file_size, bool) or not isinstance(info.file_size, int) or info.file_size < 0:
        raise BundleError("invalid bundle")
    if isinstance(info.compress_size, bool) or not isinstance(info.compress_size, int) or info.compress_size < 0:
        raise BundleError("invalid bundle")


def _read_member(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    *,
    max_member_bytes: int,
    remaining_total_bytes: int,
) -> bytes:
    if info.file_size > max_member_bytes or info.file_size > remaining_total_bytes:
        raise BundleError("bundle limit exceeded")
    output = bytearray()
    try:
        with archive.open(info, "r") as member:
            while True:
                remaining = min(max_member_bytes, remaining_total_bytes) - len(output)
                chunk = member.read(min(64 * 1024, remaining + 1))
                if not chunk:
                    break
                output.extend(chunk)
                if len(output) > max_member_bytes or len(output) > remaining_total_bytes:
                    raise BundleError("bundle limit exceeded")
    except BundleError:
        raise
    except (OSError, RuntimeError, EOFError, zipfile.BadZipFile, ValueError):
        raise BundleError("invalid bundle") from None
    if len(output) != info.file_size:
        raise BundleError("invalid bundle")
    return bytes(output)


def _parse_root_manifest(raw: bytes) -> list[dict[str, Any]]:
    value = _parse_json(raw)
    if (
        not isinstance(value, dict)
        or set(value) != _ROOT_KEYS
        or value.get("bundle_format") != BUNDLE_FORMAT
        or isinstance(value.get("bundle_version"), bool)
        or value.get("bundle_version") != BUNDLE_VERSION
        or value.get("inventory_complete") is not True
        or not isinstance(value.get("items"), list)
        or len(value["items"]) > MAX_BUNDLE_ITEMS
    ):
        raise BundleError("invalid bundle")

    items: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for item in value["items"]:
        if not isinstance(item, dict) or set(item) != _ITEM_KEYS:
            raise BundleError("invalid bundle")
        source_id, source_version = _validate_identity(
            item.get("source_id"), item.get("source_version")
        )
        state = item.get("lifecycle_state")
        if not isinstance(state, str) or state not in _LIFECYCLE_STATES:
            raise BundleError("invalid bundle")
        identity = (source_id, source_version)
        if identity in seen:
            raise BundleError("invalid bundle")
        seen.add(identity)
        items.append({
            "source_id": source_id,
            "source_version": source_version,
            "lifecycle_state": state,
        })
    if items != sorted(items, key=lambda item: (item["source_id"], item["source_version"])):
        raise BundleError("invalid bundle")
    return items


def _read_source_manifest(raw: bytes) -> SourceManifest:
    value = _parse_json(raw)
    if (
        not isinstance(value, dict)
        or set(value) != _MANIFEST_KEYS
        or isinstance(value.get("schema_version"), bool)
        or value.get("schema_version") != 1
    ):
        raise BundleError("invalid bundle")
    try:
        manifest = SourceManifest.from_bytes(raw)
    except (CustodyError, TypeError, ValueError):
        raise BundleError("invalid bundle") from None
    return manifest


def decode_bundle(raw: bytes, *, limits: JournalLimits | None = None) -> tuple[BundleRecord, ...]:
    """Validate every archive entry and lifecycle marker before returning records."""
    limits = limits or JournalLimits()
    if not isinstance(raw, bytes) or not raw or len(raw) > _max_bundle_bytes(limits):
        raise BundleError("bundle limit exceeded")

    try:
        archive = zipfile.ZipFile(io.BytesIO(raw), "r")
    except (OSError, ValueError, zipfile.BadZipFile):
        raise BundleError("invalid bundle") from None

    with archive:
        infos = archive.infolist()
        if not infos or len(infos) > 1 + MAX_BUNDLE_ITEMS * 2:
            raise BundleError("bundle limit exceeded")
        info_by_name: dict[str, zipfile.ZipInfo] = {}
        declared_total = 0
        max_total = _max_uncompressed_bytes(limits)
        for info in infos:
            _check_zip_info(info)
            if info.filename in info_by_name:
                raise BundleError("invalid bundle")
            info_by_name[info.filename] = info
            declared_total += info.file_size
            if declared_total > max_total:
                raise BundleError("bundle limit exceeded")

        root_info = info_by_name.get("bundle.json")
        if root_info is None:
            raise BundleError("invalid bundle")
        root_raw = _read_member(
            archive,
            root_info,
            max_member_bytes=_MAX_BUNDLE_MANIFEST_BYTES,
            remaining_total_bytes=max_total,
        )
        items = _parse_root_manifest(root_raw)

        expected_names = {"bundle.json"}
        expected_paths: list[tuple[dict[str, Any], str | None, str | None]] = []
        for item in items:
            source_id = item["source_id"]
            source_version = item["source_version"]
            if item["lifecycle_state"] == "deleted":
                expected_paths.append((item, None, None))
            else:
                manifest_name = _member_path(source_id, source_version, "manifest.json")
                original_name = _member_path(source_id, source_version, "original.bin")
                expected_names.update((manifest_name, original_name))
                expected_paths.append((item, manifest_name, original_name))
        if set(info_by_name) != expected_names:
            raise BundleError("invalid bundle")

        records: list[BundleRecord] = []
        actual_total = len(root_raw)
        for item, manifest_name, original_name in expected_paths:
            if manifest_name is None or original_name is None:
                records.append(BundleRecord(
                    source_id=item["source_id"],
                    source_version=item["source_version"],
                    lifecycle_state="deleted",
                ))
                continue

            manifest_info = info_by_name[manifest_name]
            manifest_raw = _read_member(
                archive,
                manifest_info,
                max_member_bytes=_MAX_SOURCE_MANIFEST_BYTES,
                remaining_total_bytes=max_total - actual_total,
            )
            actual_total += len(manifest_raw)
            manifest = _read_source_manifest(manifest_raw)
            if (
                manifest.source_id != item["source_id"]
                or manifest.source_version != item["source_version"]
            ):
                raise BundleError("invalid bundle")

            original_info = info_by_name[original_name]
            original = _read_member(
                archive,
                original_info,
                max_member_bytes=limits.max_file_bytes,
                remaining_total_bytes=max_total - actual_total,
            )
            actual_total += len(original)
            record = BundleRecord(
                source_id=item["source_id"],
                source_version=item["source_version"],
                lifecycle_state=item["lifecycle_state"],
                manifest=manifest,
                original_bytes=original,
            )
            records.append(_validate_record(record, limits.max_file_bytes))

        if actual_total != declared_total:
            raise BundleError("invalid bundle")
        return tuple(records)
