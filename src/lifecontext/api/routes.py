"""Typed V1 HTTP routes over ``JournalApplication``."""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from datetime import date, datetime
from urllib.parse import quote, unquote_to_bytes
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Header, Path, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError

from lifecontext.api.app import PublicAPIError
from lifecontext.bundle import BUNDLE_MEDIA_TYPE
from lifecontext.api.models import (
    CurationRequest,
    DocumentResponse,
    DocumentListResponse,
    DocumentOperationResponse,
    DocumentRetryRequest,
    _basename,
    ExactEvidenceResponse,
    ErrorResponse,
    ExportRequest,
    HealthResponse,
    OperationResponse,
    ProvenanceInput,
    RecallRequest,
    RecallResponse,
    ReflectRequest,
    ReflectResponse,
    RelationshipDecisionRequest,
    RelationshipListResponse,
    RelationshipRecord,
    RelationshipScanRequest,
    RelationshipScanResponse,
    RetainRequest,
    RetainResponse,
    SafeSourceMetadata,
    SourceId,
    StatusResponse,
    omit_source_text,
)
from lifecontext.application import ApplicationError
from lifecontext.config import JournalLimits


from lifecontext.api.models import (
    CategoryRecord, CategoryListResponse, CategoryProposalRecord, CategoryProposalListResponse, CategoryMutationResponse,
    CategoryProposeRequest, CategoryDecisionRequest, CategoryApproveRequest,
    CategoryRedirectRequest, CategoryRenameRequest, CategoryAliasRequest,
    CategoryMergeRequest, CategoryReassignRequest, WikiPageRecord, WikiListResponse,
    WikiRevisionRecord, WikiHistoryResponse, WikiShowResponse, WikiStatusResponse, WikiRebuildRequest,
    WikiLintResponse, WikiReviewListResponse, WikiReviewMutationResponse, WikiReviewRecord,
    WikiReviewResolveRequest, BriefResponse,
)
from lifecontext.categories import CategoryClassification
from lifecontext.api.app import DOCUMENT_MAX_BYTES
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect
from starlette.responses import StreamingResponse


router = APIRouter(
    responses={
        status: {"model": ErrorResponse}
        for status in (400, 403, 404, 409, 413, 415, 422, 500, 502, 503)
    }
)


def _application(request: Request):
    application = getattr(request.app.state, "application", None)
    if application is None:
        raise PublicAPIError(503, "provider_unavailable", retryable=True)
    return application


def _required_idempotency_key(request: Request, value: str) -> str:
    values = [
        item
        for name, item in request.scope.get("headers", ())
        if name.lower() == b"idempotency-key"
    ]
    if len(values) != 1:
        raise PublicAPIError(422, "invalid_request")
    try:
        raw_value = values[0].decode("ascii")
    except UnicodeError:
        raise PublicAPIError(422, "invalid_request") from None
    if (
        not isinstance(value, str)
        or value != raw_value
        or not 1 <= len(values[0]) <= 255
        or any(byte < 0x21 or byte > 0x7E for byte in values[0])
    ):
        raise PublicAPIError(422, "invalid_request")
    return raw_value


def _check_bytes(value: str, maximum: int, code: str = "invalid_request") -> None:
    try:
        if len(value.encode("utf-8")) > maximum:
            raise PublicAPIError(413, "body_too_large")
    except UnicodeError:
        raise PublicAPIError(422, code) from None


def _safe_metadata(manifest: Any) -> SafeSourceMetadata:
    provenance = manifest.provenance
    values: dict[str, str] = {}
    if not isinstance(provenance, dict) and not hasattr(provenance, "get"):
        provenance = {}
    for key in ("source", "captured_at"):
        value = provenance.get(key)
        if (
            isinstance(value, str)
            and value
            and "/" not in value
            and "\\" not in value
            and not any(part == ".." for part in value.split("."))
        ):
            values[key] = value
    basename = provenance.get("basename")
    if isinstance(basename, str) and "/" not in basename and "\\" not in basename:
        values["basename"] = basename
    try:
        return SafeSourceMetadata.model_validate(values)
    except ValidationError:
        return SafeSourceMetadata()


