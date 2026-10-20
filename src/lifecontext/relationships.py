"""Bounded, same-vault relationship discovery and ledger decisions."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import PurePosixPath
from typing import Any

import yaml

from lifecontext.config import JournalLimits
from lifecontext.vault import ManagedVault, VaultError


EDGE_TYPES = frozenset(
    {"references", "about", "same_topic", "follows", "supports", "contradicts", "supersedes"}
)
_SOURCE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_EDGE_ID = re.compile(r"edge_[0-9a-f]{64}\Z")
_NOTE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_WIKILINK = re.compile(r"(?<!!)\[\[([^\[\]\r\n]+)\]\]")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")
_MAX_FRONTMATTER_BYTES = 8_192
_LEDGER_MAX_BYTES = 64 * 1024
_MAX_SCAN_DOCUMENTS = 2_000
_MAX_LINKS_PER_FILE = 128
_MAX_HEADINGS_PER_FILE = 64
_MAX_ALIASES_PER_FILE = 32
_MAX_HINT_BYTES = 512
_MAX_RELATIONSHIP_ROOTS = 128
_EXCLUDED_NAMES = frozenset(
    {
        ".obsidian",
        ".git",
        ".trash",
        ".stversions",
        "attachments",
        "assets",
        "node_modules",
        "_archive",
        "_attachments",
    }
)
_EXCLUDED_JOURNAL_DIRS = frozenset({"entries", "relationships", "indices", "reports", "expectations"})
_TEMP_SUFFIXES = (".tmp", ".swp", ".bak", "~")


class RelationshipError(ValueError):
    """Stable relationship failure without note or ledger content."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class _Document:
    identity: str
    path: str
    is_journal: bool
    revision: str
    digest: str
    title: str
    aliases: tuple[str, ...]
    headings: tuple[str, ...]
    links: tuple[tuple[str, str | None], ...]
    links_truncated: bool = False
    source_id: str | None = None
    source_version: int | None = None
    note_id: str | None = None


def _safe_relative_root(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value.startswith("/")
        or "\\" in value
        or "\x00" in value
        or any(
            ord(char) < 32
            or 0x7F <= ord(char) <= 0x9F
            or 0xD800 <= ord(char) <= 0xDFFF
            for char in value
        )
    ):
        raise RelationshipError("invalid_relationship_root")
    parts = value.split("/")
    if (
        any(part in {"", ".", ".."} for part in parts)
        or any(part.startswith(".") for part in parts)
        or parts[0].casefold() == "journal"
    ):
        raise RelationshipError("invalid_relationship_root")
    return "/".join(parts)


def _frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---", 4)
    if end < 0 or end > _MAX_FRONTMATTER_BYTES:
        raise RelationshipError("invalid_frontmatter")
    raw = text[4:end]
    try:
        loaded = yaml.safe_load(raw)
    except Exception:
        raise RelationshipError("invalid_frontmatter") from None
    if loaded is None:
        loaded = {}
    if not isinstance(loaded, Mapping) or any(not isinstance(key, str) for key in loaded):
        raise RelationshipError("invalid_frontmatter")
    return dict(loaded), text[end + 4 :].lstrip("\r\n")


def _plain_markdown(body: str) -> str:
    """Drop fenced code before interpreting headings and wikilinks."""
    visible: list[str] = []
    fence: str | None = None
    for line in body.splitlines():
        stripped = line.lstrip()
        marker = stripped[:3]
        if marker in {"```", "~~~"}:
            if fence is None:
                fence = marker
            elif marker == fence:
                fence = None
            continue
        if fence is None:
            visible.append(line)
    return "\n".join(visible)


def _normalize_hint(value: str) -> str:
    value = value.strip().replace("\\", "/")
    if value.casefold().endswith(".md"):
        value = value[:-3]
    return " ".join(value.casefold().split())


def _utf8_within(value: str, limit: int) -> bool:
    try:
        return len(value.encode("utf-8")) <= limit
    except UnicodeError:
        return False


def _excluded_file(name: str) -> bool:
    folded = name.casefold()
    return (
        name.startswith(".")
        or folded.startswith("~$")
        or ".sync-conflict-" in folded
        or "conflicted copy" in folded
        or folded.endswith(_TEMP_SUFFIXES)
    )


