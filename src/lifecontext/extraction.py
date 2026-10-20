"""Attributed extraction with bounded ZIP preflight and resource-limited workers.

Native parsers and optional local OCR run in a separate POSIX process with hard
address-space, CPU, file-size and parent wall-time limits. This is resource
isolation, not a security sandbox. No macros, links or embedded objects execute.
"""

from __future__ import annotations

import json
import math
import os
import re
import stat
import struct
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from functools import lru_cache
from hashlib import sha256
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Literal
from zipfile import BadZipFile, ZipFile
from zlib import error as ZlibError

Status = Literal[
    "completed", "blocked", "unsupported", "locked", "failed", "limit_exceeded"
]
Format = Literal[
    "text", "markdown", "pdf", "docx", "xlsx", "pptx", "png", "jpeg", "tiff"
]
EXTRACTOR_VERSION = "1"


@dataclass(frozen=True)
class ExtractionLimits:
    max_original_bytes: int = 32 * 1024 * 1024
    max_text_bytes: int = 8 * 1024 * 1024
    max_text_characters: int = 4 * 1024 * 1024
    max_zip_entries: int = 2048
    max_zip_entry_bytes: int = 16 * 1024 * 1024
    max_zip_expanded_bytes: int = 64 * 1024 * 1024
    max_zip_ratio: float = 100.0
    max_pages: int = 5000
    max_sheets: int = 50
    max_slides: int = 200
    max_pixels: int = 20_000_000
    max_seconds: float = 600.0
    max_temp_bytes: int = 256 * 1024 * 1024
    max_worker_bytes: int = 512 * 1024 * 1024
    max_output_bytes: int = 32 * 1024 * 1024
    max_cells: int = 100_000
    max_frames: int = 200
    max_segments: int = 100_000
    max_ocr_seconds: float = 30.0
    ocr_dpi: int = 150
    # Basis points (1/100 of a percent) keep thresholds integral and in limits.identity.
    # Measured on the vault books: dividers and ornaments cover under 0.5% of a page;
    # image headings and logos start near 1.3%, figures and photos well above.
    figure_min_area_bp: int = 100
    figure_min_paths: int = 150
    # A page whose largest image covers this much and whose text is all invisible
    # (render mode 3) is a searchable scan: its OCR layer stands for that image.
    scan_min_area_bp: int = 9000
    # Small images placed identically on this many pages are page furniture (logos).
    furniture_min_pages: int = 3
    furniture_max_area_bp: int = 2500
    suspect_text_bp: int = 500
    # Blank means at most this many dark pixels AND at most this share of the page;
    # one handwritten word at the OCR resolution already exceeds both.
    blank_max_ink_pixels: int = 64
    blank_max_ink_bp: int = 10
    max_vision_bytes: int = 64 * 1024 * 1024
    vision_max_edge: int = 2000

    def __post_init__(self) -> None:
        for key, value in asdict(self).items():
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"invalid extraction limit: {key}")
            if key not in {
                "max_seconds",
                "max_zip_ratio",
                "max_ocr_seconds",
            } and not isinstance(value, int):
                raise ValueError(f"integer extraction limit required: {key}")

    @property
    def identity(self) -> str:
        return sha256(
            json.dumps(asdict(self), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


@dataclass(frozen=True)
class Locator:
    """One-based original coordinates; character offsets are zero-based, exclusive."""

    kind: Literal[
        "lines", "page", "paragraph", "table_cell", "sheet_cell", "slide", "image"
    ]
    start: int
    end: int
    container: str | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1 or self.kind not in {
            "lines",
            "page",
            "paragraph",
            "table_cell",
            "sheet_cell",
            "slide",
            "image",
        }:
            raise ValueError("invalid locator schema")
        if (
            any(
                isinstance(v, bool) or not isinstance(v, int)
                for v in (self.start, self.end)
            )
            or not 1 <= self.start <= self.end
        ):
            raise ValueError("invalid locator bounds")
        if self.kind in {"table_cell", "sheet_cell"} and not self.container:
            raise ValueError("cell locator requires container")
        if self.container is not None and (
            not isinstance(self.container, str) or not self.container.strip()
        ):
            raise ValueError("invalid locator container")


@dataclass(frozen=True)
class TextSegment:
    text: str
    start: int
    end: int
    locator: Locator

    def __post_init__(self) -> None:
        if (
            not isinstance(self.text, str)
            or not self.text
            or not isinstance(self.locator, Locator)
        ):
            raise ValueError("invalid text segment")
        if (
            any(
                isinstance(v, bool) or not isinstance(v, int)
                for v in (self.start, self.end)
            )
            or self.start < 0
            or self.end != self.start + len(self.text)
        ):
            raise ValueError("invalid segment offsets")


@dataclass(frozen=True)
class ExtractionArtifact:
    source_id: str
    source_version: int
    original_checksum: str
    extraction_revision: int
    config_identity: str
    status: Status
    segments: tuple[TextSegment, ...] = ()
    warnings: tuple[str, ...] = ()
    reason: str | None = None
    format: Format | None = None
    extractor: str = "life-context-extraction"
    extractor_version: str = EXTRACTOR_VERSION
    schema_version: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.source_id, str) or not self.source_id.strip():
            raise ValueError("invalid source identity")
        for value in (self.source_version, self.extraction_revision):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError("invalid revision")
        for digest in (self.original_checksum, self.config_identity):
            if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
                raise ValueError("invalid checksum")
        if self.status not in {
            "completed",
            "blocked",
            "unsupported",
            "locked",
            "failed",
            "limit_exceeded",
        }:
            raise ValueError("invalid extraction status")
        if (
            not isinstance(self.segments, tuple)
            or not isinstance(self.warnings, tuple)
            or any(not isinstance(w, str) for w in self.warnings)
        ):
            raise ValueError("immutable segments and warnings required")
        if self.status == "completed":
            if not self.segments or self.reason is not None:
                raise ValueError("completed extraction requires text")
        elif self.segments or not self.reason:
            raise ValueError("failure requires reason and no partial text")
        if self.schema_version != 1 or not self.extractor or not self.extractor_version:
            raise ValueError("invalid extractor identity")
        if self.format is not None and self.format not in {
            "text",
            "markdown",
            "pdf",
            "docx",
            "xlsx",
            "pptx",
            "png",
            "jpeg",
            "tiff",
        }:
            raise ValueError("invalid extraction format")
        offset = 0
        for segment in self.segments:
            if not isinstance(segment, TextSegment) or segment.start != offset:
                raise ValueError("segments must be ordered and contiguous")
            offset = segment.end

    @property
    def text(self) -> str:
        return "".join(segment.text for segment in self.segments)

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def artifact_checksum(self) -> str:
        return sha256(
            json.dumps(
                self.to_dict(),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode()
        ).hexdigest()


# Ledger codes. Each warning reads "<code>:<item>[; detail]".
_UNIT_KINDS = {"page": "page", "frame": "image"}
PARTIAL_CODES = frozenset(
    {
        "figure_not_read",
        "text_layer_suspect",
        "embedded_image_not_read",
        "chart_not_read",
        "figure_check_unavailable",
        "ledger_truncated",
    }
)
_MAX_COVERAGE_ITEMS = 1000
# The catalog and bundle formats accept at most 1000 warnings of at most 1024 characters.
MAX_LEDGER_ENTRIES = 1000
_COMPACT_ABOVE = 900
_MAX_ENTRY = 1024
_RANGE = re.compile(r"([1-9][0-9]*)-([1-9][0-9]*)\Z")


def ledger_items(warning: str) -> tuple[str, list[str], str]:
    """Split "<code>:<item>[,<item>...][; detail]" into code, expanded items, detail.

    Numeric items may be compacted into ranges ("12-15"); a warning without ":"
    before its first ";" concerns the whole document and has no items.
    """
    head, _, detail = warning.partition(";")
    code, colon, items = head.partition(":")
    if not colon:
        return code.strip(), [], detail.strip()
    expanded: list[str] = []
    for token in items.split(","):
        token = token.strip()
        match = _RANGE.fullmatch(token)
        if match and int(match.group(1)) <= int(match.group(2)) <= 1_000_000:
            expanded.extend(str(n) for n in range(int(match.group(1)), int(match.group(2)) + 1))
        elif token:
            expanded.append(token)
    return code.strip(), expanded, detail.strip()


def _tokens(items: list[str]) -> list[str]:
    numbers = sorted({int(i) for i in items if i.isdigit()})
    tokens, start = [], None
    for index, number in enumerate(numbers):
        if start is None:
            start = number
        if index + 1 == len(numbers) or numbers[index + 1] != number + 1:
            tokens.append(str(start) if start == number else f"{start}-{number}")
            start = None
    return tokens + [i for i in items if not i.isdigit()]


def _pack(groups, singles) -> list[str]:
    """Item lists per (code, detail), each entry within the catalog's UTF-8 byte bound."""
    compacted: list[str] = []
    for (code, detail), items in groups.items():
        if not items:
            compacted.append(singles[(code, detail)])
            continue
        suffix = f"; {detail}" if detail else ""
        line = ""
        for token in _tokens(items):
            candidate = f"{line},{token}" if line else f"{code}:{token}"
            if line and len(candidate.encode()) + len(suffix.encode()) > _MAX_ENTRY:
                compacted.append(line + suffix)
                candidate = f"{code}:{token}"
            line = candidate
        compacted.append(line + suffix)
    return compacted


def compact_ledger(warnings) -> tuple[str, ...]:
    """Keep the ledger within the catalog's bounds without dropping an item.

    Short ledgers are returned unchanged. Long ones merge entries that share a
    code and detail into item lists with numeric ranges, in first-seen order.
    """
    warnings = tuple(warnings)
    if len(warnings) <= _COMPACT_ABOVE:
        return warnings
    groups: dict[tuple[str, str], list[str]] = {}
    singles: dict[tuple[str, str], str] = {}
    for warning in warnings:
        code, items, detail = ledger_items(warning)
        key = (code, detail)
        if not items:
            singles.setdefault(key, warning)
            groups.setdefault(key, [])
        else:
            groups.setdefault(key, []).extend(items)
    compacted = _pack(groups, singles)
    if len(compacted) > MAX_LEDGER_ENTRIES:
        # Too many distinct details (one per picture location): keep every item and
        # drop only the details.
        merged: dict[tuple[str, str], list[str]] = {}
        for (code, detail), items in groups.items():
            if items:
                merged.setdefault((code, "details omitted; see the original"), []).extend(items)
            else:
                merged.setdefault((code, detail), [])
        compacted = _pack(merged, singles)
    if len(compacted) > MAX_LEDGER_ENTRIES:
        dropped = len(compacted) - MAX_LEDGER_ENTRIES + 1
        compacted = compacted[: MAX_LEDGER_ENTRIES - 1] + [
            f"ledger_truncated; {dropped} entries omitted"
        ]
    return tuple(compacted)


def expand_ledger(warnings) -> tuple[str, ...]:
    """Inverse of :func:`compact_ledger`: one "<code>:<item>[; detail]" entry per item."""
    expanded: list[str] = []
    for warning in warnings:
        code, items, detail = ledger_items(warning)
        if len(items) <= 1:
            expanded.append(warning)
            continue
        suffix = f"; {detail}" if detail else ""
        expanded.extend(f"{code}:{item}{suffix}" for item in items)
    return tuple(expanded)


def coverage_summary(payload) -> dict:
    """Account for every page or frame from segment locators plus ledger warnings.

    No catalog column or artifact field is needed: after extraction each page or
    frame either carries a segment or a ledger entry, so the union is the total.
    """
    unit = {"pdf": "page", "png": "frame", "jpeg": "frame", "tiff": "frame"}.get(
        payload.get("format")
    )
    with_text: set[int] = set()
    methods: dict[int, str] = {}
    partial: list[str] = []
    vision = 0
    for segment in payload.get("segments", ()):
        locator = segment["locator"]
        if unit and locator["kind"] == _UNIT_KINDS[unit]:
            with_text.add(locator["start"])
    for warning in payload.get("warnings", ()):
        code, items, _ = ledger_items(warning)
        if code in PARTIAL_CODES:
            if items:
                partial.extend(f"{code}:{item}" for item in items)
            else:
                partial.append(code)
            continue
        if code in {"vision_page", "vision_figure", "vision_frame", "vision_picture"}:
            vision += len(items)
        if not unit:
            continue
        for item in items:
            if not item.isdigit():
                continue
            index = int(item)
            if code == f"ocr_{unit}":
                methods[index] = "ocr"
            elif code == f"vision_{unit}":
                methods[index] = "vision"
            elif code == f"blank_{unit}":
                methods[index] = "blank"
            elif code in {f"ocr_no_text_{unit}", f"ocr_failed_{unit}", f"ocr_unavailable_{unit}"}:
                methods.setdefault(index, "unresolved")
    counts = {"text": 0, "ocr": 0, "vision": 0, "blank": 0}
    unresolved: list[str] = []
    indices = sorted(with_text | set(methods))
    for index in indices:
        method = methods.get(index) or ("text" if index in with_text else "unresolved")
        if method == "unresolved":
            unresolved.append(f"{unit}:{index}")
        else:
            counts[method] += 1
    return {
        "unit": unit,
        "total": len(indices) if unit else None,
        "resolved": len(indices) - len(unresolved) if unit else None,
        "text": counts["text"],
        "ocr": counts["ocr"],
        "blank": counts["blank"],
        # Every model-read item: whole pages or frames, figures on text pages, pictures.
        "vision": vision,
        "unresolved": unresolved[:_MAX_COVERAGE_ITEMS],
        "partial": partial[:_MAX_COVERAGE_ITEMS],
        # Nothing read is never complete, even when nothing is listed as unread.
        "complete": bool(payload.get("segments")) and not unresolved and not partial,
    }


class _Rejected(Exception):
    def __init__(self, status: Status, reason: str):
        self.status, self.reason = status, reason


def validate_filename(filename: str) -> str:
    if (
        not isinstance(filename, str)
        or not filename
        or len(filename.encode("utf-8")) > 255
    ):
        raise ValueError("unsafe filename")
    if (
        filename in {".", ".."}
        or filename[-1:] in {" ", "."}
        or any(ord(c) < 32 or ord(c) == 127 or c in "/\\:" for c in filename)
    ):
        raise ValueError("unsafe filename")
    return filename


def _zip_directory_preflight(
    data: bytes, limits: ExtractionLimits, deadline: float
) -> None:
    # Count actual central records before ZipFile can allocate ZipInfo objects.
    end = data.rfind(b"PK\x05\x06", max(0, len(data) - 65557))
    if end < 0 or end + 22 > len(data):
        raise _Rejected("failed", "malformed_archive")
    disk, cd_disk, disk_count, count, size, offset, comment = struct.unpack_from(
        "<4H2IH", data, end + 4
    )
    if disk or cd_disk or disk_count != count:
        raise _Rejected("unsupported", "multipart_zip")
    if (
        count == 65535
        or size == 0xFFFFFFFF
        or offset == 0xFFFFFFFF
        or data[max(0, end - 20) : end].startswith(b"PK\x06\x07")
    ):
        raise _Rejected("unsupported", "zip64_not_supported")
    if end + 22 + comment != len(data) or offset + size != end:
        raise _Rejected("failed", "malformed_archive")
    pos, actual = offset, 0
    while pos < end:
        if time.monotonic() > deadline:
            raise _Rejected("limit_exceeded", "time_limit")
        if pos + 46 > end or data[pos : pos + 4] != b"PK\x01\x02":
            raise _Rejected("failed", "malformed_archive")
        name, extra, note = struct.unpack_from("<3H", data, pos + 28)
        next_pos = pos + 46 + name + extra + note
        if next_pos > end:
            raise _Rejected("failed", "malformed_archive")
        extra_pos = pos + 46 + name
        while extra_pos < pos + 46 + name + extra:
            if extra_pos + 4 > pos + 46 + name + extra:
                raise _Rejected("failed", "malformed_archive")
            tag, length = struct.unpack_from("<HH", data, extra_pos)
            if tag == 1:
                raise _Rejected("unsupported", "zip64_not_supported")
            extra_pos += 4 + length
        if extra_pos != pos + 46 + name + extra:
            raise _Rejected("failed", "malformed_archive")
        actual += 1
        if actual > limits.max_zip_entries:
            raise _Rejected("limit_exceeded", "zip_entry_limit")
        pos = next_pos
    if actual != count:
        raise _Rejected("failed", "malformed_archive")


def _zip_preflight(
    data: bytes, format: Format, limits: ExtractionLimits, deadline: float
) -> None:
    _zip_directory_preflight(data, limits, deadline)
    required = {
        "docx": "word/document.xml",
        "xlsx": "xl/workbook.xml",
        "pptx": "ppt/presentation.xml",
    }[format]
    with ZipFile(BytesIO(data)) as archive:
        entries = archive.infolist()
        if len(entries) > limits.max_zip_entries:
            raise _Rejected("limit_exceeded", "zip_entry_limit")
        names: set[str] = set()
        expanded = 0
        for entry in entries:
            if time.monotonic() > deadline:
                raise _Rejected("limit_exceeded", "time_limit")
            name = entry.filename
            path = PurePosixPath(name)
            if (
                not name
                or len(name) > 1024
                or path.is_absolute()
                or any(p in {".", "..", ""} for p in name.rstrip("/").split("/"))
                or any(ord(c) < 32 or c in "\\:" for c in name)
                or name in names
                or stat.S_ISLNK(entry.external_attr >> 16)
            ):
                raise _Rejected("failed", "unsafe_zip_path")
            names.add(name)
            if entry.flag_bits & 1:
                raise _Rejected("locked", "encrypted_zip")
            expanded += entry.file_size
            if (
                entry.file_size > limits.max_zip_entry_bytes
                or expanded > limits.max_zip_expanded_bytes
                or entry.file_size / max(entry.compress_size, 1) > limits.max_zip_ratio
            ):
                raise _Rejected("limit_exceeded", "zip_expansion_limit")
            if entry.compress_type not in {0, 8}:
                raise _Rejected("unsupported", "zip_compression")
            # Stream every member to verify actual expansion and CRC, bounded before allocation.
            actual = 0
            with archive.open(entry) as member:
                while chunk := member.read(64 * 1024):
                    actual += len(chunk)
                    if actual > entry.file_size or actual > limits.max_zip_entry_bytes:
                        raise _Rejected("limit_exceeded", "zip_expansion_limit")
                    if time.monotonic() > deadline:
                        raise _Rejected("limit_exceeded", "time_limit")
            if actual != entry.file_size:
                raise _Rejected("failed", "malformed_zip")
        if required not in names or "[Content_Types].xml" not in names:
            raise _Rejected("failed", "ooxml_signature_mismatch")
        if any(name.lower().endswith("vbaproject.bin") for name in names):
            raise _Rejected("unsupported", "macro_enabled_office")


@dataclass(frozen=True)
class RoutedImage:
    """One page, frame or picture the worker rendered for an optional vision reader.

    ``after`` is the segment position the reading belongs at; ``replaces`` names an
    OCR segment that a successful reading supersedes, and ``resolves`` the ledger
    entries it settles.
    """

    item: str
    mode: Literal["page", "figure", "frame", "picture"]
    kind: Literal["page", "image", "slide"]
    index: int
    after: int
    data: bytes
    replaces: int | None = None
    resolves: tuple[str, ...] = ()
    context: str | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.item, str)
            or not re.fullmatch(r"(page|frame|picture):[1-9][0-9]{0,6}", self.item)
            or self.mode not in {"page", "figure", "frame", "picture"}
            or self.kind not in {"page", "image", "slide"}
            or any(
                isinstance(v, bool) or not isinstance(v, int)
                for v in (self.index, self.after)
            )
            or self.index < 1
            or self.after < 0
            or (self.replaces is not None and (
                isinstance(self.replaces, bool)
                or not isinstance(self.replaces, int)
                or self.replaces != self.after
            ))
            or not isinstance(self.data, bytes)
            or not self.data.startswith(b"\xff\xd8\xff")
            or not isinstance(self.resolves, tuple)
            or any(not isinstance(w, str) for w in self.resolves)
            or (self.context is not None and not isinstance(self.context, str))
        ):
            raise ValueError("invalid routed image")