def _map_record(value: dict[str, Any]) -> RelationshipRecord:
    try:
        return RelationshipRecord.model_validate(value)
    except ValidationError:
        raise ApplicationError("invalid_provider_response") from None


@router.get("/healthz", name="health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Return process liveness only; no dependency probes are performed."""
    return HealthResponse(status="ok")


@router.get("/v1/status", response_model=StatusResponse)
async def status(request: Request) -> dict[str, Any]:
    """Return redacted dependency readiness, separate from process liveness."""
    return _application(request).readiness()


@router.post("/v1/projections/reconcile")
async def reconcile_projections(
    request: Request, limit: Annotated[int, Query(ge=1, le=4096)] = 200,
) -> dict[str, int]:
    """Reconcile derived output from exact custody without provider resubmission."""
    return _application(request).reconcile_projections(limit=limit)


@router.post(
    "/v1/retain",
    response_model=RetainResponse,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {"schema": {"$ref": "#/components/schemas/RetainRequest"}},
                "application/octet-stream": {"schema": {"type": "string", "format": "binary"}},
            },
        }
    },
)
async def retain(
    request: Request,
    response: Response,
    idempotency_key_header: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)],
    source_id: Annotated[SourceId, Query()] | None = None,
    source_version: Annotated[int, Query(ge=1, le=2_147_483_647)] | None = None,
    source_basename: Annotated[str | None, Header(alias="X-Source-Basename")] = None,
) -> RetainResponse:
    """Capture bounded UTF-8 JSON text or a bounded streamed octet upload."""
    idempotency_key = _required_idempotency_key(request, idempotency_key_header)
    application = _application(request)
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().casefold()
    if content_type == "application/json":
        try:
            raw = await request.body()
            value = RetainRequest.model_validate_json(raw)
        except (ValidationError, ValueError, UnicodeError):
            raise PublicAPIError(422, "invalid_request") from None
        if len(value.content.encode("utf-8")) > application.limits.max_text_bytes:
            raise PublicAPIError(413, "body_too_large")
        source_id_value = value.source_id
        source_version_value = value.source_version
        content = value.content.encode("utf-8")
        provenance = value.provenance.model_dump(exclude_none=True)
    elif content_type == "application/octet-stream":
        if source_id is None or source_version is None:
            raise PublicAPIError(422, "invalid_request")
        try:
            provenance_model = ProvenanceInput(source="upload", basename=source_basename)
        except ValidationError:
            raise PublicAPIError(422, "invalid_request") from None
        chunks: list[bytes] = []
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > application.limits.max_text_bytes:
                raise PublicAPIError(413, "body_too_large")
            chunks.append(chunk)
        source_id_value = source_id
        source_version_value = source_version
        content = b"".join(chunks)
        provenance = provenance_model.model_dump(exclude_none=True)
    else:
        raise PublicAPIError(415, "invalid_request")

    if not content:
        raise PublicAPIError(422, "invalid_request")
    result = await application.retain(
        source_id_value,
        source_version_value,
        content,
        provenance=provenance,
        idempotency_key=idempotency_key,
    )
    value = RetainResponse.model_validate(result)
    response.headers["X-Lifeweave-Operation-ID"] = result.operation_id
    return value


@router.post("/v1/recall", response_model=RecallResponse)
async def recall(payload: RecallRequest, request: Request) -> RecallResponse:
    application = _application(request)
    _check_bytes(payload.query, application.limits.max_query_bytes)
    max_results = payload.max_results or min(JournalLimits().max_result_count, application.limits.max_result_count)
    if max_results > application.limits.max_result_count:
        raise PublicAPIError(422, "invalid_request")
    value = await application.recall(
        payload.query,
        max_results=max_results,
        max_tokens=payload.max_tokens,
        budget=payload.budget,
    )
    try:
        # The application keeps the provider memory ID only for internal
        # attribution. It is not part of the public recall DTO.
        return RecallResponse.model_validate(
            {
                "results": [
                    {
                        "text": item["text"],
                        "type": item.get("type"),
                        "evidence": item["evidence"],
                    }
                    for item in value["results"]
                ],
                "truncated": value["truncated"],
                "omitted_unresolvable": value.get("omitted_unresolvable", 0),
            }
        )
    except (KeyError, TypeError, ValidationError):
        raise ApplicationError("invalid_provider_response") from None


