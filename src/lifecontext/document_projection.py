"""Rebuildable local projections; neither ingestion nor provider submission."""
from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from pathlib import PurePosixPath
from urllib.parse import quote

import yaml

from .projection_names import readable_basename
from .vault import VaultError
from .wiki import PAGE_STATES, WITHDRAWN_TITLE

# File names this service gives a withdrawn page and its history files.
_WITHDRAWN_NAME = re.compile(re.escape(WITHDRAWN_TITLE) + r"(?: \(\d+\))?(?: - r\d+(?: \(\d+\))?)?\.md")
MANAGED_START = "<!-- life-context:managed:start -->"
MANAGED_END = "<!-- life-context:managed:end -->"
USER_START = "<!-- life-context:user:start -->"
USER_END = "<!-- life-context:user:end -->"


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _identity(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value):
        raise VaultError("unsafe_path")
    return value


_UNREAD = {
    "ocr_no_text_page": "no text found",
    "ocr_failed_page": "could not be read",
    "ocr_unavailable_page": "needs OCR, which is not installed",
    "ocr_no_text_frame": "no text found",
    "ocr_failed_frame": "could not be read",
    "ocr_unavailable_frame": "needs OCR, which is not installed",
    "figure_not_read": "images or drawings not read",
    "text_layer_suspect": "text layer may be garbled",
    "embedded_image_not_read": "picture not read",
    "chart_not_read": "chart not read",
    "figure_check_unavailable": "pages could not be checked for images",
    "vision_skipped": "over the vision budget",
    "vision_refused": "the vision model declined",
    "vision_failed": "the vision model gave no usable answer",
    "ledger_truncated": "more items than the ledger can list",
}


def _segment_heading(locator):
    kind, start = locator.get("kind"), locator.get("start")
    container = locator.get("container") or ""
    if kind == "page":
        return f"Page {start}"
    if kind == "slide":
        return f"Slide {start}"
    if kind == "image":
        # A model-read picture sits inline, under the text it belongs to; its label
        # line names it.
        return None if ":picture:" in container else f"Image {start}"
    if kind == "sheet_cell":
        return "Sheet " + container.rsplit("!", 1)[0]
    if container.startswith("section:"):
        number, _, part = container.partition("/")
        return f"{part.split('/', 1)[0].replace('_', ' ').capitalize()} (section {number[8:]})"
    if container.startswith(("footnote:", "endnote:")):
        return "Footnotes" if container.startswith("footnote:") else "Endnotes"
    return None


def _readable_extraction(payload):
    """Render the extraction as headed sections with a coverage summary first."""
    from .extraction import coverage_summary, ledger_items

    coverage = coverage_summary(payload)
    lines = [""]
    if coverage["unit"]:
        lines.append(f"Coverage: {coverage['resolved']} of {coverage['total']} {coverage['unit']}s resolved "
                     f"({coverage['text']} text layer, {coverage['ocr']} OCR, {coverage['vision']} model-read, "
                     f"{coverage['blank']} blank)")
    else:
        lines.append("Coverage: " + ("complete" if coverage["complete"] else "some content was not read"))
    unread = []
    for warning in payload.get("warnings", []):
        code, items, _ = ledger_items(warning)
        if code not in _UNREAD:
            continue
        for item in items or ["document"]:
            if item.isdigit():
                item = ("frame " if code.endswith("_frame") else "page ") + item
            unread.append(f"- {item.replace(':', ' ', 1)}: {_UNREAD[code]}")
    if unread:
        lines.extend(["", "## Not read", "", *unread])
    heading = None
    for segment in payload.get("segments", []):
        locator = segment.get("locator", {})
        current = _segment_heading(locator)
        if current != heading and current is not None:
            lines.extend(["", f"## {current}", ""])
            heading = current
        text = segment.get("text", "")
        if locator.get("kind") == "sheet_cell":
            text = f"{locator.get('container', '').rsplit('!', 1)[-1]}: {text}"
        lines.append(text.rstrip("\n"))
    return lines