def extract_document(
    data: bytes,
    filename: str,
    *,
    source_id: str,
    source_version: int = 1,
    extraction_revision: int = 1,
    limits: ExtractionLimits | None = None,
    tesseract_path: str | None = None,
) -> ExtractionArtifact:
    """Extract an original; OCR requires an explicit absolute executable path."""
    return extract_document_with_images(
        data,
        filename,
        source_id=source_id,
        source_version=source_version,
        extraction_revision=extraction_revision,
        limits=limits,
        tesseract_path=tesseract_path,
    )[0]


def extract_document_with_images(
    data: bytes,
    filename: str,
    *,
    source_id: str,
    source_version: int = 1,
    extraction_revision: int = 1,
    limits: ExtractionLimits | None = None,
    tesseract_path: str | None = None,
    vision_items: int = 0,
) -> tuple[ExtractionArtifact, tuple[RoutedImage, ...]]:
    """Extract and, when ``vision_items`` > 0, also return bounded routed images."""
    if not isinstance(data, bytes):
        raise TypeError("original must be bytes")
    if isinstance(vision_items, bool) or not isinstance(vision_items, int) or vision_items < 0:
        raise ValueError("invalid vision item budget")
    limits = limits or ExtractionLimits()
    deadline = time.monotonic() + limits.max_seconds
    identity = {
        "source_id": source_id,
        "source_version": source_version,
        "original_checksum": sha256(data).hexdigest(),
        "extraction_revision": extraction_revision,
        "config_identity": sha256(
            (limits.identity + (tesseract_path or "")).encode()
        ).hexdigest(),
    }
    ExtractionArtifact(**identity, status="failed", reason="not_started")
    format: Format | None = None
    try:
        try:
            validate_filename(filename)
        except (ValueError, UnicodeError):
            raise _Rejected("failed", "unsafe_filename") from None
        if len(data) > limits.max_original_bytes:
            raise _Rejected("limit_exceeded", "original_byte_limit")
        suffix = PurePosixPath(filename).suffix.lower()
        formats = {
            ".txt": "text",
            ".md": "markdown",
            ".markdown": "markdown",
            ".pdf": "pdf",
            ".docx": "docx",
            ".xlsx": "xlsx",
            ".pptx": "pptx",
            ".png": "png",
            ".jpg": "jpeg",
            ".jpeg": "jpeg",
            ".tif": "tiff",
            ".tiff": "tiff",
        }
        format = formats.get(suffix)
        if data.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
            raise _Rejected(
                "locked" if suffix in {".docx", ".xlsx", ".pptx"} else "unsupported",
                "encrypted_or_legacy_office",
            )
        if format is None:
            raise _Rejected("unsupported", "unsupported_format")
        if format in {"text", "markdown"}:
            if len(data) > limits.max_text_bytes:
                raise _Rejected("limit_exceeded", "text_byte_limit")
            if (
                data.startswith((b"%PDF-", b"PK\x03\x04", b"\x89PNG", b"\xff\xd8\xff"))
                or b"\x00" in data
            ):
                raise _Rejected("failed", "text_signature_mismatch")
            try:
                text = data.decode("utf-8-sig")
            except UnicodeError:
                raise _Rejected("failed", "invalid_utf8") from None
            if len(text) > limits.max_text_characters:
                raise _Rejected("limit_exceeded", "text_character_limit")
            if not text.strip():
                raise _Rejected("failed", "no_extractable_text")
            segments = []
            offset = 0
            for line, value in enumerate(text.splitlines(keepends=True), 1):
                if time.monotonic() > deadline:
                    raise _Rejected("limit_exceeded", "time_limit")
                if len(segments) >= limits.max_segments:
                    raise _Rejected("limit_exceeded", "segment_limit")
                segments.append(
                    TextSegment(
                        value, offset, offset + len(value), Locator("lines", line, line)
                    )
                )
                offset += len(value)
            return ExtractionArtifact(
                **identity, status="completed", format=format, segments=tuple(segments)
            ), ()
        signatures = {
            "pdf": (b"%PDF-",),
            "docx": (b"PK\x03\x04",),
            "xlsx": (b"PK\x03\x04",),
            "pptx": (b"PK\x03\x04",),
            "png": (b"\x89PNG\r\n\x1a\n",),
            "jpeg": (b"\xff\xd8\xff",),
            "tiff": (b"II*\x00", b"MM\x00*"),
        }
        if not data.startswith(signatures[format]):
            raise _Rejected("failed", "signature_mismatch")
        if format in {"docx", "xlsx", "pptx"}:
            _zip_preflight(data, format, limits, deadline)
        result = _run_worker(
            data, format, limits, deadline, tesseract_path, vision_items
        )
        segments = tuple(
            TextSegment(s["text"], s["start"], s["end"], Locator(**s["locator"]))
            for s in result["segments"]
        )
        return ExtractionArtifact(
            **identity,
            status=result["status"],
            reason=result["reason"],
            format=format,
            segments=segments,
            warnings=tuple(result["warnings"]),
        ), tuple(result.get("routed", ()))
    except _Rejected as error:
        return ExtractionArtifact(
            **identity, status=error.status, reason=error.reason, format=format
        ), ()
    except (
        BadZipFile,
        ZlibError,
        RuntimeError,
        NotImplementedError,
        EOFError,
        OSError,
        ValueError,
    ):
        return ExtractionArtifact(
            **identity, status="failed", reason="malformed_archive", format=format
        ), ()


