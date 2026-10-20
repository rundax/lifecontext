"""Approval-only category lifecycle over the transactional catalog.

Classifier output is supplied by the caller; this service neither calls a model
nor invents a taxonomy. Operation/idempotency orchestration belongs to the
application's validated durable operation envelope.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import Any

from lifecontext.catalog import CatalogConflict, CatalogError, LifeContextCatalog


def _normalize(value: str) -> str:
    return " ".join(value.split()).casefold()


@dataclass(frozen=True)
class CategoryClassification:
    """Validated classifier result: broad category versus narrow tags."""

    name: str
    description: str
    rationale: str
    confidence: float
    broad: bool
    tags: tuple[str, ...] = ()
    closest_category_id: str | None = None

    def __post_init__(self) -> None:
        for value, maximum, required in (
            (self.name, 256, True),
            (self.description, 4096, False),
            (self.rationale, 4096, True),
        ):
            if (
                not isinstance(value, str)
                or len(value) > maximum
                or (required and not value.strip())
                or "\x00" in value
            ):
                raise CatalogError("invalid category classification text")
        if (
            isinstance(self.confidence, bool)
            or not isinstance(self.confidence, (int, float))
            or not math.isfinite(self.confidence)
            or not 0 <= self.confidence <= 1
        ):
            raise CatalogError("invalid category confidence")
        if (
            not isinstance(self.broad, bool)
            or not isinstance(self.tags, tuple)
            or len(self.tags) > 100
            or any(
                not isinstance(tag, str)
                or not tag.strip()
                or len(tag) > 256
                or "\x00" in tag
                for tag in self.tags
            )
        ):
            raise CatalogError("invalid category classification tags")
        if self.closest_category_id is not None and (
            not isinstance(self.closest_category_id, str)
            or not self.closest_category_id
        ):
            raise CatalogError("invalid closest category")


class CategoryService:
    def __init__(
        self,
        catalog: LifeContextCatalog,
        *,
        category_budget: int = 10,
        confidence_threshold: float = 0.8,
    ) -> None:
        if (
            isinstance(category_budget, bool)
            or not isinstance(category_budget, int)
            or category_budget < 1
        ):
            raise CatalogError("invalid category budget")
        if (
            isinstance(confidence_threshold, bool)
            or not isinstance(confidence_threshold, (int, float))
            or not math.isfinite(confidence_threshold)
            or not 0 <= confidence_threshold <= 1
        ):
            raise CatalogError("invalid category confidence threshold")
        self.catalog = catalog
        self.category_budget = category_budget
        self.confidence_threshold = confidence_threshold

    def list(self) -> list[dict[str, Any]]:
        return self.catalog.list_categories()

    def list_proposals(self, *, state: str = "pending") -> list[dict[str, Any]]:
        return self.catalog.list_category_proposals(state=state)

    def _category(self, category_id: str) -> dict[str, Any]:
        for category in self.list():
            if category["category_id"] == category_id:
                return category
        raise CatalogError("category target unavailable")

    def propose(
        self, classification: CategoryClassification, *, documents: list[dict[str, Any]]
    ) -> dict[str, Any]:
        if not isinstance(classification, CategoryClassification):
            raise CatalogError("validated category classification required")
        if not isinstance(documents, list) or not 1 <= len(documents) <= 100:
            raise CatalogError("invalid proposal documents")
        for document in documents:
            if not isinstance(document, dict) or set(document) != {
                "source_id",
                "source_version",
            }:
                raise CatalogError("invalid proposal document")
            if (
                self.catalog.get_document_metadata(
                    document["source_id"], document["source_version"]
                )
                is None
            ):
                raise CatalogError("document source required")
        categories = self.list()
        key = _normalize(classification.name)
        matches = [
            category
            for category in categories
            if key
            in {
                _normalize(category["name"]),
                *(_normalize(alias) for alias in category["aliases"]),
            }
        ]
        if len(matches) > 1:
            raise CatalogConflict("ambiguous category alias")
        # Existing names and aliases are considered before proposing additions;
        # uncertainty never silently reclassifies evidence.
        if (
            classification.confidence < self.confidence_threshold
            or not classification.broad
        ):
            return {
                "status": "inbox",
                "category_id": "inbox",
                "tags": list(classification.tags),
            }
        if matches:
            return {
                "status": "reuse",
                "category_id": matches[0]["category_id"],
                "tags": list(classification.tags),
            }
        if classification.closest_category_id is not None:
            self._category(classification.closest_category_id)
        pending = next(
            (
                proposal
                for proposal in self.list_proposals()
                if _normalize(proposal["name"]) == key
            ),
            None,
        )
        # Include the previous decision count so a new proposal after rejection
        # receives a fresh stable identity instead of reviving a decided record.
        decided = sum(
            _normalize(proposal["name"]) == key
            for state in ("approved", "rejected", "redirected")
            for proposal in self.list_proposals(state=state)
        )
        proposal_id = (
            pending["proposal_id"]
            if pending
            else "proposal_"
            + hashlib.sha256(f"{key}\0{decided}".encode()).hexdigest()[:32]
        )
        proposal = self.catalog.record_category_proposal(
            proposal_id,
            name=pending["name"] if pending else " ".join(classification.name.split()),
            description=pending["description"]
            if pending
            else classification.description,
            rationale=pending["rationale"] if pending else classification.rationale,
            documents=documents,
            closest_category_id=pending["closest_category_id"]
            if pending
            else classification.closest_category_id,
        )
        return {
            "status": "pending",
            "category_id": "inbox",
            "proposal": proposal,
            "tags": list(classification.tags),
            "budget_exceeded": len(
                [
                    category
                    for category in categories
                    if category["category_id"] != "inbox"
                ]
            )
            >= self.category_budget,
            "reuse_candidates": categories,
        }

    def approve(
        self,
        proposal_id: str,
        *,
        expected_revision: int,
        category_id: str,
        budget_override: bool = False,
    ) -> dict[str, Any]:
        return self.catalog.decide_category_proposal(
            proposal_id,
            expected_revision=expected_revision,
            decision="approve",
            category_id=category_id,
            category_budget=self.category_budget,
            budget_override=budget_override,
        )

    def reject(self, proposal_id: str, *, expected_revision: int) -> dict[str, Any]:
        return self.catalog.decide_category_proposal(
            proposal_id,
            expected_revision=expected_revision,
            decision="reject",
            category_budget=self.category_budget,
        )

    def redirect(
        self, proposal_id: str, category_id: str, *, expected_revision: int
    ) -> dict[str, Any]:
        return self.catalog.decide_category_proposal(
            proposal_id,
            expected_revision=expected_revision,
            decision="redirect",
            category_id=category_id,
            category_budget=self.category_budget,
        )

    def rename(
        self,
        category_id: str,
        name: str,
        *,
        expected_revision: int,
        description: str | None = None,
    ) -> dict[str, Any]:
        category = self._category(category_id)
        return self.catalog.update_category(
            category_id,
            expected_revision=expected_revision,
            name=name,
            description=category["description"] if description is None else description,
            aliases=category["aliases"],
        )

    def alias(
        self, category_id: str, alias: str, *, expected_revision: int
    ) -> dict[str, Any]:
        category = self._category(category_id)
        if not isinstance(alias, str) or not alias.strip():
            raise CatalogError("invalid category alias")
        key = _normalize(alias)
        if any(
            other["category_id"] != category_id
            and key
            in {
                _normalize(other["name"]),
                *(_normalize(value) for value in other["aliases"]),
            }
            for other in self.list()
        ):
            raise CatalogConflict("category alias already in use")
        aliases = category["aliases"][:]
        if key not in {_normalize(value) for value in aliases}:
            aliases.append(" ".join(alias.split()))
        return self.catalog.update_category(
            category_id,
            expected_revision=expected_revision,
            name=category["name"],
            description=category["description"],
            aliases=aliases,
        )

    def reassign(
        self, source_id: str, category_id: str, *, expected_revision: int
    ) -> dict[str, Any]:
        return self.catalog.assign_document_category(
            source_id, category_id, expected_revision=expected_revision
        )

    def merge(
        self,
        source_category_id: str,
        target_category_id: str,
        *,
        expected_source_revision: int,
        expected_target_revision: int,
    ) -> dict[str, Any]:
        return self.catalog.merge_categories(
            source_category_id,
            target_category_id,
            expected_source_revision=expected_source_revision,
            expected_target_revision=expected_target_revision,
        )