class DocumentProjectionService:
    def __init__(self, catalog, custody, derived, vault, wiki_service=None):
        self.catalog = catalog
        self.custody = custody
        self.derived = derived
        self.vault = vault
        self.wiki_service = wiki_service
        self._headings = {}

    @staticmethod
    def _move_id(artifact_id):
        return "move_" + _hash(artifact_id.encode())

    @staticmethod
    def _publication_id(layer, artifact_id):
        return "wiki_" + _hash(artifact_id.encode()) if layer == "wiki" else artifact_id

    def _pending_identity(self, receipt, *, layer="document"):
        proof = self.catalog.get_artifact_publication(self._publication_id(layer, receipt["artifact_id"]), receipt["revision"])
        if (proof is None or proof["state"] != "pending"
                or proof["projection_path"] != receipt["projection_path"]
                or proof["desired_hash"] != receipt["last_projected_hash"]):
            raise VaultError("conflict")
        try:
            staged, identity = self.vault.read_artifact_identity(proof["projection_path"], staged_name=proof["staged_name"])
        except VaultError as exc:
            if exc.code == "not_found":
                raise VaultError("conflict") from exc
            raise
        expected = (proof["device"], proof["inode"])
        if identity != expected or _hash(staged) != proof["desired_hash"]:
            raise VaultError("conflict")
        return expected

    def _finish_publication(self, artifact_id, revision, *, layer="document"):
        publication_id = self._publication_id(layer, artifact_id)
        proof = self.catalog.get_artifact_publication(publication_id, revision)
        if proof is None or proof["state"] == "completed":
            return
        self.catalog.set_artifact_publication(publication_id, revision,
            projection_path=proof["projection_path"], desired_hash=proof["desired_hash"],
            device=proof["device"], inode=proof["inode"], staged_name=proof["staged_name"], state="retired")
        identity = (proof["device"], proof["inode"]) if proof["device"] is not None else (-1, -1)
        for name in (proof["staged_name"][:-5], proof["staged_name"]):
            self.vault.safe_remove_artifact(proof["projection_path"], expected_hash=proof["desired_hash"],
                artifact_id=artifact_id, allow_missing=True, expected_identity=identity,
                staged_name=name)
        self.catalog.set_artifact_publication(publication_id, revision,
            projection_path=proof["projection_path"], desired_hash=proof["desired_hash"],
            device=proof["device"], inode=proof["inode"], staged_name=proof["staged_name"], state="completed")

    def _recover_publication(self, artifact_id, revision, *, layer="document"):
        receipt = self.catalog.get_artifact_projection(layer, artifact_id, revision)
        proof = self.catalog.get_artifact_publication(self._publication_id(layer, artifact_id), revision)
        if proof is None:
            return
        if receipt and receipt["state"] == "pending" and proof["state"] == "pending":
            try:
                actual, identity = self.vault.read_artifact_identity(receipt["projection_path"])
            except VaultError as exc:
                if exc.code != "not_found":
                    raise
                if layer == "wiki":
                    previous = self.catalog.get_artifact_projection(layer, self._move_id(artifact_id), revision)
                    if previous and previous["state"] != "not_projected":
                        actual = self.vault.read_artifact(previous["projection_path"])
                        if _hash(actual) != previous["last_projected_hash"]:
                            raise VaultError("conflict")
                        self._restore_wiki_baseline(artifact_id, revision, receipt, previous)
                    else:
                        self.catalog.set_artifact_projection(layer, artifact_id, revision,
                            projection_path=receipt["projection_path"], desired_hash=receipt["desired_hash"],
                            state="not_projected", last_projected_hash=None)
            else:
                if _hash(actual) == receipt["last_projected_hash"]:
                    if identity != self._pending_identity(receipt, layer=layer):
                        raise VaultError("conflict")
                    self.catalog.set_artifact_projection(layer, artifact_id, revision,
                        projection_path=receipt["projection_path"], desired_hash=receipt["desired_hash"],
                        state="projected", last_projected_hash=receipt["last_projected_hash"])
                else:
                    previous = self.catalog.get_artifact_projection(layer, self._move_id(artifact_id), revision)
                    if (previous is None or previous["state"] == "not_projected"
                            or previous["projection_path"] != receipt["projection_path"]
                            or _hash(actual) != previous["last_projected_hash"]):
                        raise VaultError("conflict")
                    if layer == "wiki":
                        self._restore_wiki_baseline(artifact_id, revision, receipt, previous)
        self._finish_publication(artifact_id, revision, layer=layer)

    def _restore_wiki_baseline(self, artifact_id, revision, receipt, previous):
        prior = [row for row in self.catalog.list_artifact_projections(layer="wiki", artifact_id=artifact_id)
                 if row["revision"] < revision and row["projection_path"] == previous["projection_path"]
                 and row["last_projected_hash"] == previous["last_projected_hash"]]
        self.catalog.set_artifact_projection("wiki", artifact_id, revision,
            projection_path=receipt["projection_path"] if prior else previous["projection_path"],
            desired_hash=receipt["desired_hash"] if prior else previous["last_projected_hash"],
            state="not_projected" if prior else "projected",
            last_projected_hash=None if prior else previous["last_projected_hash"])

    def _finish_move(self, artifact_id, revision, current_path, *, layer="document"):
        move_id = self._move_id(artifact_id)
        receipt = self.catalog.get_artifact_projection(layer, move_id, revision)
        if receipt is None or receipt["state"] == "not_projected":
            return False
        if receipt["projection_path"] != current_path:
            self.vault.safe_remove_artifact(receipt["projection_path"],
                expected_hash=receipt["last_projected_hash"], artifact_id=artifact_id,
                allow_missing=True)
        self.catalog.set_artifact_projection(layer, move_id, revision,
            projection_path=receipt["projection_path"], desired_hash=receipt["desired_hash"],
            state="not_projected", last_projected_hash=receipt["last_projected_hash"])
        return receipt["projection_path"] != current_path

    def _project(self, layer, artifact_id, revision, path, data, *, expected_old=None, previous_projection=None):
        _identity(artifact_id)
        old = self.catalog.get_artifact_projection(layer, artifact_id, revision)
        desired = _hash(data)
        try:
            if layer in {"document", "wiki"}:
                self._recover_publication(artifact_id, revision, layer=layer)
                old = self.catalog.get_artifact_projection(layer, artifact_id, revision)
            if layer == "wiki" and (old is None or old["last_projected_hash"] is None):
                old = previous_projection
            if old and old["projection_path"] != path:
                try:
                    existing, identity = self.vault.read_artifact_identity(old["projection_path"])
                    if old["state"] == "pending" and identity != self._pending_identity(old, layer=layer):
                        raise VaultError("conflict")
                except VaultError as exc:
                    if exc.code != "not_found":
                        raise
                else:
                    if not old["last_projected_hash"] or _hash(existing) != old["last_projected_hash"]:
                        raise VaultError("conflict")
            try:
                existing, identity = self.vault.read_artifact_identity(path)
                if (old and old["state"] == "pending"
                        and old["projection_path"] == path and _hash(existing) == old["last_projected_hash"]
                        and identity != self._pending_identity(old, layer=layer)):
                    raise VaultError("conflict")
            except VaultError as exc:
                if exc.code != "not_found":
                    raise
                expected = None
                rebuilt = True
            else:
                expected = (old["last_projected_hash"] if old and old["projection_path"] == path
                            else expected_old if old is None else None)
                rebuilt = False
                if expected is not None and _hash(existing) != expected:
                    previous = self.catalog.get_artifact_projection(layer, self._move_id(artifact_id), revision)
                    if (previous and previous["state"] != "not_projected"
                            and previous["projection_path"] == path
                            and _hash(existing) == previous["last_projected_hash"]):
                        expected = previous["last_projected_hash"]
                if expected is None or _hash(existing) != expected:
                    raise VaultError("conflict")
            changed = rebuilt or _hash(existing) != desired or bool(old and old["projection_path"] != path)
            if old and old["projection_path"] != path:
                changed = self._finish_move(artifact_id, revision, old["projection_path"], layer=layer) or changed

            def before_stage(staged_name):
                self.catalog.set_artifact_publication(self._publication_id(layer, artifact_id), revision,
                    projection_path=path, desired_hash=desired, device=None, inode=None,
                    staged_name=staged_name, state="planned")

            def before_publish(device, inode, staged_name):
                self.catalog.set_artifact_publication(self._publication_id(layer, artifact_id), revision,
                    projection_path=path, desired_hash=desired, device=device, inode=inode, staged_name=staged_name)
                baseline = old if old and old["last_projected_hash"] else (
                    {"projection_path": path, "last_projected_hash": expected} if expected is not None else None
                )
                if baseline:
                    previous = self.catalog.get_artifact_projection(layer, self._move_id(artifact_id), revision)
                    if previous is None or previous["state"] == "not_projected":
                        self.catalog.set_artifact_projection(layer, self._move_id(artifact_id), revision,
                            projection_path=baseline["projection_path"], desired_hash=baseline["last_projected_hash"],
                            state="pending", last_projected_hash=baseline["last_projected_hash"])
                self.catalog.set_artifact_projection(layer, artifact_id, revision,
                    projection_path=path, desired_hash=desired, state="pending", last_projected_hash=desired)

                def rollback():
                    self.catalog.set_artifact_publication(self._publication_id(layer, artifact_id), revision,
                        projection_path=path, desired_hash=desired, device=device, inode=inode,
                        staged_name=staged_name, state="retired")
                    self.catalog.set_artifact_projection(layer, artifact_id, revision,
                        projection_path=old["projection_path"] if old and old["revision"] == revision else path,
                        desired_hash=desired, state="conflict",
                        last_projected_hash=old["last_projected_hash"] if old and old["revision"] == revision else None)
                return rollback

            self.vault.safe_write_bytes(path, data, expected_hash=expected, artifact_id=artifact_id,
                before_publish=before_publish,
                before_stage=before_stage)
            self.catalog.set_artifact_projection(layer, artifact_id, revision,
                projection_path=path, desired_hash=desired, state="projected", last_projected_hash=desired)
            if layer in {"document", "wiki"}:
                self._finish_publication(artifact_id, revision, layer=layer)
                changed = self._finish_move(artifact_id, revision, path, layer=layer) or changed
            return {"state": "completed", "path": path, "rebuilt": rebuilt, "changed": changed}
        except VaultError as exc:
            recorded = self.catalog.get_artifact_projection(layer, artifact_id, revision)
            self.catalog.set_artifact_projection(layer, artifact_id, revision,
                projection_path=recorded["projection_path"] if recorded else path,
                desired_hash=recorded["desired_hash"] if recorded and recorded["state"] == "pending" else desired,
                state="pending" if recorded and recorded["state"] == "pending" else "conflict",
                last_projected_hash=recorded["last_projected_hash"] if recorded else None)
            return {"state": "conflict", "path": path, "error": exc.code, "rebuilt": False}

    def project_document(self, source_id, version):
        _identity(source_id)
        metadata = self.catalog.get_document_metadata(source_id, version)
        if metadata is None or self.catalog.get_tombstone(source_id, version):
            return {"state": "pending", "error": "document_unavailable"}
        original = self.custody.resolve_exact(source_id, version)
        category = _identity(metadata["category_id"])
        filename = re.sub(r"[^A-Za-z0-9_. -]", "_", metadata["filename"])[:120].strip(" .") or "original"
        stable = f"{source_id}-v{version}"
        attachment = f"documents/{category}/attachments/{stable}--{filename}"
        if _hash(original.original_bytes) != original.manifest.sha256:
            raise VaultError("conflict")
        results = [self._project("document", source_id + "_attachment", version,
                                 attachment, original.original_bytes)]
        lines = [f"# {metadata['title']}", "", f"Source: {source_id} / v{version}",
                 f"Original checksum: {original.manifest.sha256}",
                 f"[Original](attachments/{stable}--{filename})", ""]
        for stage in ("extraction", "indexing", "wiki"):
            row = self.catalog.get_document_stage(source_id, version, stage)
            lines.append(f"{stage}: {row['state'] if row else 'pending'}")
        extractions = self.catalog.list_extractions(source_id, version)
        if extractions:
            row = extractions[-1]
            payload = self.derived.read(source_id, version, row["extraction_revision"],
                                        expected_sha256=row["artifact_sha256"])
            lines.extend(_readable_extraction(payload))
        results.append(self._project("document", source_id + "_note", version,
                       f"documents/{category}/{stable}.md", ("\n".join(lines) + "\n").encode()))
        return {"state": "completed" if all(r["state"] == "completed" for r in results) else "conflict",
                "artifacts": results, "rebuilt": sum(r["rebuilt"] for r in results)}

    def remove_document(self, source_id, version, *, allow_missing=False):
        _identity(source_id)
        manifest = self.catalog.get_manifest(source_id, version)
        metadata = self.catalog.get_document_metadata(source_id, version)
        stable = f"{source_id}-v{version}"
        for suffix in ("attachment", "note"):
            artifact_id = source_id + "_" + suffix
            self._recover_publication(artifact_id, version)
            receipt = self.catalog.get_artifact_projection("document", artifact_id, version)
            if receipt is None:
                if metadata is None:
                    continue
                category = _identity(metadata["category_id"])
                filename = re.sub(r"[^A-Za-z0-9_. -]", "_", metadata["filename"])[:120].strip(" .") or "original"
                path = (f"documents/{category}/attachments/{stable}--{filename}"
                        if suffix == "attachment" else f"documents/{category}/{stable}.md")
                try:
                    self.vault.read_artifact(path)
                except VaultError as exc:
                    if exc.code != "not_found":
                        raise
                    continue
                raise VaultError("conflict")
            move = self.catalog.get_artifact_projection("document", self._move_id(artifact_id), version)
            removed_paths = set()
            receipts = ([move] if move and move["state"] != "not_projected" else []) + [receipt]
            for recorded in receipts:
                path = recorded["projection_path"]
                parts = path.split("/")
                if (parts[0] != "documents" or len(parts) != (4 if suffix == "attachment" else 3)
                        or (suffix == "attachment" and (parts[2] != "attachments" or not parts[3].startswith(stable + "--")))
                        or (suffix == "note" and parts[2] != stable + ".md")):
                    raise VaultError("unsafe_path")
                _identity(parts[1])
                digest = recorded["last_projected_hash"]
                if digest is None:
                    try:
                        self.vault.read_artifact(path)
                    except VaultError as exc:
                        if exc.code != "not_found":
                            raise
                    else:
                        raise VaultError("conflict")
                else:
                    if suffix == "attachment" and (manifest is None or digest != manifest["sha256"]):
                        raise VaultError("conflict")
                    removal_digest = digest
                    if (recorded is move and path == receipt["projection_path"]
                            and receipt["last_projected_hash"] and receipt["last_projected_hash"] != digest):
                        try:
                            actual = self.vault.read_artifact(path)
                        except VaultError as exc:
                            if exc.code != "not_found":
                                raise
                        else:
                            if _hash(actual) == receipt["last_projected_hash"]:
                                removal_digest = receipt["last_projected_hash"]
                    expected_identity = None
                    if path == receipt["projection_path"] and removal_digest == receipt["last_projected_hash"]:
                        proof = self.catalog.get_artifact_publication(artifact_id, version)
                        if proof and proof["projection_path"] == path and proof["desired_hash"] == removal_digest:
                            expected_identity = (proof["device"], proof["inode"])
                        if receipt["state"] == "pending":
                            expected_identity = (-1, -1)
                    try:
                        self.vault.safe_remove_artifact(path, expected_hash=removal_digest, artifact_id=artifact_id,
                            allow_missing=allow_missing or recorded["state"] in {"not_projected", "pending"}
                            or path in removed_paths, expected_identity=expected_identity)
                    except VaultError:
                        self.catalog.set_artifact_projection("document", recorded["artifact_id"], version,
                            projection_path=path, desired_hash=recorded["desired_hash"],
                            state="pending" if recorded["state"] == "pending" else "conflict",
                            last_projected_hash=digest)
                        raise
                self.catalog.set_artifact_projection("document", recorded["artifact_id"], version,
                    projection_path=path, desired_hash=recorded["desired_hash"], state="not_projected",
                    last_projected_hash=recorded["last_projected_hash"])
                removed_paths.add(path)

    @staticmethod
    def _wiki_path_safe(path, *, history=False):
        parts = path.split("/") if isinstance(path, str) else []
        return (
            len(parts) == (3 if history else 2)
            and parts[0] == "wiki" and (not history or parts[1] == "history")
            and parts[-1].endswith(".md") and parts[-1].casefold() != "schema.md"
            and all(part not in {"", ".", ".."} for part in parts)
            and "\\" not in path and "\x00" not in path
        )

    def _wiki_filename(self, title, *, directory, artifact_id, revision, legacy_path, suffix="", withdrawn=False):
        """A page's (or one history revision's) file: the one it has, else a free readable name.

        A ``withdrawn`` page and its history files move to the neutral name of
        WITHDRAWN_TITLE, and back to a readable one if the page shows claims again.
        """
        receipts = self.catalog.list_artifact_projections(layer="wiki", artifact_id=artifact_id)
        history = directory == "wiki/history"
        if history:
            receipts = [row for row in receipts if row["revision"] == revision]
        old = max(receipts, key=lambda row: row["revision"], default=None)
        if (old and old["projection_path"] != legacy_path
                and bool(_WITHDRAWN_NAME.fullmatch(PurePosixPath(old["projection_path"]).name)) == withdrawn):
            if not self._wiki_path_safe(old["projection_path"], history=history):
                raise VaultError("unsafe_path")
            return old["projection_path"]
        basename = readable_basename(title, fallback="Wiki page", suffix=suffix)
        candidate = f"{directory}/{basename}.md"
        key = lambda value: unicodedata.normalize("NFC", value).casefold()
        if old and key(candidate) == key(legacy_path):
            return legacy_path
        # Briefs, the log and move receipts are not pages and do not count toward the limit.
        projections = self.catalog.list_wiki_page_projections()
        if len(projections) >= 4096:
            raise VaultError("result_limit_exceeded")
        # Names this artifact had before are its own to take again.
        reserved = {key(row["projection_path"]) for row in projections if row["artifact_id"] != artifact_id}
        prefix = self.vault.journal_path.relative_to(self.vault.vault_root).as_posix()
        try:
            entries, truncated = self.vault.list_vault_directory(f"{prefix}/{directory}", limit=100_000)
        except VaultError as exc:
            if exc.code != "not_found":
                raise
            entries, truncated = [], False
        if truncated:
            raise VaultError("result_limit_exceeded")
        reserved.update(key(f"{directory}/{name}") for name, _ in entries)
        if old:
            reserved.discard(key(old["projection_path"]))
        reserved.update({"wiki/schema.md", "wiki/log.md"})
        number = 2
        while key(candidate) in reserved:
            candidate = f"{directory}/{basename} ({number}).md"
            number += 1
        return candidate

    @staticmethod
    def _wiki_metadata(page_id, title, revision, managed, *, history=False, state=None):
        fields = {
            "ownership_marker": "life-context-wiki",
            "page_id": page_id, "title": title, "revision": revision,
            "managed_checksum": _hash(managed.encode("utf-8")),
            "aliases": [f"{page_id}-r{revision}" if history else page_id],
        }
        if state is not None:
            fields["state"] = state
        return "---\n" + yaml.safe_dump(fields, allow_unicode=True, sort_keys=True) + "---\n\n"

    @staticmethod
    def _file_title(text):
        """The title in the front matter of a projected file, or None."""
        head = text.split("\n---\n", 1)[0] if text.startswith("---\n") else ""
        try:
            fields = yaml.safe_load(head)
        except yaml.YAMLError:
            return None
        return fields.get("title") if isinstance(fields, dict) and isinstance(fields.get("title"), str) else None

    def _managed_base(self, text, page_id, titles, revision):
        """The generated start of a projected page, if this service wrote it for ``revision``.

        The managed block depends on the page state, on forgotten sources and on where
        linked notes are projected, so it is not rebuilt for the comparison: the front
        matter must be exactly what this service writes for the block found in the
        file (under one of ``titles``), including the block's checksum. A hand edit of
        the block, its checksum or any field is a conflict. The History line may name
        history files where they were before their last move: they move before the
        page is written, which can fail or be cut short in between.
        """
        start = text.find(MANAGED_START)
        end = text.find(MANAGED_END, start) if start >= 0 else -1
        if end < 0:
            return None
        managed = text[start:end + len(MANAGED_END)] + "\n"
        history = self._history_pattern(page_id, revision)
        for title in titles:
            for state in (None, *PAGE_STATES):
                base = self._wiki_metadata(page_id, title, revision, managed, state=state) + managed
                found = history.match(text, len(base)) if text.startswith(base) else None
                if found and text.startswith(USER_START + "\n", found.end()):
                    return text[:found.end()]
        return None

    def _history_pattern(self, page_id, revision):
        """The History lines ``_history_links`` may have written for ``revision``: each
        revision unlinked, or linked to the file its history receipt names now or
        named before its last move."""
        history = page_id + "_history"
        links = []
        for number in range(1, revision + 1):
            receipts = [self.catalog.get_artifact_projection("wiki", artifact, number)
                        for artifact in (history, self._move_id(history))]
            paths = [row["projection_path"] for row in receipts if row] or [f"wiki/history/{page_id}-r{number}.md"]
            if not all(self._wiki_path_safe(path, history=True) for path in paths):
                raise VaultError("unsafe_path")
            targets = "|".join(re.escape(quote(path.removeprefix("wiki/"))) for path in dict.fromkeys(paths))
            links.append(rf"(?:\[r{number}\]\((?:{targets})\)|r{number})")
        return re.compile(r"\nHistory: " + r" \| ".join(links) + r"\n\n")

    def _wiki_link(self, page_id):
        """A relative link from one wiki page to another one's projected file, if it has one."""
        receipts = [row for row in self.catalog.list_artifact_projections(layer="wiki", artifact_id=page_id)
                    if row["state"] == "projected" and self._wiki_path_safe(row["projection_path"])]
        latest = max(receipts, key=lambda row: row["revision"], default=None)
        return quote(PurePosixPath(latest["projection_path"]).name) if latest else None

    def _note_headings(self, source_id, version, extraction_revision):
        """The source note's heading above each passage, or None when the note shows another extraction.

        Extraction artifacts are immutable, so the headings are kept per artifact
        checksum across pages and runs (a few artifacts at a time).
        """
        rows = self.catalog.list_extractions(source_id, version)
        if not rows or rows[-1]["extraction_revision"] != extraction_revision or not rows[-1]["artifact_sha256"]:
            return None
        key = (source_id, version, extraction_revision, rows[-1]["artifact_sha256"])
        if key in self._headings:
            return self._headings[key]
        try:
            payload = self.derived.read(source_id, version, extraction_revision, expected_sha256=rows[-1]["artifact_sha256"])
        except (ValueError, OSError, KeyError, TypeError):
            return None
        headings, current = [], None
        for segment in payload.get("segments", []):
            heading = _segment_heading(segment.get("locator", {}))
            current = current if heading is None else heading
            headings.append(current)
        if len(self._headings) >= 32:
            self._headings.pop(next(iter(self._headings)))
        self._headings[key] = headings
        return headings

    def _citation_links(self):
        """Links from a wiki page (always directly under ``wiki/``) to the projected source
        note of each cited document version, anchored at the heading above the passage."""
        notes, headings = {}, {}

        def link(citation):
            key = (citation.source_id, citation.source_version)
            if key not in notes:
                try:
                    receipt = self.catalog.get_artifact_projection("document", citation.source_id + "_note", citation.source_version)
                except ValueError:
                    receipt = None
                notes[key] = receipt["projection_path"] if receipt and receipt["state"] == "projected" else None
            if notes[key] is None:
                return None
            revision = (*key, citation.extraction_revision)
            if revision not in headings:
                headings[revision] = self._note_headings(*revision)
            passages = headings[revision] or ()
            anchor = passages[citation.passage_start] if citation.passage_start < len(passages) else None
            return quote("../" + notes[key]) + ("#" + quote(anchor, safe="") if anchor else "")
        return link

    def project_wiki(self, page_id):
        try:
            with self.vault.writer_transaction():
                return self._project_wiki_locked(page_id)
        except (VaultError, UnicodeError) as exc:
            return {"state": "conflict", "error": exc.code if isinstance(exc, VaultError) else "invalid_file"}

    def _project_wiki_locked(self, page_id):
        _identity(page_id)
        if page_id == "SCHEMA" or self.wiki_service is None:
            return {"state": "pending", "error": "wiki_unavailable"}
        page = self.catalog.get_wiki_page(page_id)
        state = page["state"] if page and page["state"] in PAGE_STATES else None
        # A state change or a newly projected link target re-renders the same revision.
        current = self.wiki_service.show(page_id, state=state, page_link=self._wiki_link,
                                         citation_link=self._citation_links())
        if not current or not current["revision"]:
            return {"state": "pending", "error": "wiki_unavailable"}
        revision = current["revision"]["revision"]
        # A page that cites only forgotten documents shows a neutral title instead of
        # theirs, and its files take a neutral name.
        title = current.get("title", page["title"])
        withdrawn = current.get("title_withdrawn", title != page["title"])
        for receipt in self.catalog.list_artifact_projections(layer="wiki", artifact_id=page_id):
            self._recover_publication(page_id, receipt["revision"], layer="wiki")
        self._recover_publication(page_id, revision, layer="wiki")
        path = self._wiki_filename(title, directory="wiki", artifact_id=page_id,
            revision=revision, legacy_path=f"wiki/{page_id}.md", withdrawn=withdrawn)
        previous = [row for row in self.catalog.list_artifact_projections(layer="wiki", artifact_id=page_id)
                    if row["last_projected_hash"]]
        old = max(previous, key=lambda row: row["revision"], default=None)
        existing_path = old["projection_path"] if old else path
        if not self._wiki_path_safe(existing_path):
            raise VaultError("unsafe_path")
        user = f"{USER_START}\n\n{USER_END}\n"
        expected = None
        try:
            existing = self.vault.read_artifact(existing_path)
        except VaultError as exc:
            if exc.code != "not_found":
                raise
        else:
            if old is None:
                return {"state": "conflict", "error": "unmanaged_file"}
            text = existing.decode("utf-8")
            # A withdrawn page is rewritten under the neutral title whatever its file names now;
            # a delete may have taken the catalog's title before the file was written again.
            titles = (page["title"], title, WITHDRAWN_TITLE, *([self._file_title(text)] if withdrawn else []))
            base = self._managed_base(text, page_id, dict.fromkeys(titles), old["revision"])
            proof = self.catalog.get_artifact_publication(self._publication_id("wiki", page_id), old["revision"])
            if base is None and existing_path == f"wiki/{page_id}.md" and (
                proof is None or proof["desired_hash"] != old["last_projected_hash"] and _hash(existing) == old["last_projected_hash"]
            ):
                # Written before front matter existed, by the frozen v1.1 renderer.
                legacy = (self.wiki_service.legacy_managed(page_id, old["revision"])
                          + self._history_links(page_id, old["revision"], legacy=True))
                base = legacy if text.startswith(legacy + USER_START + "\n") else None
            if (base is None or not text.endswith(USER_END + "\n")
                    or text.count(USER_START) != 1 or text.count(USER_END) != 1):
                self.catalog.record_wiki_review(f"projection_{page_id}_{revision}_{_hash(existing)[:16]}", page_id,
                    revision, code="manual_edit", details={"actual_hash": _hash(existing),
                        "expected_hash": old["last_projected_hash"]})
                return {"state": "conflict", "error": "managed_edit"}
            user = text[len(base):]
            expected = _hash(existing)
        results = []
        for row in self.catalog.list_wiki_revisions(page_id):
            historical = self.wiki_service.show(page_id, row["revision"])
            history_id = page_id + "_history"
            self._recover_publication(history_id, row["revision"], layer="wiki")
            history_path = self._wiki_filename(PurePosixPath(path).stem, directory="wiki/history",
                artifact_id=history_id, revision=row["revision"],
                legacy_path=f"wiki/history/{page_id}-r{row['revision']}.md", suffix=f" - r{row['revision']}",
                withdrawn=withdrawn)
            history_data = (self._wiki_metadata(page_id, historical.get("title", page["title"]), row["revision"],
                historical["managed"], history=True) + historical["managed"]).encode()
            results.append(self._project("wiki", history_id, row["revision"], history_path, history_data))
            # A hand-edited history file stays as it is; it never keeps a withdrawn page's own file showing text.
            if results[-1]["state"] != "completed" and not withdrawn:
                return {"state": "conflict", "artifacts": results, "rebuilt": sum(r["rebuilt"] for r in results)}
        data = (self._wiki_metadata(page_id, title, revision, current["managed"], state=state)
                + current["managed"] + self._history_links(page_id, revision) + user).encode()
        if old and expected is not None:
            self.catalog.set_artifact_projection("wiki", page_id, old["revision"], projection_path=existing_path,
                desired_hash=expected, state="projected", last_projected_hash=expected)
            old = self.catalog.get_artifact_projection("wiki", page_id, old["revision"])
        results.append(self._project("wiki", page_id, revision, path, data,
            expected_old=expected, previous_projection=old))
        if results[-1]["state"] != "completed":
            return {"state": "conflict", "artifacts": results, "rebuilt": sum(r["rebuilt"] for r in results)}
        for receipt in self.catalog.list_artifact_projections(layer="wiki", artifact_id=page_id):
            if receipt["revision"] != revision:
                self._finish_move(page_id, receipt["revision"], path, layer="wiki")
        index = "# Wiki\n\n"
        for listed in self.catalog.list_wiki_pages():
            receipts = [row for row in self.catalog.list_artifact_projections(layer="wiki", artifact_id=listed["page_id"])
                        if row["last_projected_hash"]]
            target = None
            for row in sorted(receipts, key=lambda row: row["revision"], reverse=True):
                if not self._wiki_path_safe(row["projection_path"]):
                    raise VaultError("unsafe_path")
                try:
                    self.vault.read_artifact(row["projection_path"])
                except VaultError as exc:
                    if exc.code != "not_found":
                        raise
                else:
                    target = row["projection_path"]
                    break
            if target is None:
                continue
            label = WITHDRAWN_TITLE if self.wiki_service.forgotten(listed["page_id"]) else listed["title"]
            label = label.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
            index += f"- [{label}]({quote('../' + target)})\n"
        results.append(self._project("wiki", "wiki_index", 1, "indices/wiki.md", index.encode()))
        return {"state": "completed" if all(r["state"] == "completed" for r in results) else "conflict",
                "artifacts": results, "rebuilt": sum(r["rebuilt"] for r in results)}

    def project_generated(self, artifact_id, path, text):
        """Write a model-free generated wiki note (brief, log) through the safe writer.

        Every write expects the bytes last projected, so a hand-edited copy is a
        projection conflict and is never overwritten.
        """
        try:
            with self.vault.writer_transaction():
                return self._project("wiki", artifact_id, 1, path, text.encode("utf-8"))
        except VaultError as exc:
            return {"state": "conflict", "error": exc.code}

    def _history_links(self, page_id, revision, *, legacy=False):
        links = []
        for number in range(1, revision + 1):
            receipt = None if legacy else self.catalog.get_artifact_projection("wiki", page_id + "_history", number)
            if receipt and receipt["state"] == "conflict":
                # A hand-edited history file keeps its name, which may be a withdrawn title.
                links.append(f"r{number}")
                continue
            path = receipt["projection_path"] if receipt else f"wiki/history/{page_id}-r{number}.md"
            if not self._wiki_path_safe(path, history=True):
                raise VaultError("unsafe_path")
            target = path.removeprefix("wiki/")
            links.append(f"[r{number}]({target if legacy else quote(target)})")
        return "\nHistory: " + " | ".join(links) + "\n\n"

    def project_wiki_pages(self, page_ids):
        """Project wiki pages; a second pass when one is new, so links between them resolve."""
        page_ids = list(dict.fromkeys(page_ids))
        new = any(not any(row["state"] == "projected" for row in
                          self.catalog.list_artifact_projections(layer="wiki", artifact_id=page_id))
                  for page_id in page_ids)
        results = [self.project_wiki(page_id) for page_id in page_ids]
        if new and len(page_ids) > 1 and all(result["state"] == "completed" for result in results):
            results = [self.project_wiki(page_id) for page_id in page_ids]
        return results

    def project_document_with_wiki(self, source_id, version):
        """Project a document, then the wiki pages any revision of which cites it.

        Pages that just dropped the document's claims, or turned stale because it
        changed, are re-rendered too, history included. The index is projected last
        and best-effort: it may have changed without citing this document, and a
        hand-edited index must not hold the document's own pages back.
        """
        outcome = self.project_document(source_id, version)
        if outcome["state"] != "completed":
            return outcome
        for projected in self.project_wiki_citing(source_id):
            if projected["state"] != "completed":
                return projected
        return outcome

    def project_wiki_citing(self, source_id):
        """Project the wiki pages any revision of which cites the source, and their results.

        The index is projected last and best-effort, and not returned: it may have
        changed without citing the source, and a hand-edited index must not hold the
        source's pages back. After a forget or delete this rewrites, under neutral
        names, every vault file that showed the source.
        """
        index = self.catalog.get_wiki_page("index")
        pages = [page_id for page_id in self.catalog.list_wiki_pages_citing(source_id, any_revision=True) if page_id != "index"]
        results = self.project_wiki_pages(pages)
        if index is not None and index["current_revision"] and index["state"] != "conflict":
            try:
                projected = self.project_wiki("index")
            except Exception as exc:  # noqa: BLE001 -- reconcile repairs the index projection later
                projected = {"state": "conflict", "error": type(exc).__name__}
            if projected["state"] != "completed":
                logging.getLogger(__name__).warning("Wiki index projection deferred: %s", projected.get("error"))
        return results

    def reconcile_documents(self, *, documents=None, wiki_limit=4096):
        if documents is None:
            documents = self.catalog.list_documents(limit=4096)
        reports = []
        for document in documents:
            try:
                reports.append(self.project_document(document["source_id"], document["source_version"]))
            except Exception:  # noqa: BLE001 -- each artifact is reconciled independently
                reports.append({"state": "conflict", "error": "projection_failed"})
        if self.wiki_service is not None:
            for page in self.catalog.list_wiki_pages()[:wiki_limit]:
                try:
                    reports.append(self.project_wiki(page["page_id"]))
                except Exception:  # noqa: BLE001 -- each artifact is reconciled independently
                    reports.append({"state": "conflict", "error": "projection_failed"})
        return {"state": "completed" if all(r["state"] == "completed" for r in reports) else "conflict",
                "reports": reports, "rebuilt": sum(r.get("rebuilt", 0) for r in reports),
                "conflicts": sum(r["state"] == "conflict" for r in reports)}
