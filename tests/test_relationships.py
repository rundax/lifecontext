"""Relationship scanning and ledger decisions use disposable vaults only."""

from __future__ import annotations

import hashlib
import os
import pwd
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from lifecontext.config import ConfigError, JournalLimits, load_settings
from lifecontext.application import ApplicationError, JournalApplication
from lifecontext.hindsight_adapter import LifeContextHindsightAdapter
from lifecontext.paths import LifeContextPaths
from lifecontext.relationships import EDGE_TYPES, RelationshipError, RelationshipStore
from lifecontext.vault import ManagedVault
from local_paths import scratch_root


class RelationshipTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="lifeweave-relationships-test.", dir=scratch_root()
        )
        self.base = Path(self.temporary.name).resolve(strict=True)
        self.home = self.base / "home"
        self.home.mkdir(mode=0o700)
        self.home_patch = patch.dict(os.environ, {"HOME": str(self.home)})
        self.home_patch.start()
        self.paths = LifeContextPaths.current()
        self.paths.ensure_private_home()

        self.root = self.base / "vault"
        self.journal = self.root / "Journal"
        self.entries = self.journal / "entries"
        self.notes = self.root / "Notes"
        self.entries.mkdir(mode=0o700, parents=True)
        self.notes.mkdir(mode=0o700)
        self.root.chmod(0o700)
        self.journal.chmod(0o700)
        self.entries.chmod(0o700)
        self.notes.chmod(0o700)
        self.vault = ManagedVault(self.root, self.journal)
        self.limits = JournalLimits(
            max_result_count=20,
            max_relationship_files=100,
            max_relationship_file_bytes=16_384,
            max_relationship_bytes=65_536,
            max_relationship_scan_ms=2_000,
        )
        self.store = RelationshipStore(
            self.vault, allowlist_roots=("Notes",), limits=self.limits
        )

    def tearDown(self) -> None:
        self.vault.close()
        self.home_patch.stop()
        self.temporary.cleanup()

    def _write(self, path: Path, content: str) -> bytes:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        raw = content.encode("utf-8")
        path.write_bytes(raw)
        return raw

    def _journal_entry(self, source_id: str, body: str, *, version: int = 1) -> Path:
        content = (
            "---\nownership_marker: lifeweave-journal\n"
            f"source_id: {source_id}\nsource_version: {version}\n"
            f"source_checksum: {hashlib.sha256(body.encode()).hexdigest()}\n---\n\n{body}\n"
        )
        target = self.entries / f"{source_id}-v{version}.md"
        self._write(target, content)
        target.chmod(0o600)
        return target

    def _edge(self) -> dict[str, object]:
        self._journal_entry("src_relationship", "See [[plan-note#Architecture|Plan]].")
        note = self.notes / "Projects" / "plan-note.md"
        self._write(
            note,
            "---\nnote_id: plan-note\ntitle: Project plan\naliases: [Plan]\n---\n"
            "## Architecture\nSecret note body must not appear in review responses.\n",
        )
        result = self.store.scan_relationships()
        self.assertEqual(len(result["edges"]), 1)
        return result["edges"][0]

    def test_scanner_resolves_allowed_note_identity_and_writes_only_journal_ledger(self) -> None:
        entry = self._journal_entry(
            "src_planning",
            "See [[plan-note#Architecture|Plan]].\n\n````\n[[fake-note]]\n````",
        )
        note = self.notes / "Projects" / "plan-note.md"
        note_bytes = self._write(
            note,
            "---\nnote_id: plan-note\ntitle: Project plan\naliases: [Plan]\n---\n"
            "## Architecture\nPrivate note body that must stay out of responses.\n",
        )
        outside = self.base / "outside.md"
        self._write(outside, "outside content")
        (self.notes / "link.md").symlink_to(outside)
        self._write(self.notes / ".obsidian" / "workspace.md", "hidden")
        self._write(self.notes / "Projects" / "draft.md.tmp", "temporary")

        result = self.store.scan_relationships()

        self.assertFalse(result["truncated"])
        self.assertEqual(len(result["edges"]), 1)
        edge = result["edges"][0]
        self.assertEqual(edge["from"], "journal:src_planning")
        self.assertEqual(edge["to"], "note:plan-note")
        self.assertEqual(edge["status"], "proposed")
        self.assertEqual(edge["relation"], "references")
        self.assertNotIn("source_reference", edge)
        self.assertNotIn("target_reference", edge)
        self.assertNotIn("Private note body", repr(result))
        self.assertTrue(all("path" not in item for item in result["review"]))
        self.assertEqual(note.read_bytes(), note_bytes)
        self.assertEqual(entry.read_text(encoding="utf-8").count("fake-note"), 1)
        ledger_path = self.journal / "relationships" / f"{edge['edge_id']}.md"
        self.assertTrue(ledger_path.is_file())
        self.assertEqual(ledger_path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("[[", ledger_path.read_text())

    def test_missing_and_ambiguous_targets_are_content_free_review_items(self) -> None:
        self._journal_entry("src_review", "[[Shared title]] and [[personal sentence]].")
        self._write(self.notes / "one.md", "---\ntitle: Shared title\n---\nOne private body.")
        self._write(self.notes / "two.md", "---\ntitle: Shared title\n---\nAnother private body.")

        result = self.store.scan_relationships()

        self.assertEqual(result["edges"], [])
        self.assertEqual(
            {item["code"] for item in result["review"]},
            {"ambiguous_target", "missing_target"},
        )
        self.assertTrue(all(set(item) <= {"code", "source", "hint_sha256"} for item in result["review"]))
        self.assertNotIn("Shared title", repr(result))
        self.assertNotIn("personal sentence", repr(result))
        self.assertNotIn("private body", repr(result).casefold())

    def test_alias_resolution_uses_normalized_relative_path_identity(self) -> None:
        self._journal_entry("src_path_identity", "See [[B#Architecture]].")
        self._write(
            self.notes / "team" / "project.md",
            "---\ntitle: Project Beta\naliases: [B]\n---\n## Architecture\nPrivate text.\n",
        )

        result = self.store.scan_relationships()

        self.assertEqual(len(result["edges"]), 1)
        self.assertEqual(result["edges"][0]["to"], "path:Notes/team/project.md")
        self.assertEqual(
            EDGE_TYPES,
            {"references", "about", "same_topic", "follows", "supports", "contradicts", "supersedes"},
        )

    def test_allowlisted_note_folders_with_generated_journal_names_are_scanned(self) -> None:
        self._journal_entry("src_folder_names", "See [[ordinary-note]].")
        note = self.notes / "entries" / "ordinary.md"
        self._write(note, "---\ntitle: ordinary-note\n---\nPrivate ordinary note.\n")

        result = self.store.scan_relationships()

        self.assertEqual(len(result["edges"]), 1)
        self.assertEqual(result["edges"][0]["to"], "path:Notes/entries/ordinary.md")

    def test_latest_journal_version_is_the_only_indexed_source(self) -> None:
        self._journal_entry("src_versions", "See [[old-note]].")
        self._journal_entry("src_versions", "See [[new-note]].", version=2)
        self._journal_entry("src_versions", "See [[new-note]].", version=10)
        self._write(self.notes / "old.md", "---\nnote_id: old-note\n---\nOld target.\n")
        self._write(self.notes / "new.md", "---\nnote_id: new-note\n---\nCurrent target.\n")

        result = self.store.scan_relationships()

        self.assertEqual(len(result["edges"]), 1)
        self.assertEqual(result["edges"][0]["to"], "note:new-note")
        self.assertEqual(result["edges"][0]["source_revision"].split(":", 1)[0], "10")

    def test_link_count_ceiling_marks_scan_truncated(self) -> None:
        links = " ".join("[[one-note]]" for _ in range(140))
        self._journal_entry("src_link_limit", links)
        self._write(self.notes / "one.md", "---\nnote_id: one-note\n---\nTarget.\n")

        result = self.store.scan_relationships()

        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["edges"]), 1)

    def test_config_normalizes_allowlist_order(self) -> None:
        self.paths.config_path.write_text(
            yaml.safe_dump(
                {
                    "vault": {
                        "root": str(self.root),
                        "journal_path": str(self.journal),
                        "relationship_roots": ["Zeta", "Notes"],
                    }
                }
            ),
            encoding="utf-8",
        )
        self.paths.config_path.chmod(0o600)

        loaded = load_settings()

        self.assertEqual(loaded["vault"]["relationship_roots"], ["Notes", "Zeta"])

    def test_scan_enforces_file_and_total_byte_budgets(self) -> None:
        self._journal_entry("src_limit", "No links.")
        self._write(self.notes / "too-large.md", "x" * 32_000)
        limited = JournalLimits(
            max_result_count=5,
            max_relationship_files=100,
            max_relationship_file_bytes=16,
            max_relationship_bytes=32,
            max_relationship_scan_ms=2_000,
        )
        scanner = RelationshipStore(self.vault, allowlist_roots=("Notes",), limits=limited)

        result = scanner.scan_relationships()

        self.assertFalse(result["truncated"])
        self.assertLessEqual(result["scanned_files"], 100)
        self.assertIn("oversized_file", {item["code"] for item in result["review"]})
        self.assertTrue(all("path" not in item for item in result["review"]))

        aggregate_limited = JournalLimits(
            max_result_count=5,
            max_relationship_files=100,
            max_relationship_file_bytes=16_384,
            max_relationship_bytes=1,
            max_relationship_scan_ms=2_000,
        )
        aggregate_scanner = RelationshipStore(
            self.vault, allowlist_roots=("Notes",), limits=aggregate_limited
        )
        aggregate_result = aggregate_scanner.scan_relationships()
        self.assertTrue(aggregate_result["truncated"])
        self.assertLessEqual(aggregate_result["scanned_files"], 1)

    def test_scan_stops_when_the_time_budget_expires(self) -> None:
        limited = JournalLimits(
            max_result_count=5,
            max_relationship_files=100,
            max_relationship_file_bytes=16_384,
            max_relationship_bytes=65_536,
            max_relationship_scan_ms=1,
        )
        scanner = RelationshipStore(self.vault, allowlist_roots=("Notes",), limits=limited)
        with patch("lifecontext.relationships.time.monotonic", side_effect=[0.0, 0.01]):
            result = scanner.scan_relationships()
        self.assertTrue(result["truncated"])
        self.assertEqual(result["edges"], [])

    def test_decisions_are_revision_guarded_idempotent_and_audited(self) -> None:
        edge = self._edge()
        edge_id = edge["edge_id"]
        ledger_path = self.journal / "relationships" / f"{edge_id}.md"
        self.assertNotIn("[[", ledger_path.read_text(encoding="utf-8"))

        approved = self.store.decide(edge_id, "approve", 1, principal="local-user")
        self.assertIn("[[Journal/entries", ledger_path.read_text(encoding="utf-8"))
        repeated = self.store.decide(edge_id, "approve", 2, principal="local-user")
        self.assertEqual(approved["status"], "approved")
        self.assertEqual(approved["revision"], 2)
        self.assertEqual(repeated["revision"], 2)
        with self.assertRaises(RelationshipError) as raised:
            self.store.decide(edge_id, "reject", 1, principal="local-user")
        self.assertEqual(raised.exception.code, "conflict")

        rejected = self.store.decide(edge_id, "reject", 2, principal="local-user")
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(rejected["revision"], 3)
        ledger = ledger_path.read_text()
        self.assertNotIn("[[", ledger)
        self.assertIn("decision_principal: local-user", ledger)

    def test_changed_evidence_reopens_prior_decision_with_cas_revision(self) -> None:
        edge = self._edge()
        edge_id = edge["edge_id"]
        self.store.decide(edge_id, "approve", 1, principal="local-user")
        note = self.notes / "Projects" / "plan-note.md"
        note.write_text(
            "---\nnote_id: plan-note\ntitle: Project plan\naliases: [Plan]\n---\n"
            "## Architecture\nUpdated private note body.\n",
            encoding="utf-8",
        )

        result = self.store.scan_relationships()

        self.assertEqual(len(result["edges"]), 1)
        refreshed = result["edges"][0]
        self.assertEqual(refreshed["status"], "proposed")
        self.assertEqual(refreshed["revision"], 3)
        self.assertNotIn("decision_principal", refreshed)
        ledger = (self.journal / "relationships" / f"{edge_id}.md").read_text(encoding="utf-8")
        self.assertNotIn("[[", ledger)

    def test_application_decision_records_local_os_principal(self) -> None:
        self._journal_entry("src_local_decision", "See [[local-note]].")
        self._write(self.notes / "local.md", "---\nnote_id: local-note\n---\nLocal target.\n")
        self.paths.config_path.write_text(
            yaml.safe_dump(
                {
                    "vault": {
                        "root": str(self.root),
                        "journal_path": str(self.journal),
                        "relationship_roots": ["Notes"],
                    }
                }
            ),
            encoding="utf-8",
        )
        self.paths.config_path.chmod(0o600)
        app_vault = ManagedVault(self.root, self.journal)
        application = JournalApplication(
            paths=self.paths,
            hindsight=LifeContextHindsightAdapter(object(), bank_id="lifeweave"),
            vault=app_vault,
        )
        try:
            edge = application.scan_relationships(
                idempotency_key="local-principal-scan"
            )["edges"][0]
            with self.assertRaises(TypeError):
                application.decide_relationship(
                    edge["edge_id"],
                    "approve",
                    1,
                    principal="forged-user",
                    idempotency_key="local-principal-forged",
                )
            decision = application.decide_relationship(
                edge["edge_id"],
                "approve",
                1,
                idempotency_key="local-principal-decision",
            )
            self.assertEqual(
                decision["decision_principal"], pwd.getpwuid(os.geteuid()).pw_name
            )
            self.assertEqual(
                application.list_relationships(edge_id=edge["edge_id"])[0]["status"],
                "approved",
            )
            self.assertTrue(
                all("source_reference" not in item and "target_reference" not in item
                    for item in application.list_relationships())
            )
        finally:
            application.close()

    def test_application_relationship_operations_replay_after_reopen(self) -> None:
        self._journal_entry("src_replay", "See [[replay-note]].")
        self._write(
            self.notes / "replay.md",
            "---\nnote_id: replay-note\ntitle: Replay note\n---\nPrivate note body.\n",
        )
        self.paths.config_path.write_text(
            yaml.safe_dump(
                {
                    "vault": {
                        "root": str(self.root),
                        "journal_path": str(self.journal),
                        "relationship_roots": ["Notes"],
                    }
                }
            ),
            encoding="utf-8",
        )
        self.paths.config_path.chmod(0o600)

        def open_application() -> JournalApplication:
            return JournalApplication(
                paths=self.paths,
                hindsight=LifeContextHindsightAdapter(object(), bank_id="lifeweave"),
                vault=ManagedVault(self.root, self.journal),
            )

        application = open_application()
        try:
            scan_key = "relationship-application-scan"
            decision_key = "relationship-application-decision"
            with patch.object(
                application.relationships,
                "scan_relationships",
                wraps=application.relationships.scan_relationships,
            ) as scan_call:
                first_scan = application.scan_relationships(
                    source_id="src_replay", max_results=20, idempotency_key=scan_key
                )
                scan_call.assert_called_once_with(source_id="src_replay", max_results=20)
            edge = first_scan["edges"][0]
            with patch.object(
                application.relationships,
                "decide",
                wraps=application.relationships.decide,
            ) as decide_call:
                approved = application.decide_relationship(
                    edge["edge_id"], "approve", 1, idempotency_key=decision_key
                )
                replayed_approval = application.decide_relationship(
                    edge["edge_id"], "approve", 1, idempotency_key=decision_key
                )
                decide_call.assert_called_once()
            self.assertEqual(replayed_approval, approved)

            with sqlite3.connect(self.paths.catalog_path) as connection:
                operation_id = connection.execute(
                    "SELECT operation_id FROM operations WHERE operation_kind='relationship_scan'"
                ).fetchone()[0]
            status = application.operation_status(operation_id)
            self.assertEqual(status["summary"], {"item_count": 1})
            self.assertNotIn("edges", repr(status))
            self.assertNotIn("Private note body", repr(status))

            application.close()
            application = open_application()
            with patch.object(
                application.relationships,
                "scan_relationships",
                side_effect=AssertionError("replayed scan must not rescan the vault"),
            ), patch.object(
                application.relationships,
                "decide",
                side_effect=AssertionError("replayed decision must not rewrite the ledger"),
            ):
                self.assertEqual(
                    application.scan_relationships(
                        source_id="src_replay", max_results=20, idempotency_key=scan_key
                    ),
                    first_scan,
                )
                self.assertEqual(
                    application.decide_relationship(
                        edge["edge_id"], "approve", 1, idempotency_key=decision_key
                    ),
                    approved,
                )
                with self.assertRaises(ApplicationError) as changed_scan:
                    application.scan_relationships(
                        source_id="src_replay", max_results=19, idempotency_key=scan_key
                    )
                self.assertEqual(changed_scan.exception.code, "conflict")
                with self.assertRaises(ApplicationError) as changed_decision:
                    application.decide_relationship(
                        edge["edge_id"], "reject", 1, idempotency_key=decision_key
                    )
                self.assertEqual(changed_decision.exception.code, "conflict")
        finally:
            application.close()

    def test_config_rejects_traversal_and_overlapping_relationship_roots(self) -> None:
        cases = (["../outside"], ["Notes", "Notes/Private"], ["Journal/entries"])
        for roots in cases:
            with self.subTest(roots=roots):
                self.paths.config_path.write_text(
                    yaml.safe_dump(
                        {
                            "vault": {
                                "root": str(self.root),
                                "journal_path": str(self.journal),
                                "relationship_roots": roots,
                            }
                        }
                    ),
                    encoding="utf-8",
                )
                self.paths.config_path.chmod(0o600)
                with self.assertRaises(ConfigError):
                    load_settings()


if __name__ == "__main__":
    unittest.main()