class RelationshipStore:
    """Use one pinned vault, a fixed allowlist, and Journal-only writes."""

    def __init__(
        self,
        vault: ManagedVault,
        *,
        allowlist_roots: Sequence[str] = (),
        limits: JournalLimits | None = None,
    ) -> None:
        if len(allowlist_roots) > _MAX_RELATIONSHIP_ROOTS:
            raise RelationshipError("invalid_relationship_root")
        roots = tuple(_safe_relative_root(root) for root in allowlist_roots)
        if len(set(roots)) != len(roots) or any(
            left != right and right.startswith(left + "/")
            for left in roots
            for right in roots
        ):
            raise RelationshipError("invalid_relationship_root")
        self.vault = vault
        self.allowlist_roots = tuple(sorted(roots))
        self.limits = limits or JournalLimits()
        try:
            self.journal_relative = vault.journal_path.relative_to(vault.vault_root).as_posix()
        except ValueError:
            raise RelationshipError("unsafe_vault") from None

    def count_legacy_path_references(self) -> int:
        """Count files with possible missing literal path references needing review.

        Return -1 when the read-only, allowlisted audit cannot be completed.
        """
        deadline = time.monotonic() + self.limits.max_relationship_scan_ms / 1000
        target_pattern = re.compile(
            r"/?" + re.escape(self.journal_relative)
            + r"/entries/[A-Za-z0-9][A-Za-z0-9_-]{0,127}-v[1-9][0-9]*\.md\Z"
        )
        markdown_links = re.compile(
            r"(?<!!)\[[^\]\r\n]*\]\(\s*(<[^>\r\n]+>|[^\s)]+)(?:\s+[^)\r\n]*)?\)"
        )
        stack = list(self.allowlist_roots)
        visited_directories = visited_files = total_bytes = count = 0
        missing_targets: dict[str, bool] = {}
        while stack:
            if time.monotonic() >= deadline:
                return -1
            directory = stack.pop()
            visited_directories += 1
            if visited_directories > self.limits.max_relationship_files:
                return -1
            try:
                entries, truncated = self.vault.list_vault_directory(
                    directory, limit=self.limits.max_relationship_files + 1
                )
            except VaultError as exc:
                if exc.code == "not_found":
                    continue
                return -1
            if truncated:
                return -1
            for name, kind in entries:
                if time.monotonic() >= deadline:
                    return -1
                if _excluded_file(name) or any(
                    ord(char) < 32 or 0x7F <= ord(char) <= 0x9F
                    or 0xD800 <= ord(char) <= 0xDFFF for char in name
                ):
                    continue
                relative = directory + "/" + name
                if kind == "directory":
                    if name.casefold() not in _EXCLUDED_NAMES:
                        stack.append(relative)
                    continue
                if kind != "file" or not name.casefold().endswith(".md"):
                    continue
                visited_files += 1
                if visited_files > self.limits.max_relationship_files:
                    return -1
                try:
                    raw = self.vault.read_vault_file(
                        relative, max_bytes=self.limits.max_relationship_file_bytes
                    )
                    total_bytes += len(raw)
                    if total_bytes > self.limits.max_relationship_bytes:
                        return -1
                    _, body = _frontmatter(raw.decode("utf-8"))
                except (VaultError, RelationshipError, UnicodeError):
                    return -1
                visible = _plain_markdown(body)
                targets = [(match.group(1).strip(), True) for match in _WIKILINK.finditer(visible)]
                targets.extend((match.group(1).strip("<>"), False) for match in markdown_links.finditer(visible))
                definitions = dict(re.findall(
                    r"^ {0,3}\[([^\]\r\n]+)\]:\s*(<[^>\r\n]+>|[^\s]+)",
                    visible, re.MULTILINE,
                ))
                definitions = {" ".join(label.casefold().split()): target.strip("<>") for label, target in definitions.items()}
                for match in re.finditer(
                    r"(?<!!)(?<!\[)\[([^\[\]\r\n]+)\](?:\[([^\]\r\n]*)\])?(?![(:\[])", visible
                ):
                    label = " ".join((match.group(2) or match.group(1)).casefold().split())
                    if label in definitions:
                        targets.append((definitions[label], False))
                found = False
                for target, wikilink in targets:
                    if time.monotonic() >= deadline:
                        return -1
                    target = target.partition("|")[0].partition("#")[0].strip()
                    wiki_target = target + ".md" if wikilink and not target.endswith(".md") else target
                    if not target_pattern.fullmatch(wiki_target):
                        continue
                    target = wiki_target.lstrip("/")
                    if target not in missing_targets:
                        try:
                            target_raw = self.vault.read_vault_file(
                                target, max_bytes=self.limits.max_relationship_file_bytes
                            )
                        except VaultError as exc:
                            if exc.code == "not_found":
                                missing_targets[target] = True
                            elif exc.code == "file_too_large":
                                missing_targets[target] = False
                            else:
                                return -1
                        else:
                            total_bytes += len(target_raw)
                            if total_bytes > self.limits.max_relationship_bytes:
                                return -1
                            missing_targets[target] = False
                    found = found or missing_targets[target]
                count += int(found)
        return -1 if time.monotonic() >= deadline else count

    def list_relationships(
        self,
        *,
        edge_id: str | None = None,
        source_id: str | None = None,
        status: str | None = None,
        max_results: int | None = None,
    ) -> list[dict[str, Any]]:
        limit = self._validate_result_options(edge_id, source_id, status, max_results)
        directory = self.journal_relative + "/relationships"
        try:
            entries, truncated = self.vault.list_vault_directory(directory, limit=limit + 1)
        except VaultError as exc:
            if exc.code == "not_found":
                return []
            raise RelationshipError(exc.code) from None
        if truncated:
            raise RelationshipError("result_limit_exceeded")
        result: list[dict[str, Any]] = []
        for name, kind in entries:
            if kind != "file" or not _EDGE_ID.fullmatch(name[:-3]) or not name.endswith(".md"):
                continue
            try:
                raw = self.vault.read_vault_file(
                    directory + "/" + name, max_bytes=_LEDGER_MAX_BYTES
                )
                fields = self._decode_ledger(raw, name[:-3])
            except (VaultError, RelationshipError):
                continue
            if edge_id is not None and fields["edge_id"] != edge_id:
                continue
            if status is not None and fields["status"] != status:
                continue
            if source_id is not None and f"journal:{source_id}" not in {
                fields["from"], fields["to"]
            }:
                continue
            result.append(self._edge_view(fields))
            if len(result) > limit:
                raise RelationshipError("result_limit_exceeded")
        result.sort(key=lambda item: item["edge_id"])
        return result[:limit]

    def relocate_journal_projection(
        self, source_id: str, old_path: str, new_path: str, *,
        original_projection: bytes | None = None,
        migrated_projection: bytes | None = None,
    ) -> None:
        """Refresh references and remap verified metadata-only migration evidence."""
        self._validate_result_options(None, source_id, None, None)
        if source_id is None:
            raise RelationshipError("invalid_request")
        for path in (old_path, new_path):
            if (
                not isinstance(path, str)
                or not path.startswith("entries/")
                or not path.endswith(".md")
                or "\\" in path
                or any(part in {"", ".", ".."} or part.startswith(".") for part in path.split("/"))
                or any(ord(char) < 32 or 0x7F <= ord(char) <= 0x9F or 0xD800 <= ord(char) <= 0xDFFF for char in path)
            ):
                raise RelationshipError("invalid_request")
        evidence_mapping: dict[str, str] = {}
        if original_projection is not None or migrated_projection is not None:
            if (
                not isinstance(original_projection, bytes)
                or not isinstance(migrated_projection, bytes)
                or max(len(original_projection), len(migrated_projection)) > self.limits.max_file_bytes
            ):
                raise RelationshipError("invalid_request")
            original = self._parse_document(
                original_projection, self.journal_relative + "/" + old_path, journal=True
            )
            migrated = self._parse_document(
                migrated_projection, self.journal_relative + "/" + new_path, journal=True
            )
            original_fields, original_body = _frontmatter(original_projection.decode("utf-8"))
            migrated_fields, migrated_body = _frontmatter(migrated_projection.decode("utf-8"))
            if (
                original.identity != "journal:" + source_id
                or migrated.identity != original.identity
                or migrated.source_version != original.source_version
                or migrated_fields.get("source_checksum") != original_fields.get("source_checksum")
                or migrated.revision != original.revision
                or migrated_body != original_body
                or migrated.links != original.links
                or original.links_truncated or migrated.links_truncated
            ):
                raise RelationshipError("invalid_request")
            for hint, _ in original.links:
                old_evidence = "sha256:" + hashlib.sha256((original.digest + "\0" + hint).encode("utf-8")).hexdigest()
                evidence_mapping[old_evidence] = "sha256:" + hashlib.sha256((migrated.digest + "\0" + hint).encode("utf-8")).hexdigest()
        directory = self.journal_relative + "/relationships"
        updates: list[tuple[bytes, dict[str, Any], dict[str, Any]]] = []
        try:
            try:
                entries, truncated = self.vault.list_vault_directory(
                    directory, limit=self.limits.max_relationship_files + 1
                )
            except VaultError as exc:
                if exc.code == "not_found":
                    return
                raise
            if truncated or len(entries) > self.limits.max_relationship_files:
                raise RelationshipError("result_limit_exceeded")
            for name, kind in entries:
                if re.match(r"edge_[0-9a-f]{64}\.md\.sync-conflict-", name):
                    raise RelationshipError("sync_conflict")
                if not name.endswith(".md") or not _EDGE_ID.fullmatch(name[:-3]):
                    continue
                if kind != "file":
                    raise RelationshipError("invalid_ledger")
                raw = self.vault.read_vault_file(directory + "/" + name, max_bytes=_LEDGER_MAX_BYTES)
                current = self._decode_ledger(raw, name[:-3])
                refreshed = dict(current)
                for identity, reference in (("from", "source_reference"), ("to", "target_reference")):
                    if (
                        current[identity] == "journal:" + source_id
                        and current[reference] == self.journal_relative + "/" + old_path
                    ):
                        refreshed[reference] = self.journal_relative + "/" + new_path
                        if (
                            identity == "from"
                            and current["relation"] == "references"
                            and current["provenance"] == "wikilink"
                            and evidence_mapping
                            and current["source_revision"] == original.revision
                            and current["evidence_id"] in evidence_mapping
                        ):
                            refreshed["evidence_id"] = evidence_mapping[current["evidence_id"]]
                if refreshed == current:
                    continue
                rendered = (
                    "[[" + refreshed["source_reference"] + "]] " + refreshed["relation"]
                    + " [[" + refreshed["target_reference"] + "]].\n"
                )
                refreshed["rendered_hash"] = "sha256:" + hashlib.sha256(rendered.encode("utf-8")).hexdigest()
                refreshed["revision"] = current["revision"] + 1
                updates.append((raw, current, refreshed))
            for raw, current, refreshed in updates:
                self.vault.safe_write_relationship_text(
                    current["edge_id"], self._ledger_text(refreshed),
                    expected_hash=hashlib.sha256(raw).hexdigest(),
                    expected_revision=current["revision"],
                )
        except VaultError as exc:
            raise RelationshipError(exc.code) from None

    def scan_relationships(
        self, *, source_id: str | None = None, max_results: int | None = None
    ) -> dict[str, Any]:
        limit = self._validate_result_options(None, source_id, None, max_results)
        deadline = time.monotonic() + self.limits.max_relationship_scan_ms / 1000
        files: list[tuple[str, bool]] = [(self.journal_relative + "/entries", True)]
        files.extend((root, False) for root in self.allowlist_roots)
        documents: list[_Document] = []
        review: list[dict[str, Any]] = []
        total_bytes = 0
        visited_files = 0
        visited_directories = 0
        truncated = False
        hints_truncated = False

        while files:
            base, journal = files.pop(0)
            stack = [base]
            while stack:
                if time.monotonic() >= deadline:
                    truncated = True
                    break
                directory = stack.pop()
                visited_directories += 1
                if visited_directories > self.limits.max_relationship_files:
                    truncated = True
                    break
                try:
                    entries, more_entries = self.vault.list_vault_directory(
                        directory,
                        limit=max(1, self.limits.max_relationship_files - visited_files + 1),
                    )
                except VaultError as exc:
                    if exc.code != "not_found":
                        review.append({"code": "unreadable_directory"})
                    continue
                truncated = truncated or more_entries
                for name, kind in entries:
                    if time.monotonic() >= deadline:
                        truncated = True
                        break
                    relative = directory + "/" + name
                    if any(
                        ord(char) < 32
                        or 0x7F <= ord(char) <= 0x9F
                        or 0xD800 <= ord(char) <= 0xDFFF
                        for char in name
                    ):
                        continue
                    if kind == "directory":
                        if (
                            name.startswith(".")
                            or name.casefold() in _EXCLUDED_NAMES
                            or journal and name.casefold() in _EXCLUDED_JOURNAL_DIRS
                            or _excluded_file(name)
                        ):
                            continue
                        stack.append(relative)
                        continue
                    if kind != "file" or _excluded_file(name) or not name.casefold().endswith(".md"):
                        continue
                    visited_files += 1
                    if visited_files > self.limits.max_relationship_files:
                        truncated = True
                        break
                    try:
                        raw = self.vault.read_vault_file(
                            relative, max_bytes=self.limits.max_relationship_file_bytes
                        )
                    except VaultError as exc:
                        code = "oversized_file" if exc.code == "file_too_large" else "unreadable_file"
                        review.append({"code": code})
                        continue
                    if total_bytes + len(raw) > self.limits.max_relationship_bytes:
                        truncated = True
                        break
                    total_bytes += len(raw)
                    try:
                        document = self._parse_document(raw, relative, journal=journal)
                    except RelationshipError as exc:
                        review.append({"code": exc.code})
                        continue
                    if document is not None and document.links_truncated:
                        hints_truncated = True
                    if document is not None and (
                        source_id is None or not document.is_journal or document.source_id == source_id
                    ):
                        if len(documents) >= min(self.limits.max_relationship_files, _MAX_SCAN_DOCUMENTS):
                            truncated = True
                            break
                        documents.append(document)
                    if truncated:
                        break
                if truncated:
                    break
            if truncated:
                break

        edges, missing, resolution_truncated = self._resolve_links(
            documents, source_id=source_id, deadline=deadline, max_results=limit
        )
        truncated = truncated or hints_truncated or resolution_truncated
        review.extend(missing)
        proposals: list[dict[str, Any]] = []
        for candidate in edges:
            try:
                raw = self.vault.read_vault_file(
                    self.journal_relative + "/relationships/" + candidate["edge_id"] + ".md",
                    max_bytes=_LEDGER_MAX_BYTES,
                )
                current = self._decode_ledger(raw, candidate["edge_id"])
            except VaultError as exc:
                if exc.code != "not_found":
                    review.append({"code": "ledger_conflict", "edge_id": candidate["edge_id"]})
                    continue
                content = self._ledger_text(candidate)
                try:
                    self.vault.safe_write_relationship_text(candidate["edge_id"], content)
                except VaultError as exc:
                    if exc.code != "conflict":
                        review.append({"code": "ledger_conflict", "edge_id": candidate["edge_id"]})
                        continue
                try:
                    current = self._read_ledger(candidate["edge_id"])
                except (VaultError, RelationshipError):
                    review.append({"code": "ledger_conflict", "edge_id": candidate["edge_id"]})
                    continue
            except (VaultError, RelationshipError):
                review.append({"code": "ledger_conflict", "edge_id": candidate["edge_id"]})
                continue
            else:
                snapshot_fields = (
                    "source_revision", "target_revision", "evidence_id",
                    "source_reference", "target_reference", "rendered_hash",
                )
                if any(current.get(key) != candidate.get(key) for key in snapshot_fields):
                    refreshed = dict(candidate)
                    if all(current.get(key) == candidate.get(key) for key in (
                        "source_revision", "target_revision", "relation", "provenance", "evidence_id",
                    )):
                        refreshed = dict(current)
                        refreshed.update({key: candidate[key] for key in snapshot_fields})
                    refreshed["revision"] = current["revision"] + 1
                    try:
                        self.vault.safe_write_relationship_text(
                            candidate["edge_id"],
                            self._ledger_text(refreshed),
                            expected_hash=hashlib.sha256(raw).hexdigest(),
                            expected_revision=current["revision"],
                        )
                    except VaultError:
                        review.append({"code": "ledger_conflict", "edge_id": candidate["edge_id"]})
                        continue
                    current = refreshed
            proposals.append(self._edge_view(current))
            if len(proposals) >= limit:
                if len(edges) > len(proposals):
                    truncated = True
                break
        review = review[:limit]
        return {
            "edges": proposals,
            "review": review,
            "scanned_files": visited_files,
            "truncated": truncated,
        }

    def decide(
        self,
        edge_id: str,
        decision: str,
        expected_revision: int,
        *,
        principal: str,
    ) -> dict[str, Any]:
        if not isinstance(edge_id, str) or not _EDGE_ID.fullmatch(edge_id):
            raise RelationshipError("invalid_request")
        if not isinstance(decision, str) or decision not in {"approve", "reject"}:
            raise RelationshipError("invalid_request")
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 1
            or not isinstance(principal, str)
            or not principal
            or not _utf8_within(principal, 256)
        ):
            raise RelationshipError("invalid_request")
        try:
            raw = self.vault.read_vault_file(
                self.journal_relative + "/relationships/" + edge_id + ".md",
                max_bytes=_LEDGER_MAX_BYTES,
            )
            fields = self._decode_ledger(raw, edge_id)
        except (VaultError, RelationshipError):
            raise RelationshipError("relationship_unavailable") from None
        if fields["revision"] != expected_revision:
            raise RelationshipError("conflict")
        desired = "approved" if decision == "approve" else "rejected"
        if fields["status"] == desired:
            return self._edge_view(fields)
        next_fields = dict(fields)
        next_fields.update(
            {
                "status": desired,
                "revision": expected_revision + 1,
                "decision_principal": principal,
            }
        )
        content = self._ledger_text(next_fields)
        try:
            self.vault.safe_write_relationship_text(
                edge_id,
                content,
                expected_hash=hashlib.sha256(raw).hexdigest(),
                expected_revision=expected_revision,
            )
        except VaultError as exc:
            raise RelationshipError("conflict" if exc.code == "conflict" else exc.code) from None
        return self._edge_view(next_fields)

    def _validate_result_options(
        self,
        edge_id: str | None,
        source_id: str | None,
        status: str | None,
        max_results: int | None,
    ) -> int:
        limit = self.limits.max_result_count if max_results is None else max_results
        if (
            edge_id is not None and (not isinstance(edge_id, str) or not _EDGE_ID.fullmatch(edge_id))
            or source_id is not None and (not isinstance(source_id, str) or not _SOURCE_ID.fullmatch(source_id))
            or status is not None and (
                not isinstance(status, str) or status not in {"proposed", "approved", "rejected"}
            )
            or isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= self.limits.max_result_count
        ):
            raise RelationshipError("invalid_request")
        return limit

    def _parse_document(self, raw: bytes, path: str, *, journal: bool) -> _Document | None:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise RelationshipError("invalid_markdown") from None
        fields, body = _frontmatter(text)
        if journal:
            source_id = fields.get("source_id")
            version = fields.get("source_version")
            if (
                fields.get("ownership_marker") != "lifeweave-journal"
                or not isinstance(source_id, str)
                or not _SOURCE_ID.fullmatch(source_id)
                or isinstance(version, bool)
                or not isinstance(version, int)
                or version < 1
            ):
                raise RelationshipError("invalid_journal_entry")
            identity = "journal:" + source_id
            revision = str(version) + ":" + str(fields.get("source_checksum", ""))
            digest = hashlib.sha256(raw).hexdigest()
            source_version = version
            title = fields.get("title")
            if not isinstance(title, str):
                provenance = fields.get("provenance")
                title = provenance.get("title", "") if isinstance(provenance, Mapping) else ""
            source = source_id
        else:
            note_id = fields.get("note_id")
            if not isinstance(note_id, str) or not _NOTE_ID.fullmatch(note_id):
                note_id = None
            identity = "note:" + note_id if note_id is not None else "path:" + path
            revision = hashlib.sha256(raw).hexdigest()
            digest = revision
            source_version = None
            title = fields.get("title", "")
            source = None
        if not isinstance(title, str):
            title = ""
        links_truncated = False
        try:
            if not _utf8_within(title, _MAX_HINT_BYTES):
                title = ""
                links_truncated = True
        except UnicodeError:
            title = ""
            links_truncated = True
        aliases_raw = fields.get("aliases", ())
        if isinstance(aliases_raw, str):
            aliases = (aliases_raw,) if _utf8_within(aliases_raw, _MAX_HINT_BYTES) else ()
            if not aliases:
                links_truncated = True
        elif isinstance(aliases_raw, Sequence) and not isinstance(aliases_raw, (bytes, str)):
            selected_aliases = [
                item for item in aliases_raw[: _MAX_ALIASES_PER_FILE]
                if isinstance(item, str) and _utf8_within(item, _MAX_HINT_BYTES)
            ]
            aliases = tuple(selected_aliases)
            if len(aliases_raw) > _MAX_ALIASES_PER_FILE or len(selected_aliases) != len(aliases_raw):
                links_truncated = True
        else:
            aliases = ()
        visible = _plain_markdown(body)
        heading_values: list[str] = []
        for line in visible.splitlines():
            match = _HEADING.match(line)
            if match:
                if len(heading_values) >= _MAX_HEADINGS_PER_FILE:
                    links_truncated = True
                    break
                heading = match.group(1)
                if not _utf8_within(heading, _MAX_HINT_BYTES):
                    links_truncated = True
                else:
                    heading_values.append(heading)
        headings = tuple(heading_values)
        links: list[tuple[str, str | None]] = []
        for match in _WIKILINK.finditer(visible):
            if len(links) >= _MAX_LINKS_PER_FILE:
                links_truncated = True
                break
            target = match.group(1).strip()
            target, separator, alias = target.partition("|")
            path_hint, hash_mark, heading = target.partition("#")
            path_hint = path_hint.strip()
            if path_hint:
                if not _utf8_within(path_hint, _MAX_HINT_BYTES):
                    links_truncated = True
                    continue
                links.append((path_hint, heading.strip() if hash_mark else None))
        return _Document(
            identity=identity,
            path=path,
            is_journal=journal,
            revision=revision,
            digest=digest,
            title=title,
            aliases=aliases,
            headings=headings,
            links=tuple(links),
            links_truncated=links_truncated,
            source_id=source,
            source_version=source_version,
            note_id=note_id if not journal else None,
        )

    def _resolve_links(
        self,
        documents: Sequence[_Document],
        *,
        source_id: str | None,
        deadline: float,
        max_results: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
        latest_journal: dict[str, _Document] = {}
        note_documents: list[_Document] = []
        for document in documents:
            if not document.is_journal:
                note_documents.append(document)
                continue
            prior = latest_journal.get(document.source_id or "")
            if prior is None or (document.source_version or 0, document.path) > (
                prior.source_version or 0, prior.path
            ):
                latest_journal[document.source_id or ""] = document
        documents = sorted(
            [*latest_journal.values(), *note_documents],
            key=lambda item: (item.identity, item.path),
        )

        note_id_counts: dict[str, int] = defaultdict(int)
        for document in documents:
            if not document.is_journal and document.note_id is not None:
                note_id_counts[document.note_id.casefold()] += 1
        documents = [
            replace(document, identity="path:" + document.path)
            if not document.is_journal
            and document.note_id is not None
            and note_id_counts[document.note_id.casefold()] > 1
            else document
            for document in documents
        ]

        note_id_index: dict[str, list[_Document]] = defaultdict(list)
        hint_index: dict[str, list[_Document]] = defaultdict(list)
        path_index: dict[str, list[_Document]] = defaultdict(list)
        for document in documents:
            path_no_ext = document.path[:-3] if document.path.casefold().endswith(".md") else document.path
            path_index[path_no_ext.casefold()].append(document)
            path_index[document.path.casefold()].append(document)
            path_index[PurePosixPath(document.path).stem.casefold()].append(document)
            hint_values = [document.title, *document.aliases]
            for hint in hint_values:
                if hint.strip():
                    hint_index[_normalize_hint(hint)].append(document)
            if document.is_journal:
                note_id_index[(document.source_id or "").casefold()].append(document)
                hint_index[_normalize_hint(document.source_id or "")].append(document)
            elif document.note_id is not None:
                note_id_index[document.note_id.casefold()].append(document)
                hint_index[_normalize_hint(document.note_id)].append(document)
            for heading in document.headings:
                hint_index[_normalize_hint(heading)].append(document)

        edges_by_id: dict[str, dict[str, Any]] = {}
        review: list[dict[str, Any]] = []
        work_limit = min(_MAX_LINKS_PER_FILE, max_results * 10)
        work_count = 0
        truncated = False
        for source in documents:
            if not source.is_journal and not any(link for link in source.links):
                continue
            for target_hint, heading in source.links:
                if time.monotonic() >= deadline or work_count >= work_limit:
                    truncated = True
                    break
                work_count += 1
                if source_id is not None and source.is_journal and source.source_id != source_id:
                    continue
                resolved: list[_Document] = []
                clean_hint = _normalize_hint(target_hint)
                if clean_hint in note_id_index:
                    resolved = note_id_index[clean_hint]
                else:
                    path_key = target_hint.replace("\\", "/")
                    if path_key.startswith("/"):
                        path_key = path_key.lstrip("/")
                    elif "/" in path_key and not path_key.startswith("./"):
                        path_key = str(PurePosixPath(source.path).parent / path_key)
                    elif path_key.startswith("./"):
                        path_key = str(PurePosixPath(source.path).parent / path_key[2:])
                    path_key = str(PurePosixPath(path_key))
                    if path_key == ".." or path_key.startswith("../"):
                        review.append(self._unresolved(source, target_hint, "missing_target"))
                        continue
                    path_key = _normalize_hint(path_key)
                    if path_key in path_index:
                        resolved = path_index[path_key]
                    else:
                        resolved = hint_index.get(clean_hint, [])
                unique = {item.path: item for item in resolved}
                if len(unique) != 1:
                    review.append(self._unresolved(
                        source, target_hint, "missing_target" if not unique else "ambiguous_target"
                    ))
                    if len(review) >= max_results:
                        truncated = True
                        break
                    continue
                target = next(iter(unique.values()))
                if heading and _normalize_hint(heading) not in {
                    _normalize_hint(item) for item in target.headings
                }:
                    review.append(self._unresolved(source, target_hint + "#" + heading, "missing_heading"))
                    if len(review) >= max_results:
                        truncated = True
                        break
                    continue
                if source.is_journal == target.is_journal:
                    continue
                if source_id is not None and source.source_id != source_id and target.source_id != source_id:
                    continue
                from_document, to_document = source, target
                canonical = json.dumps(
                    [from_document.identity, to_document.identity, "references"],
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                identity_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
                edge_id = "edge_" + identity_hash
                rendered = (
                    "[[" + from_document.path + "]] references [[" + to_document.path + "]].\n"
                )
                edges_by_id[edge_id] = {
                    "edge_id": edge_id,
                    "schema_version": 1,
                    "identity": "sha256:" + identity_hash,
                    "from": from_document.identity,
                    "to": to_document.identity,
                    "relation": "references",
                    "status": "proposed",
                    "revision": 1,
                    "source_revision": from_document.revision,
                    "target_revision": to_document.revision,
                    "provenance": "wikilink",
                    "evidence_id": "sha256:" + hashlib.sha256(
                        (source.digest + "\0" + target_hint).encode("utf-8")
                    ).hexdigest(),
                    "source_reference": from_document.path,
                    "target_reference": to_document.path,
                    "rendered_hash": "sha256:" + hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
                }
                if len(edges_by_id) >= max_results:
                    truncated = True
                    break
            if truncated:
                break
        return list(edges_by_id.values()), review[:max_results], truncated

    @staticmethod
    def _unresolved(source: _Document, hint: str, code: str) -> dict[str, Any]:
        return {
            "code": code,
            "source": source.identity,
            "hint_sha256": hashlib.sha256(hint.encode("utf-8")).hexdigest(),
        }

    def _ledger_text(self, fields: Mapping[str, Any]) -> str:
        rendered = (
            "[[" + str(fields["source_reference"]) + "]] "
            + str(fields["relation"]) + " [[" + str(fields["target_reference"]) + "]].\n"
        )
        body = rendered if fields.get("status") == "approved" else ""
        values = dict(fields)
        values["ownership_marker"] = "lifeweave-relationship"
        values["schema_version"] = 1
        return "---\n" + yaml.safe_dump(
            values, allow_unicode=True, sort_keys=True, width=_LEDGER_MAX_BYTES
        ) + "---\n\n" + body

    def _read_ledger(self, edge_id: str) -> dict[str, Any]:
        raw = self.vault.read_vault_file(
            self.journal_relative + "/relationships/" + edge_id + ".md",
            max_bytes=_LEDGER_MAX_BYTES,
        )
        return self._decode_ledger(raw, edge_id)

    @staticmethod
    def _decode_ledger(raw: bytes, expected_id: str) -> dict[str, Any]:
        try:
            text = raw.decode("utf-8")
            fields, body = _frontmatter(text)
        except (UnicodeDecodeError, RelationshipError):
            raise RelationshipError("invalid_ledger") from None
        required = {
            "edge_id", "schema_version", "identity", "from", "to", "relation",
            "status", "revision", "source_revision", "target_revision", "provenance",
            "evidence_id", "source_reference", "target_reference", "rendered_hash",
            "ownership_marker",
        }
        if (
            not required <= set(fields)
            or fields.get("ownership_marker") != "lifeweave-relationship"
            or fields.get("schema_version") != 1
            or fields.get("edge_id") != expected_id
            or fields.get("status") not in {"proposed", "approved", "rejected"}
            or fields.get("relation") not in EDGE_TYPES
            or isinstance(fields.get("revision"), bool)
            or not isinstance(fields.get("revision"), int)
            or fields["revision"] < 1
            or not all(isinstance(fields.get(key), str) for key in required - {"schema_version", "revision"})
        ):
            raise RelationshipError("invalid_ledger")
        journal_from = fields["from"].startswith("journal:")
        journal_to = fields["to"].startswith("journal:")
        if journal_from == journal_to:
            raise RelationshipError("invalid_ledger")
        try:
            canonical = json.dumps(
                [fields["from"], fields["to"], fields["relation"]],
                ensure_ascii=False,
                separators=(",", ":"),
            )
            identity_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            rendered = (
                "[[" + fields["source_reference"] + "]] "
                + fields["relation"] + " [[" + fields["target_reference"] + "]].\n"
            )
            expected_body = rendered if fields["status"] == "approved" else ""
            rendered_hash = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
        except (TypeError, UnicodeError):
            raise RelationshipError("invalid_ledger") from None
        if (
            expected_id != "edge_" + identity_hash
            or fields["identity"] != "sha256:" + identity_hash
            or fields["rendered_hash"] != "sha256:" + rendered_hash
            or body != expected_body
        ):
            raise RelationshipError("invalid_ledger")
        return dict(fields)

    @staticmethod
    def _edge_view(fields: Mapping[str, Any]) -> dict[str, Any]:
        result = {
            key: fields[key]
            for key in (
                "edge_id", "revision", "from", "to", "relation", "status",
                "source_revision", "target_revision", "provenance", "evidence_id",
            )
        }
        if "decision_principal" in fields:
            result["decision_principal"] = fields["decision_principal"]
        return result