@router.post("/v1/reflect", response_model=ReflectResponse)
async def reflect(payload: ReflectRequest, request: Request) -> ReflectResponse:
    application = _application(request)
    _check_bytes(payload.query, application.limits.max_query_bytes)
    max_results = payload.max_results or min(JournalLimits().max_result_count, application.limits.max_result_count)
    if max_results > application.limits.max_result_count:
        raise PublicAPIError(422, "invalid_request")
    value = await application.reflect(
        payload.query,
        max_results=max_results,
        max_tokens=payload.max_tokens,
        budget=payload.budget,
    )
    try:
        return ReflectResponse.model_validate(value)
    except ValidationError:
        raise ApplicationError("invalid_provider_response") from None


@router.get("/v1/evidence/{source_id}", response_model=ExactEvidenceResponse, response_model_exclude_none=True)
async def exact_evidence(
    source_id: SourceId,
    request: Request,
    version: Annotated[int, Query(ge=1, le=2_147_483_647)],
    include_content: bool = False,
) -> ExactEvidenceResponse:
    evidence = _application(request).exact_evidence(source_id, version)
    manifest = evidence.manifest
    if manifest.source_id != source_id or manifest.source_version != version:
        raise ApplicationError("evidence_unavailable")
    content: str | None = None
    source = _application(request).catalog.get_source(source_id)
    if include_content and source is not None and source["layer"] == "document":
        raise PublicAPIError(422, "invalid_request")
    if include_content:
        try:
            content = evidence.original_bytes.decode("utf-8")
        except UnicodeDecodeError:
            raise ApplicationError("invalid_source_text") from None
    descriptor = {
        "source_id": manifest.source_id,
        "source_version": manifest.source_version,
        "sha256": manifest.sha256,
        "size_bytes": manifest.size_bytes,
        "metadata": _safe_metadata(manifest).model_dump(exclude_none=True),
        "citations": [
            {
                "source_id": manifest.source_id,
                "source_version": manifest.source_version,
                "sha256": manifest.sha256,
            }
        ],
        "content": content,
    }
    if not include_content:
        descriptor = omit_source_text(descriptor)
    try:
        return ExactEvidenceResponse.model_validate(descriptor)
    except ValidationError:
        raise ApplicationError("invalid_provider_response") from None


@router.post("/v1/curation", response_model=OperationResponse)
async def curation(
    payload: CurationRequest,
    request: Request,
    response: Response,
    idempotency_key_header: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)],
) -> OperationResponse:
    idempotency_key = _required_idempotency_key(request, idempotency_key_header)
    result = await _application(request).curate(
        payload.model_dump(), idempotency_key=idempotency_key
    )
    try:
        value = OperationResponse.model_validate(result)
    except ValidationError:
        raise ApplicationError("invalid_provider_response") from None
    response.headers["X-Lifeweave-Operation-ID"] = value.operation_id
    return value


@router.get("/v1/relationships", response_model=RelationshipListResponse)
async def list_relationships(
    request: Request,
    edge_id: str | None = Query(default=None, pattern=r"^edge_[0-9a-f]{64}$"),
    source_id: SourceId | None = None,
    relationship_status: Annotated[str | None, Query(alias="status", pattern="^(proposed|approved|rejected)$")] = None,
    max_results: int | None = Query(default=None, ge=1, le=JournalLimits().max_result_count),
) -> RelationshipListResponse:
    application = _application(request)
    if max_results is not None and max_results > application.limits.max_result_count:
        raise PublicAPIError(422, "invalid_request")
    result = application.list_relationships(
        edge_id=edge_id,
        source_id=source_id,
        status=relationship_status,
        max_results=max_results,
    )
    if len(result) > application.limits.max_result_count:
        raise PublicAPIError(502, "invalid_provider_response")
    return RelationshipListResponse(relationships=[_map_record(item) for item in result])