_ROUTED_FILE = re.compile(r"vision-[0-9]{4}\.jpg\Z")


def _read_routed(root, result, limits, vision_items):
    """Copy routed images out of the worker directory before it is removed."""
    routed, total = [], 0
    entries = result.get("images", [])
    if not isinstance(entries, list) or len(entries) > vision_items:
        raise _Rejected("limit_exceeded", "worker_output_limit")
    for entry in entries:
        if not isinstance(entry, dict) or not _ROUTED_FILE.fullmatch(str(entry.get("file"))):
            raise _Rejected("failed", "worker_resource_or_parser_failure")
        path = root / entry["file"]
        info = path.lstat()
        total += info.st_size
        if not stat.S_ISREG(info.st_mode) or total > limits.max_vision_bytes:
            raise _Rejected("limit_exceeded", "worker_output_limit")
        routed.append(
            RoutedImage(
                item=entry["item"],
                mode=entry["mode"],
                kind=entry["kind"],
                index=entry["index"],
                after=entry["after"],
                data=path.read_bytes(),
                replaces=entry.get("replaces"),
                resolves=tuple(entry.get("warnings", ())),
                context=entry.get("context"),
            )
        )
    return routed


def _run_worker(data, format, limits, deadline, tesseract_path, vision_items=0):
    if os.name != "posix":
        raise _Rejected("blocked", "resource_isolation_unavailable")
    if tesseract_path is not None and (
        not Path(tesseract_path).is_absolute() or not os.access(tesseract_path, os.X_OK)
    ):
        raise _Rejected("blocked", "ocr_executable_unavailable")
    request_bytes = json.dumps(
        {
            "format": format,
            "limits": asdict(limits),
            "tesseract": tesseract_path,
            "vision_items": vision_items,
            "seconds": max(0.0, deadline - time.monotonic()),
        }
    ).encode()
    # Routed images live beside the result and share the same temporary budget.
    reserved = limits.max_vision_bytes if vision_items else 0
    if (
        len(data) + len(request_bytes) + limits.max_output_bytes + reserved
        > limits.max_temp_bytes
    ):
        raise _Rejected("limit_exceeded", "temp_byte_limit")
    with tempfile.TemporaryDirectory(prefix="lifecontext-extract-") as directory:
        root = Path(directory)
        (root / "original").write_bytes(data)
        (root / "request.json").write_bytes(request_bytes)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _Rejected("limit_exceeded", "time_limit")
        # A new session permits killing OCR grandchildren too on deadline.
        process = subprocess.Popen(
            [
                sys.executable,
                "-I",
                str(Path(__file__).resolve()),
                "--worker",
                directory,
            ],
            cwd=directory,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env={
                "PATH": "/usr/bin:/bin",
                "HOME": directory,
                "TMPDIR": directory,
                "OMP_THREAD_LIMIT": "1",
                "OPENBLAS_NUM_THREADS": "1",
            },
        )
        try:
            process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            import signal

            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            raise _Rejected("limit_exceeded", "time_limit") from None
        output = root / "result.json"
        if process.returncode != 0 or not output.exists():
            raise _Rejected(
                "limit_exceeded" if process.returncode < 0 else "failed",
                "worker_resource_or_parser_failure",
            )
        if output.stat().st_size > limits.max_output_bytes:
            raise _Rejected("limit_exceeded", "worker_output_limit")
        result = json.loads(output.read_bytes())
        text = "".join(s["text"] for s in result["segments"])
        if (
            len(text) > limits.max_text_characters
            or len(text.encode()) > limits.max_text_bytes
        ):
            raise _Rejected("limit_exceeded", "text_limit")
        result["routed"] = _read_routed(root, result, limits, vision_items)
        return result


