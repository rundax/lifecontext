"""Strict, bounded request and response models for the V1 HTTP contract."""

from __future__ import annotations

from typing import Annotated, Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from lifecontext.config import LIMIT_CEILINGS, JournalLimits


_SOURCE_ID = r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}"
_EDGE_ID = r"edge_[0-9a-f]{64}"
_SHA256 = r"[0-9a-f]{64}"
_SOURCE_TEXT_KEYS = frozenset(
    {"content", "source_text", "original_text", "body", "text", "excerpt", "snippet", "quote"}
)

SourceId: TypeAlias = Annotated[str, StringConstraints(min_length=1, max_length=128, pattern=_SOURCE_ID)]
EdgeId: TypeAlias = Annotated[str, StringConstraints(pattern=_EDGE_ID)]
Sha256: TypeAlias = Annotated[str, StringConstraints(pattern=_SHA256)]
Version: TypeAlias = Annotated[int, Field(ge=1, le=2_147_483_647)]
ShortText: TypeAlias = Annotated[str, StringConstraints(min_length=1, max_length=4096)]
_BUDGETS = Literal["low", "mid", "high"]
_RELATIONS = Literal[
    "references", "about", "same_topic", "follows", "supports", "contradicts", "supersedes"
]
_RELATIONSHIP_STATES = Literal["proposed", "approved", "rejected"]
_OPERATION_STATES = Literal[
    "pending", "running", "retryable_failed", "permanent_failed", "completed", "cancelled"
]
_ERROR_CODES = Literal[
    "invalid_request",
    "unauthorized",
    "body_too_large",
    "response_too_large",
    "host_not_allowed",
    "origin_not_allowed",
    "source_not_found",
    "source_conflict",
    "evidence_unavailable",
    "invalid_source_text",
    "invalid_provider_response",
    "provider_unavailable",
    "retain_failed",
    "recall_failed",
    "reflect_failed",
    "unattributed_evidence",
    "result_limit_exceeded",
    "operation_status_unavailable",
    "curation_unavailable",
    "transfer_unavailable",
    "conflict",
    "relationship_unavailable",
    "unsafe_path",
    "unsafe_permissions",
    "vault_unavailable",
    "catalog_busy",
    "internal_error",
]


class APIModel(BaseModel):
    """Base model that rejects coercion and undeclared public fields."""

    model_config = ConfigDict(extra="forbid", strict=True)


def _utf8_bounded(value: str, maximum: int, code: str = "invalid_request") -> str:
    try:
        if len(value.encode("utf-8")) > maximum:
            raise ValueError(code)
    except UnicodeError:
        raise ValueError(code) from None
    return value


def _basename(value: str | None) -> str | None:
    if value is None:
        return None
    if (
        not value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or ":" in value
        or any(ord(char) < 32 or 0x7F <= ord(char) <= 0x9F for char in value)
    ):
        raise ValueError("invalid basename")
    return _utf8_bounded(value, 255)