@router.post("/v1/relationships/scan", response_model=RelationshipScanResponse)
async def scan_relationships(
    payload: RelationshipScanRequest,
    request: Request,
    response: Response,
    idempotency_key_header: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)],
) -> RelationshipScanResponse:
    idempotency_key = _required_idempotency_key(request, idempotency_key_header)
    application = _application(request)
    max_results = payload.max_results
    if "max_results" not in payload.model_fields_set:
        max_results = min(max_results, application.limits.max_result_count)
    if max_results > application.limits.max_result_count:
        raise PublicAPIError(422, "invalid_request")
    result = application.scan_relationships(
        source_id=payload.source_id,
        max_results=max_results,
        idempotency_key=idempotency_key,
    )
    try:
        edges = [_map_record(item) for item in result["edges"]]
        value = RelationshipScanResponse(
            edges=edges,
            review=result["review"],
            scanned_files=result["scanned_files"],
            truncated=result["truncated"],
        )
    except (KeyError, TypeError, ValidationError):
        raise ApplicationError("invalid_provider_response") from None
    response.headers["X-Lifeweave-Operation-ID"] = result.operation_id
    return value


@router.post("/v1/relationships/{edge_id}/decision", response_model=RelationshipRecord)
async def decide_relationship(
    edge_id: Annotated[str, Path(pattern=r"^edge_[0-9a-f]{64}$")],
    payload: RelationshipDecisionRequest,
    request: Request,
    response: Response,
    idempotency_key_header: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)],
) -> RelationshipRecord:
    idempotency_key = _required_idempotency_key(request, idempotency_key_header)
    result = _application(request).decide_relationship(
        edge_id,
        payload.decision,
        payload.expected_revision,
        idempotency_key=idempotency_key,
    )
    value = _map_record(result)
    response.headers["X-Lifeweave-Operation-ID"] = result.operation_id
    return value


@router.post(
    "/v1/imports",
    response_model=OperationResponse,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/octet-stream": {
                    "schema": {"type": "string", "format": "binary"}
                }
            },
        }
    },
)
async def import_bundle(
    request: Request,
    response: Response,
    idempotency_key_header: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)],
) -> OperationResponse:
    idempotency_key = _required_idempotency_key(request, idempotency_key_header)
    application = _application(request)
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().casefold()
    if content_type != "application/octet-stream":
        raise PublicAPIError(415, "invalid_request")
    result = await application.import_bundle(
        await request.body(), idempotency_key=idempotency_key
    )
    try:
        value = OperationResponse.model_validate(
            application.operation_status(result.operation_id)
        )
    except ValidationError:
        raise ApplicationError("invalid_provider_response") from None
    response.headers["X-Lifeweave-Operation-ID"] = result.operation_id
    return value


@router.post(
    "/v1/exports",
    response_class=Response,
    response_model=None,
    responses={
        200: {
            "description": "Encoded LifeContext bundle bytes.",
            "content": {
                BUNDLE_MEDIA_TYPE: {
                    "schema": {"type": "string", "format": "binary"}
                }
            },
        }
    },
)
async def export_bundle(
    payload: ExportRequest,
    request: Request,
    idempotency_key_header: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)],
) -> Response:
    _required_idempotency_key(request, idempotency_key_header)
    bundle = _application(request).export_bundle(source_ids=payload.source_ids)
    return Response(content=bundle, media_type=BUNDLE_MEDIA_TYPE)


def _public_record(model, value):
    try:
        return model.model_validate({key: value[key] for key in model.model_fields if key in value})
    except (TypeError, ValidationError):
        raise ApplicationError("invalid_provider_response") from None