class _BudgetWriter:
    """No fileno exposure: encoders must pass every growth through write/seek."""

    def __init__(self, stream, maximum):
        self.stream = stream
        self.maximum = maximum

    def write(self, data):
        if self.stream.tell() + len(data) > self.maximum:
            raise _Rejected("limit_exceeded", "temp_byte_limit")
        return self.stream.write(data)

    def seek(self, offset, whence=0):
        position = (
            offset
            if whence == 0
            else self.stream.tell() + offset
            if whence == 1
            else None
        )
        if position is None:
            current = self.stream.tell()
            end = self.stream.seek(0, 2)
            self.stream.seek(current)
            position = end + offset
        if not 0 <= position <= self.maximum:
            raise _Rejected("limit_exceeded", "temp_byte_limit")
        return self.stream.seek(position)

    def tell(self):
        return self.stream.tell()

    def flush(self):
        return self.stream.flush()


@lru_cache(maxsize=4)
def _ocr_languages(tesseract):
    """Use installed language data; English-only installations remain supported."""
    try:
        available = subprocess.check_output(
            [tesseract, "--list-langs"], stderr=subprocess.DEVNULL, timeout=5,
        ).decode("utf-8").splitlines()[1:]
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return "eng"
    selected = [language for language in ("eng", "rus", "ukr") if language in available]
    return "+".join(selected) or "eng"