class ProvenanceInput(APIModel):
    source: str | None = Field(default=None, max_length=128)
    source_ref: str | None = Field(default=None, max_length=512)
    captured_at: str | None = Field(default=None, max_length=64)
    basename: str | None = Field(default=None, max_length=255)

    @field_validator("source", "source_ref", "captured_at")
    @classmethod
    def bound_provenance_strings(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if "/" in value or "\\" in value or value.startswith("~"):
            raise ValueError("filesystem paths are not accepted")
        return _utf8_bounded(value, 1024)

    @field_validator("basename")
    @classmethod
    def basename_only(cls, value: str | None) -> str | None:
        return _basename(value)


class RetainRequest(APIModel):
    source_id: SourceId
    source_version: Version
    content: str = Field(min_length=1, max_length=LIMIT_CEILINGS["max_text_bytes"])
    provenance: ProvenanceInput = Field(default_factory=ProvenanceInput)

    @field_validator("content")
    @classmethod
    def bounded_content(cls, value: str) -> str:
        return _utf8_bounded(value, LIMIT_CEILINGS["max_text_bytes"])


class RecallRequest(APIModel):
    query: str = Field(min_length=1, max_length=LIMIT_CEILINGS["max_query_bytes"])
    max_results: int | None = Field(default=None, ge=1, le=LIMIT_CEILINGS["max_result_count"])
    max_tokens: int = Field(default=4096, ge=1, le=8192)
    budget: _BUDGETS = "mid"

    @field_validator("query")
    @classmethod
    def bounded_query(cls, value: str) -> str:
        return _utf8_bounded(value, LIMIT_CEILINGS["max_query_bytes"])


class ReflectRequest(RecallRequest):
    budget: _BUDGETS = "low"


class Citation(APIModel):
    source_id: SourceId
    source_version: Version
    sha256: Sha256


class RecallResult(APIModel):
    text: str = Field(min_length=1, max_length=LIMIT_CEILINGS["max_response_bytes"])
    type: str | None = Field(default=None, max_length=128)
    evidence: Citation


class RecallResponse(APIModel):
    results: list[RecallResult] = Field(max_length=LIMIT_CEILINGS["max_result_count"])
    truncated: bool
    omitted_unresolvable: int = Field(default=0, ge=0, le=LIMIT_CEILINGS["max_result_count"])


class ReflectResponse(APIModel):
    text: str = Field(min_length=1, max_length=LIMIT_CEILINGS["max_response_bytes"])
    evidence: list[Citation] = Field(min_length=1, max_length=LIMIT_CEILINGS["max_result_count"])


class SafeSourceMetadata(APIModel):
    source: str | None = Field(default=None, max_length=128)
    captured_at: str | None = Field(default=None, max_length=64)
    basename: str | None = Field(default=None, max_length=255)

    @field_validator("source", "captured_at")
    @classmethod
    def bounded_metadata(cls, value: str | None) -> str | None:
        return None if value is None else _utf8_bounded(value, 1024)

    @field_validator("basename")
    @classmethod
    def safe_basename(cls, value: str | None) -> str | None:
        return _basename(value)


class ExactEvidenceResponse(APIModel):
    source_id: SourceId
    source_version: Version
    sha256: Sha256
    size_bytes: int = Field(ge=0, le=LIMIT_CEILINGS["max_text_bytes"])
    metadata: SafeSourceMetadata
    citations: list[Citation] = Field(min_length=1, max_length=1)
    content: str | None = Field(default=None, max_length=LIMIT_CEILINGS["max_text_bytes"])

    @field_validator("content")
    @classmethod
    def bounded_evidence_content(cls, value: str | None) -> str | None:
        return None if value is None else _utf8_bounded(value, LIMIT_CEILINGS["max_text_bytes"])


class DocumentStage(APIModel):
    state: Literal["pending", "running", "accepted", "blocked", "failed", "conflict", "completed"]
    revision: Version
    attempt: int = Field(ge=0)
    error_code: str | None = Field(default=None, pattern=r"^[a-z0-9_]{1,64}$")


class DocumentStages(APIModel):
    custody: DocumentStage
    extraction: DocumentStage
    indexing: DocumentStage
    wiki: DocumentStage
    projection: DocumentStage


class DocumentCoverage(APIModel):
    """Every page or frame of the latest extraction in an explicit state."""

    unit: Literal["page", "frame"] | None
    total: int | None = Field(default=None, ge=0)
    resolved: int | None = Field(default=None, ge=0)
    text: int = Field(ge=0)
    ocr: int = Field(ge=0)
    blank: int = Field(ge=0)
    vision: int = Field(ge=0)
    unresolved: list[Annotated[str, StringConstraints(max_length=128)]] = Field(default_factory=list, max_length=1000)
    partial: list[Annotated[str, StringConstraints(max_length=128)]] = Field(default_factory=list, max_length=1000)
    complete: bool


class DocumentResponse(APIModel):
    source_id: SourceId
    source_version: Version
    filename: str = Field(min_length=1, max_length=255)
    title: str = Field(min_length=1, max_length=1024)
    mime_type: str = Field(min_length=3, max_length=255)
    category_id: SourceId
    category_revision: Version
    accepted: bool
    stages: DocumentStages
    download_url: str
    extraction_warnings: list[Annotated[str, StringConstraints(max_length=1024)]] = Field(default_factory=list, max_length=1000)
    coverage: DocumentCoverage | None = None

    @field_validator("filename")
    @classmethod
    def safe_filename(cls, value: str) -> str:
        return _basename(value)


class DocumentListResponse(APIModel):
    documents: list[DocumentResponse] = Field(max_length=4096)


class DocumentRetryRequest(APIModel):
    rerun_extraction: bool = False
    # None inherits the document's recorded choice; only valid with rerun_extraction.
    vision: bool | None = None


class RetainResponse(Citation):
    size_bytes: int = Field(ge=0, le=LIMIT_CEILINGS["max_text_bytes"])
    submission_state: Literal["submitted"]


class HealthResponse(APIModel):
    status: Literal["ok"]


class HindsightReadiness(APIModel):
    ready: bool
    capabilities: dict[Literal["aretain", "arecall", "areflect", "attributed_reflect"], bool]


class VaultReadiness(APIModel):
    ready: bool
    permissions: bool
    error: Literal["unsafe_path", "unsafe_permissions", "vault_unavailable"] | None = None


class CatalogReadiness(APIModel):
    ready: bool


class ReadinessDependencies(APIModel):
    hindsight: HindsightReadiness
    vault: VaultReadiness
    catalog: CatalogReadiness


class ReconciliationSummary(APIModel):
    fact_count: int = Field(ge=0)
    facts: dict[Literal["missing_original", "corrupt_original", "stale_projection"], int]


class StatusResponse(APIModel):
    ready: bool
    max_file_bytes: int = Field(ge=1, le=LIMIT_CEILINGS["max_file_bytes"])
    dependencies: ReadinessDependencies
    reconciliation: ReconciliationSummary


class SourceVersionSummary(APIModel):
    source_id: SourceId
    source_version: Version
    sha256: Sha256


class OperationSummary(APIModel):
    sources: list[SourceVersionSummary] = Field(default_factory=list, max_length=LIMIT_CEILINGS["max_result_count"])
    item_count: int | None = Field(default=None, ge=0, le=2_147_483_647)
    error_code: _ERROR_CODES | None = None


class OperationResponse(APIModel):
    operation_id: Annotated[str, StringConstraints(min_length=1, max_length=128, pattern=r"[A-Za-z0-9_-]+")]
    state: _OPERATION_STATES
    summary: OperationSummary = Field(default_factory=OperationSummary)


class DocumentOperationResponse(OperationResponse):
    document: DocumentResponse | None = None


class RelationshipRecord(APIModel):
    edge_id: EdgeId
    revision: int = Field(ge=1)
    from_: str = Field(alias="from", min_length=1, max_length=1024)
    to: str = Field(min_length=1, max_length=1024)
    relation: _RELATIONS
    status: _RELATIONSHIP_STATES
    source_revision: Annotated[str, StringConstraints(pattern=r"(?:[1-9][0-9]*:[0-9a-f]{64}|[0-9a-f]{64})")]
    target_revision: Annotated[str, StringConstraints(pattern=r"(?:[1-9][0-9]*:[0-9a-f]{64}|[0-9a-f]{64})")]
    provenance: Literal["wikilink"]
    evidence_id: Annotated[str, StringConstraints(pattern=r"sha256:[0-9a-f]{64}")]
    decision_principal: str | None = Field(default=None, max_length=256)

    @field_validator("from_", "to")
    @classmethod
    def normalized_reference_only(cls, value: str) -> str:
        kind, separator, identity = value.partition(":")
        if not separator or kind not in {"journal", "note", "path"} or not identity:
            raise ValueError("invalid relationship reference")
        if "\\" in identity or identity.startswith("/"):
            raise ValueError("absolute relationship references are forbidden")
        if kind == "path" and any(part in {"", ".", ".."} for part in identity.split("/")):
            raise ValueError("relationship paths must be normalized and relative")
        return _utf8_bounded(value, 1024)


class RelationshipReviewItem(APIModel):
    code: str = Field(min_length=1, max_length=64)
    source: str | None = Field(default=None, max_length=1024)
    hint_sha256: Annotated[str, StringConstraints(pattern=_SHA256)] | None = None
    edge_id: EdgeId | None = None


class RelationshipListResponse(APIModel):
    relationships: list[RelationshipRecord] = Field(max_length=LIMIT_CEILINGS["max_result_count"])


class RelationshipScanResponse(APIModel):
    edges: list[RelationshipRecord] = Field(max_length=LIMIT_CEILINGS["max_result_count"])
    review: list[RelationshipReviewItem] = Field(max_length=LIMIT_CEILINGS["max_result_count"])
    scanned_files: int = Field(ge=0, le=LIMIT_CEILINGS["max_relationship_files"])
    truncated: bool


class RelationshipDecisionRequest(APIModel):
    decision: Literal["approve", "reject"]
    expected_revision: int = Field(ge=1, le=2_147_483_647)


class RelationshipScanRequest(APIModel):
    source_id: SourceId | None = None
    max_results: int = Field(default=JournalLimits().max_result_count, ge=1, le=LIMIT_CEILINGS["max_result_count"])


class CurationBase(APIModel):
    source_id: SourceId


class CorrectSource(CurationBase):
    action: Literal["correct"]
    expected_version: Version
    content: str = Field(min_length=1, max_length=LIMIT_CEILINGS["max_text_bytes"])

    @field_validator("content")
    @classmethod
    def bounded_content(cls, value: str) -> str:
        return _utf8_bounded(value, LIMIT_CEILINGS["max_text_bytes"])


class ReindexSource(CurationBase):
    action: Literal["reindex"]
    expected_version: Version | None = None


class PrepareForget(CurationBase):
    action: Literal["prepare_forget"]
    expected_version: Version


class ForgetSource(CurationBase):
    action: Literal["forget"]
    expected_version: Version
    prepared_operation_id: str = Field(min_length=1, max_length=128)


class PrepareDelete(CurationBase):
    action: Literal["prepare_delete"]
    expected_version: Version


class DeleteSource(CurationBase):
    action: Literal["delete"]
    expected_version: Version
    prepared_operation_id: str = Field(min_length=1, max_length=128)


CurationRequest: TypeAlias = Annotated[
    CorrectSource | ReindexSource | PrepareForget | ForgetSource | PrepareDelete | DeleteSource,
    Field(discriminator="action"),
]


class ExportRequest(APIModel):
    source_ids: list[SourceId] | None = Field(default=None, max_length=LIMIT_CEILINGS["max_result_count"])


class CategoryRecord(APIModel):
    category_id: SourceId
    name: str = Field(min_length=1, max_length=256)
    description: str = Field(max_length=4096)
    aliases: list[str] = Field(default_factory=list, max_length=100)
    revision: Version


class CategoryListResponse(APIModel):
    categories: list[CategoryRecord] = Field(max_length=4096)


class CategoryProposalRecord(APIModel):
    proposal_id: SourceId
    name: str = Field(min_length=1, max_length=256)
    description: str = Field(max_length=4096)
    rationale: str = Field(max_length=4096)
    closest_category_id: SourceId | None = None
    category_id: SourceId | None = None
    state: Literal["pending", "approved", "rejected", "redirected"]
    revision: Version


class CategoryProposalListResponse(APIModel):
    proposals: list[CategoryProposalRecord] = Field(max_length=4096)


class CategoryMutationResponse(OperationResponse):
    category: CategoryRecord | None = None
    proposal: CategoryProposalRecord | None = None
    classification_status: Literal["inbox", "reuse", "pending"] | None = None


class CategoryClassificationInput(APIModel):
    name: str = Field(min_length=1, max_length=256)
    description: str = Field(max_length=4096)
    rationale: str = Field(min_length=1, max_length=4096)
    confidence: float = Field(ge=0, le=1)
    broad: bool
    tags: list[str] = Field(default_factory=list, max_length=100)
    closest_category_id: SourceId | None = None


class DocumentReference(APIModel):
    source_id: SourceId
    source_version: Version


class CategoryProposeRequest(APIModel):
    classification: CategoryClassificationInput
    documents: list[DocumentReference] = Field(default_factory=list, max_length=4096)


class CategoryDecisionRequest(APIModel):
    expected_revision: Version


class CategoryApproveRequest(CategoryDecisionRequest):
    category_id: SourceId
    budget_override: bool = False


class CategoryRedirectRequest(CategoryDecisionRequest):
    category_id: SourceId


class CategoryRenameRequest(CategoryDecisionRequest):
    name: str = Field(min_length=1, max_length=256)
    description: str | None = Field(default=None, max_length=4096)


class CategoryAliasRequest(CategoryDecisionRequest):
    alias: str = Field(min_length=1, max_length=256)


class CategoryMergeRequest(APIModel):
    target_category_id: SourceId
    expected_source_revision: Version
    expected_target_revision: Version


class CategoryReassignRequest(CategoryRedirectRequest):
    source_id: SourceId


class WikiPageRecord(APIModel):
    page_id: SourceId
    title: str = Field(min_length=1, max_length=1024)
    current_revision: int = Field(ge=0)
    state: Literal["pending", "current", "stale", "conflict"]


class WikiListResponse(APIModel):
    pages: list[WikiPageRecord] = Field(max_length=4096)


class WikiRevisionRecord(APIModel):
    page_id: SourceId
    revision: Version
    parent_revision: Version | None = None
    artifact_sha256: Sha256


class WikiHistoryResponse(APIModel):
    revisions: list[WikiRevisionRecord] = Field(max_length=4096)


class WikiShowResponse(APIModel):
    page: WikiPageRecord
    revision: WikiRevisionRecord | None = None
    managed: str = Field(max_length=LIMIT_CEILINGS["max_response_bytes"])


class WikiStatusResponse(WikiListResponse):
    semantic_support_verified: bool
    summary_model_generated: bool


class WikiRebuildRequest(APIModel):
    source_id: SourceId | None = None
    version: Version | None = None


class WikiLintFinding(APIModel):
    code: Literal[
        "revision_unreadable", "contradiction_pending", "page_stale", "page_conflict", "link_missing",
        "page_near_limit", "page_orphan", "title_near_duplicate", "stage_failed", "stage_pending",
        "source_page_missing", "coverage_unresolved", "coverage_partial", "category_proposal_pending",
        "projection_conflict", "summary_missing", "source_unrouted", "page_links_capped",
    ]
    severity: Literal["error", "warning", "info"]
    page_id: SourceId | None = None
    source_id: SourceId | None = None
    detail: str | None = Field(default=None, max_length=512)
    hint: str = Field(min_length=1, max_length=1024)


class WikiLintCounts(APIModel):
    error: int = Field(ge=0)
    warning: int = Field(ge=0)
    info: int = Field(ge=0)


class WikiLintResponse(APIModel):
    findings: list[WikiLintFinding] = Field(max_length=500)
    counts: WikiLintCounts
    omitted: int = Field(ge=0)
    documents_omitted: int = Field(ge=0)


class WikiReviewResolution(APIModel):
    note: ShortText
    resolved_at: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class WikiReviewRecord(APIModel):
    review_id: SourceId
    page_id: SourceId
    revision: Version
    code: Literal["manual_edit", "content_conflict", "contradiction"]
    state: Literal["pending", "resolved"]
    details: dict[str, Any]
    resolution: WikiReviewResolution | None = None


class WikiReviewListResponse(APIModel):
    reviews: list[WikiReviewRecord] = Field(max_length=100)
    omitted: int = Field(ge=0)


class WikiReviewResolveRequest(APIModel):
    note: ShortText


class WikiReviewMutationResponse(OperationResponse):
    review: WikiReviewRecord | None = None


_LOCAL_TIME = r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$"
_UTC_TIME = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"
_STAGE_STATES = Literal["pending", "running", "accepted", "blocked", "failed", "conflict", "completed"]


class BriefDocument(APIModel):
    source_id: SourceId
    source_version: Version
    title: str = Field(min_length=1, max_length=1024)
    at: str = Field(pattern=_LOCAL_TIME)
    stages: dict[Literal["custody", "extraction", "indexing", "wiki", "projection"], _STAGE_STATES]
    unresolved: int = Field(ge=0)
    partial: int = Field(ge=0)


class BriefDocuments(APIModel):
    items: list[BriefDocument] = Field(max_length=100)
    omitted: int = Field(ge=0)


class BriefWikiChange(APIModel):
    page_id: SourceId
    title: str = Field(min_length=1, max_length=1024)
    parent_revision: Version | None = None
    revision: Version
    claims_added: int | None = Field(default=None, ge=0)
    claims_removed: int | None = Field(default=None, ge=0)
    at: str = Field(pattern=_LOCAL_TIME)


class BriefWikiChanges(APIModel):
    items: list[BriefWikiChange] = Field(max_length=100)
    omitted: int = Field(ge=0)


class BriefSide(APIModel):
    text: str = Field(min_length=1, max_length=2048)
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")


class BriefContradiction(APIModel):
    review_id: SourceId
    page_id: SourceId
    title: str = Field(min_length=1, max_length=1024)
    revision: Version
    state: Literal["pending", "resolved"]
    left: BriefSide
    right: BriefSide


class BriefContradictions(APIModel):
    items: list[BriefContradiction] = Field(max_length=50)
    omitted: int = Field(ge=0)


class BriefPage(APIModel):
    page_id: SourceId
    title: str = Field(min_length=1, max_length=1024)
    state: Literal["stale", "conflict"]


class BriefPages(APIModel):
    items: list[BriefPage] = Field(max_length=100)
    omitted: int = Field(ge=0)


class BriefStage(APIModel):
    source_id: SourceId
    detail: str = Field(max_length=512)
    severity: Literal["error", "warning"]


class BriefStages(APIModel):
    items: list[BriefStage] = Field(max_length=100)
    omitted: int = Field(ge=0)


class BriefLint(APIModel):
    counts: WikiLintCounts
    omitted: int = Field(ge=0)
    documents_omitted: int = Field(ge=0)
    codes: dict[str, int]


class BriefProjection(APIModel):
    brief: Literal["completed", "conflict"]
    log: Literal["completed", "conflict"]


class BriefOpenNow(APIModel):
    attention: BriefPages
    stages: BriefStages
    reviews_pending: int = Field(ge=0)
    lint: BriefLint


class BriefResponse(APIModel):
    date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    since: str = Field(pattern=_UTC_TIME)
    until: str = Field(pattern=_UTC_TIME)
    timezone: str = Field(min_length=1, max_length=128)
    documents: BriefDocuments
    journal_entries: int = Field(ge=0)
    wiki_changes: BriefWikiChanges
    contradictions: BriefContradictions
    open_now: BriefOpenNow
    projection: BriefProjection | None = None


class ErrorResponse(APIModel):
    request_id: Annotated[str, StringConstraints(min_length=16, max_length=64, pattern=r"[0-9a-f]+")]
    error_code: _ERROR_CODES
    retryable: bool


def omit_source_text(value: Any) -> Any:
    """Recursively remove source-text-bearing keys from exact-evidence metadata."""
    if isinstance(value, dict):
        return {
            key: omit_source_text(child)
            for key, child in value.items()
            if not isinstance(key, str) or key.casefold() not in _SOURCE_TEXT_KEYS
        }
    if isinstance(value, list):
        return [omit_source_text(child) for child in value]
    if isinstance(value, tuple):
        return tuple(omit_source_text(child) for child in value)
    return value