def _category_result(application, result, response):
    value = CategoryMutationResponse.model_validate(application.operation_status(result.operation_id))
    if "proposal_id" in result:
        value.proposal = _public_record(CategoryProposalRecord, result)
    elif "name" in result and "category_id" in result:
        value.category = _public_record(CategoryRecord, result)
    if "status" in result:
        value.classification_status = result["status"]
        if result.get("proposal"):
            value.proposal = _public_record(CategoryProposalRecord, result["proposal"])
    response.headers["X-Lifeweave-Operation-ID"] = result.operation_id
    return value


@router.get("/v1/categories", response_model=CategoryListResponse)
async def list_categories(request: Request):
    return CategoryListResponse(categories=[_public_record(CategoryRecord, row) for row in _application(request).list_categories()])


@router.get("/v1/categories/proposals", response_model=CategoryProposalListResponse)
async def list_category_proposals(request: Request, state: Literal["pending", "approved", "rejected", "redirected"] = "pending"):
    return CategoryProposalListResponse(proposals=[_public_record(CategoryProposalRecord, row) for row in _application(request).list_category_proposals(state=state)])


@router.post("/v1/categories/proposals", response_model=CategoryMutationResponse)
async def propose_category(payload: CategoryProposeRequest, request: Request, response: Response,
                           idempotency_key_header: Annotated[str, Header(alias="Idempotency-Key")]):
    key = _required_idempotency_key(request, idempotency_key_header)
    fields = payload.classification.model_dump()
    fields["tags"] = tuple(fields["tags"])
    app = _application(request)
    result = app.propose_category(CategoryClassification(**fields), documents=[row.model_dump() for row in payload.documents], idempotency_key=key)
    return _category_result(app, result, response)


@router.post("/v1/categories/proposals/{proposal_id}/approve", response_model=CategoryMutationResponse)
async def approve_category(proposal_id: SourceId, payload: CategoryApproveRequest, request: Request, response: Response,
                           idempotency_key_header: Annotated[str, Header(alias="Idempotency-Key")]):
    app = _application(request)
    result = app.approve_category(proposal_id, **payload.model_dump(), idempotency_key=_required_idempotency_key(request, idempotency_key_header))
    return _category_result(app, result, response)


@router.post("/v1/categories/proposals/{proposal_id}/reject", response_model=CategoryMutationResponse)
async def reject_category(proposal_id: SourceId, payload: CategoryDecisionRequest, request: Request, response: Response,
                          idempotency_key_header: Annotated[str, Header(alias="Idempotency-Key")]):
    app = _application(request)
    result = app.reject_category(proposal_id, **payload.model_dump(), idempotency_key=_required_idempotency_key(request, idempotency_key_header))
    return _category_result(app, result, response)


@router.post("/v1/categories/proposals/{proposal_id}/redirect", response_model=CategoryMutationResponse)
async def redirect_category(proposal_id: SourceId, payload: CategoryRedirectRequest, request: Request, response: Response,
                            idempotency_key_header: Annotated[str, Header(alias="Idempotency-Key")]):
    app = _application(request)
    result = app.redirect_category(proposal_id, **payload.model_dump(), idempotency_key=_required_idempotency_key(request, idempotency_key_header))
    return _category_result(app, result, response)


@router.post("/v1/categories/{category_id}/rename", response_model=CategoryMutationResponse)
async def rename_category(category_id: SourceId, payload: CategoryRenameRequest, request: Request, response: Response,
                          idempotency_key_header: Annotated[str, Header(alias="Idempotency-Key")]):
    app = _application(request)
    result = app.rename_category(category_id, **payload.model_dump(), idempotency_key=_required_idempotency_key(request, idempotency_key_header))
    return _category_result(app, result, response)


@router.post("/v1/categories/{category_id}/alias", response_model=CategoryMutationResponse)
async def alias_category(category_id: SourceId, payload: CategoryAliasRequest, request: Request, response: Response,
                         idempotency_key_header: Annotated[str, Header(alias="Idempotency-Key")]):
    app = _application(request)
    result = app.alias_category(category_id, **payload.model_dump(), idempotency_key=_required_idempotency_key(request, idempotency_key_header))
    return _category_result(app, result, response)


