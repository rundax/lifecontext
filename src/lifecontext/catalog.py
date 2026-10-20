"""Transactional metadata catalog for local custody and reconciliation."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import sqlite3
import stat
import uuid
from datetime import date
from collections.abc import Iterator
from pathlib import Path, PurePosixPath
from typing import Any

from lifecontext.custody import SourceManifest
from lifecontext.paths import LifeContextPaths


_SCHEMA_VERSION = 9
_SOURCE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_IDEMPOTENCY_KEY = re.compile(r"[\x21-\x7e]{1,255}\Z")
_OPERATION_ID = re.compile(r"op_[0-9a-f]{32}\Z")
_EDGE_ID = re.compile(r"edge_[0-9a-f]{64}\Z")
_RELATIONSHIP_REVISION = re.compile(r"(?:[1-9][0-9]*:[0-9a-f]{64}|[0-9a-f]{64})\Z")
_RELATIONSHIP_NOTE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_RESOLVED_AT = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z")
_CATALOG_TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}\Z")
# One document version's lifecycle over the SQL columns {sid} and {version}, as the
# wiki's ``source_withdrawn`` decides it: 2 when the source was deleted, 1 when it was
# forgotten (a tombstone, or a version that can no longer be recalled: a document
# forget writes no tombstone), else 0.
_VERSION_GONE = (
    "(CASE WHEN EXISTS(SELECT 1 FROM tombstones t WHERE t.source_id={sid} AND t.tombstone_type='deleted') "
    "OR EXISTS(SELECT 1 FROM transfer_tombstones t WHERE t.source_id={sid} AND t.tombstone_type='deleted') THEN 2 "
    "WHEN EXISTS(SELECT 1 FROM tombstones t WHERE t.source_id={sid}) "
    "OR EXISTS(SELECT 1 FROM transfer_tombstones t WHERE t.source_id={sid}) "
    "OR EXISTS(SELECT 1 FROM provider_states p WHERE p.source_id={sid} AND p.source_version={version} "
    "AND p.recall_visibility='not_visible') THEN 1 ELSE 0 END)")
_GONE_NAME = "CASE {} WHEN 2 THEN 'deleted' WHEN 1 THEN 'forgotten' END"


def _page_gone(page: str, revision: str) -> str:
    """SQL naming what a page revision may show of its title: a document's own page
    takes the worst lifecycle of the versions it cites (NULL, forgotten or deleted);
    any other page is forgotten once every version it cites is gone."""
    level = _VERSION_GONE.format(sid="d.source_id", version="d.source_version")
    rows = f"FROM wiki_dependencies d WHERE d.page_id={page} AND d.revision={revision}"
    return (f"CASE WHEN {page} LIKE 'source\\_%' ESCAPE '\\' THEN (SELECT {_GONE_NAME.format(f'max({level})')} {rows}) "
            f"WHEN (SELECT min({level}) {rows})>0 THEN 'forgotten' END")
_PROVIDER_SUBMISSION_STATES = {"pending", "submitted", "failed"}
_PROVIDER_COMPLETION_STATES = {"unknown", "pending", "completed", "failed"}
_RECALL_VISIBILITY_STATES = {"unknown", "visible", "not_visible"}
_PROJECTION_STATES = {"not_projected", "pending", "projected", "conflict", "failed"}
_RECONCILIATION_FACTS = {"missing_original", "corrupt_original", "stale_projection"}

_SCHEMA = (
    """
    CREATE TABLE sources (
        source_id TEXT PRIMARY KEY,
        lifecycle_state TEXT NOT NULL DEFAULT 'active'
            CHECK (lifecycle_state IN ('active')),
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE source_versions (
        source_id TEXT NOT NULL,
        source_version INTEGER NOT NULL CHECK (source_version > 0),
        sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
        size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
        provenance_json TEXT NOT NULL,
        manifest_path TEXT NOT NULL,
        manifest_json TEXT NOT NULL,
        PRIMARY KEY (source_id, source_version),
        FOREIGN KEY (source_id) REFERENCES sources(source_id)
    )
    """,
    """
    CREATE TABLE provider_states (
        source_id TEXT NOT NULL,
        source_version INTEGER NOT NULL,
        submission_state TEXT NOT NULL DEFAULT 'pending'
            CHECK (submission_state IN ('pending', 'submitted', 'failed')),
        completion_state TEXT NOT NULL DEFAULT 'unknown'
            CHECK (completion_state IN ('unknown', 'pending', 'completed', 'failed')),
        recall_visibility TEXT NOT NULL DEFAULT 'unknown'
            CHECK (recall_visibility IN ('unknown', 'visible', 'not_visible')),
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (source_id, source_version),
        FOREIGN KEY (source_id, source_version)
            REFERENCES source_versions(source_id, source_version)
    )
    """,
    """
    CREATE TABLE projection_states (
        source_id TEXT NOT NULL,
        source_version INTEGER NOT NULL,
        state TEXT NOT NULL DEFAULT 'not_projected'
            CHECK (state IN ('not_projected', 'pending', 'projected', 'conflict', 'failed')),
        projection_path TEXT,
        desired_hash TEXT,
        last_projected_hash TEXT,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (source_id, source_version),
        FOREIGN KEY (source_id, source_version)
            REFERENCES source_versions(source_id, source_version)
    )
    """,
    """
    CREATE TABLE reconciliation_facts (
        fact_id INTEGER PRIMARY KEY,
        source_id TEXT NOT NULL,
        source_version INTEGER NOT NULL,
        fact TEXT NOT NULL CHECK (fact IN (
            'missing_original', 'corrupt_original', 'stale_projection'
        )),
        expected_hash TEXT,
        actual_hash TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (source_id, source_version)
            REFERENCES source_versions(source_id, source_version)
    )
    """,
    "CREATE INDEX source_versions_hash_idx ON source_versions(sha256)",
    "CREATE INDEX reconciliation_facts_source_idx ON reconciliation_facts(source_id, source_version, fact_id)",
)
_OPERATION_STATES = (
    "pending",
    "running",
    "retryable_failed",
    "permanent_failed",
    "completed",
    "cancelled",
)
_CURATION_ACTIONS = (
    "correct",
    "reindex",
    "prepare_forget",
    "forget",
    "prepare_delete",
    "delete",
)
_TOMBSTONE_TYPES = ("forgotten", "deleted")
_TOMBSTONE_PROGRESS_STATES = {"pending", "running", "completed", "failed"}
_TOMBSTONE_PROGRESS_FIELDS = {
    "provider_delete", "projection_delete", "custody_delete",
}
_LEGACY_OPERATION_KINDS = ("retain", "curation", "relationship_scan", "relationship_decision")
_V5_OPERATION_KINDS = (*_LEGACY_OPERATION_KINDS, "import")
_V8_OPERATION_KINDS = (*_V5_OPERATION_KINDS, "document_import", "document_retry", "wiki_rebuild", "category_decision")
_OPERATION_KINDS = (*_V8_OPERATION_KINDS, "wiki_review")
_OPERATION_TRANSITIONS = {
    "pending": {"running", "permanent_failed", "cancelled"},
    "running": {"retryable_failed", "permanent_failed", "completed"},
    "retryable_failed": {"running", "permanent_failed", "completed", "cancelled"},
    "permanent_failed": set(),
    "completed": set(),
    "cancelled": set(),
}
_OPERATION_ERROR_CODES = {
    "invalid_request", "body_too_large", "response_too_large", "host_not_allowed",
    "origin_not_allowed", "source_not_found", "source_conflict", "evidence_unavailable",
    "invalid_source_text", "invalid_provider_response", "provider_unavailable",
    "retain_failed", "recall_failed", "reflect_failed", "unattributed_evidence",
    "result_limit_exceeded", "operation_status_unavailable", "curation_unavailable",
    "transfer_unavailable", "conflict", "relationship_unavailable", "unsafe_path",
    "unsafe_permissions", "vault_unavailable", "internal_error",
}
_OPERATION_RESULT_FIELDS = {
    "sources", "item_count", "size_bytes", "error_code", "recovery_intent"
}
_MAX_OPERATION_RESULT_BYTES = 4_194_304
_RELATIONSHIP_REVIEW_CODES = {
    "ambiguous_target", "invalid_frontmatter", "invalid_journal_entry", "invalid_markdown",
    "ledger_conflict", "missing_heading", "missing_target", "oversized_file",
    "unreadable_directory", "unreadable_file",
}
_RELATIONSHIP_EDGE_KEYS = {
    "edge_id", "revision", "from", "to", "relation", "status", "source_revision",
    "target_revision", "provenance", "evidence_id", "decision_principal",
}
_RELATIONSHIP_EDGE_REQUIRED_KEYS = _RELATIONSHIP_EDGE_KEYS - {"decision_principal"}
_RELATIONSHIP_REVIEW_KEYS = {"code", "source", "hint_sha256", "edge_id"}
_RECOVERABLE_OPERATION_STATES = ("pending", "running", "retryable_failed")
_SCHEMA_V2 = (
    """
    CREATE TABLE operations (
        operation_id TEXT PRIMARY KEY NOT NULL CHECK (
            length(operation_id) = 35
            AND substr(operation_id, 1, 3) = 'op_'
            AND substr(operation_id, 4) NOT GLOB '*[^0-9a-f]*'
        ),
        idempotency_key_digest TEXT NOT NULL UNIQUE
            CHECK (length(idempotency_key_digest) = 64
                AND idempotency_key_digest NOT GLOB '*[^0-9a-f]*'),
        request_digest TEXT NOT NULL
            CHECK (length(request_digest) = 64
                AND request_digest NOT GLOB '*[^0-9a-f]*'),
        operation_kind TEXT NOT NULL
            CHECK (operation_kind IN (
                'retain', 'curation', 'relationship_scan', 'relationship_decision'
            )),
        state TEXT NOT NULL CHECK (state IN (
            'pending', 'running', 'retryable_failed', 'permanent_failed',
            'completed', 'cancelled'
        )),
        result_json TEXT NOT NULL DEFAULT '{}' CHECK (length(result_json) <= 65536),
        error_code TEXT CHECK (error_code IS NULL OR (
            length(error_code) BETWEEN 1 AND 64
            AND error_code NOT GLOB '*[^a-z0-9_]*'
            AND substr(error_code, 1, 1) GLOB '[a-z]'
        )),
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE curation_history (
        history_id INTEGER PRIMARY KEY AUTOINCREMENT,
        operation_id TEXT NOT NULL UNIQUE,
        source_id TEXT NOT NULL,
        source_version INTEGER NOT NULL CHECK (source_version > 0),
        action TEXT NOT NULL CHECK (action IN (
            'correct', 'reindex', 'prepare_forget', 'forget', 'prepare_delete', 'delete'
        )),
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (operation_id) REFERENCES operations(operation_id),
        FOREIGN KEY (source_id, source_version)
            REFERENCES source_versions(source_id, source_version)
    )
    """,
    """
    CREATE TABLE tombstones (
        source_id TEXT NOT NULL,
        source_version INTEGER NOT NULL CHECK (source_version > 0),
        tombstone_type TEXT NOT NULL CHECK (tombstone_type IN ('forgotten', 'deleted')),
        operation_id TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (source_id, source_version),
        FOREIGN KEY (operation_id) REFERENCES operations(operation_id),
        FOREIGN KEY (source_id, source_version)
            REFERENCES source_versions(source_id, source_version)
    )
    """,
    "CREATE INDEX operations_state_created_idx ON operations(state, created_at, operation_id)",
    "CREATE INDEX curation_history_source_idx ON curation_history(source_id, source_version, history_id)",
)
_SCHEMA_V3 = (
    """
    CREATE TABLE operations_v3 (
        operation_id TEXT PRIMARY KEY NOT NULL CHECK (
            length(operation_id) = 35
            AND substr(operation_id, 1, 3) = 'op_'
            AND substr(operation_id, 4) NOT GLOB '*[^0-9a-f]*'
        ),
        idempotency_key_digest TEXT NOT NULL UNIQUE
            CHECK (length(idempotency_key_digest) = 64
                AND idempotency_key_digest NOT GLOB '*[^0-9a-f]*'),
        request_digest TEXT NOT NULL
            CHECK (length(request_digest) = 64
                AND request_digest NOT GLOB '*[^0-9a-f]*'),
        operation_kind TEXT NOT NULL
            CHECK (operation_kind IN (
                'retain', 'curation', 'relationship_scan', 'relationship_decision'
            )),
        state TEXT NOT NULL CHECK (state IN (
            'pending', 'running', 'retryable_failed', 'permanent_failed',
            'completed', 'cancelled'
        )),
        result_json TEXT NOT NULL DEFAULT '{}'
            CHECK (length(CAST(result_json AS BLOB)) <= 4194304),
        error_code TEXT CHECK (error_code IS NULL OR (
            length(error_code) BETWEEN 1 AND 64
            AND error_code NOT GLOB '*[^a-z0-9_]*'
            AND substr(error_code, 1, 1) GLOB '[a-z]'
        )),
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """INSERT INTO operations_v3(
           operation_id, idempotency_key_digest, request_digest, operation_kind,
           state, result_json, error_code, created_at, updated_at
       ) SELECT operation_id, idempotency_key_digest, request_digest, operation_kind,
                state, result_json, error_code, created_at, updated_at FROM operations""",
    "DROP TABLE operations",
    "ALTER TABLE operations_v3 RENAME TO operations",
    "CREATE INDEX operations_state_created_idx ON operations(state, created_at, operation_id)",
    "PRAGMA user_version=3",
)
_SCHEMA_V1_COLUMNS = {
    "sources": {"source_id", "lifecycle_state", "created_at"},
    "source_versions": {
        "source_id", "source_version", "sha256", "size_bytes",
        "provenance_json", "manifest_path", "manifest_json",
    },
    "provider_states": {
        "source_id", "source_version", "submission_state", "completion_state",
        "recall_visibility", "updated_at",
    },
    "projection_states": {
        "source_id", "source_version", "state", "projection_path", "desired_hash",
        "last_projected_hash", "updated_at",
    },
    "reconciliation_facts": {
        "fact_id", "source_id", "source_version", "fact", "expected_hash",
        "actual_hash", "created_at",
    },
}
_SCHEMA_V2_COLUMNS = {
    **_SCHEMA_V1_COLUMNS,
    "operations": {
        "operation_id", "idempotency_key_digest", "request_digest", "operation_kind",
        "state", "result_json", "error_code", "created_at", "updated_at",
    },
    "curation_history": {
        "history_id", "operation_id", "source_id", "source_version", "action", "created_at",
    },
    "tombstones": {
        "source_id", "source_version", "tombstone_type", "operation_id", "created_at",
    },
}
_SCHEMA_V3_COLUMNS = _SCHEMA_V2_COLUMNS
_SCHEMA_V4_COLUMNS = {
    **_SCHEMA_V1_COLUMNS,
    "operations": _SCHEMA_V2_COLUMNS["operations"],
    "curation_history": _SCHEMA_V2_COLUMNS["curation_history"] | {"prepared_operation_id"},
    "tombstones": _SCHEMA_V2_COLUMNS["tombstones"] | {"progress_json"},
}
_SCHEMA_V5_COLUMNS = {
    **_SCHEMA_V4_COLUMNS,
    "transfers": {"operation_id", "bundle_digest", "item_count", "size_bytes"},
    "transfer_items": {
        "operation_id", "source_id", "source_version", "lifecycle_state",
        "sha256", "state",
    },
    "transfer_tombstones": {
        "source_id", "source_version", "tombstone_type", "operation_id", "created_at",
    },
}
_TRANSFER_STATES = (
    "pending", "custody_written", "catalog_registered", "projected",
    "provider_submitted", "tombstoned", "completed",
)
_TRANSFER_STATE_ORDER = {state: index for index, state in enumerate(_TRANSFER_STATES)}
_LIFECYCLE_STATES = {"active", "forgotten", "deleted"}


# V6 artifacts are references to managed files, never custody or operation payloads.
_DOCUMENT_STAGE_STATES = {
    "custody": {"pending", "running", "accepted", "failed"},
    "extraction": {"pending", "running", "blocked", "failed", "completed"},
    "indexing": {"pending", "running", "failed", "completed"},
    "wiki": {"pending", "running", "failed", "conflict", "completed"},
    "projection": {"pending", "running", "failed", "conflict", "completed"},
}
_SCHEMA_V6 = (
    """CREATE TABLE categories (
        category_id TEXT PRIMARY KEY NOT NULL,
        name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 256),
        name_key TEXT NOT NULL UNIQUE,
        description TEXT NOT NULL DEFAULT '' CHECK (length(description) <= 4096),
        aliases_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(aliases_json)),
        revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE TABLE document_metadata (
        source_id TEXT NOT NULL, source_version INTEGER NOT NULL CHECK (source_version > 0),
        filename TEXT NOT NULL CHECK (length(filename) BETWEEN 1 AND 255),
        title TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 1024),
        mime_type TEXT NOT NULL CHECK (length(mime_type) BETWEEN 3 AND 255),
        PRIMARY KEY (source_id, source_version),
        FOREIGN KEY (source_id, source_version) REFERENCES source_versions(source_id, source_version)
    )""",
    """CREATE TABLE document_categories (
        source_id TEXT PRIMARY KEY NOT NULL,
        category_id TEXT NOT NULL DEFAULT 'inbox',
        revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
        FOREIGN KEY (source_id) REFERENCES sources(source_id),
        FOREIGN KEY (category_id) REFERENCES categories(category_id)
    )""",
    """CREATE TABLE extractions (
        source_id TEXT NOT NULL, source_version INTEGER NOT NULL CHECK (source_version > 0),
        extraction_revision INTEGER NOT NULL CHECK (extraction_revision > 0),
        original_sha256 TEXT NOT NULL CHECK (length(original_sha256)=64 AND original_sha256 NOT GLOB '*[^0-9a-f]*'),
        extractor TEXT NOT NULL CHECK (length(extractor) BETWEEN 1 AND 256),
        extractor_version TEXT NOT NULL CHECK (length(extractor_version) BETWEEN 1 AND 256),
        config_sha256 TEXT NOT NULL CHECK (length(config_sha256)=64 AND config_sha256 NOT GLOB '*[^0-9a-f]*'),
        artifact_path TEXT, artifact_sha256 TEXT CHECK (artifact_sha256 IS NULL OR (length(artifact_sha256)=64 AND artifact_sha256 NOT GLOB '*[^0-9a-f]*')),
        status TEXT NOT NULL CHECK (status IN ('completed','blocked','failed','unsupported','locked','limit_exceeded')),
        warnings_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(warnings_json)),
        passage_count INTEGER NOT NULL CHECK (passage_count >= 0),
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        CHECK ((artifact_path IS NULL) = (artifact_sha256 IS NULL)),
        CHECK (status != 'completed' OR (artifact_path IS NOT NULL AND passage_count > 0)),
        PRIMARY KEY (source_id,source_version,extraction_revision),
        FOREIGN KEY (source_id,source_version) REFERENCES document_metadata(source_id,source_version)
    )""",
    """CREATE TABLE document_stages (
        source_id TEXT NOT NULL, source_version INTEGER NOT NULL CHECK (source_version > 0),
        stage TEXT NOT NULL CHECK (stage IN ('custody','extraction','indexing','wiki','projection')),
        state TEXT NOT NULL CHECK (state IN ('pending','running','accepted','blocked','failed','conflict','completed')),
        revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
        attempt INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
        error_code TEXT, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        CHECK (error_code IS NULL OR (length(error_code) BETWEEN 1 AND 64 AND error_code NOT GLOB '*[^a-z0-9_]*')),
        CHECK (state IN ('pending','running','failed') OR
            (stage='custody' AND state='accepted') OR
            (stage='extraction' AND state IN ('blocked','completed')) OR
            (stage IN ('indexing','wiki','projection') AND state='completed') OR
            (stage IN ('wiki','projection') AND state='conflict')),
        PRIMARY KEY (source_id,source_version,stage),
        FOREIGN KEY (source_id,source_version) REFERENCES document_metadata(source_id,source_version)
    )""",
    """CREATE TABLE provider_chunks (
        provider_id TEXT PRIMARY KEY NOT NULL CHECK (length(provider_id) BETWEEN 1 AND 512),
        source_id TEXT NOT NULL, source_version INTEGER NOT NULL,
        extraction_revision INTEGER NOT NULL,
        passage_start INTEGER NOT NULL CHECK (passage_start >= 0),
        passage_end INTEGER NOT NULL CHECK (passage_end > passage_start),
        content_sha256 TEXT NOT NULL CHECK (length(content_sha256)=64 AND content_sha256 NOT GLOB '*[^0-9a-f]*'),
        submission_state TEXT NOT NULL DEFAULT 'pending' CHECK (submission_state IN ('pending','submitted','failed','retired')),
        completion_state TEXT NOT NULL DEFAULT 'unknown' CHECK (completion_state IN ('unknown','pending','completed','failed')),
        revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        CHECK (completion_state != 'completed' OR submission_state IN ('submitted','retired')),
        UNIQUE (source_id,source_version,extraction_revision,passage_start,passage_end),
        FOREIGN KEY (source_id,source_version,extraction_revision) REFERENCES extractions(source_id,source_version,extraction_revision)
    )""",
    """CREATE TABLE category_proposals (
        proposal_id TEXT PRIMARY KEY NOT NULL, name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 256),
        name_key TEXT NOT NULL, description TEXT NOT NULL CHECK (length(description) <= 4096),
        rationale TEXT NOT NULL CHECK (length(rationale) <= 4096),
        closest_category_id TEXT, category_id TEXT,
        state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','approved','rejected','redirected')),
        revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (closest_category_id) REFERENCES categories(category_id),
        FOREIGN KEY (category_id) REFERENCES categories(category_id),
        CHECK ((state IN ('approved','redirected')) = (category_id IS NOT NULL))
    )""",
    "CREATE UNIQUE INDEX category_proposals_pending_idx ON category_proposals(name_key) WHERE state='pending'",
    """CREATE TABLE category_proposal_documents (
        proposal_id TEXT NOT NULL, source_id TEXT NOT NULL, source_version INTEGER NOT NULL,
        PRIMARY KEY (proposal_id,source_id,source_version),
        FOREIGN KEY (proposal_id) REFERENCES category_proposals(proposal_id),
        FOREIGN KEY (source_id,source_version) REFERENCES document_metadata(source_id,source_version)
    )""",
    """CREATE TABLE wiki_pages (
        page_id TEXT PRIMARY KEY NOT NULL, title TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 1024),
        current_revision INTEGER NOT NULL DEFAULT 0 CHECK (current_revision >= 0),
        state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','current','stale','conflict')),
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE TABLE wiki_revisions (
        page_id TEXT NOT NULL, revision INTEGER NOT NULL CHECK (revision > 0),
        parent_revision INTEGER CHECK (parent_revision IS NULL OR (parent_revision > 0 AND parent_revision < revision)),
        artifact_path TEXT NOT NULL,
        artifact_sha256 TEXT NOT NULL CHECK (length(artifact_sha256)=64 AND artifact_sha256 NOT GLOB '*[^0-9a-f]*'),
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (page_id,revision),
        FOREIGN KEY (page_id) REFERENCES wiki_pages(page_id),
        FOREIGN KEY (page_id,parent_revision) REFERENCES wiki_revisions(page_id,revision)
    )""",
    """CREATE TABLE wiki_dependencies (
        page_id TEXT NOT NULL, revision INTEGER NOT NULL,
        source_id TEXT NOT NULL, source_version INTEGER NOT NULL, extraction_revision INTEGER NOT NULL,
        passage_start INTEGER NOT NULL CHECK (passage_start >= 0),
        passage_end INTEGER NOT NULL CHECK (passage_end > passage_start),
        original_sha256 TEXT NOT NULL CHECK (length(original_sha256)=64 AND original_sha256 NOT GLOB '*[^0-9a-f]*'),
        PRIMARY KEY (page_id,revision,source_id,source_version,extraction_revision,passage_start,passage_end),
        FOREIGN KEY (page_id,revision) REFERENCES wiki_revisions(page_id,revision),
        FOREIGN KEY (source_id,source_version,extraction_revision) REFERENCES extractions(source_id,source_version,extraction_revision)
    )""",
    """CREATE TABLE wiki_reviews (
        review_id TEXT PRIMARY KEY NOT NULL, page_id TEXT NOT NULL, revision INTEGER NOT NULL,
        code TEXT NOT NULL CHECK (code IN ('manual_edit','content_conflict','contradiction')),
        details_json TEXT NOT NULL CHECK (json_valid(details_json) AND length(CAST(details_json AS BLOB)) <= 65536),
        state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','resolved')),
        FOREIGN KEY (page_id,revision) REFERENCES wiki_revisions(page_id,revision)
    )""",
    """CREATE TABLE artifact_projections (
        layer TEXT NOT NULL CHECK (layer IN ('journal','document','wiki')),
        artifact_id TEXT NOT NULL, revision INTEGER NOT NULL CHECK (revision > 0),
        projection_path TEXT NOT NULL, desired_hash TEXT NOT NULL CHECK (length(desired_hash)=64 AND desired_hash NOT GLOB '*[^0-9a-f]*'),
        last_projected_hash TEXT CHECK (last_projected_hash IS NULL OR (length(last_projected_hash)=64 AND last_projected_hash NOT GLOB '*[^0-9a-f]*')),
        state TEXT NOT NULL CHECK (state IN ('not_projected','pending','projected','conflict','failed')),
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        CHECK (state != 'projected' OR (last_projected_hash IS NOT NULL AND last_projected_hash=desired_hash)),
        CHECK (length(projection_path) > 0 AND substr(projection_path,1,1) != '/' AND instr(projection_path,char(92))=0 AND instr(projection_path,'//')=0 AND instr('/'||projection_path||'/','/../')=0 AND instr('/'||projection_path||'/','/./')=0),
        PRIMARY KEY (layer,artifact_id,revision)
    )""",
)


_SCHEMA_V7 = ("""CREATE TABLE artifact_publications (
    artifact_id TEXT NOT NULL, revision INTEGER NOT NULL CHECK (revision > 0),
    projection_path TEXT NOT NULL,
    desired_hash TEXT NOT NULL CHECK (length(desired_hash)=64 AND desired_hash NOT GLOB '*[^0-9a-f]*'),
    device INTEGER CHECK (device IS NULL OR device >= 0), inode INTEGER CHECK (inode IS NULL OR inode > 0),
    staged_name TEXT NOT NULL, state TEXT NOT NULL CHECK (state IN ('planned','pending','retired','completed')),
    CHECK ((device IS NULL) = (inode IS NULL)),
    CHECK (state != 'pending' OR device IS NOT NULL),
    CHECK (state != 'planned' OR device IS NULL),
    CHECK (length(projection_path) > 0 AND substr(projection_path,1,1) != '/' AND instr(projection_path,char(92))=0 AND instr(projection_path,'//')=0 AND instr('/'||projection_path||'/','/../')=0 AND instr('/'||projection_path||'/','/./')=0),
    PRIMARY KEY (artifact_id,revision)
)""",)


_SCHEMA_V8 = ("""CREATE TABLE journal_projection_moves (
    source_id TEXT NOT NULL,
    source_version INTEGER NOT NULL CHECK (source_version > 0),
    old_path TEXT NOT NULL,
    new_path TEXT NOT NULL UNIQUE,
    old_hash TEXT NOT NULL CHECK (length(old_hash)=64 AND old_hash NOT GLOB '*[^0-9a-f]*'),
    new_hash TEXT NOT NULL CHECK (length(new_hash)=64 AND new_hash NOT GLOB '*[^0-9a-f]*'),
    CHECK (old_path != new_path),
    PRIMARY KEY (source_id,source_version),
    FOREIGN KEY (source_id,source_version) REFERENCES source_versions(source_id,source_version)
)""",)


# The owner's note on a resolved review; the review itself and its claims never change.
_SCHEMA_V9 = ("""CREATE TABLE wiki_review_resolutions (
    review_id TEXT PRIMARY KEY NOT NULL,
    note TEXT NOT NULL CHECK (length(CAST(note AS BLOB)) BETWEEN 1 AND 4096),
    resolved_at TEXT NOT NULL CHECK (resolved_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'),
    FOREIGN KEY (review_id) REFERENCES wiki_reviews(review_id)
)""",)


class CatalogError(ValueError):
    """Stable, redacted catalog prerequisite or operation failure."""


class CatalogConflict(CatalogError):
    """A source/version identity is already registered with different data."""


class OperationConflict(CatalogConflict):
    """A durable operation key or state conflicts with an existing operation."""


class CatalogBusy(CatalogError):
    """Another writer held the catalog past the busy timeout; the same request may be retried."""


def _validate_identity(source_id: str, source_version: int) -> tuple[str, int]:
    if not isinstance(source_id, str) or not _SOURCE_ID.fullmatch(source_id):
        raise CatalogError("invalid catalog identity")
    if (
        isinstance(source_version, bool)
        or not isinstance(source_version, int)
        or not 1 <= source_version <= 2_147_483_647
    ):
        raise CatalogError("invalid catalog identity")
    return source_id, source_version


def _validate_hash(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise CatalogError("invalid catalog hash")
    return value


def _validate_tombstone_progress(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != _TOMBSTONE_PROGRESS_FIELDS:
        raise CatalogError("invalid tombstone progress")
    if any(state not in _TOMBSTONE_PROGRESS_STATES for state in value.values()):
        raise CatalogError("invalid tombstone progress")
    return dict(value)


def _utf8_size(value: str, maximum: int) -> int:
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError:
        raise CatalogError("invalid operation result") from None
    if size > maximum:
        raise CatalogError("invalid operation result")
    return size


def _has_control_characters(value: str) -> bool:
    return any(ord(char) < 32 or 0x7F <= ord(char) <= 0x9F for char in value)


def _validate_relationship_reference(value: Any, *, review_source: bool = False) -> str:
    if not isinstance(value, str) or not value:
        raise CatalogError("invalid operation result")
    if review_source:
        if len(value) > 1024:
            raise CatalogError("invalid operation result")
        _utf8_size(value, 4096)
    else:
        _utf8_size(value, 1024)
    kind, separator, identity = value.partition(":")
    if not separator or kind not in {"journal", "note", "path"} or not identity:
        raise CatalogError("invalid operation result")
    if "\\" in identity or identity.startswith("/") or _has_control_characters(identity):
        raise CatalogError("invalid operation result")
    if kind == "journal" and review_source and not _SOURCE_ID.fullmatch(identity):
        raise CatalogError("invalid operation result")
    if kind == "note" and review_source and not _RELATIONSHIP_NOTE_ID.fullmatch(identity):
        raise CatalogError("invalid operation result")
    if kind == "path" and any(part in {"", ".", ".."} for part in identity.split("/")):
        raise CatalogError("invalid operation result")
    return value


def _validate_relationship_edge(value: Any) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or _RELATIONSHIP_EDGE_REQUIRED_KEYS - set(value)
        or set(value) - _RELATIONSHIP_EDGE_KEYS
    ):
        raise CatalogError("invalid operation result")
    edge_id = value["edge_id"]
    revision = value["revision"]
    relation = value["relation"]
    status = value["status"]
    source_revision = value["source_revision"]
    target_revision = value["target_revision"]
    provenance = value["provenance"]
    evidence_id = value["evidence_id"]
    principal = value.get("decision_principal")
    if not isinstance(edge_id, str) or not _EDGE_ID.fullmatch(edge_id):
        raise CatalogError("invalid operation result")
    if (
        isinstance(revision, bool)
        or not isinstance(revision, int)
        or not 1 <= revision <= 2_147_483_647
    ):
        raise CatalogError("invalid operation result")
    if value["from"] != _validate_relationship_reference(value["from"]):
        raise CatalogError("invalid operation result")
    if value["to"] != _validate_relationship_reference(value["to"]):
        raise CatalogError("invalid operation result")
    if relation not in {
        "references", "about", "same_topic", "follows", "supports", "contradicts", "supersedes"
    }:
        raise CatalogError("invalid operation result")
    if status not in {"proposed", "approved", "rejected"}:
        raise CatalogError("invalid operation result")
    if (
        not isinstance(source_revision, str)
        or not _RELATIONSHIP_REVISION.fullmatch(source_revision)
        or not isinstance(target_revision, str)
        or not _RELATIONSHIP_REVISION.fullmatch(target_revision)
    ):
        raise CatalogError("invalid operation result")
    if provenance != "wikilink":
        raise CatalogError("invalid operation result")
    if not isinstance(evidence_id, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", evidence_id):
        raise CatalogError("invalid operation result")
    if principal is not None:
        if not isinstance(principal, str):
            raise CatalogError("invalid operation result")
        _utf8_size(principal, 256)
    return dict(value)


def _validate_relationship_review_item(value: Any) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or set(value) - _RELATIONSHIP_REVIEW_KEYS
        or "code" not in value
        or not isinstance(value["code"], str)
        or value["code"] not in _RELATIONSHIP_REVIEW_CODES
    ):
        raise CatalogError("invalid operation result")
    result = {"code": value["code"]}
    if "source" in value:
        source = value["source"]
        result["source"] = (
            None if source is None else _validate_relationship_reference(source, review_source=True)
        )
    if "hint_sha256" in value:
        hint = value["hint_sha256"]
        if hint is not None and (not isinstance(hint, str) or not _SHA256.fullmatch(hint)):
            raise CatalogError("invalid operation result")
        result["hint_sha256"] = hint
    if "edge_id" in value:
        edge_id = value["edge_id"]
        if edge_id is not None and (not isinstance(edge_id, str) or not _EDGE_ID.fullmatch(edge_id)):
            raise CatalogError("invalid operation result")
        result["edge_id"] = edge_id
    return result


def _validate_relationship_scan(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"edges", "review", "scanned_files", "truncated"}:
        raise CatalogError("invalid operation result")
    edges = value["edges"]
    review = value["review"]
    scanned_files = value["scanned_files"]
    truncated = value["truncated"]
    if not isinstance(edges, list) or len(edges) > 100:
        raise CatalogError("invalid operation result")
    if not isinstance(review, list) or len(review) > 100:
        raise CatalogError("invalid operation result")
    if (
        isinstance(scanned_files, bool)
        or not isinstance(scanned_files, int)
        or not 0 <= scanned_files <= 20_000
        or not isinstance(truncated, bool)
    ):
        raise CatalogError("invalid operation result")
    return {
        "edges": [_validate_relationship_edge(edge) for edge in edges],
        "review": [_validate_relationship_review_item(item) for item in review],
        "scanned_files": scanned_files,
        "truncated": truncated,
    }


def _validate_category_result(value: Any) -> dict[str, Any]:
    shapes = {
        "classification": ({"kind", "status", "category_id"}, {"proposal_id", "revision", "budget_exceeded"}),
        "proposal": ({"kind", "proposal_id", "revision", "state", "category_id"}, set()),
        "category": ({"kind", "category_id", "revision"}, set()),
        "assignment": ({"kind", "source_id", "category_id", "revision"}, set()),
    }
    if not isinstance(value, dict) or not isinstance(value.get("kind"), str) or value["kind"] not in shapes:
        raise CatalogError("invalid category result")
    required, optional = shapes[value["kind"]]
    if not required <= set(value) or set(value) - required - optional:
        raise CatalogError("invalid category result")
    for key in ("proposal_id", "category_id", "source_id"):
        if key in value:
            if value[key] is None and key != "category_id":
                raise CatalogError("invalid category receipt identity")
            if value[key] is not None:
                _validate_identity(value[key], 1)
    if "revision" in value:
        _validate_identity("revision", value["revision"])
    if value["kind"] == "classification":
        if not isinstance(value["status"], str) or value["status"] not in {"inbox", "reuse", "pending"} or value["category_id"] is None:
            raise CatalogError("invalid category classification result")
        if (value["status"] == "pending") != ({"proposal_id", "revision", "budget_exceeded"} <= set(value)):
            raise CatalogError("invalid category proposal receipt")
        if value["status"] != "pending" and set(value) != required:
            raise CatalogError("invalid category result")
        if "budget_exceeded" in value and not isinstance(value["budget_exceeded"], bool):
            raise CatalogError("invalid category budget receipt")
    elif value["kind"] == "proposal":
        if value["state"] not in {"approved", "rejected", "redirected"} or (value["state"] != "rejected") != (value["category_id"] is not None):
            raise CatalogError("invalid category decision receipt")
    elif value["category_id"] is None:
        raise CatalogError("invalid category receipt")
    return dict(value)


def _validate_operation_result(value: Any) -> str:
    """Serialize only bounded, content-free operation result envelopes."""
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise CatalogError("invalid operation result")
    result: dict[str, Any] = {}
    recovery_intent = None
    if "category_result" in value:
        result["category_result"] = _validate_category_result(value["category_result"])
        value = {key: child for key, child in value.items() if key != "category_result"}
    if "recovery_intent" in value:
        recovery_intent = _validate_recovery_intent(value["recovery_intent"])
        value = {key: child for key, child in value.items() if key != "recovery_intent"}
    if "relationship_scan" in value or "relationship_decision" in value:
        if set(value) == {"relationship_scan"}:
            result["relationship_scan"] = _validate_relationship_scan(value["relationship_scan"])
        elif set(value) == {"relationship_decision"}:
            result["relationship_decision"] = _validate_relationship_edge(value["relationship_decision"])
        else:
            raise CatalogError("invalid operation result")
    elif set(value) - _OPERATION_RESULT_FIELDS:
        raise CatalogError("invalid operation result")
    if "sources" in value:
        sources = value["sources"]
        if not isinstance(sources, list) or len(sources) > 100:
            raise CatalogError("invalid operation result")
        validated_sources = []
        for source in sources:
            if not isinstance(source, dict) or set(source) != {
                "source_id", "source_version", "sha256"
            }:
                raise CatalogError("invalid operation result")
            source_id, source_version = _validate_identity(
                source["source_id"], source["source_version"]
            )
            sha256 = source["sha256"]
            if not isinstance(sha256, str) or not _SHA256.fullmatch(sha256):
                raise CatalogError("invalid operation result")
            validated_sources.append(
                {"source_id": source_id, "source_version": source_version, "sha256": sha256}
            )
        result["sources"] = validated_sources
    if "item_count" in value:
        item_count = value["item_count"]
        if (
            isinstance(item_count, bool)
            or not isinstance(item_count, int)
            or not 0 <= item_count <= 2_147_483_647
        ):
            raise CatalogError("invalid operation result")
        result["item_count"] = item_count
    if "size_bytes" in value:
        size_bytes = value["size_bytes"]
        if (
            isinstance(size_bytes, bool)
            or not isinstance(size_bytes, int)
            or not 0 <= size_bytes <= 2_147_483_647
        ):
            raise CatalogError("invalid operation result")
        result["size_bytes"] = size_bytes
    if "error_code" in value:
        error_code = value["error_code"]
        if not isinstance(error_code, str) or error_code not in _OPERATION_ERROR_CODES:
            raise CatalogError("invalid operation result")
        result["error_code"] = error_code
    if recovery_intent is not None:
        result["recovery_intent"] = recovery_intent
    try:
        serialized = json.dumps(
            result, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        serialized_bytes = serialized.encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise CatalogError("invalid operation result") from None
    if len(serialized_bytes) > _MAX_OPERATION_RESULT_BYTES:
        raise CatalogError("invalid operation result")
    return serialized


def _validate_recovery_intent(value: Any) -> dict[str, Any]:
    """Accept only bounded identities needed to reconcile one interrupted effect."""
    if not isinstance(value, dict) or not isinstance(value.get("kind"), str):
        raise CatalogError("invalid operation intent")
    kind = value["kind"]
    if kind == "retain":
        if set(value) != {"kind", "source_id", "source_version", "sha256", "size_bytes"}:
            raise CatalogError("invalid operation intent")
        source_id, version = _validate_identity(value["source_id"], value["source_version"])
        checksum = value["sha256"]
        size_bytes = value["size_bytes"]
        if not isinstance(checksum, str) or not _SHA256.fullmatch(checksum):
            raise CatalogError("invalid operation intent")
        if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or not 0 <= size_bytes <= 2_147_483_647:
            raise CatalogError("invalid operation intent")
        return {
            "kind": kind,
            "source_id": source_id,
            "source_version": version,
            "sha256": checksum,
            "size_bytes": size_bytes,
        }
    if kind in {"document_import", "document_retry"}:
        required = {"kind", "source_id", "source_version", "sha256", "size_bytes"}
        if kind == "document_import":
            required |= {"filename", "mime_type", "title", "provenance"}
        # "vision" records a per-document consent choice; absent means "inherit".
        optional = {"request_digest", "extraction_revision", "vision"}
        if kind == "document_retry":
            optional.add("rerun_extraction")
        if not required <= set(value) or set(value) - required - optional:
            raise CatalogError("invalid operation intent")
        base = _validate_recovery_intent({key: child for key, child in value.items() if key in {"source_id", "source_version", "sha256", "size_bytes"}} | {"kind": "retain"})
        base["kind"] = kind
        if "request_digest" in value:
            if _validate_hash(value["request_digest"]) is None:
                raise CatalogError("invalid operation intent")
            base["request_digest"] = value["request_digest"]
        if "extraction_revision" in value:
            _validate_identity(base["source_id"], value["extraction_revision"])
            base["extraction_revision"] = value["extraction_revision"]
        if "rerun_extraction" in value:
            if not isinstance(value["rerun_extraction"], bool):
                raise CatalogError("invalid operation intent")
            base["rerun_extraction"] = value["rerun_extraction"]
        if "vision" in value:
            if not isinstance(value["vision"], bool):
                raise CatalogError("invalid operation intent")
            base["vision"] = value["vision"]
        if kind == "document_import":
            base.update(_validate_document_fields(value["filename"], value["title"], value["mime_type"]))
            provenance = value["provenance"]
            if not isinstance(provenance, dict) or set(provenance) - {"source", "source_ref", "channel", "captured_at", "source_device", "source_app", "external_id"}:
                raise CatalogError("invalid operation intent")
            base["provenance"] = {key: _document_text(child, 1024) for key, child in provenance.items()}
        return base
    if kind == "wiki_rebuild":
        if set(value) == {"kind", "page_id"}:
            return {"kind": kind, "page_id": None if value["page_id"] is None else _validate_identity(value["page_id"], 1)[0]}
        if set(value) != {"kind", "source_id", "source_version"} or (value["source_id"] is None) != (value["source_version"] is None):
            raise CatalogError("invalid operation intent")
        if value["source_id"] is not None:
            _validate_identity(value["source_id"], value["source_version"])
        return dict(value)
    if kind == "wiki_review":
        if set(value) != {"kind", "review_id"}:
            raise CatalogError("invalid operation intent")
        return {"kind": kind, "review_id": _validate_identity(value["review_id"], 1)[0]}
    if kind == "category_decision":
        if value.get("decision") == "classify":
            if set(value) != {"kind", "decision", "source_id", "source_version", "extraction_revision", "artifact_sha256"}:
                raise CatalogError("invalid classification intent")
            _validate_identity(value["source_id"], value["source_version"])
            _validate_identity(value["source_id"], value["extraction_revision"])
            if _validate_hash(value["artifact_sha256"]) is None:
                raise CatalogError("invalid classification artifact")
            return dict(value)
        if value.get("decision") in ("propose", "rename", "alias", "merge", "reassign"):
            return _validate_category_mutation_intent(value)
        required = {"kind", "proposal_id", "decision", "category_id", "expected_revision"}
        if not required <= set(value) or set(value) - required - {"category_budget", "budget_override"} or value["decision"] not in {"approve", "reject", "redirect"}:
            raise CatalogError("invalid operation intent")
        _validate_identity(value["proposal_id"], value["expected_revision"])
        if (value["decision"] != "reject") != (value["category_id"] is not None):
            raise CatalogError("invalid operation intent")
        if value["category_id"] is not None:
            _validate_identity(value["category_id"], 1)
        if "category_budget" in value:
            _validate_identity("budget", value["category_budget"])
        if "budget_override" in value and not isinstance(value["budget_override"], bool):
            raise CatalogError("invalid operation intent")
        return dict(value)
    if kind == "curation":
        required = {"kind", "action", "source_id", "source_version", "sha256", "size_bytes"}
        allowed = required | {
            "prepared_operation_id", "target_sha256", "target_size_bytes"
        }
        if not required <= set(value) or set(value) - allowed:
            raise CatalogError("invalid operation intent")
        if value["action"] not in _CURATION_ACTIONS:
            raise CatalogError("invalid operation intent")
        has_target_hash = "target_sha256" in value
        has_target_size = "target_size_bytes" in value
        if (
            has_target_hash != has_target_size
            or (value["action"] == "correct") != has_target_hash
        ):
            raise CatalogError("invalid operation intent")
        if (value["action"] in {"forget", "delete"}) != (
            "prepared_operation_id" in value
        ):
            raise CatalogError("invalid operation intent")
        source_id, version = _validate_identity(value["source_id"], value["source_version"])
        checksum = value["sha256"]
        size_bytes = value["size_bytes"]
        if not isinstance(checksum, str) or not _SHA256.fullmatch(checksum):
            raise CatalogError("invalid operation intent")
        if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or not 0 <= size_bytes <= 2_147_483_647:
            raise CatalogError("invalid operation intent")
        result = {
            "kind": kind,
            "action": value["action"],
            "source_id": source_id,
            "source_version": version,
            "sha256": checksum,
            "size_bytes": size_bytes,
        }
        if "target_sha256" in value:
            target_hash = value["target_sha256"]
            target_size = value["target_size_bytes"]
            if (
                not isinstance(target_hash, str)
                or not _SHA256.fullmatch(target_hash)
                or isinstance(target_size, bool)
                or not isinstance(target_size, int)
                or not 0 <= target_size <= 2_147_483_647
            ):
                raise CatalogError("invalid operation intent")
            result["target_sha256"] = target_hash
            result["target_size_bytes"] = target_size
        prepared_id = value.get("prepared_operation_id")
        if prepared_id is not None:
            if not isinstance(prepared_id, str) or not _OPERATION_ID.fullmatch(prepared_id):
                raise CatalogError("invalid operation intent")
            result["prepared_operation_id"] = prepared_id
        return result
    if kind == "relationship_scan":
        if set(value) != {"kind", "source_id", "max_results"}:
            raise CatalogError("invalid operation intent")
        source_id = value["source_id"]
        if source_id is not None:
            source_id = _validate_identity(source_id, 1)[0]
        max_results = value["max_results"]
        if max_results is not None and (
            isinstance(max_results, bool)
            or not isinstance(max_results, int)
            or not 1 <= max_results <= 100
        ):
            raise CatalogError("invalid operation intent")
        return {"kind": kind, "source_id": source_id, "max_results": max_results}
    if kind == "relationship_decision":
        if set(value) != {
            "kind", "edge_id", "decision", "expected_revision", "principal"
        }:
            raise CatalogError("invalid operation intent")
        if (
            not isinstance(value["edge_id"], str)
            or not _EDGE_ID.fullmatch(value["edge_id"])
            or value["decision"] not in {"approve", "reject"}
            or isinstance(value["expected_revision"], bool)
            or not isinstance(value["expected_revision"], int)
            or not 1 <= value["expected_revision"] <= 2_147_483_647
            or not isinstance(value["principal"], str)
            or not value["principal"]
            or _utf8_size(value["principal"], 256) < 1
        ):
            raise CatalogError("invalid operation intent")
        return dict(value)
    raise CatalogError("invalid operation intent")


def _validate_projection_path(value: str | None) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value
        or "\x00" in value
        or "\\" in value
        or PurePosixPath(value).is_absolute()
        or any(part in ("", ".", "..") for part in value.split("/"))
    ):
        raise CatalogError("invalid projection identity")
    return value


def _document_text(value: Any, maximum: int, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not value and not empty) or _has_control_characters(value):
        raise CatalogError("invalid document metadata")
    _utf8_size(value, maximum)
    return value


def validate_review_note(note: Any) -> str:
    """One printable line of at most 4096 UTF-8 bytes recording the owner's decision."""
    if not isinstance(note, str) or not note.strip():
        raise CatalogError("invalid wiki review note")
    return _document_text(note, 4096)


def _validate_document_fields(filename: str, title: str, mime_type: str) -> dict[str, str]:
    _document_text(filename, 255)
    if filename in {".", ".."} or "/" in filename or "\\" in filename:
        raise CatalogError("invalid document filename")
    _document_text(title, 1024)
    _document_text(mime_type, 255)
    if not re.fullmatch(r"[a-z0-9][a-z0-9.+_-]*/[a-z0-9][a-z0-9.+_-]*", mime_type):
        raise CatalogError("invalid document MIME")
    return {"filename": filename, "title": title, "mime_type": mime_type}


def _validate_category_mutation_intent(value: dict[str, Any]) -> dict[str, Any]:
    decision = value["decision"]
    fields = {
        "propose": {"classification", "documents"},
        "rename": {"category_id", "name", "description", "expected_revision"},
        "alias": {"category_id", "alias", "expected_revision"},
        "merge": {"source_category_id", "target_category_id", "expected_source_revision", "expected_target_revision"},
        "reassign": {"source_id", "category_id", "expected_revision"},
    }
    if set(value) != {"kind", "decision"} | fields[decision]:
        raise CatalogError("invalid operation intent")
    if decision == "propose":
        classification = value["classification"]
        if not isinstance(classification, dict) or set(classification) != {"name", "description", "rationale", "confidence", "broad", "tags", "closest_category_id"}:
            raise CatalogError("invalid classification intent")
        _document_text(classification["name"], 256)
        _document_text(classification["description"], 4096, empty=True)
        _document_text(classification["rationale"], 4096, empty=True)
        confidence = classification["confidence"]
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1 or not isinstance(classification["broad"], bool):
            raise CatalogError("invalid classification intent")
        tags = classification["tags"]
        if not isinstance(tags, list) or len(tags) > 100:
            raise CatalogError("invalid classification tags")
        for tag in tags:
            _document_text(tag, 256)
        if classification["closest_category_id"] is not None:
            _validate_identity(classification["closest_category_id"], 1)
        documents = value["documents"]
        if not isinstance(documents, list) or not 1 <= len(documents) <= 100:
            raise CatalogError("invalid proposal documents")
        for document in documents:
            if not isinstance(document, dict) or set(document) != {"source_id", "source_version"}:
                raise CatalogError("invalid proposal document")
            _validate_identity(document["source_id"], document["source_version"])
    elif decision == "merge":
        _validate_identity(value["source_category_id"], value["expected_source_revision"])
        _validate_identity(value["target_category_id"], value["expected_target_revision"])
    else:
        _validate_identity(value["category_id"], value["expected_revision"])
        if decision == "reassign":
            _validate_identity(value["source_id"], 1)
        elif decision == "rename":
            _document_text(value["name"], 256)
            _document_text(value["description"], 4096, empty=True)
        else:
            _document_text(value["alias"], 256)
    return dict(value)


def _validate_wiki_contradiction(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"left", "right", "left_date", "right_date"}:
        raise CatalogError("invalid wiki contradiction")
    result = {}
    for key in ("left_date", "right_date"):
        raw = _document_text(value[key], 10)
        try:
            if date.fromisoformat(raw).isoformat() != raw:
                raise ValueError
        except ValueError:
            raise CatalogError("invalid contradiction date") from None
        result[key] = raw
    for side in ("left", "right"):
        claim = value[side]
        if not isinstance(claim, dict) or set(claim) != {"text", "citations"}:
            raise CatalogError("invalid contradiction claim")
        text = _document_text(claim["text"], 2048)
        citations = claim["citations"]
        if not isinstance(citations, (list, tuple)) or not 1 <= len(citations) <= 8:
            raise CatalogError("invalid contradiction citations")
        normalized = []
        for citation in citations:
            if not isinstance(citation, dict) or set(citation) != {"source_id", "source_version", "extraction_revision", "passage_start", "passage_end", "original_sha256", "quote"}:
                raise CatalogError("invalid contradiction citation")
            _validate_identity(citation["source_id"], citation["source_version"])
            _validate_identity(citation["source_id"], citation["extraction_revision"])
            start, end = citation["passage_start"], citation["passage_end"]
            if isinstance(start, bool) or not isinstance(start, int) or not 0 <= start < 2_147_483_647:
                raise CatalogError("invalid contradiction passage")
            _validate_identity("passage", end)
            if start >= end or _validate_hash(citation["original_sha256"]) is None:
                raise CatalogError("invalid contradiction citation")
            _document_text(citation["quote"], 2048)
            normalized.append(dict(citation))
        result[side] = {"text": text, "citations": normalized}
    return result


def _document_warnings(value: Any) -> str:
    if not isinstance(value, list) or len(value) > 1000:
        raise CatalogError("invalid document warnings")
    for warning in value:
        _document_text(warning, 1024)
    return json.dumps(value, separators=(",", ":"))


def _private_file_info(path: Path) -> os.stat_result:
    try:
        before = path.lstat()
    except OSError:
        raise CatalogError("unsafe catalog file") from None
    if not stat.S_ISREG(before.st_mode):
        raise CatalogError("unsafe catalog file")
    if os.name == "posix" and (
        before.st_uid != os.geteuid() or stat.S_IMODE(before.st_mode) != 0o600
    ):
        raise CatalogError("unsafe catalog file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise CatalogError("unsafe catalog file") from None
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            or (
                os.name == "posix"
                and (opened.st_uid != os.geteuid() or stat.S_IMODE(opened.st_mode) != 0o600)
            )
        ):
            raise CatalogError("unsafe catalog file")
        return opened
    except OSError:
        raise CatalogError("unsafe catalog file") from None
    finally:
        os.close(descriptor)


def _ensure_catalog_file(paths: LifeContextPaths) -> tuple[Path, tuple[int, int]]:
    try:
        paths.ensure_private_home()
        before_home = paths.lifeweave_home.lstat()
        if (
            not stat.S_ISDIR(before_home.st_mode)
            or (
                os.name == "posix"
                and (
                    before_home.st_uid != os.geteuid()
                    or stat.S_IMODE(before_home.st_mode) != 0o700
                )
            )
        ):
            raise CatalogError("unsafe catalog home")
        home_fd = os.open(
            paths.lifeweave_home,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            opened_home = os.fstat(home_fd)
            if (opened_home.st_dev, opened_home.st_ino) != (before_home.st_dev, before_home.st_ino):
                raise CatalogError("unsafe catalog home")
            try:
                descriptor = os.open(
                    paths.catalog_path.name,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_CLOEXEC", 0),
                    0o600,
                    dir_fd=home_fd,
                )
            except FileExistsError:
                pass
            else:
                try:
                    if os.name == "posix":
                        os.fchmod(descriptor, 0o600)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        finally:
            os.close(home_fd)
        info = _private_file_info(paths.catalog_path)
        return paths.catalog_path, (info.st_dev, info.st_ino)
    except CatalogError:
        raise
    except OSError:
        raise CatalogError("unsafe catalog file") from None


class LifeContextCatalog:
    """Own the minimal transactional SQLite ledger for V1 source versions."""

    def __init__(
        self,
        paths: LifeContextPaths | None = None,
        *,
        busy_timeout_ms: int = 5000,
    ) -> None:
        if (
            isinstance(busy_timeout_ms, bool)
            or not isinstance(busy_timeout_ms, int)
            or not 1 <= busy_timeout_ms <= 30_000
        ):
            raise CatalogError("invalid catalog timeout")
        self.paths = paths or LifeContextPaths.current()
        self.path, self._database_identity = _ensure_catalog_file(self.paths)
        self._closed = False
        try:
            self._connection = sqlite3.connect(
                self.path,
                timeout=busy_timeout_ms / 1000,
                isolation_level=None,
            )
            self._connection.row_factory = sqlite3.Row
            self._connection.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.execute("PRAGMA synchronous=FULL")
            # Freed row content is zeroed, so an erasure leaves no old text in the database file.
            self._connection.execute("PRAGMA secure_delete=ON")
            if self._connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
                raise CatalogError("catalog constraints unavailable")
            self._verify_open_database()
            self._initialize_schema()
            if os.name == "posix":
                self.path.chmod(0o600)
                _private_file_info(self.path)
        except CatalogError:
            self.close()
            raise
        except (OSError, sqlite3.Error):
            self.close()
            raise CatalogError("catalog unavailable") from None

    def _verify_open_database(self) -> None:
        info = _private_file_info(self.path)
        if (info.st_dev, info.st_ino) != self._database_identity:
            raise CatalogError("unsafe catalog file")

    def _initialize_schema(self) -> None:
        try:
            version = int(self._connection.execute("PRAGMA user_version").fetchone()[0])
            tables = {
                str(row[0])
                for row in self._connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' "
                    "AND name NOT LIKE 'sqlite_%'"
                )
            }
            if version not in (0, 1, 2, 3, 4, 5, 6, 7, 8, _SCHEMA_VERSION):
                raise CatalogError("unsupported catalog schema")
            if version == 0 and tables:
                raise CatalogError("unsupported catalog schema")
            if version == 0:
                with self.transaction() as connection:
                    for statement in (*_SCHEMA, *_SCHEMA_V2):
                        connection.execute(statement)
                    connection.execute("PRAGMA user_version=2")
                self._migrate_v2_to_v3()
                self._migrate_v3_to_v4()
                self._migrate_v4_to_v5()
                self._migrate_v5_to_v6()
                self._migrate_v6_to_v7()
                return
            if version == 1:
                self._validate_schema(tables, _SCHEMA_V1_COLUMNS)
                if tables != set(_SCHEMA_V1_COLUMNS):
                    raise CatalogError("unsupported catalog schema")
                with self.transaction() as connection:
                    for statement in _SCHEMA_V2:
                        connection.execute(statement)
                    connection.execute("PRAGMA user_version=2")
                self._migrate_v2_to_v3()
                self._migrate_v3_to_v4()
                self._migrate_v4_to_v5()
                self._migrate_v5_to_v6()
                self._migrate_v6_to_v7()
                return
            if version == 2:
                self._validate_schema(tables, _SCHEMA_V2_COLUMNS)
                self._validate_v2_state_checks(operation_kinds=_LEGACY_OPERATION_KINDS)
                self._validate_result_size_check(65_536, bytewise=False)
                self._migrate_v2_to_v3()
                self._migrate_v3_to_v4()
                self._migrate_v4_to_v5()
                self._migrate_v5_to_v6()
                self._migrate_v6_to_v7()
                return
            if version == 3:
                self._validate_schema(tables, _SCHEMA_V3_COLUMNS)
                self._validate_v2_state_checks(operation_kinds=_LEGACY_OPERATION_KINDS)
                self._validate_result_size_check(_MAX_OPERATION_RESULT_BYTES, bytewise=True)
                self._migrate_v3_to_v4()
                self._migrate_v4_to_v5()
                self._migrate_v5_to_v6()
                self._migrate_v6_to_v7()
                return
            if version == 4:
                self._validate_schema(tables, _SCHEMA_V4_COLUMNS)
                self._validate_v2_state_checks(operation_kinds=_LEGACY_OPERATION_KINDS)
                self._validate_result_size_check(_MAX_OPERATION_RESULT_BYTES, bytewise=True)
                self._migrate_v4_to_v5()
                self._migrate_v5_to_v6()
                self._migrate_v6_to_v7()
                return
            if version == 5:
                self._validate_schema(tables, _SCHEMA_V5_COLUMNS)
                self._validate_v2_state_checks(operation_kinds=_V5_OPERATION_KINDS)
                self._validate_result_size_check(_MAX_OPERATION_RESULT_BYTES, bytewise=True)
                self._migrate_v5_to_v6()
                self._migrate_v6_to_v7()
                return
            if version == 6:
                self._validate_v6_schema()
                self._migrate_v6_to_v7()
                return
            if version == 7:
                self._validate_v6_schema(include_publications=True)
                self._migrate_v7_to_v8()
                return
            if version == 8:
                self._validate_v6_schema(include_publications=True, include_moves=True)
                self._migrate_v8_to_v9()
                return
            if version == _SCHEMA_VERSION:
                self._validate_v6_schema(include_publications=True, include_moves=True, include_reviews=True)
                return
            raise CatalogError("unsupported catalog schema")
        except CatalogError:
            raise
        except sqlite3.Error:
                raise CatalogError("unsupported catalog schema") from None

    def _validate_result_size_check(self, maximum: int, *, bytewise: bool) -> None:
        row = self._connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='operations'"
        ).fetchone()
        if row is None or not isinstance(row[0], str):
            raise CatalogError("unsupported catalog schema")
        normalized = re.sub(r"\s+", "", row[0]).upper().replace("'", "").replace('"', "")
        expression = (
            f"CHECK(LENGTH(CAST(RESULT_JSONASBLOB))<={maximum})"
            if bytewise
            else f"CHECK(LENGTH(RESULT_JSON)<={maximum})"
        )
        if expression not in normalized:
            raise CatalogError("unsupported catalog schema")

    def _migrate_v2_to_v3(self) -> None:
        """Expand the durable result bound without losing operation references."""
        try:
            self._connection.execute("PRAGMA foreign_keys=OFF")
            if self._connection.execute("PRAGMA foreign_keys").fetchone()[0] != 0:
                raise CatalogError("catalog constraints unavailable")
            with self.transaction() as connection:
                for statement in _SCHEMA_V3:
                    connection.execute(statement)
                if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise CatalogError("catalog migration failed")
        except CatalogError:
            raise
        except sqlite3.Error:
            raise CatalogError("catalog migration failed") from None
        finally:
            try:
                self._connection.execute("PRAGMA foreign_keys=ON")
                if self._connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
                    raise CatalogError("catalog constraints unavailable")
            except sqlite3.Error:
                raise CatalogError("catalog constraints unavailable") from None

    def _migrate_v3_to_v4(self) -> None:
        """Persist prepared-action consumption and delete progress atomically."""
        try:
            self._connection.execute("PRAGMA foreign_keys=OFF")
            if self._connection.execute("PRAGMA foreign_keys").fetchone()[0] != 0:
                raise CatalogError("catalog constraints unavailable")
            with self.transaction() as connection:
                history_columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(curation_history)")
                }
                if "prepared_operation_id" not in history_columns:
                    connection.execute(
                        """ALTER TABLE curation_history ADD COLUMN prepared_operation_id TEXT
                           REFERENCES operations(operation_id)"""
                    )
                prepared_index = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='index' "
                    "AND name='curation_history_prepared_idx'"
                ).fetchone()
                if prepared_index is None:
                    connection.execute(
                        "CREATE INDEX curation_history_prepared_idx "
                        "ON curation_history(prepared_operation_id, history_id)"
                    )
                tombstone_columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(tombstones)")
                }
                progress_expression = (
                    "progress_json"
                    if "progress_json" in tombstone_columns
                    else "'{\"provider_delete\":\"pending\",\"projection_delete\":\"pending\",\"custody_delete\":\"pending\"}'"
                )
                connection.execute(
                    """CREATE TABLE tombstones_v4 (
                        source_id TEXT NOT NULL,
                        source_version INTEGER NOT NULL CHECK (source_version > 0),
                        tombstone_type TEXT NOT NULL CHECK (tombstone_type IN ('forgotten', 'deleted')),
                        operation_id TEXT NOT NULL,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        progress_json TEXT NOT NULL DEFAULT '{"provider_delete":"pending","projection_delete":"pending","custody_delete":"pending"}'
                            CHECK (length(CAST(progress_json AS BLOB)) <= 512),
                        PRIMARY KEY (source_id, source_version),
                        FOREIGN KEY (operation_id) REFERENCES operations(operation_id),
                        FOREIGN KEY (source_id, source_version)
                            REFERENCES source_versions(source_id, source_version)
                    )"""
                )
                connection.execute(
                    f"""INSERT INTO tombstones_v4(
                           source_id, source_version, tombstone_type, operation_id,
                           created_at, progress_json
                       ) SELECT source_id, source_version, tombstone_type, operation_id,
                                created_at, {progress_expression}
                         FROM tombstones"""
                )
                connection.execute("DROP TABLE tombstones")
                connection.execute("ALTER TABLE tombstones_v4 RENAME TO tombstones")
                connection.execute(
                    "CREATE INDEX tombstones_operation_idx ON tombstones(operation_id, source_id)"
                )
                connection.execute("PRAGMA user_version=4")
                if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise CatalogError("catalog migration failed")
        except CatalogError:
            raise
        except sqlite3.Error:
            raise CatalogError("catalog migration failed") from None
        finally:
            try:
                self._connection.execute("PRAGMA foreign_keys=ON")
                if self._connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
                    raise CatalogError("catalog constraints unavailable")
            except sqlite3.Error:
                raise CatalogError("catalog constraints unavailable") from None

    def _migrate_v4_to_v5(self) -> None:
        """Add import progress and content-free imported lifecycle tombstones."""
        try:
            self._connection.execute("PRAGMA foreign_keys=OFF")
            if self._connection.execute("PRAGMA foreign_keys").fetchone()[0] != 0:
                raise CatalogError("catalog constraints unavailable")
            with self.transaction() as connection:
                connection.execute("DROP INDEX IF EXISTS operations_state_created_idx")
                connection.execute(
                    """CREATE TABLE operations_v5 (
                        operation_id TEXT PRIMARY KEY NOT NULL CHECK (
                            length(operation_id) = 35
                            AND substr(operation_id, 1, 3) = 'op_'
                            AND substr(operation_id, 4) NOT GLOB '*[^0-9a-f]*'
                        ),
                        idempotency_key_digest TEXT NOT NULL UNIQUE CHECK (
                            length(idempotency_key_digest) = 64
                            AND idempotency_key_digest NOT GLOB '*[^0-9a-f]*'
                        ),
                        request_digest TEXT NOT NULL CHECK (
                            length(request_digest) = 64
                            AND request_digest NOT GLOB '*[^0-9a-f]*'
                        ),
                        operation_kind TEXT NOT NULL CHECK (operation_kind IN (
                            'retain', 'curation', 'relationship_scan', 'relationship_decision', 'import'
                        )),
                        state TEXT NOT NULL CHECK (state IN (
                            'pending', 'running', 'retryable_failed', 'permanent_failed',
                            'completed', 'cancelled'
                        )),
                        result_json TEXT NOT NULL DEFAULT '{}'
                            CHECK (length(CAST(result_json AS BLOB)) <= 4194304),
                        error_code TEXT CHECK (error_code IS NULL OR (
                            length(error_code) BETWEEN 1 AND 64
                            AND error_code NOT GLOB '*[^a-z0-9_]*'
                            AND substr(error_code, 1, 1) GLOB '[a-z]'
                        )),
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )"""
                )
                connection.execute(
                    """INSERT INTO operations_v5(
                           operation_id, idempotency_key_digest, request_digest,
                           operation_kind, state, result_json, error_code, created_at, updated_at
                       ) SELECT operation_id, idempotency_key_digest, request_digest,
                                operation_kind, state, result_json, error_code, created_at, updated_at
                         FROM operations"""
                )
                connection.execute("DROP TABLE operations")
                connection.execute("ALTER TABLE operations_v5 RENAME TO operations")
                connection.execute(
                    "CREATE INDEX operations_state_created_idx "
                    "ON operations(state, created_at, operation_id)"
                )
                connection.execute(
                    """CREATE TABLE transfers (
                        operation_id TEXT PRIMARY KEY NOT NULL,
                        bundle_digest TEXT NOT NULL CHECK (
                            length(bundle_digest) = 64
                            AND bundle_digest NOT GLOB '*[^0-9a-f]*'
                        ),
                        item_count INTEGER NOT NULL CHECK (item_count BETWEEN 0 AND 4096),
                        size_bytes INTEGER NOT NULL CHECK (size_bytes BETWEEN 1 AND 4194304),
                        FOREIGN KEY (operation_id) REFERENCES operations(operation_id)
                    )"""
                )
                connection.execute(
                    """CREATE TABLE transfer_items (
                        operation_id TEXT NOT NULL,
                        source_id TEXT NOT NULL,
                        source_version INTEGER NOT NULL CHECK (source_version > 0),
                        lifecycle_state TEXT NOT NULL CHECK (
                            lifecycle_state IN ('active', 'forgotten', 'deleted')
                        ),
                        sha256 TEXT CHECK (sha256 IS NULL OR (
                            length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*'
                        )),
                        state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN (
                            'pending', 'custody_written', 'catalog_registered', 'projected',
                            'provider_submitted', 'tombstoned', 'completed'
                        )),
                        PRIMARY KEY (operation_id, source_id, source_version),
                        FOREIGN KEY (operation_id) REFERENCES operations(operation_id)
                    )"""
                )
                connection.execute(
                    "CREATE INDEX transfer_items_state_idx "
                    "ON transfer_items(operation_id, state, source_id, source_version)"
                )
                connection.execute(
                    """CREATE TABLE transfer_tombstones (
                        source_id TEXT NOT NULL,
                        source_version INTEGER NOT NULL CHECK (source_version > 0),
                        tombstone_type TEXT NOT NULL CHECK (
                            tombstone_type IN ('forgotten', 'deleted')
                        ),
                        operation_id TEXT NOT NULL,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (source_id, source_version),
                        FOREIGN KEY (operation_id) REFERENCES operations(operation_id)
                    )"""
                )
                connection.execute(
                    "CREATE INDEX transfer_tombstones_operation_idx "
                    "ON transfer_tombstones(operation_id, source_id)"
                )
                connection.execute("PRAGMA user_version=5")
                if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise CatalogError("catalog migration failed")
        except CatalogError:
            raise
        except sqlite3.Error:
            raise CatalogError("catalog migration failed") from None
        finally:
            try:
                self._connection.execute("PRAGMA foreign_keys=ON")
                if self._connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
                    raise CatalogError("catalog constraints unavailable")
            except sqlite3.Error:
                raise CatalogError("catalog constraints unavailable") from None

    def _migrate_v5_to_v6(self) -> None:
        """Rebuild operation admission and install document ledgers atomically."""
        try:
            self._connection.execute("PRAGMA foreign_keys=OFF")
            if self._connection.execute("PRAGMA foreign_keys").fetchone()[0] != 0:
                raise CatalogError("catalog constraints unavailable")
            with self.transaction() as connection:
                operation_sql = connection.execute(
                    "SELECT sql FROM sqlite_master WHERE name='operations' AND type='table'"
                ).fetchone()[0]
                # V5's SQL is validated before this migration; rebuild, do not edit sqlite_master.
                new_sql = re.sub(
                    r"CHECK\s*\(\s*operation_kind\s+IN\s*\([^)]*\)\s*\)",
                    "CHECK (operation_kind IN (" + ", ".join("'" + kind + "'" for kind in _V8_OPERATION_KINDS) + "))",
                    operation_sql, count=1, flags=re.IGNORECASE,
                )
                if new_sql == operation_sql:
                    raise CatalogError("catalog migration failed")
                new_sql = re.sub(r"CREATE\s+TABLE\s+[\"`\[]?operations[\"`\]]?", "CREATE TABLE operations_v6", new_sql, count=1, flags=re.IGNORECASE)
                connection.execute(new_sql)
                connection.execute("INSERT INTO operations_v6 SELECT * FROM operations")
                connection.execute("DROP TABLE operations")
                connection.execute("ALTER TABLE operations_v6 RENAME TO operations")
                connection.execute("CREATE INDEX operations_state_created_idx ON operations(state,created_at,operation_id)")
                connection.execute("ALTER TABLE sources ADD COLUMN layer TEXT NOT NULL DEFAULT 'journal' CHECK (layer IN ('journal','document'))")
                for statement in _SCHEMA_V6:
                    connection.execute(statement)
                connection.execute("INSERT INTO categories(category_id,name,name_key) VALUES('inbox','Inbox','inbox')")
                connection.execute("PRAGMA user_version=6")
                self._validate_v6_schema()
                if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise CatalogError("catalog migration failed")
        except sqlite3.Error:
            raise CatalogError("catalog migration failed") from None
        finally:
            self._connection.execute("PRAGMA foreign_keys=ON")
            if self._connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
                raise CatalogError("catalog constraints unavailable")

    def _migrate_v6_to_v7(self) -> None:
        with self.transaction() as connection:
            for statement in _SCHEMA_V7:
                connection.execute(statement)
            connection.execute("PRAGMA user_version=7")
            self._validate_v6_schema(include_publications=True)
        self._migrate_v7_to_v8()

    def _migrate_v7_to_v8(self) -> None:
        with self.transaction() as connection:
            for statement in _SCHEMA_V8:
                connection.execute(statement)
            connection.execute("PRAGMA user_version=8")
            self._validate_v6_schema(include_publications=True, include_moves=True)
        self._migrate_v8_to_v9()

    def _migrate_v8_to_v9(self) -> None:
        """Admit review resolutions as operations and keep the owner's notes, atomically."""
        try:
            self._connection.execute("PRAGMA foreign_keys=OFF")
            if self._connection.execute("PRAGMA foreign_keys").fetchone()[0] != 0:
                raise CatalogError("catalog constraints unavailable")
            with self.transaction() as connection:
                operation_sql = connection.execute(
                    "SELECT sql FROM sqlite_master WHERE name='operations' AND type='table'"
                ).fetchone()[0]
                # V8's SQL is validated before this migration; rebuild, do not edit sqlite_master.
                new_sql = re.sub(
                    r"CHECK\s*\(\s*operation_kind\s+IN\s*\([^)]*\)\s*\)",
                    "CHECK (operation_kind IN (" + ", ".join("'" + kind + "'" for kind in _OPERATION_KINDS) + "))",
                    operation_sql, count=1, flags=re.IGNORECASE,
                )
                if new_sql == operation_sql:
                    raise CatalogError("catalog migration failed")
                new_sql = re.sub(r"CREATE\s+TABLE\s+[\"`\[]?operations[\"`\]]?", "CREATE TABLE operations_v9", new_sql, count=1, flags=re.IGNORECASE)
                connection.execute(new_sql)
                connection.execute("INSERT INTO operations_v9 SELECT * FROM operations")
                connection.execute("DROP TABLE operations")
                connection.execute("ALTER TABLE operations_v9 RENAME TO operations")
                connection.execute("CREATE INDEX operations_state_created_idx ON operations(state,created_at,operation_id)")
                for statement in _SCHEMA_V9:
                    connection.execute(statement)
                connection.execute("PRAGMA user_version=9")
                self._validate_v6_schema(include_publications=True, include_moves=True, include_reviews=True)
                if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise CatalogError("catalog migration failed")
        except sqlite3.Error:
            raise CatalogError("catalog migration failed") from None
        finally:
            self._connection.execute("PRAGMA foreign_keys=ON")
            if self._connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
                raise CatalogError("catalog constraints unavailable")

    def _validate_v6_schema(
        self, *, include_publications: bool = False, include_moves: bool = False,
        include_reviews: bool = False,
    ) -> None:
        """Compare new DDL and all column sets; weakened checks are not accepted."""
        reference = sqlite3.connect(":memory:")
        try:
            for statement in (
                *_SCHEMA_V6, *(_SCHEMA_V7 if include_publications else ()),
                *(_SCHEMA_V8 if include_moves else ()), *(_SCHEMA_V9 if include_reviews else ()),
            ):
                reference.execute(statement)
            required = {**_SCHEMA_V5_COLUMNS, "sources": _SCHEMA_V1_COLUMNS["sources"] | {"layer"}}
            for row in reference.execute("SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL"):
                name, expected_sql = row
                actual = self._connection.execute("SELECT sql FROM sqlite_master WHERE name=?", (name,)).fetchone()
                normalize = lambda sql: re.sub(r"\s+", "", sql).casefold()
                if actual is None or normalize(actual[0]) != normalize(expected_sql):
                    raise CatalogError("unsupported catalog schema")
                if expected_sql.startswith("CREATE TABLE"):
                    required[name] = {column[1] for column in reference.execute(f"PRAGMA table_info({name})")}
            tables = {row[0] for row in self._connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            self._validate_schema(tables, required)
            for table, columns in required.items():
                if columns != {row[1] for row in self._connection.execute(f"PRAGMA table_info({table})")}:
                    raise CatalogError("unsupported catalog schema")
            source_sql = self._connection.execute("SELECT sql FROM sqlite_master WHERE name='sources'").fetchone()[0]
            if "check(layerin('journal','document'))" not in re.sub(r"\s+", "", source_sql).casefold():
                raise CatalogError("unsupported catalog schema")
            self._validate_v2_state_checks(
                operation_kinds=_OPERATION_KINDS if include_reviews else _V8_OPERATION_KINDS
            )
            self._validate_result_size_check(_MAX_OPERATION_RESULT_BYTES, bytewise=True)
        finally:
            reference.close()

    def _validate_schema(
        self,
        tables: set[str],
        required: dict[str, set[str]],
    ) -> None:
        if tables != set(required):
            raise CatalogError("unsupported catalog schema")
        for table, expected_columns in required.items():
            columns = {
                str(row[1])
                for row in self._connection.execute(f"PRAGMA table_info({table})").fetchall()
            }
            if not expected_columns.issubset(columns):
                raise CatalogError("unsupported catalog schema")

    def _validate_v2_state_checks(
        self, *, operation_kinds: tuple[str, ...] = _OPERATION_KINDS
    ) -> None:
        expected = {
            "operations": (
                ("state", _OPERATION_STATES),
                ("operation_kind", operation_kinds),
            ),
            "curation_history": (("action", _CURATION_ACTIONS),),
            "tombstones": (("tombstone_type", _TOMBSTONE_TYPES),),
        }
        for table, checks in expected.items():
            row = self._connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if row is None or not isinstance(row[0], str):
                raise CatalogError("unsupported catalog schema")
            normalized = re.sub(r"\s+", "", row[0]).upper().replace("'", "").replace('"', "")
            for column, values in checks:
                expected_check = (
                    f"CHECK({column.upper()}IN({','.join(value.upper() for value in values)}))"
                )
                if expected_check not in normalized:
                    raise CatalogError("unsupported catalog schema")

    def begin_operation(
        self,
        idempotency_key: str,
        request_digest: str,
        operation_kind: str,
    ) -> dict[str, Any]:
        """Atomically admit an operation or replay its existing key binding."""
        if not isinstance(idempotency_key, str) or not _IDEMPOTENCY_KEY.fullmatch(
            idempotency_key
        ):
            raise CatalogError("invalid idempotency key")
        if not isinstance(request_digest, str) or not _SHA256.fullmatch(request_digest):
            raise CatalogError("invalid operation digest")
        if not isinstance(operation_kind, str) or operation_kind not in _OPERATION_KINDS:
            raise CatalogError("invalid operation kind")
        key_digest = hashlib.sha256(idempotency_key.encode("ascii")).hexdigest()
        self._ensure_open()
        try:
            with self.transaction() as connection:
                existing = connection.execute(
                    """SELECT operation_id, idempotency_key_digest, request_digest,
                            operation_kind, state, result_json, error_code, created_at, updated_at
                       FROM operations WHERE idempotency_key_digest = ?""",
                    (key_digest,),
                ).fetchone()
                if existing is not None:
                    if (
                        existing["request_digest"] != request_digest
                        or existing["operation_kind"] != operation_kind
                    ):
                        raise OperationConflict("idempotency key conflict")
                    return self._operation_from_row(existing)
                operation_id = f"op_{uuid.uuid4().hex}"
                connection.execute(
                    """INSERT INTO operations(
                           operation_id, idempotency_key_digest, request_digest,
                           operation_kind, state
                       ) VALUES (?, ?, ?, ?, 'pending')""",
                    (operation_id, key_digest, request_digest, operation_kind),
                )
                row = connection.execute(
                    """SELECT operation_id, idempotency_key_digest, request_digest,
                            operation_kind, state, result_json, error_code, created_at, updated_at
                       FROM operations WHERE operation_id = ?""",
                    (operation_id,),
                ).fetchone()
                return self._operation_from_row(row)
        except (OperationConflict, CatalogError):
            raise
        except sqlite3.Error:
            raise CatalogError("catalog operation failed") from None

    def lookup_operation(
        self, idempotency_key: str, request_digest: str, operation_kind: str
    ) -> dict[str, Any] | None:
        """Read an existing idempotency binding without admitting a new operation."""
        if (
            not isinstance(idempotency_key, str)
            or not _IDEMPOTENCY_KEY.fullmatch(idempotency_key)
            or not isinstance(request_digest, str)
            or not _SHA256.fullmatch(request_digest)
            or not isinstance(operation_kind, str)
            or operation_kind not in _OPERATION_KINDS
        ):
            raise CatalogError("invalid operation identity")
        key_digest = hashlib.sha256(idempotency_key.encode("ascii")).hexdigest()
        self._ensure_open()
        try:
            row = self._connection.execute(
                "SELECT operation_id, idempotency_key_digest, request_digest, operation_kind, "
                "state, result_json, error_code, created_at, updated_at FROM operations "
                "WHERE idempotency_key_digest=?",
                (key_digest,),
            ).fetchone()
            if row is None:
                return None
            if row["request_digest"] != request_digest or row["operation_kind"] != operation_kind:
                raise OperationConflict("idempotency key conflict")
            return self._operation_from_row(row)
        except (OperationConflict, CatalogError):
            raise
        except sqlite3.Error:
            raise CatalogError("catalog operation failed") from None

    def find_category_operation(self, idempotency_key: str) -> dict[str, Any] | None:
        if not isinstance(idempotency_key, str) or not _IDEMPOTENCY_KEY.fullmatch(idempotency_key):
            raise CatalogError("invalid operation identity")
        self._ensure_open()
        row = self._connection.execute(
            "SELECT * FROM operations WHERE idempotency_key_digest=? AND operation_kind='category_decision'",
            (hashlib.sha256(idempotency_key.encode("ascii")).hexdigest(),),
        ).fetchone()
        return self._operation_from_row(row) if row else None

    def initialize_transfer(
        self,
        operation_id: str,
        bundle_digest: str,
        size_bytes: int,
        items: list[dict[str, Any]],
    ) -> None:
        """Persist content-free import identity and its complete item checklist."""
        if (
            not isinstance(operation_id, str)
            or not _OPERATION_ID.fullmatch(operation_id)
            or not isinstance(bundle_digest, str)
            or not _SHA256.fullmatch(bundle_digest)
            or isinstance(size_bytes, bool)
            or not isinstance(size_bytes, int)
            or not 1 <= size_bytes <= 4_194_304
            or not isinstance(items, list)
            or len(items) > 4096
        ):
            raise CatalogError("invalid transfer identity")
        normalized: list[tuple[str, int, str, str | None]] = []
        seen: set[tuple[str, int]] = set()
        for item in items:
            if not isinstance(item, dict) or set(item) != {
                "source_id", "source_version", "lifecycle_state", "sha256"
            }:
                raise CatalogError("invalid transfer item")
            source_id, source_version = _validate_identity(
                item["source_id"], item["source_version"]
            )
            lifecycle_state = item["lifecycle_state"]
            checksum = item["sha256"]
            if lifecycle_state not in _LIFECYCLE_STATES:
                raise CatalogError("invalid transfer item")
            if lifecycle_state == "deleted":
                if checksum is not None:
                    raise CatalogError("invalid transfer item")
            elif not isinstance(checksum, str) or not _SHA256.fullmatch(checksum):
                raise CatalogError("invalid transfer item")
            identity = (source_id, source_version)
            if identity in seen:
                raise CatalogError("invalid transfer item")
            seen.add(identity)
            normalized.append((source_id, source_version, lifecycle_state, checksum))
        if normalized != sorted(normalized):
            raise CatalogError("invalid transfer item")

        self._ensure_open()
        try:
            with self.transaction() as connection:
                operation = connection.execute(
                    "SELECT operation_kind, request_digest, state FROM operations "
                    "WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                if (
                    operation is None
                    or operation["operation_kind"] != "import"
                    or operation["request_digest"] != bundle_digest
                    or operation["state"] not in {"pending", "running", "completed"}
                ):
                    raise OperationConflict("transfer operation conflict")
                existing = connection.execute(
                    "SELECT bundle_digest, item_count, size_bytes FROM transfers "
                    "WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                if existing is not None:
                    rows = connection.execute(
                        "SELECT source_id, source_version, lifecycle_state, sha256 "
                        "FROM transfer_items WHERE operation_id=? "
                        "ORDER BY source_id, source_version",
                        (operation_id,),
                    ).fetchall()
                    current = [tuple(row) for row in rows]
                    if (
                        existing["bundle_digest"] != bundle_digest
                        or existing["item_count"] != len(normalized)
                        or existing["size_bytes"] != size_bytes
                        or current != normalized
                    ):
                        raise OperationConflict("transfer operation conflict")
                    return
                connection.execute(
                    "INSERT INTO transfers(operation_id,bundle_digest,item_count,size_bytes) "
                    "VALUES(?,?,?,?)",
                    (operation_id, bundle_digest, len(normalized), size_bytes),
                )
                connection.executemany(
                    "INSERT INTO transfer_items(operation_id,source_id,source_version,"
                    "lifecycle_state,sha256) VALUES(?,?,?,?,?)",
                    [(operation_id, *item) for item in normalized],
                )
        except (OperationConflict, CatalogError):
            raise
        except sqlite3.Error:
            raise CatalogError("catalog transfer failed") from None

    def list_transfer_items(self, operation_id: str) -> list[dict[str, Any]]:
        if not isinstance(operation_id, str) or not _OPERATION_ID.fullmatch(operation_id):
            raise CatalogError("invalid transfer identity")
        self._ensure_open()
        try:
            transfer = self._connection.execute(
                "SELECT item_count FROM transfers WHERE operation_id=?", (operation_id,)
            ).fetchone()
            if transfer is None:
                raise CatalogError("transfer unavailable")
            rows = self._connection.execute(
                "SELECT source_id, source_version, lifecycle_state, sha256, state "
                "FROM transfer_items WHERE operation_id=? "
                "ORDER BY source_id, source_version",
                (operation_id,),
            ).fetchall()
            if len(rows) != transfer["item_count"]:
                raise CatalogError("transfer inventory conflict")
            return [dict(row) for row in rows]
        except CatalogError:
            raise
        except sqlite3.Error:
            raise CatalogError("catalog transfer failed") from None

    def list_incomplete_transfers(self) -> list[str]:
        """List bounded import operation IDs that still need their original bundle."""
        self._ensure_open()
        try:
            rows = self._connection.execute(
                "SELECT operation_id FROM operations WHERE operation_kind='import' "
                "AND state!='completed' "
                "ORDER BY created_at,operation_id LIMIT 1001"
            ).fetchall()
            if len(rows) > 1000:
                raise CatalogError("transfer recovery limit exceeded")
            return [row["operation_id"] for row in rows]
        except CatalogError:
            raise
        except sqlite3.Error:
            raise CatalogError("catalog transfer failed") from None

    def set_transfer_item_state(
        self, operation_id: str, source_id: str, source_version: int, state: str
    ) -> None:
        identity = _validate_identity(source_id, source_version)
        if (
            not isinstance(operation_id, str)
            or not _OPERATION_ID.fullmatch(operation_id)
            or state not in _TRANSFER_STATE_ORDER
        ):
            raise CatalogError("invalid transfer item state")
        self._ensure_open()
        try:
            with self.transaction() as connection:
                row = connection.execute(
                    "SELECT state FROM transfer_items WHERE operation_id=? "
                    "AND source_id=? AND source_version=?",
                    (operation_id, *identity),
                ).fetchone()
                if row is None:
                    raise CatalogError("transfer item unavailable")
                previous = row["state"]
                if previous not in _TRANSFER_STATE_ORDER or (
                    _TRANSFER_STATE_ORDER[state] < _TRANSFER_STATE_ORDER[previous]
                ):
                    raise OperationConflict("transfer item state conflict")
                connection.execute(
                    "UPDATE transfer_items SET state=? WHERE operation_id=? "
                    "AND source_id=? AND source_version=?",
                    (state, operation_id, *identity),
                )
        except (OperationConflict, CatalogError):
            raise
        except sqlite3.Error:
            raise CatalogError("catalog transfer failed") from None

    def record_transfer_tombstone(
        self,
        operation_id: str,
        source_id: str,
        source_version: int,
        tombstone_type: str,
    ) -> None:
        identity = _validate_identity(source_id, source_version)
        if (
            not isinstance(operation_id, str)
            or not _OPERATION_ID.fullmatch(operation_id)
            or tombstone_type not in _TOMBSTONE_TYPES
        ):
            raise CatalogError("invalid transfer tombstone")
        self._ensure_open()
        try:
            with self.transaction() as connection:
                operation = connection.execute(
                    "SELECT operation_kind, state FROM operations WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                if (
                    operation is None
                    or operation["operation_kind"] != "import"
                    or operation["state"] != "running"
                ):
                    raise OperationConflict("transfer operation conflict")
                item = connection.execute(
                    "SELECT lifecycle_state FROM transfer_items WHERE operation_id=? "
                    "AND source_id=? AND source_version=?",
                    (operation_id, *identity),
                ).fetchone()
                if item is None or item["lifecycle_state"] != tombstone_type:
                    raise CatalogConflict("transfer item lifecycle conflict")
                existing = connection.execute(
                    "SELECT tombstone_type, operation_id FROM transfer_tombstones "
                    "WHERE source_id=? AND source_version=?",
                    identity,
                ).fetchone()
                if existing is not None:
                    if existing["tombstone_type"] != tombstone_type:
                        raise CatalogConflict("transfer tombstone conflict")
                    return
                ordinary = connection.execute(
                    "SELECT tombstone_type FROM tombstones WHERE source_id=? AND source_version=?",
                    identity,
                ).fetchone()
                if ordinary is not None:
                    if ordinary["tombstone_type"] != tombstone_type:
                        raise CatalogConflict("transfer tombstone conflict")
                    return
                connection.execute(
                    "INSERT INTO transfer_tombstones(source_id,source_version,tombstone_type,"
                    "operation_id) VALUES(?,?,?,?)",
                    (*identity, tombstone_type, operation_id),
                )
        except (CatalogConflict, OperationConflict, CatalogError):
            raise
        except sqlite3.Error:
            raise CatalogError("catalog transfer failed") from None

    def get_operation(self, operation_id: str) -> dict[str, Any] | None:
        """Read an operation without exposing key or request digests."""
        if not isinstance(operation_id, str) or not _OPERATION_ID.fullmatch(operation_id):
            raise CatalogError("invalid operation identity")
        self._ensure_open()
        try:
            row = self._connection.execute(
                """SELECT operation_id, operation_kind, state, result_json,
                          error_code, created_at, updated_at
                   FROM operations WHERE operation_id = ?""",
                (operation_id,),
            ).fetchone()
            return self._operation_from_row(row) if row is not None else None
        except sqlite3.Error:
            raise CatalogError("catalog operation failed") from None

    def set_operation_intent(
        self, operation_id: str, result: dict[str, Any]
    ) -> dict[str, Any]:
        """Persist bounded content-free recovery facts before an operation effect."""
        if not isinstance(operation_id, str) or not _OPERATION_ID.fullmatch(operation_id):
            raise CatalogError("invalid operation identity")
        result_json = _validate_operation_result(result)
        self._ensure_open()
        try:
            with self.transaction() as connection:
                row = connection.execute(
                    "SELECT operation_id, operation_kind, state, result_json, error_code, "
                    "created_at, updated_at FROM operations WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                if row is None:
                    raise CatalogError("operation not found")
                current = self._operation_from_row(row)
                if current["state"] != "pending":
                    if current["result"] == json.loads(result_json):
                        return current
                    raise OperationConflict("operation intent conflict")
                if current["result"] not in ({}, json.loads(result_json)):
                    raise OperationConflict("operation intent conflict")
                connection.execute(
                    "UPDATE operations SET result_json=?, updated_at=CURRENT_TIMESTAMP "
                    "WHERE operation_id=? AND state='pending'",
                    (result_json, operation_id),
                )
                row = connection.execute(
                    "SELECT operation_id, operation_kind, state, result_json, error_code, "
                    "created_at, updated_at FROM operations WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                return self._operation_from_row(row)
        except (OperationConflict, CatalogError):
            raise
        except sqlite3.Error:
            raise CatalogError("catalog operation failed") from None

    def list_recoverable_operations(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """Return a bounded ordered batch of operations eligible for recovery."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise CatalogError("invalid operation limit")
        self._ensure_open()
        placeholders = ", ".join("?" for _ in _RECOVERABLE_OPERATION_STATES)
        try:
            rows = self._connection.execute(
                f"""SELECT operation_id, operation_kind, state, result_json,
                           error_code, created_at, updated_at
                    FROM operations WHERE state IN ({placeholders})
                    ORDER BY created_at, operation_id LIMIT ?""",
                (*_RECOVERABLE_OPERATION_STATES, limit),
            ).fetchall()
            return [self._operation_from_row(row) for row in rows]
        except sqlite3.Error:
            raise CatalogError("catalog operation failed") from None

    def save_operation_result(self, operation_id: str, result: dict[str, Any]) -> dict[str, Any]:
        """Persist admitted progress without changing its running state or intent."""
        if not isinstance(operation_id, str) or not _OPERATION_ID.fullmatch(operation_id):
            raise CatalogError("invalid operation identity")
        result_json = _validate_operation_result(result)
        result_value = json.loads(result_json)
        self._ensure_open()
        try:
            with self.transaction() as connection:
                row = connection.execute(
                    "SELECT operation_id, operation_kind, state, result_json, error_code, "
                    "created_at, updated_at FROM operations WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                if row is None:
                    raise CatalogError("operation not found")
                current = self._operation_from_row(row)
                if (
                    current["operation_kind"] not in {"relationship_scan", "relationship_decision", "wiki_rebuild"}
                    or current["state"] != "running"
                    or current["result"].get("recovery_intent") is None
                    or current["result"].get("recovery_intent")
                    != result_value.get("recovery_intent")
                ):
                    raise OperationConflict("operation progress conflict")
                connection.execute(
                    "UPDATE operations SET result_json=?, updated_at=CURRENT_TIMESTAMP "
                    "WHERE operation_id=? AND state='running'",
                    (result_json, operation_id),
                )
                row = connection.execute(
                    "SELECT operation_id, operation_kind, state, result_json, error_code, "
                    "created_at, updated_at FROM operations WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                return self._operation_from_row(row)
        except (OperationConflict, CatalogError):
            raise
        except sqlite3.Error:
            raise CatalogError("catalog operation failed") from None

    def transition_operation(
        self,
        operation_id: str,
        expected_state: str,
        new_state: str,
        *,
        result: dict[str, Any] | None = None,
        error_code: str | None = None,
    ) -> dict[str, Any]:
        """Compare-and-set one legal operation transition with a safe summary."""
        if not isinstance(operation_id, str) or not _OPERATION_ID.fullmatch(operation_id):
            raise CatalogError("invalid operation identity")
        if (
            not isinstance(expected_state, str)
            or expected_state not in _OPERATION_TRANSITIONS
            or not isinstance(new_state, str)
            or new_state not in _OPERATION_TRANSITIONS
            or new_state not in _OPERATION_TRANSITIONS[expected_state]
        ):
            raise CatalogError("invalid operation transition")
        result_json = _validate_operation_result(result)
        result_value = json.loads(result_json)
        if error_code is not None and (
            not isinstance(error_code, str) or error_code not in _OPERATION_ERROR_CODES
        ):
            raise CatalogError("invalid operation error code")
        if result_value.get("error_code") is not None:
            if error_code is not None and error_code != result_value["error_code"]:
                raise CatalogError("invalid operation error code")
            error_code = result_value["error_code"]
        failed = new_state in {"retryable_failed", "permanent_failed"}
        if failed != (error_code is not None):
            raise CatalogError("invalid operation error code")
        self._ensure_open()
        try:
            with self.transaction() as connection:
                cursor = connection.execute(
                    """UPDATE operations SET state = ?, result_json = ?, error_code = ?,
                              updated_at = CURRENT_TIMESTAMP
                       WHERE operation_id = ? AND state = ?""",
                    (new_state, result_json, error_code, operation_id, expected_state),
                )
                if cursor.rowcount != 1:
                    current = connection.execute(
                        "SELECT state FROM operations WHERE operation_id = ?", (operation_id,)
                    ).fetchone()
                    if current is None:
                        raise CatalogError("operation not found")
                    raise OperationConflict("operation state conflict")
                row = connection.execute(
                    """SELECT operation_id, operation_kind, state, result_json,
                              error_code, created_at, updated_at
                       FROM operations WHERE operation_id = ?""",
                    (operation_id,),
                ).fetchone()
                return self._operation_from_row(row)
        except (OperationConflict, CatalogError):
            raise
        except sqlite3.Error:
            raise CatalogError("catalog operation failed") from None

    @staticmethod
    def _operation_from_row(row: sqlite3.Row) -> dict[str, Any]:
        try:
            operation_id = row["operation_id"]
            operation_kind = row["operation_kind"]
            state = row["state"]
            if (
                not isinstance(operation_id, str)
                or not _OPERATION_ID.fullmatch(operation_id)
                or operation_kind not in _OPERATION_KINDS
                or state not in _OPERATION_TRANSITIONS
            ):
                raise CatalogError("invalid stored operation")
            raw_result = json.loads(row["result_json"])
            result_json = _validate_operation_result(raw_result)
            result = json.loads(result_json)
            error_code = row["error_code"]
            if error_code is not None and error_code not in _OPERATION_ERROR_CODES:
                raise CatalogError("invalid stored operation")
            if (state in {"retryable_failed", "permanent_failed"}) != (error_code is not None):
                raise CatalogError("invalid stored operation")
            if error_code is not None:
                if result.get("error_code") not in (None, error_code):
                    raise CatalogError("invalid stored operation")
                result["error_code"] = error_code
            return {
                "operation_id": operation_id,
                "operation_kind": operation_kind,
                "state": state,
                "result": result,
                "error_code": error_code,
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
        except (KeyError, TypeError, ValueError, CatalogError):
            raise CatalogError("invalid stored operation") from None

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run a bounded SQLite transaction and roll back every failed body."""
        if self._closed:
            raise CatalogError("catalog is closed")
        nested = self._connection.in_transaction
        savepoint = "catalog_" + uuid.uuid4().hex
        try:
            self._connection.execute(f"SAVEPOINT {savepoint}" if nested else "BEGIN IMMEDIATE")
            yield self._connection
            self._connection.execute(f"RELEASE SAVEPOINT {savepoint}" if nested else "COMMIT")
        except BaseException as exc:
            if self._connection.in_transaction:
                try:
                    if nested:
                        self._connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                        self._connection.execute(f"RELEASE SAVEPOINT {savepoint}")
                    else:
                        self._connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            if isinstance(exc, sqlite3.Error):
                busy = ((getattr(exc, "sqlite_errorcode", None) or 0) & 0xFF) in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
                raise (CatalogBusy if busy else CatalogError)("catalog operation failed") from None
            raise

    @staticmethod
    def category_result_receipt(body: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(body, dict):
            raise CatalogError("invalid category result")
        if "status" in body:
            receipt = {"kind": "classification", "status": body["status"], "category_id": body["category_id"]}
            if body["status"] == "pending":
                proposal = body["proposal"]
                receipt.update(proposal_id=proposal["proposal_id"], revision=proposal["revision"], budget_exceeded=body["budget_exceeded"])
        elif "proposal_id" in body:
            receipt = {"kind": "proposal", **{key: body[key] for key in ("proposal_id", "revision", "state", "category_id")}}
        elif "source_id" in body:
            receipt = {"kind": "assignment", **{key: body[key] for key in ("source_id", "category_id", "revision")}}
        else:
            receipt = {"kind": "category", **{key: body[key] for key in ("category_id", "revision")}}
        return _validate_category_result(receipt)

    def category_result_response(self, receipt: dict[str, Any], intent: dict[str, Any]) -> dict[str, Any]:
        receipt = _validate_category_result(receipt)
        if receipt["kind"] == "classification":
            result = {"status": receipt["status"], "category_id": receipt["category_id"], "tags": intent["classification"]["tags"]}
            if receipt["status"] == "pending":
                result.update(proposal=self.get_category_proposal(receipt["proposal_id"]), budget_exceeded=receipt["budget_exceeded"], reuse_candidates=self.list_categories())
            return result
        if receipt["kind"] == "proposal":
            result = self.get_category_proposal(receipt["proposal_id"])
        elif receipt["kind"] == "category":
            result = next((row for row in self.list_categories() if row["category_id"] == receipt["category_id"]), None)
        else:
            result = {key: receipt[key] for key in ("source_id", "category_id", "revision")}
        if result is None:
            raise CatalogConflict("category receipt target unavailable")
        return result

    @contextlib.contextmanager
    def category_operation(self, operation_id: str) -> Iterator[dict[str, Any]]:
        """Commit registry mutation and its operation checkpoint in one transaction."""
        with self.transaction():
            operation = self.get_operation(operation_id)
            if operation is None or operation["operation_kind"] != "category_decision" or operation["state"] not in {"pending", "running", "retryable_failed"}:
                raise OperationConflict("category operation conflict")
            intent = operation["result"].get("recovery_intent")
            if intent is None or intent["kind"] != "category_decision":
                raise CatalogError("category operation intent required")
            if operation["state"] != "running":
                operation = self.transition_operation(operation_id, operation["state"], "running", result=operation["result"])
            yield operation
            self.transition_operation(operation_id, "running", "completed", result=operation["result"])

    def register_manifest(
        self, manifest: SourceManifest, *, transfer_operation_id: str | None = None,
        layer: str = "journal"
    ) -> None:
        if not isinstance(manifest, SourceManifest):
            raise CatalogError("invalid source manifest")
        _validate_identity(manifest.source_id, manifest.source_version)
        if not isinstance(layer, str) or layer not in {"journal", "document"}:
            raise CatalogError("invalid source layer")
        manifest_json = manifest.to_bytes().decode("utf-8")
        provenance_json = json.dumps(
            manifest.to_dict()["provenance"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        manifest_path = (
            f"sources/{manifest.source_id}/v{manifest.source_version}/manifest.json"
        )
        try:
            with self.transaction() as connection:
                if transfer_operation_id is not None:
                    if not isinstance(transfer_operation_id, str) or not _OPERATION_ID.fullmatch(
                        transfer_operation_id
                    ):
                        raise CatalogError("invalid transfer identity")
                    transfer = connection.execute(
                        "SELECT operation_kind,state FROM operations WHERE operation_id=?",
                        (transfer_operation_id,),
                    ).fetchone()
                    transfer_item = connection.execute(
                        "SELECT lifecycle_state FROM transfer_items WHERE operation_id=? "
                        "AND source_id=? AND source_version=?",
                        (transfer_operation_id, manifest.source_id, manifest.source_version),
                    ).fetchone()
                    if (
                        transfer is None
                        or transfer["operation_kind"] != "import"
                        or transfer["state"] != "running"
                        or transfer_item is None
                        or transfer_item["lifecycle_state"] not in {"active", "forgotten"}
                    ):
                        raise CatalogConflict("transfer manifest conflict")
                    tombstones = connection.execute(
                        "SELECT source_version,tombstone_type FROM tombstones "
                        "WHERE source_id=? UNION ALL "
                        "SELECT source_version,tombstone_type FROM transfer_tombstones "
                        "WHERE source_id=?",
                        (manifest.source_id, manifest.source_id),
                    ).fetchall()
                    if any(
                        row["tombstone_type"] != "forgotten"
                        or connection.execute(
                            "SELECT 1 FROM transfer_items WHERE operation_id=? "
                            "AND source_id=? AND source_version=? AND lifecycle_state='forgotten'",
                            (transfer_operation_id, manifest.source_id, row["source_version"]),
                        ).fetchone() is None
                        for row in tombstones
                    ):
                        raise CatalogConflict("source identity is tombstoned")
                elif connection.execute(
                    "SELECT 1 FROM tombstones WHERE source_id=? LIMIT 1",
                    (manifest.source_id,),
                ).fetchone() is not None:
                    raise CatalogConflict("source identity is tombstoned")
                if transfer_operation_id is None and connection.execute(
                    "SELECT 1 FROM transfer_tombstones WHERE source_id=? LIMIT 1",
                    (manifest.source_id,),
                ).fetchone() is not None:
                    raise CatalogConflict("source identity is tombstoned")
                source = connection.execute("SELECT layer FROM sources WHERE source_id=?", (manifest.source_id,)).fetchone()
                if source is not None and source["layer"] != layer:
                    raise CatalogConflict("source layer conflict")
                existing = connection.execute(
                    "SELECT manifest_json FROM source_versions "
                    "WHERE source_id = ? AND source_version = ?",
                    (manifest.source_id, manifest.source_version),
                ).fetchone()
                if existing is not None:
                    if existing["manifest_json"] != manifest_json:
                        raise CatalogConflict("source version identity conflict")
                    return
                connection.execute(
                    "INSERT OR IGNORE INTO sources(source_id,layer) VALUES (?,?)",
                    (manifest.source_id, layer),
                )
                connection.execute(
                    """
                    INSERT INTO source_versions(
                        source_id, source_version, sha256, size_bytes,
                        provenance_json, manifest_path, manifest_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        manifest.source_id,
                        manifest.source_version,
                        manifest.sha256,
                        manifest.size_bytes,
                        provenance_json,
                        manifest_path,
                        manifest_json,
                    ),
                )
                identity = (manifest.source_id, manifest.source_version)
                connection.execute(
                    "INSERT INTO provider_states(source_id, source_version) VALUES (?, ?)",
                    identity,
                )
                connection.execute(
                    "INSERT INTO projection_states(source_id, source_version) VALUES (?, ?)",
                    identity,
                )
        except (CatalogConflict, CatalogError):
            raise
        except sqlite3.Error:
            raise CatalogError("catalog operation failed") from None

    def register_document_metadata(
        self, source_id: str, source_version: int, *, filename: str, title: str, mime_type: str
    ) -> dict[str, Any]:
        """Bind immutable descriptive metadata to an original version; bootstrap Inbox/stages."""
        identity = _validate_identity(source_id, source_version)
        fields = _validate_document_fields(filename, title, mime_type)
        with self.transaction() as connection:
            source = connection.execute("SELECT layer FROM sources WHERE source_id=?", (source_id,)).fetchone()
            if source is None or source["layer"] != "document":
                raise CatalogError("document source required")
            old = connection.execute("SELECT * FROM document_metadata WHERE source_id=? AND source_version=?", identity).fetchone()
            if old is not None:
                if any(old[key] != value for key, value in fields.items()):
                    raise CatalogConflict("document metadata conflict")
            else:
                connection.execute("INSERT INTO document_metadata(source_id,source_version,filename,title,mime_type) VALUES(?,?,?,?,?)", (*identity, filename, title, mime_type))
                connection.execute("INSERT OR IGNORE INTO document_categories(source_id) VALUES(?)", (source_id,))
                connection.executemany("INSERT INTO document_stages(source_id,source_version,stage,state) VALUES(?,?,?,?)", [(*identity, stage, "accepted" if stage == "custody" else "pending") for stage in _DOCUMENT_STAGE_STATES])
        return self.get_document_metadata(*identity)

    def get_document_metadata(self, source_id: str, source_version: int) -> dict[str, Any] | None:
        identity = _validate_identity(source_id, source_version)
        self._ensure_open()
        row = self._connection.execute("SELECT m.*,c.category_id,c.revision AS category_revision FROM document_metadata m JOIN document_categories c USING(source_id) WHERE m.source_id=? AND m.source_version=?", identity).fetchone()
        return dict(row) if row else None

    def list_documents(self, *, limit: int = 100, deleted: bool = True) -> list[dict[str, Any]]:
        """Document versions with their category; ``deleted=False`` leaves out deleted documents."""
        _validate_identity("limit", limit)
        if limit > 4096:
            raise CatalogError("invalid document limit")
        self._ensure_open()
        live = "" if deleted else f"WHERE {_VERSION_GONE.format(sid='m.source_id', version='m.source_version')}<2 "
        return [dict(row) for row in self._connection.execute("SELECT m.*,c.category_id,c.revision AS category_revision FROM document_metadata m JOIN document_categories c USING(source_id) "
                                                              f"{live}ORDER BY m.source_id,m.source_version LIMIT ?", (limit,))]

    def assign_document_category(self, source_id: str, category_id: str, *, expected_revision: int) -> dict[str, Any]:
        _validate_identity(source_id, expected_revision)
        _validate_identity(category_id, 1)
        with self.transaction() as connection:
            changed = connection.execute("UPDATE document_categories SET category_id=?,revision=revision+1 WHERE source_id=? AND revision=?", (category_id, source_id, expected_revision))
            if changed.rowcount != 1:
                raise CatalogConflict("document category conflict")
            self._invalidate_document_navigation(connection, [source_id])
            return dict(connection.execute("SELECT * FROM document_categories WHERE source_id=?", (source_id,)).fetchone())

    def record_extraction(
        self, source_id: str, source_version: int, extraction_revision: int, *,
        original_sha256: str, extractor: str, extractor_version: str, config_sha256: str,
        artifact_path: str | None, artifact_sha256: str | None, status: str,
        passage_count: int, warnings: list[str] | None = None,
    ) -> dict[str, Any]:
        identity = _validate_identity(source_id, source_version)
        _validate_identity(source_id, extraction_revision)
        for checksum in (original_sha256, config_sha256):
            if _validate_hash(checksum) is None:
                raise CatalogError("extraction checksum required")
        _validate_hash(artifact_sha256)
        _validate_projection_path(artifact_path)
        _document_text(extractor, 256)
        _document_text(extractor_version, 256)
        if not isinstance(status, str) or status not in {"completed", "blocked", "failed", "unsupported", "locked", "limit_exceeded"} or isinstance(passage_count, bool) or not isinstance(passage_count, int) or not 0 <= passage_count <= 2_147_483_647:
            raise CatalogError("invalid extraction state")
        if (artifact_path is None) != (artifact_sha256 is None) or (status == "completed" and (artifact_path is None or passage_count == 0)):
            raise CatalogError("invalid extraction artifact")
        warnings_json = _document_warnings([] if warnings is None else warnings)
        values = (*identity, extraction_revision, original_sha256, extractor, extractor_version, config_sha256, artifact_path, artifact_sha256, status, warnings_json, passage_count)
        with self.transaction() as connection:
            manifest = connection.execute("SELECT sha256 FROM source_versions WHERE source_id=? AND source_version=?", identity).fetchone()
            if manifest is None or manifest[0] != original_sha256:
                raise CatalogConflict("extraction original conflict")
            old = connection.execute("SELECT source_id,source_version,extraction_revision,original_sha256,extractor,extractor_version,config_sha256,artifact_path,artifact_sha256,status,warnings_json,passage_count FROM extractions WHERE source_id=? AND source_version=? AND extraction_revision=?", (*identity, extraction_revision)).fetchone()
            if old is not None and tuple(old) != values:
                raise CatalogConflict("extraction revision conflict")
            if old is None:
                connection.execute("INSERT INTO extractions(source_id,source_version,extraction_revision,original_sha256,extractor,extractor_version,config_sha256,artifact_path,artifact_sha256,status,warnings_json,passage_count) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", values)
        return self.get_extraction(*identity, extraction_revision)

    def get_extraction(self, source_id: str, source_version: int, extraction_revision: int) -> dict[str, Any] | None:
        identity = _validate_identity(source_id, source_version)
        _validate_identity(source_id, extraction_revision)
        self._ensure_open()
        row = self._connection.execute("SELECT * FROM extractions WHERE source_id=? AND source_version=? AND extraction_revision=?", (*identity, extraction_revision)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["warnings"] = json.loads(result.pop("warnings_json"))
        return result

    def list_extractions(self, source_id: str, source_version: int) -> list[dict[str, Any]]:
        identity = _validate_identity(source_id, source_version)
        self._ensure_open()
        revisions = [row[0] for row in self._connection.execute("SELECT extraction_revision FROM extractions WHERE source_id=? AND source_version=? ORDER BY extraction_revision", identity)]
        return [self.get_extraction(*identity, revision) for revision in revisions]

    def get_document_stage(self, source_id: str, source_version: int, stage: str) -> dict[str, Any] | None:
        identity = _validate_identity(source_id, source_version)
        if not isinstance(stage, str) or stage not in _DOCUMENT_STAGE_STATES:
            raise CatalogError("invalid document stage")
        self._ensure_open()
        row = self._connection.execute("SELECT * FROM document_stages WHERE source_id=? AND source_version=? AND stage=?", (*identity, stage)).fetchone()
        return dict(row) if row else None

    def transition_document_stage(
        self, source_id: str, source_version: int, stage: str, *, expected_state: str,
        expected_revision: int, state: str, error_code: str | None = None,
    ) -> dict[str, Any]:
        identity = _validate_identity(source_id, source_version)
        _validate_identity(source_id, expected_revision)
        if not isinstance(stage, str) or stage not in _DOCUMENT_STAGE_STATES:
            raise CatalogError("invalid document stage")
        allowed = _DOCUMENT_STAGE_STATES[stage]
        if not isinstance(state, str) or not isinstance(expected_state, str) or state not in allowed or expected_state not in allowed:
            raise CatalogError("invalid document stage state")
        transitions = {"pending": {"running", "failed"}, "running": allowed - {"pending", "running"}, "failed": {"running"}, "blocked": {"running"}, "conflict": {"running"}, "completed": {"pending"}, "accepted": set()}
        if state not in transitions[expected_state]:
            raise CatalogConflict("document stage transition conflict")
        if error_code is not None and error_code not in _OPERATION_ERROR_CODES:
            raise CatalogError("invalid document error code")
        if error_code is not None and state not in {"failed", "blocked", "conflict"}:
            raise CatalogError("invalid document error state")
        with self.transaction() as connection:
            if stage == "indexing" and state == "completed":
                latest = connection.execute("SELECT extraction_revision FROM extractions WHERE source_id=? AND source_version=? AND status='completed' ORDER BY extraction_revision DESC LIMIT 1", identity).fetchone()
                if latest is None:
                    raise CatalogConflict("document indexing incomplete")
                self._require_indexed_extraction(connection, *identity, latest[0])
            changed = connection.execute("UPDATE document_stages SET state=?,revision=revision+1,attempt=attempt+?,error_code=?,updated_at=CURRENT_TIMESTAMP WHERE source_id=? AND source_version=? AND stage=? AND state=? AND revision=?", (state, int(state == "running"), error_code, *identity, stage, expected_state, expected_revision))
            if changed.rowcount != 1:
                raise CatalogConflict("document stage conflict")
        return self.get_document_stage(*identity, stage)

    def record_provider_chunk(
        self, provider_id: str, source_id: str, source_version: int, extraction_revision: int, *,
        passage_start: int, passage_end: int, content_sha256: str,
    ) -> dict[str, Any]:
        identity = _validate_identity(source_id, source_version)
        _validate_identity(source_id, extraction_revision)
        _document_text(provider_id, 512)
        if _validate_hash(content_sha256) is None:
            raise CatalogError("chunk checksum required")
        self._validate_passage(passage_start, passage_end)
        values = (provider_id, *identity, extraction_revision, passage_start, passage_end, content_sha256)
        with self.transaction() as connection:
            self._require_passage(connection, *identity, extraction_revision, passage_start, passage_end)
            old = connection.execute("SELECT provider_id,source_id,source_version,extraction_revision,passage_start,passage_end,content_sha256 FROM provider_chunks WHERE provider_id=?", (provider_id,)).fetchone()
            if old is not None and tuple(old) != values:
                raise CatalogConflict("provider chunk identity conflict")
            if old is None:
                connection.execute("INSERT INTO provider_chunks(provider_id,source_id,source_version,extraction_revision,passage_start,passage_end,content_sha256) VALUES(?,?,?,?,?,?,?)", values)
        return self.get_provider_chunk(provider_id)

    @staticmethod
    def _validate_passage(start: int, end: int) -> None:
        if isinstance(start, bool) or isinstance(end, bool) or not isinstance(start, int) or not isinstance(end, int) or not 0 <= start < end <= 2_147_483_647:
            raise CatalogError("invalid passage range")

    @staticmethod
    def _require_passage(connection: sqlite3.Connection, source_id: str, source_version: int, extraction_revision: int, start: int, end: int) -> sqlite3.Row:
        row = connection.execute("SELECT status,passage_count,original_sha256 FROM extractions WHERE source_id=? AND source_version=? AND extraction_revision=?", (source_id, source_version, extraction_revision)).fetchone()
        tombstone = connection.execute("SELECT 1 FROM tombstones WHERE source_id=? UNION ALL SELECT 1 FROM transfer_tombstones WHERE source_id=?", (source_id, source_id)).fetchone()
        if row is None or row["status"] != "completed" or end > row["passage_count"] or tombstone is not None:
            raise CatalogConflict("document evidence unavailable")
        return row

    def _require_indexed_extraction(self, connection: sqlite3.Connection, source_id: str, source_version: int, extraction_revision: int) -> None:
        latest = connection.execute("SELECT extraction_revision FROM extractions WHERE source_id=? AND source_version=? AND status='completed' ORDER BY extraction_revision DESC LIMIT 1", (source_id, source_version)).fetchone()
        if latest is None or latest[0] != extraction_revision:
            raise CatalogConflict("document extraction superseded")
        chunks = connection.execute("SELECT submission_state,completion_state,passage_start,passage_end FROM provider_chunks WHERE source_id=? AND source_version=? AND extraction_revision=? AND submission_state != 'retired' ORDER BY passage_start", (source_id, source_version, extraction_revision)).fetchall()
        if not chunks or any(row[0] != "submitted" or row[1] != "completed" for row in chunks):
            raise CatalogConflict("document indexing incomplete")
        extraction = self._require_passage(connection, source_id, source_version, extraction_revision, 0, chunks[-1][3])
        cursor = 0
        for chunk in chunks:
            if chunk[2] != cursor:
                raise CatalogConflict("document indexing coverage incomplete")
            cursor = chunk[3]
        if cursor != extraction["passage_count"]:
            raise CatalogConflict("document indexing coverage incomplete")

    def get_provider_chunk(self, provider_id: str) -> dict[str, Any] | None:
        _document_text(provider_id, 512)
        self._ensure_open()
        row = self._connection.execute("SELECT * FROM provider_chunks WHERE provider_id=?", (provider_id,)).fetchone()
        return dict(row) if row else None

    def list_provider_chunks(self, source_id: str, source_version: int, *, extraction_revision: int | None = None) -> list[dict[str, Any]]:
        identity = _validate_identity(source_id, source_version)
        if extraction_revision is not None:
            _validate_identity(source_id, extraction_revision)
        self._ensure_open()
        return [dict(row) for row in self._connection.execute("SELECT * FROM provider_chunks WHERE source_id=? AND source_version=? AND (? IS NULL OR extraction_revision=?) ORDER BY extraction_revision,passage_start,provider_id", (*identity, extraction_revision, extraction_revision))]

    def update_provider_chunk(self, provider_id: str, *, expected_revision: int, submission_state: str, completion_state: str) -> dict[str, Any]:
        _document_text(provider_id, 512)
        _validate_identity("chunk", expected_revision)
        if submission_state not in _PROVIDER_SUBMISSION_STATES | {"retired"} or completion_state not in _PROVIDER_COMPLETION_STATES:
            raise CatalogError("invalid provider chunk state")
        if completion_state == "completed" and submission_state not in {"submitted", "retired"}:
            raise CatalogError("invalid provider chunk acknowledgement")
        with self.transaction() as connection:
            old = connection.execute("SELECT * FROM provider_chunks WHERE provider_id=?", (provider_id,)).fetchone()
            if old is None or old["revision"] != expected_revision or (old["submission_state"] == "retired" and submission_state != "retired"):
                raise CatalogConflict("provider chunk conflict")
            if submission_state != "retired":
                self._require_passage(connection, old["source_id"], old["source_version"], old["extraction_revision"], old["passage_start"], old["passage_end"])
            connection.execute("UPDATE provider_chunks SET submission_state=?,completion_state=?,revision=revision+1,updated_at=CURRENT_TIMESTAMP WHERE provider_id=? AND revision=?", (submission_state, completion_state, provider_id, expected_revision))
        return self.get_provider_chunk(provider_id)

    def list_categories(self) -> list[dict[str, Any]]:
        self._ensure_open()
        result = []
        for row in self._connection.execute("SELECT * FROM categories ORDER BY category_id"):
            item = dict(row)
            item["aliases"] = json.loads(item.pop("aliases_json"))
            result.append(item)
        return result

    def record_category_proposal(
        self, proposal_id: str, *, name: str, description: str, rationale: str,
        documents: list[dict[str, Any]], closest_category_id: str | None = None,
    ) -> dict[str, Any]:
        _validate_identity(proposal_id, 1)
        _document_text(name, 256)
        _document_text(description, 4096, empty=True)
        _document_text(rationale, 4096, empty=True)
        if closest_category_id is not None:
            _validate_identity(closest_category_id, 1)
        if not isinstance(documents, list) or not 1 <= len(documents) <= 100:
            raise CatalogError("invalid proposal documents")
        identities = set()
        for document in documents:
            if not isinstance(document, dict) or set(document) != {"source_id", "source_version"}:
                raise CatalogError("invalid proposal document")
            identities.add(_validate_identity(document["source_id"], document["source_version"]))
        name_key = " ".join(name.split()).casefold()
        if not name_key:
            raise CatalogError("invalid category name")
        with self.transaction() as connection:
            existing_proposal = False
            old = connection.execute("SELECT * FROM category_proposals WHERE proposal_id=?", (proposal_id,)).fetchone()
            if old is not None:
                existing_proposal = True
            if old is not None:
                if any(old[key] != value for key, value in {"name": name, "description": description, "rationale": rationale, "closest_category_id": closest_category_id}.items()) or old["state"] != "pending":
                    raise CatalogConflict("category proposal conflict")
            else:
                duplicate = connection.execute("SELECT proposal_id FROM category_proposals WHERE name_key=? AND state='pending'", (name_key,)).fetchone()
                if duplicate is not None:
                    proposal_id = duplicate[0]
                    existing_proposal = True
                else:
                    connection.execute("INSERT INTO category_proposals(proposal_id,name,name_key,description,rationale,closest_category_id) VALUES(?,?,?,?,?,?)", (proposal_id, name, name_key, description, rationale, closest_category_id))
            inserted = connection.executemany("INSERT OR IGNORE INTO category_proposal_documents(proposal_id,source_id,source_version) VALUES(?,?,?)", [(proposal_id, *identity) for identity in sorted(identities)])
            if existing_proposal and inserted.rowcount > 0:
                connection.execute("UPDATE category_proposals SET revision=revision+1 WHERE proposal_id=?", (proposal_id,))
        return self.get_category_proposal(proposal_id)

    def get_category_proposal(self, proposal_id: str) -> dict[str, Any] | None:
        _validate_identity(proposal_id, 1)
        self._ensure_open()
        row = self._connection.execute("SELECT * FROM category_proposals WHERE proposal_id=?", (proposal_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["documents"] = [dict(item) for item in self._connection.execute("SELECT source_id,source_version FROM category_proposal_documents WHERE proposal_id=? ORDER BY source_id,source_version", (proposal_id,))]
        return result

    def list_category_proposals(self, *, state: str = "pending") -> list[dict[str, Any]]:
        if state not in {"pending", "approved", "rejected", "redirected"}:
            raise CatalogError("invalid proposal state")
        self._ensure_open()
        ids = [row[0] for row in self._connection.execute("SELECT proposal_id FROM category_proposals WHERE state=? ORDER BY proposal_id", (state,))]
        return [self.get_category_proposal(identity) for identity in ids]

    def decide_category_proposal(
        self, proposal_id: str, *, expected_revision: int, decision: str,
        category_id: str | None = None, category_budget: int = 10, budget_override: bool = False,
    ) -> dict[str, Any]:
        """Explicit human decision is the only catalog API that creates categories."""
        _validate_identity(proposal_id, expected_revision)
        _validate_identity("budget", category_budget)
        if not isinstance(budget_override, bool) or decision not in {"approve", "reject", "redirect"}:
            raise CatalogError("invalid category decision")
        if (decision != "reject") != (category_id is not None):
            raise CatalogError("invalid category decision target")
        if category_id is not None:
            _validate_identity(category_id, 1)
        with self.transaction() as connection:
            proposal = connection.execute("SELECT * FROM category_proposals WHERE proposal_id=?", (proposal_id,)).fetchone()
            if proposal is None or proposal["state"] != "pending" or proposal["revision"] != expected_revision:
                raise CatalogConflict("category proposal decision conflict")
            if decision == "approve":
                self._require_unique_category_labels(connection, proposal["name"], [], excluding=set())
                count = connection.execute("SELECT count(*) FROM categories WHERE category_id != 'inbox'").fetchone()[0]
                if count >= category_budget and not budget_override:
                    raise CatalogConflict("category budget exceeded")
                connection.execute("INSERT INTO categories(category_id,name,name_key,description) VALUES(?,?,?,?)", (category_id, proposal["name"], proposal["name_key"], proposal["description"]))
            elif decision == "redirect" and connection.execute("SELECT 1 FROM categories WHERE category_id=?", (category_id,)).fetchone() is None:
                raise CatalogError("category target unavailable")
            state = {"approve": "approved", "reject": "rejected", "redirect": "redirected"}[decision]
            connection.execute("UPDATE category_proposals SET state=?,category_id=?,revision=revision+1 WHERE proposal_id=?", (state, category_id, proposal_id))
            if category_id is not None:
                connection.execute("UPDATE document_categories SET category_id=?,revision=revision+1 WHERE source_id IN (SELECT source_id FROM category_proposal_documents WHERE proposal_id=?)", (category_id, proposal_id))
                affected = [row[0] for row in connection.execute("SELECT DISTINCT source_id FROM category_proposal_documents WHERE proposal_id=?", (proposal_id,))]
                self._invalidate_document_navigation(connection, affected)
        return self.get_category_proposal(proposal_id)

    @staticmethod
    def _require_unique_category_labels(connection: sqlite3.Connection, name: str, aliases: list[str], *, excluding: set[str]) -> None:
        keys = {" ".join(label.split()).casefold() for label in [name, *aliases]}
        if "" in keys:
            raise CatalogError("invalid category label")
        for row in connection.execute("SELECT category_id,name,aliases_json FROM categories"):
            if row["category_id"] in excluding:
                continue
            existing = {" ".join(label.split()).casefold() for label in [row["name"], *json.loads(row["aliases_json"])]}
            if keys & existing:
                raise CatalogConflict("category label conflict")

    def update_category(self, category_id: str, *, expected_revision: int, name: str, description: str, aliases: list[str]) -> dict[str, Any]:
        _validate_identity(category_id, expected_revision)
        _document_text(name, 256)
        _document_text(description, 4096, empty=True)
        if category_id == "inbox" or not name.strip() or not isinstance(aliases, list) or len(aliases) > 100:
            raise CatalogError("invalid category update")
        normalized = [_document_text(alias, 256) for alias in aliases]
        if len(set(" ".join(alias.split()).casefold() for alias in normalized)) != len(normalized):
            raise CatalogError("duplicate category alias")
        with self.transaction() as connection:
            self._require_unique_category_labels(connection, name, normalized, excluding={category_id})
            changed = connection.execute("UPDATE categories SET name=?,name_key=?,description=?,aliases_json=?,revision=revision+1 WHERE category_id=? AND revision=?", (name, " ".join(name.split()).casefold(), description, json.dumps(normalized), category_id, expected_revision))
            if changed.rowcount != 1:
                raise CatalogConflict("category update conflict")
            affected = [row[0] for row in connection.execute("SELECT source_id FROM document_categories WHERE category_id=?", (category_id,))]
            self._invalidate_document_navigation(connection, affected)
        return next(item for item in self.list_categories() if item["category_id"] == category_id)

    @staticmethod
    def _invalidate_document_navigation(connection: sqlite3.Connection, source_ids: list[str]) -> None:
        for source_id in source_ids:
            connection.execute("UPDATE document_stages SET state='pending',revision=revision+1,error_code=NULL,updated_at=CURRENT_TIMESTAMP WHERE source_id=? AND stage='projection'", (source_id,))
            connection.execute("UPDATE wiki_pages SET state='stale' WHERE state != 'conflict' AND EXISTS (SELECT 1 FROM wiki_dependencies d WHERE d.page_id=wiki_pages.page_id AND d.revision=wiki_pages.current_revision AND d.source_id=?)", (source_id,))

    def merge_categories(
        self, source_category_id: str, target_category_id: str, *,
        expected_source_revision: int, expected_target_revision: int,
    ) -> dict[str, Any]:
        """Merge registry/navigation metadata without moving or reindexing evidence."""
        _validate_identity(source_category_id, expected_source_revision)
        _validate_identity(target_category_id, expected_target_revision)
        if source_category_id == "inbox" or source_category_id == target_category_id:
            raise CatalogError("invalid category merge")
        with self.transaction() as connection:
            source = connection.execute("SELECT * FROM categories WHERE category_id=?", (source_category_id,)).fetchone()
            target = connection.execute("SELECT * FROM categories WHERE category_id=?", (target_category_id,)).fetchone()
            if source is None or target is None or source["revision"] != expected_source_revision or target["revision"] != expected_target_revision:
                raise CatalogConflict("category merge conflict")
            aliases = json.loads(target["aliases_json"])
            keys = {" ".join(label.split()).casefold() for label in [target["name"], *aliases]}
            for alias in [source["name"], *json.loads(source["aliases_json"])]:
                key = " ".join(alias.split()).casefold()
                if key not in keys:
                    aliases.append(alias)
                    keys.add(key)
            self._require_unique_category_labels(connection, target["name"], aliases, excluding={source_category_id, target_category_id})
            if len(aliases) > 100:
                raise CatalogError("category aliases too large")
            affected = [row[0] for row in connection.execute("SELECT source_id FROM document_categories WHERE category_id=?", (source_category_id,))]
            connection.execute("UPDATE document_categories SET category_id=?,revision=revision+1 WHERE category_id=?", (target_category_id, source_category_id))
            connection.execute("UPDATE category_proposals SET category_id=?,revision=revision+1 WHERE category_id=?", (target_category_id, source_category_id))
            connection.execute("UPDATE category_proposals SET closest_category_id=?,revision=revision+1 WHERE closest_category_id=?", (target_category_id, source_category_id))
            connection.execute("UPDATE categories SET aliases_json=?,revision=revision+1 WHERE category_id=?", (json.dumps(aliases), target_category_id))
            connection.execute("DELETE FROM categories WHERE category_id=?", (source_category_id,))
            self._invalidate_document_navigation(connection, affected)
        return next(item for item in self.list_categories() if item["category_id"] == target_category_id)

    def export_document_snapshot(self, *, source_ids: list[str] | None = None) -> dict[str, Any]:
        if source_ids is not None:
            if not isinstance(source_ids, list) or len(source_ids) > 4096:
                raise CatalogError("invalid snapshot selection")
            for source_id in source_ids:
                _validate_identity(source_id, 1)
        with self.transaction():
            inventory = self.list_inventory()
            selected = {row["source_id"] for row in inventory} if source_ids is None else set(source_ids)
            pages = self.list_wiki_pages()
            histories = {page["page_id"]: self.list_wiki_revisions(page["page_id"]) for page in pages}
            proposals = [self.get_category_proposal(row[0]) for row in self._connection.execute("SELECT proposal_id FROM category_proposals ORDER BY proposal_id")]
            included_pages = set(histories) if source_ids is None else set()
            included_proposals = {row["proposal_id"] for row in proposals} if source_ids is None else set()
            while True:
                previous = (len(selected), len(included_pages), len(included_proposals))
                for page_id, revisions in histories.items():
                    dependencies = {dependency["source_id"] for revision in revisions for dependency in revision["dependencies"]}
                    if page_id in included_pages or dependencies & selected:
                        included_pages.add(page_id)
                        selected.update(dependencies)
                for proposal in proposals:
                    documents = {row["source_id"] for row in proposal["documents"]}
                    if proposal["proposal_id"] in included_proposals or documents & selected:
                        included_proposals.add(proposal["proposal_id"])
                        selected.update(documents)
                if previous == (len(selected), len(included_pages), len(included_proposals)):
                    break
            source_rows = []
            for row in inventory:
                if row["source_id"] in selected:
                    source = self.get_source(row["source_id"])
                    source_rows.append({"source_id": row["source_id"], "source_version": row["source_version"],
                        "layer": source["layer"] if source else "journal", "lifecycle_state": row["lifecycle_state"],
                        "sha256": row["manifest"].sha256 if row["lifecycle_state"] != "deleted" and row["manifest"] else None})
            deleted = {(row["source_id"], row["source_version"]) for row in source_rows if row["lifecycle_state"] == "deleted"}
            revisions = [row for page_id in sorted(included_pages) for row in histories[page_id]]
            if any((dep["source_id"], dep["source_version"]) in deleted for row in revisions for dep in row["dependencies"]):
                raise CatalogError("deleted wiki evidence unavailable")
            metadata = [self.get_document_metadata(row["source_id"], row["source_version"]) for row in source_rows if row["layer"] == "document" and row["lifecycle_state"] != "deleted"]
            if any(row is None for row in metadata):
                raise CatalogError("document metadata unavailable")
            extractions = [extraction for row in metadata for extraction in self.list_extractions(row["source_id"], row["source_version"])]
            stages = []
            for row in metadata:
                for stage in _DOCUMENT_STAGE_STATES:
                    current = self.get_document_stage(row["source_id"], row["source_version"], stage)
                    stages.append({key: current[key] for key in ("source_id", "source_version", "stage", "state", "revision", "attempt", "error_code")})
            included = [row for row in proposals if row["proposal_id"] in included_proposals]
            if any((row["source_id"], row["source_version"]) in deleted for proposal in included for row in proposal["documents"]):
                raise CatalogError("deleted proposal evidence unavailable")
            data = {"sources": source_rows, "metadata": metadata, "categories": self.list_categories(),
                "category_proposals": included, "extractions": extractions,
                "wiki_pages": [row for row in pages if row["page_id"] in included_pages], "wiki_revisions": revisions,
                "wiki_reviews": [{key: value for key, value in review.items() if key != "resolution" or value is not None}
                                 for page_id in sorted(included_pages)
                                 for review in self.find_wiki_reviews(page_id=page_id, limit=4096)[0]],
                "stage_intents": stages}
            return self._validate_document_snapshot(data)

    @staticmethod
    def _validate_document_snapshot(data: Any) -> dict[str, Any]:
        try:
            return LifeContextCatalog._validate_document_snapshot_rows(data)
        except CatalogError:
            raise
        except (TypeError, ValueError, KeyError, RecursionError, UnicodeError):
            raise CatalogError("invalid document snapshot") from None

    @staticmethod
    def _validate_document_snapshot_rows(data: Any) -> dict[str, Any]:
        fields = {
            "sources": ({"source_id", "source_version", "layer", "lifecycle_state", "sha256"}, set()),
            "metadata": ({"source_id", "source_version", "filename", "title", "mime_type", "category_id", "category_revision"}, set()),
            "categories": ({"category_id", "name", "name_key", "description", "aliases", "revision"}, {"created_at"}),
            "category_proposals": ({"proposal_id", "name", "name_key", "description", "rationale", "closest_category_id", "category_id", "state", "revision"}, {"created_at", "documents"}),
            "extractions": ({"source_id", "source_version", "extraction_revision", "original_sha256", "extractor", "extractor_version", "config_sha256", "artifact_sha256", "status", "warnings", "passage_count"}, {"created_at", "artifact_path"}),
            "wiki_pages": ({"page_id", "title", "current_revision", "state"}, {"created_at"}),
            "wiki_revisions": ({"page_id", "revision", "parent_revision", "artifact_sha256", "dependencies"}, {"created_at", "artifact_path"}),
            "wiki_reviews": ({"review_id", "page_id", "revision", "code", "details", "state"}, {"resolution"}),
            "stage_intents": ({"source_id", "source_version", "stage", "state"}, {"revision", "attempt", "error_code"}),
        }
        if not isinstance(data, dict) or set(data) - set(fields) or any(name not in data for name in fields if name != "sources"):
            raise CatalogError("invalid document snapshot")
        try:
            raw = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
            _utf8_size(raw, 4_194_304)
            result = json.loads(raw)
        except (TypeError, ValueError, RecursionError):
            raise CatalogError("invalid document snapshot") from None
        result.setdefault("sources", [])
        indexes = {}
        keys = {"sources": ("source_id", "source_version"), "metadata": ("source_id", "source_version"),
            "categories": ("category_id",), "category_proposals": ("proposal_id",),
            "extractions": ("source_id", "source_version", "extraction_revision"), "wiki_pages": ("page_id",),
            "wiki_revisions": ("page_id", "revision"), "wiki_reviews": ("review_id",), "stage_intents": ("source_id", "source_version", "stage")}
        for name, (required, optional) in fields.items():
            rows = result[name]
            if not isinstance(rows, list) or len(rows) > 4096:
                raise CatalogError("invalid snapshot count")
            index = {}
            for row in rows:
                if not isinstance(row, dict) or not required <= set(row) or set(row) - required - optional:
                    raise CatalogError("invalid snapshot record")
                for key in keys[name]:
                    value = row[key]
                    if key.endswith("id"):
                        _validate_identity(value, 1)
                    elif key != "stage":
                        _validate_identity("revision", value)
                identity = tuple(row[key] for key in keys[name])
                if identity in index:
                    raise CatalogError("duplicate snapshot identity")
                index[identity] = row
                if "created_at" in row:
                    _document_text(row["created_at"], 128)
            indexes[name] = index
        sources, metadata, categories = indexes["sources"], indexes["metadata"], indexes["categories"]
        source_layers, source_lifecycle = {}, {}
        for row in result["sources"]:
            if row["layer"] not in {"journal", "document"} or row["lifecycle_state"] not in {"active", "forgotten", "deleted"}:
                raise CatalogError("invalid snapshot source")
            checksum = _validate_hash(row["sha256"])
            if (row["lifecycle_state"] == "deleted") != (checksum is None):
                raise CatalogError("invalid snapshot checksum")
            sid = row["source_id"]
            if source_layers.setdefault(sid, row["layer"]) != row["layer"] or source_lifecycle.setdefault(sid, row["lifecycle_state"]) != row["lifecycle_state"]:
                raise CatalogError("snapshot source lifecycle conflict")
        labels = {}
        for row in result["categories"]:
            _validate_identity(row["category_id"], row["revision"])
            _document_text(row["name"], 256)
            _document_text(row["description"], 4096, empty=True)
            if not isinstance(row["aliases"], list) or len(row["aliases"]) > 100:
                raise CatalogError("invalid category aliases")
            name_key = " ".join(row["name"].split()).casefold()
            if not name_key or row["name_key"] != name_key:
                raise CatalogError("invalid category name key")
            alias_keys = []
            for alias in row["aliases"]:
                _document_text(alias, 256)
                alias_keys.append(" ".join(alias.split()).casefold())
            if "" in alias_keys or len(alias_keys) != len(set(alias_keys)):
                raise CatalogError("invalid category aliases")
            for key in {name_key, *alias_keys}:
                if labels.setdefault(key, row["category_id"]) != row["category_id"]:
                    raise CatalogConflict("category label conflict")
            if row["category_id"] == "inbox" and (row["name"], row["description"], row["aliases"], row["revision"]) != ("Inbox", "", [], 1):
                raise CatalogConflict("Inbox identity conflict")
        assignments = {}
        for identity, row in metadata.items():
            _validate_document_fields(row["filename"], row["title"], row["mime_type"])
            _validate_identity(row["category_id"], row["category_revision"])
            if (row["category_id"],) not in categories:
                raise CatalogError("snapshot category unavailable")
            if sources and (identity not in sources or sources[identity]["layer"] != "document" or sources[identity]["lifecycle_state"] == "deleted"):
                raise CatalogError("snapshot document source unavailable")
            assignment = (row["category_id"], row["category_revision"])
            if assignments.setdefault(row["source_id"], assignment) != assignment:
                raise CatalogError("snapshot category assignment conflict")
        pending_names = set()
        for row in result["category_proposals"]:
            _validate_identity(row["proposal_id"], row["revision"])
            _document_text(row["name"], 256)
            _document_text(row["description"], 4096, empty=True)
            _document_text(row["rationale"], 4096, empty=True)
            if not row["name"].strip() or row["name_key"] != " ".join(row["name"].split()).casefold() or row["state"] not in {"pending", "approved", "rejected", "redirected"}:
                raise CatalogError("invalid snapshot proposal")
            if (row["state"] in {"approved", "redirected"}) != (row["category_id"] is not None):
                raise CatalogError("invalid proposal decision")
            for key in ("closest_category_id", "category_id"):
                if row[key] is not None and (row[key],) not in categories:
                    raise CatalogError("proposal category unavailable")
            if row["state"] == "pending":
                if row["name_key"] in pending_names:
                    raise CatalogConflict("pending proposal name conflict")
                pending_names.add(row["name_key"])
            documents = row.setdefault("documents", [])
            if not isinstance(documents, list) or len(documents) > 100:
                raise CatalogError("invalid proposal documents")
            seen = set()
            for document in documents:
                if not isinstance(document, dict) or set(document) != {"source_id", "source_version"}:
                    raise CatalogError("invalid proposal document")
                identity = _validate_identity(document["source_id"], document["source_version"])
                if identity in seen or identity not in metadata:
                    raise CatalogError("proposal document unavailable")
                seen.add(identity)
            documents.sort(key=lambda item: (item["source_id"], item["source_version"]))
        extractions = indexes["extractions"]
        for identity, row in extractions.items():
            if identity[:2] not in metadata:
                raise CatalogError("extraction document unavailable")
            for key in ("original_sha256", "config_sha256"):
                if _validate_hash(row[key]) is None:
                    raise CatalogError("extraction checksum required")
            if sources and sources[identity[:2]]["sha256"] != row["original_sha256"]:
                raise CatalogConflict("extraction original conflict")
            for key in ("extractor", "extractor_version"):
                _document_text(row[key], 256)
            _validate_hash(row["artifact_sha256"])
            _validate_projection_path(row.get("artifact_path"))
            canonical_path = f"derived/{identity[0]}/v{identity[1]}/r{identity[2]}.json" if row["artifact_sha256"] else None
            if "artifact_path" in row and row["artifact_path"] != canonical_path:
                raise CatalogError("snapshot extraction artifact path conflict")
            if row["status"] not in {"completed", "blocked", "failed", "unsupported", "locked", "limit_exceeded"} or type(row["passage_count"]) is not int or not 0 <= row["passage_count"] <= 2_147_483_647:
                raise CatalogError("invalid extraction state")
            if row["status"] == "completed" and (row["artifact_sha256"] is None or not row["passage_count"]):
                raise CatalogError("extraction artifact unavailable")
            _document_warnings(row["warnings"])
        pages, revisions = indexes["wiki_pages"], indexes["wiki_revisions"]
        for identity, row in revisions.items():
            page_id, revision = identity
            if (page_id,) not in pages or row["parent_revision"] != (revision - 1 or None) or (revision > 1 and (page_id, revision - 1) not in revisions):
                raise CatalogError("invalid wiki ancestry")
            if _validate_hash(row["artifact_sha256"]) is None:
                raise CatalogError("wiki checksum required")
            _validate_projection_path(row.get("artifact_path"))
            canonical_path = f"wiki_revisions/{page_id}/r{revision}-{row['artifact_sha256']}.json"
            if "artifact_path" in row and row["artifact_path"] != canonical_path:
                raise CatalogError("snapshot wiki artifact path conflict")
            dependencies = row["dependencies"]
            if not isinstance(dependencies, list) or not 1 <= len(dependencies) <= 4096:
                raise CatalogError("wiki evidence required")
            seen = set()
            for dep in dependencies:
                if not isinstance(dep, dict) or set(dep) != {"source_id", "source_version", "extraction_revision", "passage_start", "passage_end", "original_sha256"}:
                    raise CatalogError("invalid wiki dependency")
                identity = (*_validate_identity(dep["source_id"], dep["source_version"]), dep["extraction_revision"])
                _validate_identity(dep["source_id"], dep["extraction_revision"])
                LifeContextCatalog._validate_passage(dep["passage_start"], dep["passage_end"])
                extraction = extractions.get(identity)
                if extraction is None or extraction["status"] != "completed" or extraction["original_sha256"] != dep["original_sha256"] or dep["passage_end"] > extraction["passage_count"]:
                    raise CatalogError("wiki evidence unavailable")
                key = (*identity, dep["passage_start"], dep["passage_end"])
                if key in seen:
                    raise CatalogError("duplicate wiki dependency")
                seen.add(key)
            dependencies.sort(key=lambda item: (item["source_id"], item["source_version"], item["extraction_revision"], item["passage_start"], item["passage_end"]))
        for (page_id,), row in pages.items():
            _document_text(row["title"], 1024)
            current = row["current_revision"]
            if type(current) is not int or current < 0 or row["state"] not in {"pending", "current", "stale", "conflict"} or (current and (page_id, current) not in revisions):
                raise CatalogError("invalid wiki page")
            if any(identity[0] == page_id and identity[1] > current for identity in revisions):
                raise CatalogError("invalid wiki pointer")
        for row in result["wiki_reviews"]:
            _validate_identity(row["page_id"], row["revision"])
            if (row["page_id"], row["revision"]) not in revisions or row["code"] not in {"manual_edit", "content_conflict", "contradiction"} or row["state"] not in {"pending", "resolved"}:
                raise CatalogError("invalid wiki review")
            details = row["details"]
            if not isinstance(details, dict) or set(details) - {"expected_hash", "actual_hash", "sources"}:
                raise CatalogError("invalid wiki review details")
            for key, value in details.items():
                if key == "sources":
                    if row["code"] == "contradiction" and isinstance(value, dict):
                        _validate_wiki_contradiction(value)
                    else:
                        _validate_operation_result({"sources": value})
                else:
                    _validate_hash(value)
            _utf8_size(json.dumps(details, ensure_ascii=False), 65536)
            if "resolution" in row:
                resolution = row["resolution"]
                if (row["state"] != "resolved" or not isinstance(resolution, dict) or set(resolution) != {"note", "resolved_at"}
                        or not isinstance(resolution["resolved_at"], str) or not _RESOLVED_AT.fullmatch(resolution["resolved_at"])):
                    raise CatalogError("invalid wiki review resolution")
                validate_review_note(resolution["note"])
        for row in result["stage_intents"]:
            identity = (row["source_id"], row["source_version"])
            if identity not in metadata or row["stage"] not in _DOCUMENT_STAGE_STATES or row["state"] not in _DOCUMENT_STAGE_STATES[row["stage"]]:
                raise CatalogError("invalid snapshot stage")
            if row["stage"] == "extraction" and row["state"] == "completed" and not any(key[:2] == identity and extraction["status"] == "completed" for key, extraction in extractions.items()):
                raise CatalogError("completed extraction unavailable")
            _validate_identity("revision", row.setdefault("revision", 1))
            attempt = row.setdefault("attempt", 0)
            error = row.setdefault("error_code", None)
            if type(attempt) is not int or not 0 <= attempt <= 2_147_483_647 or (error is not None and (error not in _OPERATION_ERROR_CODES or row["state"] not in {"blocked", "failed", "conflict"})):
                raise CatalogError("invalid snapshot stage facts")
        return result

    def preflight_document_snapshot(self, data: dict[str, Any]) -> dict[str, Any]:
        self._ensure_open()
        result = self._validate_document_snapshot(data)
        ignored = {"created_at", "artifact_path"}
        def same(old, new, *, extra_ignored=()):
            if old is not None and any(old.get(key) != value for key, value in new.items() if key not in ignored and key not in extra_ignored):
                raise CatalogConflict("document snapshot identity conflict")
        for row in result["sources"]:
            existing = self.get_source(row["source_id"])
            if existing and existing["layer"] != row["layer"]:
                raise CatalogConflict("source layer conflict")
            manifest = self.get_manifest(row["source_id"], row["source_version"])
            if manifest and row["sha256"] is not None and manifest["sha256"] != row["sha256"]:
                raise CatalogConflict("source checksum conflict")
            tombstones = self.list_tombstones(row["source_id"])
            if tombstones and any(item["tombstone_type"] != row["lifecycle_state"] for item in tombstones):
                raise CatalogConflict("source lifecycle conflict")
        existing_categories = {row["category_id"]: row for row in self.list_categories()}
        for row in result["categories"]:
            same(existing_categories.get(row["category_id"]), row)
            self._require_unique_category_labels(self._connection, row["name"], row["aliases"], excluding={row["category_id"]})
        for row in result["metadata"]:
            source = self.get_source(row["source_id"])
            if source and source["layer"] != "document":
                raise CatalogConflict("source layer conflict")
            same(self.get_document_metadata(row["source_id"], row["source_version"]), row)
            assignment = self._connection.execute("SELECT category_id,revision FROM document_categories WHERE source_id=?", (row["source_id"],)).fetchone()
            if assignment and (assignment["category_id"], assignment["revision"]) != (row["category_id"], row["category_revision"]):
                raise CatalogConflict("snapshot category assignment conflict")
        for row in result["category_proposals"]:
            same(self.get_category_proposal(row["proposal_id"]), row)
            if row["state"] == "pending":
                duplicate = self._connection.execute("SELECT proposal_id FROM category_proposals WHERE name_key=? AND state='pending'", (row["name_key"],)).fetchone()
                if duplicate and duplicate[0] != row["proposal_id"]:
                    raise CatalogConflict("pending proposal name conflict")
        for row in result["extractions"]:
            manifest = self.get_manifest(row["source_id"], row["source_version"])
            if manifest and manifest["sha256"] != row["original_sha256"]:
                raise CatalogConflict("extraction original conflict")
            same(self.get_extraction(row["source_id"], row["source_version"], row["extraction_revision"]), row)
        for row in result["wiki_pages"]:
            same(self.get_wiki_page(row["page_id"]), row, extra_ignored={"state"})
        for row in result["wiki_revisions"]:
            same(self.get_wiki_revision(row["page_id"], row["revision"]), row)
        for row in result["wiki_reviews"]:
            existing = next((item for item in self.list_wiki_reviews(row["page_id"]) if item["review_id"] == row["review_id"]), None)
            # Review state only moves from pending to resolved; restore merges it that way.
            same(existing, row, extra_ignored={"state", "resolution"})
            other = self._connection.execute("SELECT page_id FROM wiki_reviews WHERE review_id=?", (row["review_id"],)).fetchone()
            if other and other[0] != row["page_id"]:
                raise CatalogConflict("wiki review identity conflict")
        return result

    def restore_document_snapshot(self, data: dict[str, Any]) -> dict[str, Any]:
        with self.transaction() as connection:
            result = self.preflight_document_snapshot(data)
            for row in result["sources"]:
                identity = (row["source_id"], row["source_version"])
                if row["lifecycle_state"] == "deleted":
                    tombstone = self.get_tombstone(*identity)
                    if tombstone is None or tombstone["tombstone_type"] != "deleted":
                        raise CatalogConflict("deleted source reservation required")
                    connection.execute("INSERT OR IGNORE INTO sources(source_id,layer) VALUES(?,?)", (row["source_id"], row["layer"]))
                else:
                    manifest = self.get_manifest(*identity)
                    source = self.get_source(row["source_id"])
                    if manifest is None or manifest["sha256"] != row["sha256"] or source["layer"] != row["layer"]:
                        raise CatalogConflict("snapshot source registration required")
                    if row["lifecycle_state"] == "forgotten":
                        tombstone = self.get_tombstone(*identity)
                        if tombstone is None or tombstone["tombstone_type"] != "forgotten":
                            raise CatalogConflict("forgotten source reservation required")
            for row in result["categories"]:
                connection.execute("INSERT OR IGNORE INTO categories(category_id,name,name_key,description,aliases_json,revision,created_at) VALUES(?,?,?,?,?,?,COALESCE(?,CURRENT_TIMESTAMP))",
                    (row["category_id"], row["name"], row["name_key"], row["description"], json.dumps(row["aliases"]), row["revision"], row.get("created_at")))
            for row in result["metadata"]:
                identity = (row["source_id"], row["source_version"])
                source = self.get_source(row["source_id"])
                if source is None or source["layer"] != "document" or self.get_manifest(*identity) is None:
                    raise CatalogConflict("snapshot source registration required")
                old = self.get_document_metadata(*identity)
                if old is None:
                    connection.execute("INSERT INTO document_metadata(source_id,source_version,filename,title,mime_type) VALUES(?,?,?,?,?)", (*identity, row["filename"], row["title"], row["mime_type"]))
                    connection.execute("INSERT OR IGNORE INTO document_categories(source_id,category_id,revision) VALUES(?,?,?)", (row["source_id"], row["category_id"], row["category_revision"]))
                    connection.executemany("INSERT INTO document_stages(source_id,source_version,stage,state) VALUES(?,?,?,?)", [(*identity, stage, "accepted" if stage == "custody" else "pending") for stage in _DOCUMENT_STAGE_STATES])
            for row in result["extractions"]:
                identity = (row["source_id"], row["source_version"], row["extraction_revision"])
                if self.get_extraction(*identity) is None:
                    path = f"derived/{identity[0]}/v{identity[1]}/r{identity[2]}.json" if row["artifact_sha256"] else None
                    if row.get("artifact_path") is not None and row["artifact_path"] != path:
                        raise CatalogError("snapshot extraction artifact path conflict")
                    connection.execute("INSERT INTO extractions(source_id,source_version,extraction_revision,original_sha256,extractor,extractor_version,config_sha256,artifact_path,artifact_sha256,status,warnings_json,passage_count,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,COALESCE(?,CURRENT_TIMESTAMP))",
                        (*identity, row["original_sha256"], row["extractor"], row["extractor_version"], row["config_sha256"], path, row["artifact_sha256"], row["status"], json.dumps(row["warnings"]), row["passage_count"], row.get("created_at")))
            for row in result["category_proposals"]:
                connection.execute("INSERT OR IGNORE INTO category_proposals(proposal_id,name,name_key,description,rationale,closest_category_id,category_id,state,revision,created_at) VALUES(?,?,?,?,?,?,?,?,?,COALESCE(?,CURRENT_TIMESTAMP))",
                    (row["proposal_id"], row["name"], row["name_key"], row["description"], row["rationale"], row["closest_category_id"], row["category_id"], row["state"], row["revision"], row.get("created_at")))
                connection.executemany("INSERT OR IGNORE INTO category_proposal_documents(proposal_id,source_id,source_version) VALUES(?,?,?)", [(row["proposal_id"], doc["source_id"], doc["source_version"]) for doc in row["documents"]])
            for row in result["wiki_pages"]:
                connection.execute("INSERT OR IGNORE INTO wiki_pages(page_id,title,created_at) VALUES(?,?,COALESCE(?,CURRENT_TIMESTAMP))", (row["page_id"], row["title"], row.get("created_at")))
            for row in sorted(result["wiki_revisions"], key=lambda item: (item["page_id"], item["revision"])):
                path = f"wiki_revisions/{row['page_id']}/r{row['revision']}-{row['artifact_sha256']}.json"
                if row.get("artifact_path") is not None and row["artifact_path"] != path:
                    raise CatalogError("snapshot wiki artifact path conflict")
                connection.execute("INSERT OR IGNORE INTO wiki_revisions(page_id,revision,parent_revision,artifact_path,artifact_sha256,created_at) VALUES(?,?,?,?,?,COALESCE(?,CURRENT_TIMESTAMP))", (row["page_id"], row["revision"], row["parent_revision"], path, row["artifact_sha256"], row.get("created_at")))
                connection.executemany("INSERT OR IGNORE INTO wiki_dependencies(page_id,revision,source_id,source_version,extraction_revision,passage_start,passage_end,original_sha256) VALUES(?,?,?,?,?,?,?,?)", [(row["page_id"], row["revision"], dep["source_id"], dep["source_version"], dep["extraction_revision"], dep["passage_start"], dep["passage_end"], dep["original_sha256"]) for dep in row["dependencies"]])
            for row in result["wiki_pages"]:
                connection.execute("UPDATE wiki_pages SET current_revision=?,state=? WHERE page_id=?", (row["current_revision"], "stale" if row["current_revision"] else "pending", row["page_id"]))
            for row in result["wiki_reviews"]:
                connection.execute("INSERT OR IGNORE INTO wiki_reviews(review_id,page_id,revision,code,details_json,state) VALUES(?,?,?,?,?,?)", (row["review_id"], row["page_id"], row["revision"], row["code"], json.dumps(row["details"], sort_keys=True, separators=(",", ":")), row["state"]))
                # One way only: a resolved review upgrades a pending one and brings its note;
                # a pending one never reopens a review resolved here, whose own note is kept.
                if row["state"] == "resolved":
                    connection.execute("UPDATE wiki_reviews SET state='resolved' WHERE review_id=? AND state='pending'", (row["review_id"],))
                    if "resolution" in row:
                        connection.execute("INSERT OR IGNORE INTO wiki_review_resolutions(review_id,note,resolved_at) VALUES(?,?,?)",
                                           (row["review_id"], row["resolution"]["note"], row["resolution"]["resolved_at"]))
                stored = connection.execute("SELECT state FROM wiki_reviews WHERE review_id=?", (row["review_id"],)).fetchone()[0]
                if stored == "pending" and row["code"] in {"manual_edit", "content_conflict"}:
                    connection.execute("UPDATE wiki_pages SET state='conflict' WHERE page_id=? AND current_revision=?", (row["page_id"], row["revision"]))
            stages = {(row["source_id"], row["source_version"], row["stage"]): row for row in result["stage_intents"]}
            for document in result["metadata"]:
                identity = (document["source_id"], document["source_version"])
                for stage in _DOCUMENT_STAGE_STATES:
                    row = stages.get((*identity, stage), {"state": "pending", "revision": 1, "attempt": 0, "error_code": None})
                    state = "accepted" if stage == "custody" else "pending" if stage in {"indexing", "projection", "wiki"} or row["state"] == "running" else row["state"]
                    if stage == "extraction" and state == "completed" and not any(extraction["source_id"] == identity[0] and extraction["source_version"] == identity[1] and extraction["status"] == "completed" for extraction in result["extractions"]):
                        raise CatalogError("completed extraction unavailable")
                    error = row["error_code"] if state in {"blocked", "failed", "conflict"} else None
                    current = self.get_document_stage(*identity, stage)
                    revision = max(current["revision"], row["revision"]) + int((current["state"], current["error_code"]) != (state, error))
                    connection.execute("UPDATE document_stages SET state=?,revision=?,attempt=?,error_code=? WHERE source_id=? AND source_version=? AND stage=?", (state, revision, max(current["attempt"], row["attempt"]), error, *identity, stage))
            return result

    @contextlib.contextmanager
    def wiki_publication(self) -> Iterator[None]:
        with self.transaction():
            yield

    def create_wiki_page(self, page_id: str, *, title: str) -> dict[str, Any]:
        _validate_identity(page_id, 1)
        _document_text(title, 1024)
        with self.transaction() as connection:
            old = connection.execute("SELECT title FROM wiki_pages WHERE page_id=?", (page_id,)).fetchone()
            if old is not None and old[0] != title:
                raise CatalogConflict("wiki page identity conflict")
            connection.execute("INSERT OR IGNORE INTO wiki_pages(page_id,title) VALUES(?,?)", (page_id, title))
        return self.get_wiki_page(page_id)

    def get_wiki_page(self, page_id: str) -> dict[str, Any] | None:
        _validate_identity(page_id, 1)
        self._ensure_open()
        row = self._connection.execute("SELECT * FROM wiki_pages WHERE page_id=?", (page_id,)).fetchone()
        return dict(row) if row else None

    def list_wiki_pages(self) -> list[dict[str, Any]]:
        self._ensure_open()
        return [dict(row) for row in self._connection.execute("SELECT * FROM wiki_pages ORDER BY page_id")]

    def commit_wiki_revision(
        self, page_id: str, *, expected_revision: int, artifact_path: str,
        artifact_sha256: str, dependencies: list[dict[str, Any]],
    ) -> dict[str, Any]:
        _validate_identity(page_id, expected_revision + 1 if isinstance(expected_revision, int) and not isinstance(expected_revision, bool) else expected_revision)
        if expected_revision < 0:
            raise CatalogError("invalid wiki ancestry")
        if _validate_projection_path(artifact_path) is None or _validate_hash(artifact_sha256) is None:
            raise CatalogError("wiki artifact required")
        if not isinstance(dependencies, list) or not 1 <= len(dependencies) <= 4096:
            raise CatalogError("wiki evidence required")
        normalized = []
        for dependency in dependencies:
            if not isinstance(dependency, dict) or set(dependency) != {"source_id", "source_version", "extraction_revision", "passage_start", "passage_end", "original_sha256"}:
                raise CatalogError("invalid wiki dependency")
            _validate_identity(dependency["source_id"], dependency["source_version"])
            _validate_identity(dependency["source_id"], dependency["extraction_revision"])
            self._validate_passage(dependency["passage_start"], dependency["passage_end"])
            if _validate_hash(dependency["original_sha256"]) is None:
                raise CatalogError("wiki original checksum required")
            normalized.append(tuple(dependency[key] for key in ("source_id", "source_version", "extraction_revision", "passage_start", "passage_end", "original_sha256")))
        if len(set(normalized)) != len(normalized):
            raise CatalogError("duplicate wiki dependency")
        revision = expected_revision + 1
        with self.transaction() as connection:
            old = connection.execute("SELECT current_revision,state FROM wiki_pages WHERE page_id=?", (page_id,)).fetchone()
            if old is None or old[0] != expected_revision or old["state"] == "conflict":
                raise CatalogConflict("wiki ancestry conflict")
            for dependency in normalized:
                extraction = self._require_passage(connection, *dependency[:5])
                self._require_indexed_extraction(connection, *dependency[:3])
                current_version = connection.execute("SELECT max(source_version) FROM source_versions WHERE source_id=?", (dependency[0],)).fetchone()[0]
                if current_version != dependency[1]:
                    raise CatalogConflict("wiki source version superseded")
                if extraction["original_sha256"] != dependency[5]:
                    raise CatalogConflict("wiki checksum conflict")
            connection.execute("INSERT INTO wiki_revisions(page_id,revision,parent_revision,artifact_path,artifact_sha256) VALUES(?,?,?,?,?)", (page_id, revision, expected_revision or None, artifact_path, artifact_sha256))
            connection.executemany("INSERT INTO wiki_dependencies(page_id,revision,source_id,source_version,extraction_revision,passage_start,passage_end,original_sha256) VALUES(?,?,?,?,?,?,?,?)", [(page_id, revision, *dependency) for dependency in normalized])
            connection.execute("UPDATE wiki_pages SET current_revision=?,state='current' WHERE page_id=? AND current_revision=?", (revision, page_id, expected_revision))
        return self.get_wiki_revision(page_id, revision)

    def replace_wiki_artifact(self, page_id: str, revision: int, old_sha256: str, *, artifact_path: str, artifact_sha256: str) -> None:
        """Point a revision at the artifact that replaces its stored one after an erasure; ancestry and dependencies stay."""
        _validate_identity(page_id, revision)
        if _validate_projection_path(artifact_path) is None or _validate_hash(artifact_sha256) is None or _validate_hash(old_sha256) is None:
            raise CatalogError("wiki artifact required")
        with self.transaction() as connection:
            if connection.execute("UPDATE wiki_revisions SET artifact_path=?,artifact_sha256=? WHERE page_id=? AND revision=? AND artifact_sha256=?",
                                  (artifact_path, artifact_sha256, page_id, revision, old_sha256)).rowcount != 1:
                raise CatalogConflict("wiki artifact conflict")

    def retitle_wiki_page(self, page_id: str, title: str) -> None:
        """Give a page the withdrawn title after its document was deleted; a page's ID and revisions stay."""
        _validate_identity(page_id, 1)
        _document_text(title, 1024)
        with self.transaction() as connection:
            if connection.execute("UPDATE wiki_pages SET title=? WHERE page_id=?", (title, page_id)).rowcount != 1:
                raise CatalogConflict("wiki page identity conflict")

    def get_wiki_revision(self, page_id: str, revision: int) -> dict[str, Any] | None:
        _validate_identity(page_id, revision)
        self._ensure_open()
        row = self._connection.execute("SELECT * FROM wiki_revisions WHERE page_id=? AND revision=?", (page_id, revision)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["dependencies"] = [dict(item) for item in self._connection.execute("SELECT source_id,source_version,extraction_revision,passage_start,passage_end,original_sha256 FROM wiki_dependencies WHERE page_id=? AND revision=? ORDER BY source_id,source_version,extraction_revision,passage_start", (page_id, revision))]
        return result

    def list_wiki_revisions(self, page_id: str) -> list[dict[str, Any]]:
        _validate_identity(page_id, 1)
        self._ensure_open()
        revisions = [row[0] for row in self._connection.execute("SELECT revision FROM wiki_revisions WHERE page_id=? ORDER BY revision", (page_id,))]
        return [self.get_wiki_revision(page_id, revision) for revision in revisions]

    @staticmethod
    def _history_window(since: str | None, until: str | None, limit: int) -> None:
        for value in (since, until):
            if value is not None and (not isinstance(value, str) or not _CATALOG_TIME.fullmatch(value)):
                raise CatalogError("invalid history window")
        _validate_identity("limit", limit)

    def _history_rows(self, query: str, parameters: tuple, *, order: str, limit: int = -1) -> list[dict[str, Any]]:
        """Rows of a history query with a ``gone`` column: deleted sources are dropped, forgotten ones lose their title."""
        rows = []
        for row in self._connection.execute(
            f"SELECT * FROM ({query}) WHERE gone IS NOT 'deleted' ORDER BY {order} LIMIT ?", (*parameters, limit),
        ):
            item = dict(row)
            item["forgotten"] = item.pop("gone") == "forgotten"
            if item["forgotten"]:
                item["title"] = None
            rows.append(item)
        return rows

    def list_wiki_changes(self, *, since: str | None = None, until: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        """Wiki revisions, newest first, created in [since, until) (catalog UTC text), with page titles.

        A document's own page whose source was deleted is left out; one whose source
        was forgotten, and a revision citing only forgotten or deleted sources, has no title.
        """
        self._history_window(since, until, limit)
        self._ensure_open()
        return self._history_rows(
            f"SELECT r.page_id,r.revision,r.parent_revision,r.created_at,p.title,{_page_gone('r.page_id', 'r.revision')} "
            "AS gone FROM wiki_revisions r JOIN wiki_pages p USING(page_id) "
            "WHERE (? IS NULL OR r.created_at>=?) AND (? IS NULL OR r.created_at<?)", (since, since, until, until),
            order="created_at DESC,page_id DESC,revision DESC", limit=limit)

    def list_document_imports(self, *, since: str | None = None, until: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        """Document versions, newest first, whose custody was accepted in [since, until); acceptance is final.

        Deleted documents are left out and forgotten versions have no title.
        """
        self._history_window(since, until, limit)
        self._ensure_open()
        gone = _GONE_NAME.format(_VERSION_GONE.format(sid="s.source_id", version="s.source_version"))
        return self._history_rows(
            f"SELECT s.source_id,s.source_version,m.title,s.updated_at AS accepted_at,{gone} AS gone FROM document_stages s "
            "JOIN document_metadata m USING(source_id,source_version) WHERE s.stage='custody' AND s.state='accepted' "
            "AND (? IS NULL OR s.updated_at>=?) AND (? IS NULL OR s.updated_at<?)", (since, since, until, until),
            order="accepted_at DESC,source_id DESC,source_version DESC", limit=limit)

    def list_wiki_pages_needing_attention(self) -> list[dict[str, Any]]:
        """Stale or conflicted pages; a deleted source's own page is left out, a forgotten
        one's, and a page citing only forgotten or deleted sources, has no title."""
        self._ensure_open()
        return self._history_rows(
            f"SELECT p.page_id,p.title,p.state,{_page_gone('p.page_id', 'p.current_revision')} AS gone "
            "FROM wiki_pages p WHERE p.state IN ('stale','conflict')", (), order="page_id")

    def count_journal_sources(self, *, since: str, until: str) -> int:
        """Journal sources first retained in [since, until)."""
        self._history_window(since, until, 1)
        self._ensure_open()
        return self._connection.execute(
            "SELECT count(*) FROM sources WHERE layer='journal' AND created_at>=? AND created_at<?", (since, until)
        ).fetchone()[0]

    def list_wiki_pages_citing(self, source_id: str, *, any_revision: bool = False) -> list[str]:
        """Pages whose current revision (or, with ``any_revision``, any revision) cites any version of the source."""
        _validate_identity(source_id, 1)
        self._ensure_open()
        current = "" if any_revision else " AND d.revision=p.current_revision"
        return [row[0] for row in self._connection.execute(
            "SELECT DISTINCT p.page_id FROM wiki_pages p JOIN wiki_dependencies d ON d.page_id=p.page_id "
            f"WHERE d.source_id=?{current} ORDER BY p.page_id", (source_id,))]

    def mark_dependent_wiki_stale(self, source_id: str, source_version: int) -> int:
        identity = _validate_identity(source_id, source_version)
        with self.transaction() as connection:
            return connection.execute("UPDATE wiki_pages SET state='stale' WHERE EXISTS (SELECT 1 FROM wiki_dependencies d WHERE d.page_id=wiki_pages.page_id AND d.revision=wiki_pages.current_revision AND d.source_id=? AND d.source_version=?)", identity).rowcount

    def record_wiki_review(self, review_id: str, page_id: str, revision: int, *, code: str, details: dict[str, Any]) -> dict[str, Any]:
        _validate_identity(review_id, 1)
        _validate_identity(page_id, revision)
        if code not in {"manual_edit", "content_conflict", "contradiction"} or not isinstance(details, dict) or set(details) - {"expected_hash", "actual_hash", "sources"}:
            raise CatalogError("invalid wiki review")
        normalized = {}
        for key, value in details.items():
            if key == "sources":
                normalized[key] = _validate_wiki_contradiction(value) if code == "contradiction" and isinstance(value, dict) else json.loads(_validate_operation_result({"sources": value}))["sources"]
            else:
                normalized[key] = _validate_hash(value)
        serialized = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
        _utf8_size(serialized, 65536)
        with self.transaction() as connection:
            old = connection.execute("SELECT page_id,revision,code,details_json FROM wiki_reviews WHERE review_id=?", (review_id,)).fetchone()
            values = (page_id, revision, code, serialized)
            if old is not None and tuple(old) != values:
                raise CatalogConflict("wiki review conflict")
            connection.execute("INSERT OR IGNORE INTO wiki_reviews(review_id,page_id,revision,code,details_json) VALUES(?,?,?,?,?)", (review_id, *values))
            if code in {"manual_edit", "content_conflict"}:
                connection.execute("UPDATE wiki_pages SET state='conflict' WHERE page_id=? AND current_revision=?", (page_id, revision))
        return next(item for item in self.list_wiki_reviews(page_id) if item["review_id"] == review_id)

    def replace_wiki_review_sources(self, review_id: str, sources: dict[str, Any]) -> None:
        """Replace the quoted sides of a contradiction review after an erasure; nothing else about it changes."""
        _validate_identity(review_id, 1)
        serialized = json.dumps({"sources": _validate_wiki_contradiction(sources)}, sort_keys=True, separators=(",", ":"))
        _utf8_size(serialized, 65536)
        with self.transaction() as connection:
            if connection.execute("UPDATE wiki_reviews SET details_json=? WHERE review_id=? AND code='contradiction'", (serialized, review_id)).rowcount != 1:
                raise CatalogConflict("wiki review conflict")

    def list_wiki_reviews(self, page_id: str) -> list[dict[str, Any]]:
        _validate_identity(page_id, 1)
        self._ensure_open()
        result = []
        for row in self._connection.execute("SELECT * FROM wiki_reviews WHERE page_id=? ORDER BY review_id", (page_id,)):
            item = dict(row)
            item["details"] = json.loads(item.pop("details_json"))
            result.append(item)
        return result

    def find_wiki_reviews(
        self, *, review_id: str | None = None, page_id: str | None = None,
        state: str | None = None, limit: int = 100,
    ) -> tuple[list[dict[str, Any]], int]:
        """Reviews with the owner's resolution note, in page and review order, and the match count."""
        for identity in (review_id, page_id):
            if identity is not None:
                _validate_identity(identity, 1)
        if state not in {None, "pending", "resolved"}:
            raise CatalogError("invalid wiki review state")
        _validate_identity("limit", limit)
        self._ensure_open()
        where = "WHERE (? IS NULL OR r.review_id=?) AND (? IS NULL OR r.page_id=?) AND (? IS NULL OR r.state=?)"
        parameters = (review_id, review_id, page_id, page_id, state, state)
        total = self._connection.execute(f"SELECT count(*) FROM wiki_reviews r {where}", parameters).fetchone()[0]
        result = []
        for row in self._connection.execute(
            "SELECT r.*, s.note, s.resolved_at FROM wiki_reviews r LEFT JOIN wiki_review_resolutions s "
            f"USING(review_id) {where} ORDER BY r.page_id, r.review_id LIMIT ?", (*parameters, limit),
        ):
            item = dict(row)
            item["details"] = json.loads(item.pop("details_json"))
            note, resolved_at = item.pop("note"), item.pop("resolved_at")
            item["resolution"] = None if note is None else {"note": note, "resolved_at": resolved_at}
            result.append(item)
        return result, total

    def resolve_wiki_review(self, review_id: str, *, note: str) -> None:
        """Record the owner's decision; claims and revisions are never edited."""
        _validate_identity(review_id, 1)
        validate_review_note(note)
        with self.transaction() as connection:
            if connection.execute("UPDATE wiki_reviews SET state='resolved' WHERE review_id=? AND state='pending'", (review_id,)).rowcount != 1:
                raise CatalogConflict("wiki review resolution conflict")
            connection.execute(
                "INSERT INTO wiki_review_resolutions(review_id,note,resolved_at) "
                "VALUES(?,?,strftime('%Y-%m-%dT%H:%M:%SZ','now'))", (review_id, note),
            )
            connection.execute("UPDATE wiki_pages SET state='stale' WHERE state='conflict' AND page_id=(SELECT page_id FROM wiki_reviews WHERE review_id=?) AND NOT EXISTS (SELECT 1 FROM wiki_reviews r WHERE r.page_id=wiki_pages.page_id AND r.state='pending' AND r.code IN ('manual_edit','content_conflict'))", (review_id,))

    def set_artifact_publication(self, artifact_id: str, revision: int, *,
                                 projection_path: str, desired_hash: str,
                                 device: int | None, inode: int | None, staged_name: str, state: str = "pending") -> None:
        _validate_identity(artifact_id, revision)
        missing_identity = device is None and inode is None
        valid_identity = (not isinstance(device, bool) and isinstance(device, int) and 0 <= device < 2**63
                          and not isinstance(inode, bool) and isinstance(inode, int) and 0 < inode < 2**63)
        if (_validate_projection_path(projection_path) is None or _validate_hash(desired_hash) is None
                or not (valid_identity or missing_identity and state != "pending")
                or state == "planned" and not missing_identity
                or state not in {"planned", "pending", "retired", "completed"} or not isinstance(staged_name, str)
                or not re.fullmatch(r"\.publication\.[0-9a-f]{20}\.tmp\.hold", staged_name)):
            raise CatalogError("invalid artifact publication")
        with self.transaction() as connection:
            connection.execute("INSERT INTO artifact_publications(artifact_id,revision,projection_path,desired_hash,device,inode,staged_name,state) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(artifact_id,revision) DO UPDATE SET projection_path=excluded.projection_path,desired_hash=excluded.desired_hash,device=excluded.device,inode=excluded.inode,staged_name=excluded.staged_name,state=excluded.state",
                (artifact_id, revision, projection_path, desired_hash, device, inode, staged_name, state))

    def get_artifact_publication(self, artifact_id: str, revision: int) -> dict[str, Any] | None:
        _validate_identity(artifact_id, revision)
        self._ensure_open()
        row = self._connection.execute("SELECT * FROM artifact_publications WHERE artifact_id=? AND revision=?",
            (artifact_id, revision)).fetchone()
        return dict(row) if row else None

    def set_artifact_projection(
        self, layer: str, artifact_id: str, revision: int, *, projection_path: str,
        desired_hash: str, state: str, last_projected_hash: str | None = None,
    ) -> dict[str, Any]:
        _validate_identity(artifact_id, revision)
        if layer not in {"journal", "document", "wiki"} or state not in _PROJECTION_STATES:
            raise CatalogError("invalid artifact projection")
        if _validate_projection_path(projection_path) is None or _validate_hash(desired_hash) is None:
            raise CatalogError("artifact projection identity required")
        _validate_hash(last_projected_hash)
        if state == "projected" and last_projected_hash != desired_hash:
            raise CatalogError("artifact projection checksum mismatch")
        with self.transaction() as connection:
            connection.execute("INSERT INTO artifact_projections(layer,artifact_id,revision,projection_path,desired_hash,state,last_projected_hash) VALUES(?,?,?,?,?,?,?) ON CONFLICT(layer,artifact_id,revision) DO UPDATE SET projection_path=excluded.projection_path,desired_hash=excluded.desired_hash,state=excluded.state,last_projected_hash=excluded.last_projected_hash,updated_at=CURRENT_TIMESTAMP", (layer, artifact_id, revision, projection_path, desired_hash, state, last_projected_hash))
        return self.get_artifact_projection(layer, artifact_id, revision)

    def get_artifact_projection(self, layer: str, artifact_id: str, revision: int) -> dict[str, Any] | None:
        _validate_identity(artifact_id, revision)
        if layer not in {"journal", "document", "wiki"}:
            raise CatalogError("invalid artifact projection layer")
        self._ensure_open()
        row = self._connection.execute("SELECT * FROM artifact_projections WHERE layer=? AND artifact_id=? AND revision=?", (layer, artifact_id, revision)).fetchone()
        return dict(row) if row else None

    def list_artifact_projections(self, *, layer: str | None = None, artifact_id: str | None = None, limit: int = 4096) -> list[dict[str, Any]]:
        if layer is not None and (not isinstance(layer, str) or layer not in {"journal", "document", "wiki"}):
            raise CatalogError("invalid artifact projection layer")
        if artifact_id is not None:
            _validate_identity(artifact_id, 1)
        _validate_identity("limit", limit)
        if limit > 4096:
            raise CatalogError("invalid artifact projection limit")
        self._ensure_open()
        return [dict(row) for row in self._connection.execute("SELECT * FROM artifact_projections WHERE (? IS NULL OR layer=?) AND (? IS NULL OR artifact_id=?) ORDER BY layer,artifact_id,revision LIMIT ?", (layer, layer, artifact_id, artifact_id, limit))]

    def list_wiki_page_projections(self, *, limit: int = 4096) -> list[dict[str, Any]]:
        """Wiki-layer receipts of pages, their history and the index; not moves, briefs or the log."""
        _validate_identity("limit", limit)
        self._ensure_open()
        return [dict(row) for row in self._connection.execute(
            "SELECT * FROM artifact_projections WHERE layer='wiki' AND artifact_id NOT LIKE 'move\\_%' ESCAPE '\\' "
            "AND artifact_id NOT LIKE 'brief\\_%' ESCAPE '\\' AND artifact_id<>'log' ORDER BY artifact_id,revision LIMIT ?", (limit,))]

    def list_current_documents(self, *, limit: int = 4096) -> tuple[list[dict[str, Any]], int]:
        """Current versions of documents neither forgotten nor deleted, with stage states and latest extraction; and their count."""
        _validate_identity("limit", limit)
        self._ensure_open()
        current = ("FROM document_metadata m WHERE m.source_version=(SELECT max(v.source_version) FROM source_versions v "
                   f"WHERE v.source_id=m.source_id) AND {_VERSION_GONE.format(sid='m.source_id', version='m.source_version')}=0")
        total = self._connection.execute(f"SELECT count(*) {current}").fetchone()[0]
        rows = []
        for row in self._connection.execute(
            "SELECT m.source_id,m.source_version,(SELECT json_group_object(s.stage,json_object('state',s.state,'error_code',s.error_code)) "
            "FROM document_stages s WHERE s.source_id=m.source_id AND s.source_version=m.source_version) AS stages,"
            "(SELECT json_object('extraction_revision',e.extraction_revision,'artifact_sha256',e.artifact_sha256) FROM extractions e "
            "WHERE e.source_id=m.source_id AND e.source_version=m.source_version ORDER BY e.extraction_revision DESC LIMIT 1) AS extraction "
            f"{current} ORDER BY m.source_id LIMIT ?", (limit,),
        ):
            item = dict(row)
            item["stages"] = json.loads(item["stages"]) if item["stages"] else {}
            item["extraction"] = json.loads(item["extraction"]) if item["extraction"] else None
            rows.append(item)
        return rows, total

    def list_projection_conflicts(self, *, limit: int = 4096) -> list[dict[str, Any]]:
        """Latest projection receipt of each artifact, and journal entries, left in conflict or failed."""
        _validate_identity("limit", limit)
        self._ensure_open()
        return [dict(row) for row in self._connection.execute(
            "SELECT layer,artifact_id,revision,state FROM artifact_projections p WHERE state IN ('conflict','failed') "
            "AND revision=(SELECT max(revision) FROM artifact_projections q WHERE q.layer=p.layer AND q.artifact_id=p.artifact_id) "
            "UNION ALL SELECT 'journal',source_id,source_version,state FROM projection_states WHERE state IN ('conflict','failed') "
            "ORDER BY 1,2,3 LIMIT ?", (limit,))]

    def get_source(self, source_id: str) -> dict[str, Any] | None:
        if not isinstance(source_id, str) or not _SOURCE_ID.fullmatch(source_id):
            raise CatalogError("invalid catalog identity")
        self._ensure_open()
        try:
            row = self._connection.execute(
                "SELECT source_id, lifecycle_state, created_at, layer FROM sources WHERE source_id = ?",
                (source_id,),
            ).fetchone()
            return dict(row) if row is not None else None
        except sqlite3.Error:
            raise CatalogError("catalog operation failed") from None

    def get_current_manifest(self, source_id: str) -> SourceManifest | None:
        """Resolve the highest registered source version without reading custody."""
        if not isinstance(source_id, str) or not _SOURCE_ID.fullmatch(source_id):
            raise CatalogError("invalid catalog identity")
        self._ensure_open()
        try:
            row = self._connection.execute(
                "SELECT manifest_json FROM source_versions WHERE source_id = ? "
                "ORDER BY source_version DESC LIMIT 1",
                (source_id,),
            ).fetchone()
            return SourceManifest.from_bytes(row["manifest_json"].encode("utf-8")) if row else None
        except (sqlite3.Error, UnicodeError, ValueError):
            raise CatalogError("catalog operation failed") from None

    def list_source_manifests(
        self, source_id: str, *, limit: int = 100
    ) -> list[SourceManifest]:
        """Return a bounded, validated ordered manifest set for recovery."""
        if (
            not isinstance(source_id, str)
            or not _SOURCE_ID.fullmatch(source_id)
            or isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 1000
        ):
            raise CatalogError("invalid catalog identity")
        self._ensure_open()
        try:
            rows = self._connection.execute(
                "SELECT manifest_json FROM source_versions WHERE source_id=? "
                "ORDER BY source_version LIMIT ?",
                (source_id, limit + 1),
            ).fetchall()
            if len(rows) > limit:
                raise CatalogError("source version limit exceeded")
            return [
                SourceManifest.from_bytes(row["manifest_json"].encode("utf-8"))
                for row in rows
            ]
        except CatalogError:
            raise
        except (sqlite3.Error, UnicodeError, ValueError):
            raise CatalogError("catalog operation failed") from None

    def list_inventory(self, *, limit: int = 4096) -> list[dict[str, Any]]:
        """Return a bounded complete manifest and lifecycle inventory."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 4096:
            raise CatalogError("invalid catalog inventory limit")
        self._ensure_open()
        try:
            rows = self._connection.execute(
                """SELECT sv.source_id, sv.source_version, sv.manifest_json,
                          t.tombstone_type AS ordinary_state,
                          tt.tombstone_type AS transfer_state,
                          ps.recall_visibility
                   FROM source_versions AS sv
                   LEFT JOIN tombstones AS t
                     ON t.source_id=sv.source_id AND t.source_version=sv.source_version
                   LEFT JOIN transfer_tombstones AS tt
                     ON tt.source_id=sv.source_id AND tt.source_version=sv.source_version
                   LEFT JOIN provider_states AS ps
                     ON ps.source_id=sv.source_id AND ps.source_version=sv.source_version
                   ORDER BY sv.source_id, sv.source_version LIMIT ?""",
                (limit + 1,),
            ).fetchall()
            if len(rows) > limit:
                raise CatalogError("catalog inventory limit exceeded")
            results = []
            identities = set()
            for row in rows:
                source_id, source_version = _validate_identity(
                    row["source_id"], row["source_version"]
                )
                ordinary = row["ordinary_state"]
                transferred = row["transfer_state"]
                if ordinary is not None and transferred is not None and ordinary != transferred:
                    raise CatalogError("catalog lifecycle conflict")
                lifecycle = ordinary or transferred or (
                    "forgotten" if row["recall_visibility"] == "not_visible" else "active"
                )
                if lifecycle not in _LIFECYCLE_STATES:
                    raise CatalogError("invalid catalog lifecycle")
                manifest = SourceManifest.from_bytes(row["manifest_json"].encode("utf-8"))
                if (manifest.source_id, manifest.source_version) != (source_id, source_version):
                    raise CatalogError("catalog manifest identity conflict")
                identities.add((source_id, source_version))
                results.append({
                    "source_id": source_id,
                    "source_version": source_version,
                    "lifecycle_state": lifecycle,
                    "manifest": manifest,
                })
            remaining = limit - len(results)
            extra = self._connection.execute(
                """SELECT tt.source_id, tt.source_version, tt.tombstone_type
                   FROM transfer_tombstones AS tt
                   LEFT JOIN source_versions AS sv
                     ON sv.source_id=tt.source_id AND sv.source_version=tt.source_version
                   WHERE sv.source_id IS NULL
                   ORDER BY tt.source_id, tt.source_version LIMIT ?""",
                (remaining + 1,),
            ).fetchall()
            if len(extra) > remaining:
                raise CatalogError("catalog inventory limit exceeded")
            for row in extra:
                source_id, source_version = _validate_identity(
                    row["source_id"], row["source_version"]
                )
                identity = (source_id, source_version)
                if identity in identities:
                    raise CatalogError("catalog lifecycle conflict")
                if row["tombstone_type"] not in _TOMBSTONE_TYPES:
                    raise CatalogError("invalid catalog lifecycle")
                identities.add(identity)
                results.append({
                    "source_id": source_id,
                    "source_version": source_version,
                    "lifecycle_state": row["tombstone_type"],
                    "manifest": None,
                })
            return sorted(results, key=lambda item: (item["source_id"], item["source_version"]))
        except CatalogError:
            raise
        except (sqlite3.Error, UnicodeError, ValueError, TypeError):
            raise CatalogError("catalog inventory unavailable") from None

    def record_curation_history(
        self,
        operation_id: str,
        source_id: str,
        source_version: int,
        action: str,
        *,
        prepared_operation_id: str | None = None,
    ) -> None:
        """Append one content-free action fact, accepting an exact replay."""
        identity = _validate_identity(source_id, source_version)
        if (
            not isinstance(operation_id, str)
            or not _OPERATION_ID.fullmatch(operation_id)
            or not isinstance(action, str)
            or action not in _CURATION_ACTIONS
            or (
                prepared_operation_id is not None
                and (
                    not isinstance(prepared_operation_id, str)
                    or not _OPERATION_ID.fullmatch(prepared_operation_id)
                )
            )
        ):
            raise CatalogError("invalid curation history")
        self._ensure_open()
        try:
            with self.transaction() as connection:
                existing = connection.execute(
                    "SELECT source_id, source_version, action, prepared_operation_id "
                    "FROM curation_history "
                    "WHERE operation_id = ?",
                    (operation_id,),
                ).fetchone()
                if existing is not None:
                    if tuple(existing) != (*identity, action, prepared_operation_id):
                        raise CatalogConflict("curation history conflict")
                    return
                connection.execute(
                    "INSERT INTO curation_history(operation_id, source_id, source_version, action, "
                    "prepared_operation_id) VALUES (?, ?, ?, ?, ?)",
                    (operation_id, *identity, action, prepared_operation_id),
                )
        except (CatalogConflict, CatalogError):
            raise
        except sqlite3.IntegrityError:
            raise CatalogError("curation history reference unavailable") from None
        except sqlite3.Error:
            raise CatalogError("catalog operation failed") from None

    def get_curation_history(self, operation_id: str) -> dict[str, Any] | None:
        if not isinstance(operation_id, str) or not _OPERATION_ID.fullmatch(operation_id):
            raise CatalogError("invalid curation history")
        self._ensure_open()
        try:
            row = self._connection.execute(
                "SELECT operation_id, source_id, source_version, action, created_at, "
                "prepared_operation_id FROM curation_history WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            return dict(row) if row is not None else None
        except sqlite3.Error:
            raise CatalogError("catalog operation failed") from None

    def begin_confirmed_curation(
        self,
        operation_id: str,
        prepared_operation_id: str,
        source_id: str,
        source_version: int,
        action: str,
        *,
        confirmation_ttl_seconds: int,
    ) -> list[SourceManifest]:
        """Recheck a prepared action and persist its history/tombstone before effects."""
        identity = _validate_identity(source_id, source_version)
        expected_prepared_action = {
            "forget": "prepare_forget",
            "delete": "prepare_delete",
        }.get(action)
        if (
            expected_prepared_action is None
            or not isinstance(operation_id, str)
            or not _OPERATION_ID.fullmatch(operation_id)
            or not isinstance(prepared_operation_id, str)
            or not _OPERATION_ID.fullmatch(prepared_operation_id)
            or isinstance(confirmation_ttl_seconds, bool)
            or not isinstance(confirmation_ttl_seconds, int)
            or not 1 <= confirmation_ttl_seconds <= 3600
        ):
            raise CatalogError("invalid curation confirmation")
        self._ensure_open()
        try:
            with self.transaction() as connection:
                operation = connection.execute(
                    "SELECT state, operation_kind FROM operations WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                prepared = connection.execute(
                    """SELECT h.source_id, h.source_version, h.action, o.created_at, o.state
                       FROM curation_history AS h JOIN operations AS o
                         ON o.operation_id=h.operation_id
                       WHERE h.operation_id=?""",
                    (prepared_operation_id,),
                ).fetchone()
                if (
                    operation is None
                    or operation["state"] != "running"
                    or operation["operation_kind"] != "curation"
                    or prepared is None
                    or prepared["action"] != expected_prepared_action
                    or prepared["source_id"] != source_id
                    or prepared["source_version"] != source_version
                    or prepared["state"] != "completed"
                ):
                    raise CatalogConflict("curation confirmation conflict")
                age = connection.execute(
                    "SELECT (julianday('now') - julianday(?)) * 86400",
                    (prepared["created_at"],),
                ).fetchone()[0]
                if age is None or age < 0 or age > confirmation_ttl_seconds:
                    raise CatalogConflict("curation confirmation expired")
                latest = connection.execute(
                    "SELECT MAX(source_version) FROM source_versions WHERE source_id=?",
                    (source_id,),
                ).fetchone()[0]
                if latest != source_version:
                    raise CatalogConflict("curation source version conflict")

                prior = connection.execute(
                    """SELECT o.state FROM curation_history AS h JOIN operations AS o
                         ON o.operation_id=h.operation_id
                       WHERE h.prepared_operation_id=? AND h.action=?""",
                    (prepared_operation_id, action),
                ).fetchall()
                if any(row["state"] == "completed" for row in prior):
                    raise CatalogConflict("curation confirmation already consumed")

                if action == "forget" and prior:
                    provider = connection.execute(
                        "SELECT recall_visibility FROM provider_states "
                        "WHERE source_id=? AND source_version=?",
                        identity,
                    ).fetchone()
                    if provider is None or provider["recall_visibility"] != "not_visible":
                        raise CatalogConflict("curation result is uncertain")

                if action == "delete":
                    manifest_rows = connection.execute(
                        "SELECT source_version, manifest_json FROM source_versions "
                        "WHERE source_id=? ORDER BY source_version",
                        (source_id,),
                    ).fetchall()
                    if not manifest_rows:
                        raise CatalogConflict("curation source unavailable")
                    existing = connection.execute(
                        "SELECT source_version, tombstone_type, progress_json FROM tombstones "
                        "WHERE source_id=? ORDER BY source_version",
                        (source_id,),
                    ).fetchall()
                    expected_versions = [row["source_version"] for row in manifest_rows]
                    if existing:
                        if [row["source_version"] for row in existing] != expected_versions or any(
                            row["tombstone_type"] != "deleted"
                            for row in existing
                        ):
                            raise CatalogConflict("curation tombstone conflict")
                        for row in existing:
                            progress = _validate_tombstone_progress(json.loads(row["progress_json"]))
                            if progress["provider_delete"] in {"running", "failed"}:
                                raise CatalogConflict("curation provider outcome is uncertain")
                    else:
                        pending = json.dumps(
                            {
                                "provider_delete": "pending",
                                "projection_delete": "pending",
                                "custody_delete": "pending",
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        for version in expected_versions:
                            connection.execute(
                                "INSERT INTO tombstones(source_id, source_version, tombstone_type, "
                                "operation_id, progress_json) VALUES (?, ?, 'deleted', ?, ?)",
                                (source_id, version, operation_id, pending),
                            )

                connection.execute(
                    "INSERT INTO curation_history(operation_id, source_id, source_version, action, "
                    "prepared_operation_id) VALUES (?, ?, ?, ?, ?)",
                    (operation_id, *identity, action, prepared_operation_id),
                )
                return [SourceManifest.from_bytes(row["manifest_json"].encode("utf-8"))
                        for row in connection.execute(
                            "SELECT manifest_json FROM source_versions WHERE source_id=? "
                            "ORDER BY source_version", (source_id,)
                        ).fetchall()]
        except (CatalogConflict, CatalogError):
            raise
        except (sqlite3.Error, TypeError, ValueError, UnicodeError):
            raise CatalogError("catalog operation failed") from None

    def get_tombstone(self, source_id: str, source_version: int) -> dict[str, Any] | None:
        identity = _validate_identity(source_id, source_version)
        self._ensure_open()
        try:
            row = self._connection.execute(
                "SELECT source_id, source_version, tombstone_type, operation_id, created_at, "
                "progress_json FROM tombstones WHERE source_id=? AND source_version=?",
                identity,
            ).fetchone()
            if row is not None:
                result = dict(row)
                result["progress"] = _validate_tombstone_progress(
                    json.loads(result.pop("progress_json"))
                )
                return result
            row = self._connection.execute(
                "SELECT source_id,source_version,tombstone_type,operation_id,created_at "
                "FROM transfer_tombstones WHERE source_id=? AND source_version=?",
                identity,
            ).fetchone()
            if row is None:
                return None
            result = dict(row)
            result["progress"] = {
                "provider_delete": "completed",
                "projection_delete": "completed",
                "custody_delete": "completed",
            }
            return result
        except (sqlite3.Error, TypeError, ValueError):
            raise CatalogError("catalog operation failed") from None

    def list_tombstones(self, source_id: str) -> list[dict[str, Any]]:
        if not isinstance(source_id, str) or not _SOURCE_ID.fullmatch(source_id):
            raise CatalogError("invalid catalog identity")
        self._ensure_open()
        try:
            rows = self._connection.execute(
                "SELECT source_id, source_version, tombstone_type, operation_id, created_at, "
                "progress_json FROM tombstones WHERE source_id=? ORDER BY source_version",
                (source_id,),
            ).fetchall()
            results = []
            seen: set[int] = set()
            for row in rows:
                result = dict(row)
                seen.add(result["source_version"])
                result["progress"] = _validate_tombstone_progress(
                    json.loads(result.pop("progress_json"))
                )
                results.append(result)
            transfer_rows = self._connection.execute(
                "SELECT source_id,source_version,tombstone_type,operation_id,created_at "
                "FROM transfer_tombstones WHERE source_id=? ORDER BY source_version",
                (source_id,),
            ).fetchall()
            for row in transfer_rows:
                result = dict(row)
                if result["source_version"] in seen:
                    existing = next(
                        item for item in results
                        if item["source_version"] == result["source_version"]
                    )
                    if existing["tombstone_type"] != result["tombstone_type"]:
                        raise CatalogError("catalog lifecycle conflict")
                    continue
                result["progress"] = {
                    "provider_delete": "completed",
                    "projection_delete": "completed",
                    "custody_delete": "completed",
                }
                results.append(result)
            results.sort(key=lambda item: item["source_version"])
            return results
        except (sqlite3.Error, TypeError, ValueError):
            raise CatalogError("catalog operation failed") from None

    def update_tombstone_progress(
        self,
        source_id: str,
        source_version: int,
        component: str,
        state: str,
        *,
        whole_source: bool = False,
    ) -> None:
        _validate_identity(source_id, source_version)
        if component not in _TOMBSTONE_PROGRESS_FIELDS or state not in _TOMBSTONE_PROGRESS_STATES:
            raise CatalogError("invalid tombstone progress")
        self._ensure_open()
        try:
            with self.transaction() as connection:
                rows = connection.execute(
                    "SELECT source_version, progress_json FROM tombstones WHERE source_id=? "
                    + ("" if whole_source else "AND source_version=? ")
                    + "ORDER BY source_version",
                    (source_id,) if whole_source else (source_id, source_version),
                ).fetchall()
                if not rows:
                    raise CatalogError("tombstone unavailable")
                for row in rows:
                    progress = _validate_tombstone_progress(json.loads(row["progress_json"]))
                    old_state = progress[component]
                    allowed = {
                        "pending": {"pending", "running", "completed"},
                        "running": {"running", "completed", "failed"},
                        "failed": {"failed", "running", "completed"},
                        "completed": {"completed"},
                    }
                    if state not in allowed[old_state]:
                        raise CatalogConflict("tombstone progress conflict")
                    progress[component] = state
                    encoded = json.dumps(progress, sort_keys=True, separators=(",", ":"))
                    connection.execute(
                        "UPDATE tombstones SET progress_json=? WHERE source_id=? AND source_version=?",
                        (encoded, source_id, row["source_version"]),
                    )
        except (CatalogConflict, CatalogError):
            raise
        except (sqlite3.Error, TypeError, ValueError):
            raise CatalogError("catalog operation failed") from None

    def get_manifest(self, source_id: str, source_version: int) -> dict[str, Any] | None:
        identity = _validate_identity(source_id, source_version)
        self._ensure_open()
        try:
            row = self._connection.execute(
                """
                SELECT source_id, source_version, sha256, size_bytes,
                    provenance_json, manifest_path
                FROM source_versions WHERE source_id = ? AND source_version = ?
                """,
                identity,
            ).fetchone()
            if row is None:
                return None
            value = dict(row)
            value["provenance"] = json.loads(value.pop("provenance_json"))
            return value
        except (sqlite3.Error, ValueError):
            raise CatalogError("catalog operation failed") from None

    def set_provider_state(
        self,
        source_id: str,
        source_version: int,
        *,
        submission_state: str | None = None,
        completion_state: str | None = None,
        recall_visibility: str | None = None,
    ) -> None:
        identity = _validate_identity(source_id, source_version)
        if submission_state is not None and (
            not isinstance(submission_state, str)
            or submission_state not in _PROVIDER_SUBMISSION_STATES
        ):
            raise CatalogError("invalid provider state")
        if completion_state is not None and (
            not isinstance(completion_state, str)
            or completion_state not in _PROVIDER_COMPLETION_STATES
        ):
            raise CatalogError("invalid provider state")
        if recall_visibility is not None and (
            not isinstance(recall_visibility, str)
            or recall_visibility not in _RECALL_VISIBILITY_STATES
        ):
            raise CatalogError("invalid provider state")
        self._ensure_open()
        try:
            with self.transaction() as connection:
                cursor = connection.execute(
                    """
                    UPDATE provider_states SET
                        submission_state = COALESCE(?, submission_state),
                        completion_state = COALESCE(?, completion_state),
                        recall_visibility = COALESCE(?, recall_visibility),
                        updated_at = CURRENT_TIMESTAMP
                    WHERE source_id = ? AND source_version = ?
                    """,
                    (submission_state, completion_state, recall_visibility, *identity),
                )
                if cursor.rowcount != 1:
                    raise CatalogError("source version not registered")
        except CatalogError:
            raise
        except sqlite3.Error:
            raise CatalogError("catalog operation failed") from None

    def source_withdrawn(self, source_id: str, source_version: int | None = None) -> bool:
        """The version (by default the current one) was forgotten or deleted: the source
        has a tombstone, or the version can no longer be recalled (a document forget
        leaves no tombstone). ``_VERSION_GONE`` is the same rule in SQL."""
        if self.list_tombstones(source_id):
            return True
        if source_version is None:
            manifest = self.get_current_manifest(source_id)
            source_version = manifest.source_version if manifest else None
        provider = self.get_provider_state(source_id, source_version) if source_version else None
        return provider is not None and provider["recall_visibility"] == "not_visible"

    def get_provider_state(self, source_id: str, source_version: int) -> dict[str, Any] | None:
        identity = _validate_identity(source_id, source_version)
        self._ensure_open()
        try:
            row = self._connection.execute(
                """
                SELECT submission_state, completion_state, recall_visibility, updated_at
                FROM provider_states WHERE source_id = ? AND source_version = ?
                """,
                identity,
            ).fetchone()
            return dict(row) if row is not None else None
        except sqlite3.Error:
            raise CatalogError("catalog operation failed") from None

    def begin_projection_move(
        self, source_id: str, source_version: int, *,
        old_path: str, new_path: str, old_hash: str, new_hash: str,
    ) -> None:
        identity = _validate_identity(source_id, source_version)
        for path in (old_path, new_path):
            _validate_projection_path(path)
            if (
                path is None or len(path.split("/")) != 2
                or not path.startswith("entries/") or not path.endswith(".md")
                or path == "entries/.md" or _has_control_characters(path)
            ):
                raise CatalogError("invalid projection identity")
        if old_path == new_path:
            raise CatalogError("invalid projection identity")
        if _validate_hash(old_hash) is None or _validate_hash(new_hash) is None:
            raise CatalogError("invalid catalog hash")
        self._ensure_open()
        with self.transaction() as connection:
            existing = self.get_projection_move(*identity)
            move = {
                "source_id": source_id, "source_version": source_version,
                "old_path": old_path, "new_path": new_path,
                "old_hash": old_hash, "new_hash": new_hash,
            }
            if existing is not None and existing != move:
                raise CatalogConflict("projection move conflict")
            if existing is None:
                projection = connection.execute(
                    "SELECT projection_path,last_projected_hash FROM projection_states "
                    "WHERE source_id=? AND source_version=?", identity,
                ).fetchone()
                if projection is None:
                    raise CatalogError("source version not registered")
                if (
                    projection["projection_path"] != old_path
                    or projection["last_projected_hash"] != old_hash
                ):
                    raise CatalogConflict("projection move ownership conflict")
                if connection.execute(
                    "SELECT 1 FROM journal_projection_moves WHERE new_path=?", (new_path,)
                ).fetchone() is not None:
                    raise CatalogConflict("projection move destination conflict")
                connection.execute(
                    "INSERT INTO journal_projection_moves "
                    "(source_id,source_version,old_path,new_path,old_hash,new_hash) "
                    "VALUES (?,?,?,?,?,?)",
                    (*identity, old_path, new_path, old_hash, new_hash),
                )
            cursor = connection.execute(
                "UPDATE projection_states SET state='pending', updated_at=CURRENT_TIMESTAMP "
                "WHERE source_id=? AND source_version=?", identity,
            )
            if cursor.rowcount != 1:
                raise CatalogError("source version not registered")

    def get_projection_move(
        self, source_id: str, source_version: int
    ) -> dict[str, Any] | None:
        identity = _validate_identity(source_id, source_version)
        self._ensure_open()
        try:
            row = self._connection.execute(
                "SELECT * FROM journal_projection_moves WHERE source_id=? AND source_version=?",
                identity,
            ).fetchone()
            return dict(row) if row is not None else None
        except sqlite3.Error:
            raise CatalogError("catalog operation failed") from None

    def finish_projection_move(self, source_id: str, source_version: int) -> None:
        identity = _validate_identity(source_id, source_version)
        with self.transaction() as connection:
            connection.execute(
                "DELETE FROM journal_projection_moves WHERE source_id=? AND source_version=?",
                identity,
            )

    def list_projection_moves(self, *, limit: int = 1000) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise CatalogError("invalid projection limit")
        self._ensure_open()
        try:
            return [dict(row) for row in self._connection.execute(
                "SELECT * FROM journal_projection_moves ORDER BY source_id,source_version LIMIT ?",
                (limit,),
            ).fetchall()]
        except sqlite3.Error:
            raise CatalogError("catalog operation failed") from None

    def list_projection_paths(self) -> list[str]:
        self._ensure_open()
        try:
            return [row[0] for row in self._connection.execute(
                "SELECT projection_path FROM projection_states WHERE projection_path IS NOT NULL "
                "UNION SELECT new_path FROM journal_projection_moves ORDER BY 1"
            ).fetchall()]
        except sqlite3.Error:
            raise CatalogError("catalog operation failed") from None

    def set_projection_state(
        self,
        source_id: str,
        source_version: int,
        *,
        state: str,
        projection_path: str | None = None,
        desired_hash: str | None = None,
        last_projected_hash: str | None = None,
    ) -> None:
        identity = _validate_identity(source_id, source_version)
        if not isinstance(state, str) or state not in _PROJECTION_STATES:
            raise CatalogError("invalid projection state")
        projection_path = _validate_projection_path(projection_path)
        desired_hash = _validate_hash(desired_hash)
        last_projected_hash = _validate_hash(last_projected_hash)
        self._ensure_open()
        try:
            with self.transaction() as connection:
                cursor = connection.execute(
                    """
                    UPDATE projection_states SET state = ?, projection_path = ?,
                        desired_hash = ?, last_projected_hash = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE source_id = ? AND source_version = ?
                    """,
                    (state, projection_path, desired_hash, last_projected_hash, *identity),
                )
                if cursor.rowcount != 1:
                    raise CatalogError("source version not registered")
        except CatalogError:
            raise
        except sqlite3.Error:
            raise CatalogError("catalog operation failed") from None

    def get_projection_state(
        self, source_id: str, source_version: int
    ) -> dict[str, Any] | None:
        identity = _validate_identity(source_id, source_version)
        self._ensure_open()
        try:
            row = self._connection.execute(
                """
                SELECT state, projection_path, desired_hash, last_projected_hash, updated_at
                FROM projection_states WHERE source_id = ? AND source_version = ?
                """,
                identity,
            ).fetchone()
            return dict(row) if row is not None else None
        except sqlite3.Error:
            raise CatalogError("catalog operation failed") from None

    def list_recoverable_projection_states(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """Return a bounded ordered batch of interrupted projection writes."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise CatalogError("invalid projection limit")
        self._ensure_open()
        try:
            rows = self._connection.execute(
                "SELECT source_id, source_version, state, projection_path, desired_hash, "
                "last_projected_hash, updated_at FROM projection_states "
                "WHERE state='pending' OR EXISTS ("
                "SELECT 1 FROM journal_projection_moves AS move "
                "WHERE move.source_id=projection_states.source_id "
                "AND move.source_version=projection_states.source_version) "
                "ORDER BY updated_at, source_id, source_version LIMIT ?",
                (limit,),
            ).fetchall()
            results = []
            for row in rows:
                source_id, source_version = _validate_identity(
                    row["source_id"], row["source_version"]
                )
                projection_path = _validate_projection_path(row["projection_path"])
                results.append({
                    "source_id": source_id,
                    "source_version": source_version,
                    "state": row["state"],
                    "projection_path": projection_path,
                    "desired_hash": _validate_hash(row["desired_hash"]),
                    "last_projected_hash": _validate_hash(row["last_projected_hash"]),
                    "updated_at": row["updated_at"],
                })
            return results
        except (sqlite3.Error, TypeError, ValueError):
            raise CatalogError("catalog operation failed") from None

    def record_reconciliation_fact(
        self,
        source_id: str,
        source_version: int,
        fact: str,
        *,
        expected_hash: str | None = None,
        actual_hash: str | None = None,
    ) -> None:
        identity = _validate_identity(source_id, source_version)
        if not isinstance(fact, str) or fact not in _RECONCILIATION_FACTS:
            raise CatalogError("invalid reconciliation fact")
        expected_hash = _validate_hash(expected_hash)
        actual_hash = _validate_hash(actual_hash)
        self._ensure_open()
        try:
            with self.transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO reconciliation_facts(
                        source_id, source_version, fact, expected_hash, actual_hash
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (*identity, fact, expected_hash, actual_hash),
                )
        except sqlite3.IntegrityError:
            raise CatalogError("source version not registered") from None
        except CatalogError:
            raise
        except sqlite3.Error:
            raise CatalogError("catalog operation failed") from None

    def list_reconciliation_facts(
        self, source_id: str | None = None, source_version: int | None = None
    ) -> list[dict[str, Any]]:
        if (source_id is None) != (source_version is None):
            raise CatalogError("invalid catalog identity")
        identity = (
            _validate_identity(source_id, source_version)
            if source_id is not None and source_version is not None
            else None
        )
        self._ensure_open()
        try:
            if identity is None:
                rows = self._connection.execute(
                    """
                    SELECT fact_id, source_id, source_version, fact,
                        expected_hash, actual_hash, created_at
                    FROM reconciliation_facts ORDER BY fact_id
                    """
                ).fetchall()
            else:
                rows = self._connection.execute(
                    """
                    SELECT fact_id, source_id, source_version, fact,
                        expected_hash, actual_hash, created_at
                    FROM reconciliation_facts
                    WHERE source_id = ? AND source_version = ? ORDER BY fact_id
                    """,
                    identity,
                ).fetchall()
            return [dict(row) for row in rows]
        except sqlite3.Error:
            raise CatalogError("catalog operation failed") from None

    def _ensure_open(self) -> None:
        if self._closed:
            raise CatalogError("catalog is closed")

    def close(self) -> None:
        if getattr(self, "_closed", True):
            return
        self._closed = True
        connection = getattr(self, "_connection", None)
        if connection is not None:
            connection.close()

    def __enter__(self) -> "LifeContextCatalog":
        self._ensure_open()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()
