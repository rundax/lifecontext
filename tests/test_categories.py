"""Category lifecycle against disposable v6 catalog and original custody."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from local_paths import scratch_root

from lifecontext.catalog import CatalogConflict, CatalogError, LifeContextCatalog
from lifecontext.categories import CategoryClassification, CategoryService
from lifecontext.custody import LifeContextCustody
from lifecontext.paths import LifeContextPaths


class CategoryServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=scratch_root())
        self.home_patch = patch.dict(os.environ, {"HOME": self.temporary.name})
        self.home_patch.start()
        self.paths = LifeContextPaths.current()
        self.custody = LifeContextCustody(self.paths)
        self.catalog = LifeContextCatalog(self.paths)
        self.service = CategoryService(self.catalog)
        self.documents = []
        for source_id in ("src_category_one", "src_category_two"):
            manifest = self.custody.write_version(
                source_id,
                1,
                b"original category evidence",
                provenance={"source": "test"},
            )
            self.catalog.register_manifest(manifest, layer="document")
            self.catalog.register_document_metadata(
                source_id,
                1,
                filename="example.txt",
                title="Example",
                mime_type="text/plain",
            )
            self.documents.append({"source_id": source_id, "source_version": 1})

    def tearDown(self) -> None:
        self.catalog.close()
        self.home_patch.stop()
        self.temporary.cleanup()

    def propose(self, name: str = "Health", **kwargs):
        classification = CategoryClassification(
            name,
            "Broad life topic",
            "These documents concern this broad topic",
            0.95,
            True,
            **kwargs,
        )
        return self.service.propose(classification, documents=[self.documents[0]])[
            "proposal"
        ]

    def approve(self, name: str = "Health", category_id: str = "health"):
        proposal = self.propose(name)
        self.service.approve(
            proposal["proposal_id"],
            expected_revision=proposal["revision"],
            category_id=category_id,
        )
        return next(
            category
            for category in self.service.list()
            if category["category_id"] == category_id
        )

    def test_only_inbox_bootstrapped_and_validated_classification_required(self):
        self.assertEqual(
            [category["category_id"] for category in self.service.list()], ["inbox"]
        )
        with self.assertRaises(CatalogError):
            self.service.propose({"name": "Health"}, documents=self.documents)
        with self.assertRaises(CatalogError):
            CategoryClassification("Health", "", "reason", float("nan"), True)

    def test_low_confidence_and_narrow_tags_stay_inbox(self):
        for confidence, broad in ((0.2, True), (0.99, False)):
            result = self.service.propose(
                CategoryClassification(
                    "Vaccination", "", "reason", confidence, broad, ("vaccination",)
                ),
                documents=self.documents,
            )
            self.assertEqual(result["status"], "inbox")
            self.assertEqual(result["tags"], ["vaccination"])
        self.assertEqual(self.service.list_proposals(), [])

    def test_reuse_name_alias_and_pending_dedup(self):
        first = self.propose()
        second = self.service.propose(
            CategoryClassification(
                " HEALTH ", "different", "different reason", 0.9, True
            ),
            documents=[self.documents[1]],
        )["proposal"]
        self.assertEqual(first["proposal_id"], second["proposal_id"])
        self.assertEqual(len(second["documents"]), 2)
        self.assertEqual(second["rationale"], first["rationale"])
        self.assertEqual(len(self.service.list()), 1)
        self.assertEqual(second["revision"], first["revision"] + 1)
        with self.assertRaises(CatalogConflict):
            self.service.approve(
                first["proposal_id"],
                expected_revision=first["revision"],
                category_id="health",
            )
        replay = self.service.propose(
            CategoryClassification("Health", "", "reason", 0.9, True),
            documents=self.documents,
        )["proposal"]
        self.assertEqual(replay["revision"], second["revision"])
        self.service.approve(
            second["proposal_id"],
            expected_revision=second["revision"],
            category_id="health",
        )
        category = self.service.alias("health", "Wellbeing", expected_revision=1)
        self.assertEqual(category["aliases"], ["Wellbeing"])
        for name in (" health ", " WELLBEING "):
            result = self.service.propose(
                CategoryClassification(name, "", "reason", 0.9, True),
                documents=self.documents,
            )
            self.assertEqual(
                (result["status"], result["category_id"]), ("reuse", "health")
            )

    def test_rejection_redirect_and_new_proposal_after_rejection(self):
        proposal = self.propose()
        rejected = self.service.reject(
            proposal["proposal_id"], expected_revision=proposal["revision"]
        )
        self.assertEqual(rejected["state"], "rejected")
        replacement = self.propose()
        self.assertNotEqual(replacement["proposal_id"], proposal["proposal_id"])
        redirected = self.service.redirect(
            replacement["proposal_id"],
            "inbox",
            expected_revision=replacement["revision"],
        )
        self.assertEqual(redirected["state"], "redirected")
        self.assertEqual(len(self.service.list()), 1)

    def test_soft_budget_requires_explicit_override(self):
        self.service = CategoryService(self.catalog, category_budget=1)
        self.approve()
        proposal = self.propose("Finance")
        with self.assertRaises(CatalogConflict):
            self.service.approve(
                proposal["proposal_id"],
                expected_revision=proposal["revision"],
                category_id="finance",
            )
        result = self.service.approve(
            proposal["proposal_id"],
            expected_revision=proposal["revision"],
            category_id="finance",
            budget_override=True,
        )
        self.assertEqual(result["state"], "approved")

    def test_rename_reassign_and_merge_preserve_archive(self):
        health = self.approve()
        finance = self.approve("Finance", "finance")
        renamed = self.service.rename(
            "health", "Wellness", expected_revision=health["revision"]
        )
        self.assertEqual(renamed["category_id"], "health")
        document = self.catalog.get_document_metadata(**self.documents[0])
        indexing_before = self.catalog.get_document_stage(
            **self.documents[0], stage="indexing"
        )
        projection_before = self.catalog.get_document_stage(
            **self.documents[0], stage="projection"
        )
        reassigned = self.service.reassign(
            document["source_id"],
            "finance",
            expected_revision=document["category_revision"],
        )
        self.assertEqual(reassigned["category_id"], "finance")
        self.service.merge(
            "finance",
            "health",
            expected_source_revision=finance["revision"],
            expected_target_revision=renamed["revision"],
        )
        self.assertEqual(
            self.catalog.get_document_metadata(**self.documents[0])["category_id"],
            "health",
        )
        self.assertNotIn(
            "finance", [category["category_id"] for category in self.service.list()]
        )
        self.assertEqual(
            self.catalog.get_document_stage(**self.documents[0], stage="indexing"),
            indexing_before,
        )
        projection_after = self.catalog.get_document_stage(
            **self.documents[0], stage="projection"
        )
        self.assertEqual(projection_after["state"], "pending")
        self.assertGreater(projection_after["revision"], projection_before["revision"])
        for document in self.documents:
            evidence = self.custody.resolve_exact(**document)
            self.assertEqual(evidence.original_bytes, b"original category evidence")

    def test_transactional_normalized_name_alias_collision(self):
        health = self.approve()
        health = self.service.alias(
            "health", "Personal   Wellbeing", expected_revision=health["revision"]
        )
        finance = self.approve("Finance", "finance")
        categories_before = self.service.list()
        for name in (" PERSONAL wellbeing ", "  HEALTH "):
            with self.assertRaises(CatalogConflict):
                self.service.rename(
                    "finance", name, expected_revision=finance["revision"]
                )
            self.assertEqual(self.service.list(), categories_before)
        # Exercise the catalog boundary as well: service checks are not enough
        # to protect concurrent callers or an approval racing an alias update.
        with self.assertRaises(CatalogConflict):
            self.catalog.update_category(
                "finance",
                expected_revision=finance["revision"],
                name="Finance",
                description=finance["description"],
                aliases=["Personal wellbeing"],
            )
        proposal = self.catalog.record_category_proposal(
            "proposal_alias_collision",
            name=" personal  WELLBEING ",
            description="Broad topic",
            rationale="Supplied classifier proposal",
            documents=[self.documents[1]],
        )
        with self.assertRaises(CatalogConflict):
            self.service.approve(
                proposal["proposal_id"],
                expected_revision=proposal["revision"],
                category_id="duplicate_wellbeing",
            )
        self.assertEqual(self.service.list(), categories_before)
        self.assertEqual(
            self.catalog.get_category_proposal(proposal["proposal_id"])["state"],
            "pending",
        )

    def test_stale_mutation_and_alias_collision_rejected(self):
        health = self.approve()
        self.approve("Finance", "finance")
        with self.assertRaises(CatalogConflict):
            self.service.alias(
                "health", "Finance", expected_revision=health["revision"]
            )
        self.service.rename("health", "Wellness", expected_revision=health["revision"])
        with self.assertRaises(CatalogConflict):
            self.service.rename("health", "Other", expected_revision=health["revision"])