@router.post("/v1/categories/{category_id}/merge", response_model=CategoryMutationResponse)
async def merge_categories(category_id: SourceId, payload: CategoryMergeRequest, request: Request, response: Response,
                           idempotency_key_header: Annotated[str, Header(alias="Idempotency-Key")]):
    app = _application(request)
    result = app.merge_categories(category_id, **payload.model_dump(), idempotency_key=_required_idempotency_key(request, idempotency_key_header))
    return _category_result(app, result, response)


@router.post("/v1/categories/reassign", response_model=CategoryMutationResponse)
async def reassign_document_category(payload: CategoryReassignRequest, request: Request, response: Response,
                                     idempotency_key_header: Annotated[str, Header(alias="Idempotency-Key")]):
    app = _application(request)
    result = app.reassign_document_category(**payload.model_dump(), idempotency_key=_required_idempotency_key(request, idempotency_key_header))
    return _category_result(app, result, response)


@router.get("/v1/wiki", response_model=WikiListResponse)
async def list_wiki(request: Request):
    return WikiListResponse(pages=[_public_record(WikiPageRecord, row) for row in _application(request).list_wiki()])


@router.get("/v1/wiki/status", response_model=WikiStatusResponse)
async def wiki_status(request: Request):
    result = _application(request).wiki_status()
    return WikiStatusResponse(pages=[_public_record(WikiPageRecord, row) for row in result["pages"]], semantic_support_verified=result["semantic_support_verified"],
                              summary_model_generated=result["summary_model_generated"])


@router.get("/v1/wiki/lint", response_model=WikiLintResponse)
async def wiki_lint(request: Request):
    """Model-free wiki and document health findings; never writes."""
    try:
        return WikiLintResponse.model_validate(await run_in_threadpool(_application(request).wiki_lint))
    except ValidationError:
        raise ApplicationError("invalid_provider_response") from None


@router.get("/v1/wiki/reviews", response_model=WikiReviewListResponse)
async def list_wiki_reviews(request: Request, page: SourceId | None = None,
                            state: Literal["pending", "resolved"] = "pending"):
    result = _application(request).list_wiki_reviews(page_id=page, state=state)
    return WikiReviewListResponse(reviews=[_public_record(WikiReviewRecord, row) for row in result["reviews"]],
                                  omitted=result["omitted"])


@router.post("/v1/wiki/reviews/{review_id}/resolve", response_model=WikiReviewMutationResponse)
async def resolve_wiki_review(review_id: SourceId, payload: WikiReviewResolveRequest, request: Request, response: Response,
                              idempotency_key_header: Annotated[str, Header(alias="Idempotency-Key")]):
    """Record the owner's decision and note; claims are never edited and no side is chosen."""
    app = _application(request)
    result = app.resolve_wiki_review(review_id, note=payload.note,
                                     idempotency_key=_required_idempotency_key(request, idempotency_key_header))
    value = WikiReviewMutationResponse.model_validate(app.operation_status(result.operation_id))
    value.review = _public_record(WikiReviewRecord, result)
    response.headers["X-Lifeweave-Operation-ID"] = result.operation_id
    return value


@router.get("/v1/wiki/{page_id}", response_model=WikiShowResponse)
async def show_wiki(page_id: SourceId, request: Request, revision: Annotated[int | None, Query(ge=1)] = None):
    result = _application(request).show_wiki(page_id, revision=revision)
    return WikiShowResponse(page=_public_record(WikiPageRecord, result["page"]),
                            revision=_public_record(WikiRevisionRecord, result["revision"]) if result["revision"] else None,
                            managed=result["managed"])


@router.get("/v1/wiki/{page_id}/history", response_model=WikiHistoryResponse)
async def wiki_history(page_id: SourceId, request: Request):
    return WikiHistoryResponse(revisions=[_public_record(WikiRevisionRecord, row) for row in _application(request).wiki_history(page_id)])