def _ocr_image(image, root, limits, tesseract):
    if not tesseract:
        raise _Rejected("blocked", "ocr_backend_not_configured")
    image_path, output_base = root / "ocr.png", root / "ocr"
    try:
        import resource

        current_bytes = sum(p.stat().st_size for p in root.iterdir() if p.is_file())
        # Reserve both OCR output and the final result before the PNG can grow.
        png_budget = (
            limits.max_temp_bytes
            - current_bytes
            - limits.max_text_bytes
            - limits.max_output_bytes
        )
        if png_budget <= 0:
            raise _Rejected("limit_exceeded", "temp_byte_limit")
        with image_path.open("wb") as stream:
            image.save(_BudgetWriter(stream, png_budget), format="PNG")
        current_bytes = sum(p.stat().st_size for p in root.iterdir() if p.is_file())
        ocr_budget = min(
            limits.max_text_bytes,
            limits.max_temp_bytes - current_bytes - limits.max_output_bytes,
        )
        inherited_soft, inherited_hard = resource.getrlimit(resource.RLIMIT_FSIZE)
        if inherited_soft != resource.RLIM_INFINITY:
            ocr_budget = min(ocr_budget, inherited_soft)

        def cap_ocr_output():
            # Worker is single-threaded at launch; only async-safe resource setup.
            resource.setrlimit(resource.RLIMIT_FSIZE, (ocr_budget, inherited_hard))

        try:
            result = subprocess.run(
                [
                    tesseract,
                    str(image_path),
                    str(output_base),
                    "--dpi",
                    str(limits.ocr_dpi),
                    "-l",
                    _ocr_languages(tesseract),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=limits.max_ocr_seconds,
                preexec_fn=cap_ocr_output,
                cwd=root,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise _Rejected("limit_exceeded", "ocr_time_limit") from None
        if result.returncode:
            raise _Rejected("failed", "ocr_failed")
        output = output_base.with_suffix(".txt")
        if not output.exists():
            raise _Rejected("failed", "ocr_missing_output")
        if output.stat().st_size > limits.max_text_bytes:
            raise _Rejected("limit_exceeded", "ocr_output_limit")
        return output.read_text(encoding="utf-8")
    finally:
        image_path.unlink(missing_ok=True)
        output_base.with_suffix(".txt").unlink(missing_ok=True)


# Per-item reasons that leave one page or frame unresolved instead of failing the document.
_ITEM_FAILURES = frozenset(
    {
        "ocr_time_limit",
        "ocr_failed",
        "ocr_missing_output",
        "ocr_output_limit",
        "pixel_limit",
        "render_failed",
    }
)
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_VML = "{urn:schemas-microsoft-com:vml}"
_DGM = "{http://schemas.openxmlformats.org/drawingml/2006/diagram}"
_DIAGRAM_URI = "http://schemas.openxmlformats.org/drawingml/2006/diagram"
_PKG_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_LATIN = re.compile(r"[A-Za-z]")
_CYRILLIC = re.compile(r"[\u0400-\u04FF]")


def _flatten(image):
    """Composite transparency onto white so blank checks and JPEG export agree."""
    from PIL import Image

    if image.mode == "RGB":
        return image.copy()
    if image.mode in {"L", "1", "CMYK", "YCbCr"}:
        return image.convert("RGB")
    if image.mode.startswith("I") or image.mode == "F":
        # Pillow clips 16-bit and float samples at 255; scale the real range instead.
        wide = image.convert("F")
        try:
            low, high = wide.getextrema()
            if image.mode.startswith("I") and 0 <= low and high <= 65535:
                low, span = 0.0, 65535.0
            elif high > low:
                span = high - low
            else:
                # A uniform float image has no content, whatever its value.
                return Image.new("RGB", image.size, "white")
            scaled = wide.point(lambda v: (v - low) * 255.0 / span)
            try:
                gray = scaled.convert("L")
            finally:
                scaled.close()
        finally:
            wide.close()
        try:
            return gray.convert("RGB")
        finally:
            gray.close()
    rgba = image.convert("RGBA")
    try:
        background = Image.new("RGB", rgba.size, "white")
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background
    finally:
        rgba.close()


def _is_blank(image, limits):
    """No visible content: a handful of dark pixels at most, absolutely and relatively."""
    flat = _flatten(image)
    try:
        gray = flat.convert("L")
        try:
            histogram = gray.histogram()
        finally:
            gray.close()
    finally:
        flat.close()
    dark, total = sum(histogram[:200]), max(sum(histogram), 1)
    return (
        dark <= limits.blank_max_ink_pixels
        and dark * 10000 <= total * limits.blank_max_ink_bp
    )


@dataclass(frozen=True)
class _PageStats:
    images: tuple[tuple[tuple[int, int, int, int], int], ...]  # (rounded box, basis points)
    paths: int
    invisible_text: bool  # every text object uses render mode 3, as OCR layers do


def _page_stats(native_page):
    """Image boxes, vector path count and text visibility from object metadata only.

    Text pages are never rendered for this.
    """
    import pypdfium2.raw as pdfium_c

    left, bottom, right, top = native_page.get_bbox()
    area = max((right - left) * (top - bottom), 1.0)
    images, paths, texts, invisible = [], 0, 0, 0
    for item in native_page.get_objects(
        filter=[pdfium_c.FPDF_PAGEOBJ_IMAGE, pdfium_c.FPDF_PAGEOBJ_PATH, pdfium_c.FPDF_PAGEOBJ_TEXT],
        max_depth=4,
    ):
        if item.type == pdfium_c.FPDF_PAGEOBJ_PATH:
            paths += 1
            continue
        if item.type == pdfium_c.FPDF_PAGEOBJ_TEXT:
            texts += 1
            invisible += pdfium_c.FPDFTextObj_GetTextRenderMode(item.raw) == 3
            continue
        a, b, c, d = item.get_bounds()
        width, height = min(c, right) - max(a, left), min(d, top) - max(b, bottom)
        if width > 0 and height > 0:
            box = tuple(round(v / 2) for v in (a, b, c, d))
            images.append((box, min(10000, int(width * height * 10000 / area))))
    return _PageStats(tuple(images), paths, bool(texts) and invisible == texts)


def _figure_area(stats, furniture, limits):
    """Image coverage that may hold content: no page furniture, and on a searchable
    scan not the scanned page image itself."""
    images = [bp for box, bp in stats.images if (box, bp) not in furniture]
    largest = max(images, default=0)
    scanned = stats.invisible_text and largest >= limits.scan_min_area_bp
    total = sum(images) - (largest if scanned else 0)
    return min(10000, total)


def _suspect_text(text, limits):
    """Replacement, private-use and C1 characters, mixed-script words, cp1251 mojibake."""
    letters = sum(1 for c in text if c.isalpha())
    if letters < 20:
        return False
    bad = sum(
        1
        for c in text
        if c == "\ufffd" or 0xE000 <= ord(c) <= 0xF8FF or 0x80 <= ord(c) <= 0x9F
    )
    for word in re.findall(r"\w+", text):
        mixed = bool(_LATIN.search(word) and _CYRILLIC.search(word))
        mojibake = len(word) >= 3 and 2 * sum(0xC0 <= ord(c) <= 0xFF for c in word) >= len(word)
        if mixed or mojibake:
            bad += len(word)
    return bad * 10000 >= limits.suspect_text_bp * letters


@contextmanager
def _rendered(native_page, limits, scale):
    width, height = native_page.get_size()
    if math.ceil(width * scale) * math.ceil(height * scale) > limits.max_pixels:
        raise _Rejected("limit_exceeded", "pixel_limit")
    bitmap = native_page.render(scale=scale, may_draw_forms=False)
    image = None
    try:
        image = bitmap.to_pil()
        yield image
    finally:
        if image is not None:
            image.close()
        bitmap.close()


def _open_embedded(blob, limits):
    """Decode an embedded raster picture, or None when it cannot be read safely."""
    import warnings as warning_module

    from PIL import Image

    try:
        with warning_module.catch_warnings():
            warning_module.simplefilter("error", Image.DecompressionBombWarning)
            image = Image.open(BytesIO(blob))
            if (
                image.format not in {"PNG", "JPEG", "GIF", "BMP", "TIFF", "WEBP"}
                or image.width * image.height > limits.max_pixels
            ):
                image.close()
                return None
            image.load()
            return image
    except Exception:  # noqa: BLE001 -- untrusted embedded media stays recorded as not read
        return None


class _VisionRouter:
    """Write routed items as budgeted JPEGs beside result.json for the parent to read."""

    def __init__(self, root, limits, max_items):
        self.root, self.limits, self.max_items = root, limits, max_items
        self.entries, self.bytes = [], 0

    def has_room(self, warnings, item):
        """False when vision is off, or (with a ledger entry) when the budget is spent."""
        if not self.max_items:
            return False
        if len(self.entries) >= self.max_items:
            warnings.append(f"vision_skipped:{item}; vision page budget reached")
            return False
        return True

    def route(self, image, warnings, *, item, mode, kind, index, after,
              replaces=None, resolves=(), context=None):
        if not self.has_room(warnings, item):
            return
        from PIL import Image

        flat = _flatten(image)
        try:
            scale = self.limits.vision_max_edge / max(flat.width, flat.height, 1)
            if scale < 1:
                resized = flat.resize(
                    (max(1, round(flat.width * scale)), max(1, round(flat.height * scale))),
                    Image.Resampling.LANCZOS,
                )
                flat.close()
                flat = resized
            buffer = BytesIO()
            flat.save(buffer, format="JPEG", quality=85)
        finally:
            flat.close()
        payload = buffer.getvalue()
        used = sum(p.stat().st_size for p in self.root.iterdir() if p.is_file())
        if (
            self.bytes + len(payload) > self.limits.max_vision_bytes
            or used + len(payload) + self.limits.max_text_bytes + self.limits.max_output_bytes
            > self.limits.max_temp_bytes
        ):
            warnings.append(f"vision_skipped:{item}; vision image budget reached")
            return
        name = f"vision-{len(self.entries) + 1:04d}.jpg"
        with (self.root / name).open("wb") as stream:
            _BudgetWriter(stream, len(payload)).write(payload)
        self.bytes += len(payload)
        self.entries.append(
            {
                "item": item,
                "file": name,
                "mode": mode,
                "kind": kind,
                "index": index,
                "after": after,
                "replaces": replaces,
                "warnings": list(resolves),
                "context": context,
            }
        )


def _xml(raw):
    from defusedxml.ElementTree import fromstring

    return fromstring(raw, forbid_dtd=True, forbid_entities=True, forbid_external=True)


def _rels(archive, names, part):
    """Relationship targets of one package part, resolved to archive names."""
    folder, _, base = part.rpartition("/")
    rels = f"{folder}/_rels/{base}.rels"
    if rels not in names:
        return {}
    targets = {}
    for rel in _xml(archive.read(rels)).iter(_PKG_REL + "Relationship"):
        if rel.get("TargetMode") == "External":
            continue
        target = rel.get("Target", "")
        resolved = PurePosixPath(target[1:] if target.startswith("/") else f"{folder}/{target}")
        parts = []
        for piece in resolved.parts:
            if piece == "..":
                if parts:
                    parts.pop()
            elif piece != ".":
                parts.append(piece)
        targets[rel.get("Id")] = ("/".join(parts), rel.get("Type", "").rsplit("/", 1)[-1])
    return targets


def _cell(value):
    return str(value).replace("|", "\\|").replace("\n", " ").strip()


def _chart_markdown(chart):
    """Render chart series as a Markdown table from the chart XML only."""
    lines = []
    if chart.has_title and chart.chart_title.has_text_frame:
        title = chart.chart_title.text_frame.text.strip()
        if title:
            lines.append(f"Chart: {title}")
    for plot in chart.plots:
        series = list(plot.series)
        if not series:
            continue
        try:
            categories = [_cell(c) for c in plot.categories]
        except Exception:  # noqa: BLE001 -- XY and bubble plots have no category axis
            categories = []
        values = [list(s.values) for s in series]
        rows = max([len(categories)] + [len(v) for v in values])
        lines.append("| Category | " + " | ".join(_cell(s.name or f"Series {n}") for n, s in enumerate(series, 1)) + " |")
        lines.append("| --- |" + " --- |" * len(series))
        for row in range(rows):
            label = categories[row] if row < len(categories) else str(row + 1)
            cells = ["" if row >= len(v) or v[row] is None else _cell(v[row]) for v in values]
            lines.append(f"| {label} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


_MC = "{http://schemas.openxmlformats.org/markup-compatibility/2006}"


def _iter_current(element, tag):
    """Like ``element.iter(tag)``, reading each ``mc:AlternateContent`` once.

    Word stores the same text box or picture in ``mc:Choice`` and again in
    ``mc:Fallback``. The first Choice holding the element wins; otherwise the
    Fallback is the only copy (for example a picture standing in for a chart).
    """
    for child in element:
        if child.tag == _MC + "AlternateContent":
            choices = [c for c in child if c.tag == _MC + "Choice" and any(True for _ in c.iter(tag))]
            branches = choices[:1] or [c for c in child if c.tag == _MC + "Fallback"][:1]
            for branch in branches:
                yield from _iter_current(branch, tag)
            continue
        if child.tag == tag:
            yield child
        yield from _iter_current(child, tag)


def _binary_extract(data, format, limits, root, tesseract, vision_items=0, deadline=None):
    segments, warnings = [], []
    characters = encoded = cells = 0
    router = _VisionRouter(root, limits, vision_items)
    unavailable = False
    pictures = 0

    def add(text, kind, index, container=None):
        nonlocal characters, encoded
        if not text or not text.strip():
            return
        if len(segments) >= limits.max_segments:
            raise _Rejected("limit_exceeded", "segment_limit")
        text = text + "\n"
        encoded += len(text.encode("utf-8"))
        if (
            characters + len(text) > limits.max_text_characters
            or encoded > limits.max_text_bytes
        ):
            raise _Rejected("limit_exceeded", "text_limit")
        segments.append(
            TextSegment(
                text,
                characters,
                characters + len(text),
                Locator(kind, index, index, container),
            )
        )
        characters += len(text)

    def count_cell():
        nonlocal cells
        cells += 1
        if cells > limits.max_cells:
            raise _Rejected("limit_exceeded", "cell_limit")

    def picture(blob, where, kind="image", index=None):
        """Record one embedded picture in the ledger and route it when vision is on."""
        nonlocal pictures
        pictures += 1
        item = f"picture:{pictures}"
        warning = f"embedded_image_not_read:{item}; {where}"
        warnings.append(warning)
        if blob is None or not router.has_room(warnings, item):
            return
        image = _open_embedded(blob, limits)
        if image is None:
            return
        try:
            router.route(
                image,
                warnings,
                item=item,
                mode="picture",
                kind=kind,
                index=pictures if index is None else index,
                after=len(segments),
                resolves=(warning,),
                context=where,
            )
        finally:
            image.close()

    def frame_or_page(image, unit, index, kind, mode):
        """Blank check, OCR and routing shared by rendered PDF pages and image frames."""
        nonlocal unavailable
        if _is_blank(image, limits):
            warnings.append(f"blank_{unit}:{index}; no visible content")
            return
        after = len(segments)
        options = {"item": f"{unit}:{index}", "mode": mode, "kind": kind, "index": index, "after": after}
        if not tesseract:
            unavailable = True
            warning = f"ocr_unavailable_{unit}:{index}; {unit} content unresolved"
            warnings.append(warning)
            router.route(image, warnings, resolves=(warning,), **options)
            return
        if deadline is not None and time.monotonic() + limits.max_ocr_seconds > deadline:
            # Keep what was read rather than letting the parent kill the whole worker.
            warning = f"ocr_failed_{unit}:{index}; time_limit; {unit} content unresolved"
            warnings.append(warning)
            router.route(image, warnings, resolves=(warning,), **options)
            return
        try:
            text = _ocr_image(image, root, limits, tesseract)
        except _Rejected as error:
            if error.reason not in _ITEM_FAILURES:
                raise
            warning = f"ocr_failed_{unit}:{index}; {error.reason}; {unit} content unresolved"
            warnings.append(warning)
            router.route(image, warnings, resolves=(warning,), **options)
            return
        if text.strip():
            add(text, kind, index)
            warning = f"ocr_{unit}:{index}; OCR may contain errors"
            warnings.append(warning)
            router.route(image, warnings, replaces=after, resolves=(warning,), **options)
        else:
            warning = f"ocr_no_text_{unit}:{index}; {unit} content unresolved"
            warnings.append(warning)
            router.route(image, warnings, resolves=(warning,), **options)

    if format in {"docx", "xlsx", "pptx"}:
        # Reject DTD/entity payloads in every XML part before third-party XML parsers.
        with ZipFile(BytesIO(data)) as archive:
            for entry in archive.infolist():
                if entry.filename.endswith((".xml", ".rels")):
                    _xml(archive.read(entry))
    if format == "docx":
        from docx import Document
        from docx.table import Table

        document = Document(BytesIO(data))

        def pictures_in(element, part, where):
            for blip in _iter_current(element, _A + "blip"):
                rid = blip.get(_R + "embed")
                target = part.related_parts.get(rid) if rid else None
                picture(getattr(target, "blob", None), where)
            for shape in _iter_current(element, _VML + "imagedata"):
                rid = shape.get(_R + "id")
                target = part.related_parts.get(rid) if rid else None
                picture(getattr(target, "blob", None), where)

        def blocks(parent, container, kind, part):
            for index, block in enumerate(parent.iter_inner_content(), 1):
                if isinstance(block, Table):
                    seen = set()
                    for row_index, row in enumerate(block.rows, 1):
                        for column_index, cell in enumerate(row.cells, 1):
                            if cell._tc in seen:
                                continue
                            seen.add(cell._tc)
                            count_cell()
                            blocks(
                                cell,
                                f"{container}/table:{index}/row:{row_index}/col:{column_index}",
                                "table_cell",
                                part,
                            )
                else:
                    add(block.text, kind, index, container)
                    # Text boxes sit inside drawings, outside the paragraph's own runs.
                    for box, content in enumerate(_iter_current(block._p, _W + "txbxContent"), 1):
                        for line, paragraph in enumerate(content.iter(_W + "p"), 1):
                            add(
                                "".join(t.text or "" for t in paragraph.iter(_W + "t")),
                                kind,
                                index,
                                f"{container}/paragraph:{index}/textbox:{box}/line:{line}",
                            )
                    pictures_in(block._p, part, f"{container} paragraph {index}")

        blocks(document, "document", "paragraph", document.part)
        odd_even = document.settings.odd_and_even_pages_header_footer
        for number, section in enumerate(document.sections, 1):
            names = ["header", "footer"]
            if section.different_first_page_header_footer:
                names += ["first_page_header", "first_page_footer"]
            if odd_even:
                names += ["even_page_header", "even_page_footer"]
            for name in names:
                story = getattr(section, name)
                if story.is_linked_to_previous:
                    continue
                blocks(story, f"section:{number}/{name}", "paragraph", story.part)
        with ZipFile(BytesIO(data)) as archive:
            names = set(archive.namelist())
            for part_name, label in (
                ("word/footnotes.xml", "footnote"),
                ("word/endnotes.xml", "endnote"),
            ):
                if part_name not in names:
                    continue
                for note in _xml(archive.read(part_name)).iter(_W + label):
                    if note.get(_W + "type") in {
                        "separator",
                        "continuationSeparator",
                        "continuationNotice",
                    }:
                        continue
                    for line, paragraph in enumerate(note.iter(_W + "p"), 1):
                        add(
                            "".join(t.text or "" for t in paragraph.iter(_W + "t")),
                            "paragraph",
                            line,
                            f"{label}:{note.get(_W + 'id')}",
                        )
    elif format == "xlsx":
        from openpyxl import load_workbook

        workbook = load_workbook(
            BytesIO(data),
            read_only=True,
            data_only=True,
            keep_links=False,
            keep_vba=False,
        )
        warnings.append(
            "formula_cached_values_only; missing or stale caches are not recalculated"
        )
        try:
            if len(workbook.sheetnames) > limits.max_sheets:
                raise _Rejected("limit_exceeded", "sheet_limit")
            for sheet in workbook:
                # Reset untrusted dimensions; streaming counts actual emitted cells.
                sheet.reset_dimensions()
                for row in sheet.iter_rows():
                    for cell in row:
                        count_cell()
                        if cell.value is not None:
                            add(
                                str(cell.value),
                                "sheet_cell",
                                cell.row,
                                f"{sheet.title}!{cell.coordinate}",
                            )
        finally:
            workbook.close()
        # Read-only workbooks skip drawings; map charts and pictures through the package.
        with ZipFile(BytesIO(data)) as archive:
            names = set(archive.namelist())
            book = _xml(archive.read("xl/workbook.xml"))
            book_rels = _rels(archive, names, "xl/workbook.xml")
            charts = 0
            for sheet in book.iter(
                "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}sheet"
            ):
                title = sheet.get("name", "sheet")
                target = book_rels.get(sheet.get(_R + "id"), (None, None))[0]
                if target not in names:
                    continue
                for drawing, kind in _rels(archive, names, target).values():
                    if kind != "drawing" or drawing not in names:
                        continue
                    for media, media_kind in _rels(archive, names, drawing).values():
                        if media_kind == "chart":
                            charts += 1
                            # Items never carry free text: sheet titles may hold "," or ";".
                            warnings.append(
                                f"chart_not_read:chart:{charts}; sheet {title}; chart data is in the sheet cells"
                            )
                        elif media_kind == "image" and media in names:
                            picture(archive.read(media), f"sheet {title}")
    elif format == "pptx":
        from pptx import Presentation
        from pptx.enum.shapes import MSO_SHAPE_TYPE
        from pptx.shapes.picture import Picture

        presentation = Presentation(BytesIO(data))
        if len(presentation.slides) > limits.max_slides:
            raise _Rejected("limit_exceeded", "slide_limit")

        def smartart(shape, slide_part):
            data_node = shape._element.find(".//" + _A + "graphicData")
            if data_node is None or data_node.get("uri") != _DIAGRAM_URI:
                return None
            relation = data_node.find(_DGM + "relIds")
            rid = relation.get(_R + "dm") if relation is not None else None
            try:
                tree = _xml(slide_part.related_part(rid).blob)
            except (KeyError, AttributeError, TypeError):
                return None
            return [
                "".join(t.text or "" for t in point.iter(_A + "t"))
                for point in tree.iter(_DGM + "pt")
                if point.get("type") not in {"parTrans", "sibTrans", "pres"}
            ]

        def shapes(items, slide, slide_part, parent=""):
            for shape in items:
                location = f"{parent}/shape:{shape.shape_id}"
                if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
                    shapes(shape.shapes, slide, slide_part, location)
                elif shape.has_table:
                    for row_index, row in enumerate(shape.table.rows, 1):
                        for column_index, cell in enumerate(row.cells, 1):
                            count_cell()
                            add(
                                cell.text,
                                "table_cell",
                                slide,
                                f"slide:{slide}{location}/row:{row_index}/col:{column_index}",
                            )
                elif getattr(shape, "has_chart", False) and shape.has_chart:
                    try:
                        text = _chart_markdown(shape.chart)
                    except Exception:  # noqa: BLE001 -- malformed chart parts stay in the ledger
                        text = ""
                    if text:
                        add(text, "slide", slide, f"slide:{slide}{location}/chart")
                    else:
                        warnings.append(f"chart_not_read:slide:{slide}{location}")
                elif isinstance(shape, Picture):
                    try:
                        blob = shape.image.blob
                    except Exception:  # noqa: BLE001 -- linked or missing image parts
                        blob = None
                    picture(blob, f"slide {slide}", kind="slide", index=slide)
                elif (points := smartart(shape, slide_part)) is not None:
                    for point in points:
                        add(point, "slide", slide, f"slide:{slide}{location}/smartart")
                elif shape.has_text_frame:
                    for paragraph in shape.text_frame.paragraphs:
                        add(paragraph.text, "slide", slide, location)

        for index, slide in enumerate(presentation.slides, 1):
            shapes(slide.shapes, index, slide.part)
            if slide.has_notes_slide:
                frame = slide.notes_slide.notes_text_frame
                for paragraph in frame.paragraphs if frame is not None else ():
                    add(paragraph.text, "slide", index, f"slide:{index}/notes")
    elif format == "pdf":
        from pypdf import PdfReader

        reader = PdfReader(BytesIO(data))
        if reader.is_encrypted:
            raise _Rejected("locked", "encrypted_pdf")
        if len(reader.pages) > limits.max_pages:
            raise _Rejected("limit_exceeded", "page_limit")
        import pypdfium2

        pdfium = None
        try:
            try:
                pdfium = pypdfium2.PdfDocument(data)
            except pypdfium2.PdfiumError:
                warnings.append(
                    "figure_check_unavailable; pages could not be rendered or checked for images"
                )
            # Metadata pre-pass: images placed identically on several pages are furniture.
            stats, counts = {}, {}
            for index in range(1, min(len(pdfium), len(reader.pages)) + 1 if pdfium is not None else 1):
                try:
                    native_page = pdfium[index - 1]
                except pypdfium2.PdfiumError:
                    continue
                try:
                    stats[index] = _page_stats(native_page)
                except pypdfium2.PdfiumError:
                    pass
                finally:
                    native_page.close()
                for image in set(stats.get(index, _PageStats((), 0, False)).images):
                    if image[1] <= limits.furniture_max_area_bp:
                        counts[image] = counts.get(image, 0) + 1
            furniture = {image for image, pages in counts.items() if pages >= limits.furniture_min_pages}
            for index, page in enumerate(reader.pages, 1):
                try:
                    text = page.extract_text() or ""
                except MemoryError:
                    raise
                except Exception:  # noqa: BLE001 -- one malformed content stream; render the page instead
                    text = ""
                try:
                    native_page = (
                        pdfium[index - 1] if pdfium is not None and index <= len(pdfium) else None
                    )
                except pypdfium2.PdfiumError:
                    native_page = None
                if native_page is None:
                    if text.strip():
                        add(text, "page", index)
                    else:
                        warnings.append(
                            f"ocr_failed_page:{index}; render_failed; page content unresolved"
                        )
                    continue
                try:
                    if text.strip():
                        add(text, "page", index)
                        page_stats = stats.get(index)
                        resolves = []
                        if page_stats is not None and (
                            _figure_area(page_stats, furniture, limits) >= limits.figure_min_area_bp
                            or page_stats.paths >= limits.figure_min_paths
                        ):
                            resolves.append(
                                f"figure_not_read:{index}; images or drawings on this page were not read"
                            )
                        warnings.extend(resolves)
                        suspect = _suspect_text(text, limits)
                        if suspect:
                            # Stays partial even after a model reading: the garbled
                            # layer is still indexed beside it.
                            warnings.append(
                                f"text_layer_suspect:{index}; the text layer may be garbled"
                            )
                        late = deadline is not None and time.monotonic() + 15 > deadline
                        if (resolves or suspect) and late and router.max_items:
                            warnings.append(f"vision_skipped:page:{index}; time limit reached")
                        elif (resolves or suspect) and router.has_room(warnings, f"page:{index}"):
                            width, height = native_page.get_size()
                            scale = min(
                                300 / 72, limits.vision_max_edge / max(width, height, 1)
                            )
                            with _rendered(native_page, limits, scale) as image:
                                router.route(
                                    image,
                                    warnings,
                                    item=f"page:{index}",
                                    mode="page" if suspect else "figure",
                                    kind="page",
                                    index=index,
                                    after=len(segments),
                                    resolves=resolves,
                                )
                        continue
                    with _rendered(native_page, limits, limits.ocr_dpi / 72) as image:
                        frame_or_page(image, "page", index, "page", "page")
                except _Rejected as error:
                    if error.reason not in _ITEM_FAILURES:
                        raise
                    if not text.strip():
                        warnings.append(
                            f"ocr_failed_page:{index}; {error.reason}; page content unresolved"
                        )
                except pypdfium2.PdfiumError:
                    if not text.strip():
                        warnings.append(
                            f"ocr_failed_page:{index}; render_failed; page content unresolved"
                        )
                finally:
                    native_page.close()
        finally:
            if pdfium is not None:
                pdfium.close()
    else:
        import warnings as warning_module

        from PIL import Image, ImageOps

        with warning_module.catch_warnings():
            warning_module.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as image:
                if (
                    image.format
                    != {"png": "PNG", "jpeg": "JPEG", "tiff": "TIFF"}[format]
                ):
                    raise _Rejected("failed", "image_format_mismatch")
                if image.width * image.height > limits.max_pixels:
                    raise _Rejected("limit_exceeded", "pixel_limit")
                image.verify()
            with Image.open(BytesIO(data)) as image:
                frames = getattr(image, "n_frames", 1)
                if frames > limits.max_frames:
                    raise _Rejected("limit_exceeded", "frame_limit")
                for index in range(frames):
                    try:
                        image.seek(index)
                        if image.width * image.height > limits.max_pixels:
                            warnings.append(
                                f"ocr_failed_frame:{index + 1}; pixel_limit; frame content unresolved"
                            )
                            continue
                        image.load()
                        oriented = ImageOps.exif_transpose(image)
                    except (OSError, ValueError, SyntaxError, EOFError):
                        warnings.append(
                            f"ocr_failed_frame:{index + 1}; decode_failed; frame content unresolved"
                        )
                        continue
                    try:
                        frame_or_page(oriented, "frame", index + 1, "image", "frame")
                    finally:
                        oriented.close()
    if not segments:
        status, reason = (
            ("blocked", "ocr_backend_not_configured")
            if unavailable
            else ("failed", "no_extractable_text")
        )
        # The ledger says which pages failed and why; a vision reader may still
        # resolve routed items, and the parent decides.
        return {
            "status": status,
            "reason": reason,
            "segments": [],
            "warnings": list(compact_ledger(warnings)),
            "images": router.entries,
        }
    return {
        "status": "completed",
        "reason": None,
        "segments": [asdict(s) for s in segments],
        "warnings": list(compact_ledger(warnings)),
        "images": router.entries,
    }


def _worker_main(directory):
    import resource

    root = Path(directory)
    request = json.loads((root / "request.json").read_bytes())
    limits = ExtractionLimits(**request["limits"])
    resource.setrlimit(
        resource.RLIMIT_AS, (limits.max_worker_bytes, limits.max_worker_bytes)
    )
    resource.setrlimit(
        resource.RLIMIT_CPU,
        (math.ceil(limits.max_seconds), math.ceil(limits.max_seconds)),
    )
    file_limit = min(limits.max_output_bytes, limits.max_temp_bytes)
    resource.setrlimit(resource.RLIMIT_FSIZE, (file_limit, file_limit))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    try:
        result = _binary_extract(
            (root / "original").read_bytes(),
            request["format"],
            limits,
            root,
            request["tesseract"],
            request.get("vision_items", 0),
            # Stop starting OCR shortly before the parent's wall-clock deadline.
            time.monotonic() + request["seconds"] - 15 if request.get("seconds") else None,
        )
    except _Rejected as error:
        result = {
            "status": error.status,
            "reason": error.reason,
            "segments": [],
            "warnings": [],
        }
    except ImportError:
        result = {
            "status": "blocked",
            "reason": "extraction_dependency_unavailable",
            "segments": [],
            "warnings": [],
        }
    except MemoryError:
        result = {
            "status": "limit_exceeded",
            "reason": "worker_memory_limit",
            "segments": [],
            "warnings": [],
        }
    except Exception:  # noqa: BLE001 -- untrusted parser boundary; redact dependency-specific failures.
        result = {
            "status": "failed",
            "reason": "malformed_document_or_parser_failure",
            "segments": [],
            "warnings": [],
        }
    payload = json.dumps(result, ensure_ascii=False).encode()
    if len(payload) > limits.max_output_bytes:
        payload = json.dumps(
            {
                "status": "limit_exceeded",
                "reason": "worker_output_limit",
                "segments": [],
                "warnings": [],
            }
        ).encode()
    used = sum(p.stat().st_size for p in root.iterdir() if p.is_file())
    remaining = min(limits.max_output_bytes, limits.max_temp_bytes - used)
    with (root / "result.json").open("wb") as stream:
        _BudgetWriter(stream, remaining).write(payload)


if __name__ == "__main__" and len(sys.argv) == 3 and sys.argv[1] == "--worker":
    _worker_main(sys.argv[2])