@router.post("/v1/wiki/rebuild", response_model=OperationResponse)
async def rebuild_wiki(payload: WikiRebuildRequest, request: Request, response: Response,
                       idempotency_key_header: Annotated[str, Header(alias="Idempotency-Key")]):
    app = _application(request)
    result = await app.rebuild_wiki(**payload.model_dump(), idempotency_key=_required_idempotency_key(request, idempotency_key_header))
    response.headers["X-Lifeweave-Operation-ID"] = result.operation_id
    return OperationResponse.model_validate(app.operation_status(result.operation_id))


def _brief(value: dict[str, Any]) -> BriefResponse:
    try:
        return BriefResponse.model_validate(value)
    except ValidationError:
        raise ApplicationError("invalid_provider_response") from None


@router.get("/v1/brief", response_model=BriefResponse)
async def brief(request: Request, day: Annotated[date | None, Query(alias="date")] = None,
                since: datetime | None = None):
    """What changed on a local day (default today) or since a moment; model-free and read-only."""
    return _brief(await run_in_threadpool(_application(request).brief, day=day, since=since))


@router.post("/v1/brief/project", response_model=BriefResponse)
async def project_brief(request: Request, day: Annotated[date | None, Query(alias="date")] = None):
    """Write the day's brief and the change log into the vault; edited copies are never overwritten."""
    app = _application(request)
    # Build off the event loop; the vault writes stay on it with the service's catalog.
    return _brief(app.publish_brief(await run_in_threadpool(app.prepare_brief, day=day)))


def _document(value: dict[str, Any]) -> DocumentResponse:
    fields = ("source_id", "source_version", "filename", "title", "mime_type", "category_id", "category_revision", "accepted")
    try:
        return DocumentResponse.model_validate({
            **{key: value[key] for key in fields},
            "stages": {name: {key: stage[key] for key in ("state", "revision", "attempt", "error_code")}
                       for name, stage in value["stages"].items()},
            "download_url": f"/v1/documents/{value['source_id']}/original?version={value['source_version']}",
            "extraction_warnings": value.get("extractions", [{}])[-1].get("warnings", []) if value.get("extractions") else [],
            "coverage": value.get("coverage"),
        })
    except (KeyError, TypeError, ValidationError):
        raise ApplicationError("invalid_provider_response") from None


def _document_operation(application, result: dict[str, Any]) -> DocumentOperationResponse:
    envelope = application.operation_status(result["operation_id"])
    value = DocumentOperationResponse.model_validate(envelope)
    if result.get("document") is not None:
        value.document = _document(result["document"])
        if value.state == "completed" and value.document.stages.indexing.state != "completed":
            value.state = "running"
    return value


@router.post("/v1/documents", response_model=DocumentOperationResponse)
async def import_document(
    request: Request, response: Response,
    idempotency_key_header: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=255)],
) -> DocumentOperationResponse:
    key = _required_idempotency_key(request, idempotency_key_header)
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/octet-stream":
        raise PublicAPIError(415, "invalid_request")
    try:
        metadata = {}
        for header in ("x-document-filename", "x-document-mime", "x-document-provenance", "x-document-source-id", "x-document-source-version", "x-document-vision"):
            values = request.headers.getlist(header)
            if len(values) > 1:
                raise ValueError
            metadata[header] = values[0] if values else None
        encoded = metadata["x-document-filename"]
        if not isinstance(encoded, str) or len(encoded) > 765 or not encoded.isascii() or re.search(r"%(?![0-9a-fA-F]{2})", encoded):
            raise ValueError
        filename = _basename(unquote_to_bytes(encoded).decode("utf-8"))
        mime = metadata["x-document-mime"] or "application/octet-stream"
        if not re.fullmatch(r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+", mime) or len(mime) > 255:
            raise ValueError
        provenance_raw = metadata["x-document-provenance"] or "{}"
        if len(provenance_raw) > 4096:
            raise ValueError
        provenance = ProvenanceInput.model_validate_json(provenance_raw).model_dump(exclude_none=True)
        source_id = metadata["x-document-source-id"]
        source_version_raw = metadata["x-document-source-version"]
        source_version = None
        if (source_id is None) != (source_version_raw is None):
            raise ValueError
        if source_id is not None:
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", source_id):
                raise ValueError
            if not re.fullmatch(r"[1-9][0-9]{0,9}", source_version_raw):
                raise ValueError
            source_version = int(source_version_raw)
            if source_version > 2_147_483_647:
                raise ValueError
        if metadata["x-document-vision"] not in {None, "off"}:
            raise ValueError
        vision = metadata["x-document-vision"] is None
    except (ValueError, UnicodeError, ValidationError):
        raise PublicAPIError(422, "invalid_request") from None
    application = _application(request)
    # TemporaryFile is private and unlinked; disconnect/cancellation closes it.
    with tempfile.TemporaryFile(mode="w+b") as staged:
        size = 0
        try:
            async for chunk in request.stream():
                size += len(chunk)
                if size > DOCUMENT_MAX_BYTES:
                    raise PublicAPIError(413, "body_too_large")
                staged.write(chunk)
        except ClientDisconnect:
            raise PublicAPIError(400, "invalid_request") from None
        if not size:
            raise PublicAPIError(422, "invalid_request")
        staged.seek(0)
        content = staged.read(DOCUMENT_MAX_BYTES + 1)
    result = await application.import_document(
        content, filename, mime, provenance, key,
        source_id=source_id, source_version=source_version, vision=vision,
    )
    value = _document_operation(application, result)
    response.headers["X-Lifeweave-Operation-ID"] = value.operation_id
    return value


@router.get("/v1/documents", response_model=DocumentListResponse)
async def list_documents(request: Request, limit: Annotated[int, Query(ge=1, le=4096)] = 100):
    return DocumentListResponse(documents=[_document(value) for value in _application(request).list_documents(limit=limit)])


@router.get("/v1/documents/{source_id}", response_model=DocumentResponse)
async def show_document(source_id: SourceId, request: Request, version: Annotated[int | None, Query(ge=1)] = None):
    return _document(_application(request).show_document(source_id, source_version=version))


@router.post("/v1/documents/operations/{operation_id}/retry", response_model=DocumentOperationResponse)
async def retry_document(operation_id: str, payload: DocumentRetryRequest, request: Request, response: Response):
    application = _application(request)
    if payload.vision is not None and not payload.rerun_extraction:
        raise PublicAPIError(422, "invalid_request")
    result = await application.retry_document(
        operation_id, rerun_extraction=payload.rerun_extraction, vision=payload.vision
    )
    value = _document_operation(application, result)
    response.headers["X-Lifeweave-Operation-ID"] = value.operation_id
    return value


@router.get("/v1/documents/{source_id}/original", response_model=None)
async def download_document(source_id: SourceId, request: Request, version: Annotated[int | None, Query(ge=1)] = None):
    evidence = _application(request).download_document(source_id, source_version=version)
    manifest, content = evidence.manifest, evidence.original_bytes
    if manifest.source_id != source_id or (version is not None and manifest.source_version != version):
        raise ApplicationError("evidence_unavailable")
    if len(content) > DOCUMENT_MAX_BYTES:
        raise PublicAPIError(413, "body_too_large")
    if len(content) != manifest.size_bytes or hashlib.sha256(content).hexdigest() != manifest.sha256:
        raise ApplicationError("evidence_unavailable")
    document = _document(_application(request).show_document(source_id, source_version=manifest.source_version))
    async def chunks():
        for offset in range(0, len(content), 65536):
            yield content[offset:offset + 65536]
    return StreamingResponse(chunks(), media_type="application/octet-stream", headers={
        "Content-Length": str(len(content)), "ETag": f'"{manifest.sha256}"',
        "Content-Disposition": "attachment; filename=\"document\"; filename*=UTF-8''" + quote(document.filename, safe=""),
        "X-Content-Type-Options": "nosniff",
    })


@router.get("/v1/operations/{operation_id}", response_model=OperationResponse)
async def operation_status(
    operation_id: Annotated[str, Path(min_length=1, max_length=128)],
    request: Request,
) -> OperationResponse:
    value = _application(request).operation_status(operation_id)
    try:
        return OperationResponse.model_validate(value)
    except ValidationError:
        raise ApplicationError("invalid_provider_response") from None
