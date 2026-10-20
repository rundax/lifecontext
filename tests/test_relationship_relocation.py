"""Projection relocation preserves decisions and fails closed."""

import hashlib
from dataclasses import replace
import unittest
from unittest.mock import patch

from lifecontext.relationships import RelationshipError
from lifecontext.vault import VaultError
import test_relationships as fixtures


class RelationshipRelocationTests(unittest.TestCase):
    setUp = fixtures.RelationshipTests.setUp
    tearDown = fixtures.RelationshipTests.tearDown
    _write = fixtures.RelationshipTests._write
    _journal_entry = fixtures.RelationshipTests._journal_entry
    _edge = fixtures.RelationshipTests._edge

    def test_relocation_preserves_fields_decisions_and_retries(self):
        for decision in ("approve", "reject"):
            with self.subTest(decision=decision):
                edge = self._edge()
                current = self.store._read_ledger(edge["edge_id"])
                self.store.decide(edge["edge_id"], decision, current["revision"], principal="local")
                current = self.store._read_ledger(edge["edge_id"])
                current["optional_metadata"] = {"reason": "keep"}
                ledger = self.journal / "relationships" / (edge["edge_id"] + ".md")
                self._write(ledger, self.store._ledger_text(current))
                raw = ledger.read_bytes()
                with patch.object(self.vault, "safe_write_relationship_text", wraps=self.vault.safe_write_relationship_text) as write:
                    self.store.relocate_journal_projection("src_relationship", "entries/src_relationship-v1.md", "entries/moved.md")
                    self.assertEqual(write.call_args.kwargs, {
                        "expected_hash": hashlib.sha256(raw).hexdigest(),
                        "expected_revision": current["revision"],
                    })
                refreshed = self.store._read_ledger(edge["edge_id"])
                for key in current.keys() - {"revision", "source_reference", "rendered_hash"}:
                    self.assertEqual(refreshed[key], current[key])
                self.assertEqual(refreshed["source_reference"], "Journal/entries/moved.md")
                self.assertEqual(refreshed["revision"], current["revision"] + 1)
                after = ledger.read_bytes()
                self.store.relocate_journal_projection("src_relationship", "entries/src_relationship-v1.md", "entries/moved.md")
                self.assertEqual(ledger.read_bytes(), after)

    def test_incoming_reference_and_newer_reference_are_not_downgraded(self):
        self._journal_entry("src_target", "No outgoing links.")
        self._write(self.notes / "incoming.md", "---\nnote_id: incoming\n---\n[[src_target]]\n")
        edge = self.store.scan_relationships()["edges"][0]
        self.store.relocate_journal_projection("src_target", "entries/src_target-v1.md", "entries/new.md")
        fields = self.store._read_ledger(edge["edge_id"])
        self.assertEqual(fields["target_reference"], "Journal/entries/new.md")
        self.store.relocate_journal_projection("src_target", "entries/src_target-v1.md", "entries/older.md")
        self.assertEqual(self.store._read_ledger(edge["edge_id"]), fields)
        self.store.relocate_journal_projection("other", "entries/new.md", "entries/other.md")
        self.assertEqual(self.store._read_ledger(edge["edge_id"]), fields)

    def test_scan_reference_only_refresh_preserves_decisions(self):
        for decision in ("approve", "reject"):
            with self.subTest(decision=decision):
                edge = self._edge()
                self.store.decide(edge["edge_id"], decision, edge["revision"], principal="local")
                entry = self.entries / "src_relationship-v1.md"
                moved = self.entries / "moved.md"
                entry.rename(moved)
                result = self.store.scan_relationships()
                refreshed = result["edges"][0]
                self.assertEqual(refreshed["status"], "approved" if decision == "approve" else "rejected")
                self.assertEqual(refreshed["decision_principal"], "local")
                self.assertEqual(refreshed["revision"], edge["revision"] + 2)
                moved.rename(entry)

    def test_invalid_ledger_is_not_skipped_and_prevents_writes(self):
        edge = self._edge()
        invalid = self.journal / "relationships" / ("edge_" + "0" * 64 + ".md")
        self._write(invalid, "not a ledger")
        with patch.object(self.vault, "safe_write_relationship_text") as write:
            with self.assertRaises(RelationshipError):
                self.store.relocate_journal_projection("src_relationship", "entries/src_relationship-v1.md", "entries/moved.md")
            write.assert_not_called()
        self.assertEqual(self.store._read_ledger(edge["edge_id"])["revision"], 1)

    def test_conflict_is_reported(self):
        self._edge()
        with patch.object(self.vault, "safe_write_relationship_text", side_effect=VaultError("conflict")):
            with self.assertRaises(RelationshipError) as raised:
                self.store.relocate_journal_projection("src_relationship", "entries/src_relationship-v1.md", "entries/moved.md")
        self.assertEqual(raised.exception.code, "conflict")

    def test_sync_conflict_is_reported_even_on_retry(self):
        edge = self._edge()
        self._write(self.journal / "relationships" / (edge["edge_id"] + ".md.sync-conflict-copy"), "conflict")
        with self.assertRaises(RelationshipError) as raised:
            self.store.relocate_journal_projection("src_relationship", "entries/missing.md", "entries/moved.md")
        self.assertEqual(raised.exception.code, "sync_conflict")

    def test_invalid_paths_and_source_are_rejected(self):
        for path in ("../outside.md", "/entries/a.md", "entries/../a.md", "entries/a\\b.md", "entries/a.md\x00"):
            with self.subTest(path=path), self.assertRaises(RelationshipError):
                self.store.relocate_journal_projection("src", path, "entries/new.md")
        with self.assertRaises(RelationshipError):
            self.store.relocate_journal_projection(None, "entries/a.md", "entries/b.md")

    def test_missing_directory_is_noop(self):
        self.store.relocate_journal_projection("src", "entries/a.md", "entries/b.md")

    def test_legacy_audit_counts_files_with_missing_literal_targets_only(self):
        self._journal_entry("existing", "Unmigrated.")
        contents = {
            "missing.md": "[old](Journal/entries/abc123-v2.md) [[Journal/entries/abc123-v2#Heading|Old]]",
            "rooted.md": "[[/Journal/entries/src_old-v10.md]]",
            "reference.md": "[old][entry]\n\n[entry]: /Journal/entries/abc123-v2.md",
            "existing.md": "[[Journal/entries/existing-v1]]",
            "bare.md": "[[abc123-v2]] [relative](abc123-v2.md) [web](https://example.com/Journal/entries/abc123-v2.md)",
            "modern.md": "[[Journal/entries/Readable title]]",
            "code.md": "```\n[[Journal/entries/abc123-v2]]\n```",
        }
        for name, body in contents.items():
            self._write(self.notes / name, body)
        self._write(self.root / "Outside" / "outside.md", "[[Journal/entries/missing-v1]]")
        before = {path: path.read_bytes() for path in self.root.rglob("*.md")}
        with patch.object(self.vault, "safe_write_relationship_text", side_effect=AssertionError("read only")):
            self.assertEqual(self.store.count_legacy_path_references(), 3)
        self.assertEqual(before, {path: path.read_bytes() for path in self.root.rglob("*.md")})

    def test_legacy_audit_returns_incomplete_for_limits(self):
        self._write(self.notes / "one.md", "[[Journal/entries/missing-v1]]")
        self._write(self.notes / "two.md", "No links.")
        original = self.store.limits
        for options in (
            {"max_relationship_files": 1},
            {"max_relationship_file_bytes": 1},
            {"max_relationship_bytes": 1},
        ):
            with self.subTest(options=options):
                self.store.limits = replace(original, **options)
                self.assertEqual(self.store.count_legacy_path_references(), -1)
        self.store.limits = original
        with patch("lifecontext.relationships.time.monotonic", side_effect=[0, 10]):
            self.assertEqual(self.store.count_legacy_path_references(), -1)
        with patch.object(self.vault, "list_vault_directory", return_value=([], True)):
            self.assertEqual(self.store.count_legacy_path_references(), -1)
        with patch.object(self.vault, "read_vault_file", side_effect=VaultError("unsafe_path")):
            self.assertEqual(self.store.count_legacy_path_references(), -1)

    def test_legacy_audit_directory_limit_and_zero_case(self):
        self.assertEqual(self.store.count_legacy_path_references(), 0)
        (self.notes / "nested").mkdir()
        self.store.limits = replace(self.store.limits, max_relationship_files=1)
        self.assertEqual(self.store.count_legacy_path_references(), -1)
        self.store.allowlist_roots = ()
        self.assertEqual(self.store.count_legacy_path_references(), 0)

    def test_old_id_alias_resolves_renamed_journal_projection(self):
        entry = self._journal_entry("src_alias", "Journal body.")
        content = entry.read_text().replace("source_id: src_alias\n", "source_id: src_alias\naliases: [src_alias-v1]\n")
        entry.unlink()
        self._write(self.entries / "Readable title.md", content)
        (self.entries / "Readable title.md").chmod(0o600)
        self._write(self.notes / "incoming.md", "[[src_alias-v1]]")
        result = self.store.scan_relationships()
        self.assertEqual(len(result["edges"]), 1)
        self.assertEqual(result["edges"][0]["to"], "journal:src_alias")
        self.assertEqual(self.store.count_legacy_path_references(), 0)

    def test_migration_headers_and_legacy_evidence_preserve_decisions(self):
        for decision in ("approve", "reject"):
            with self.subTest(decision=decision):
                edge = self._edge()
                entry = self.entries / "src_relationship-v1.md"
                old_raw = entry.read_bytes()
                current = self.store._read_ledger(edge["edge_id"])
                current["evidence_id"] = "sha256:" + hashlib.sha256(
                    (hashlib.sha256(old_raw).hexdigest() + "\0plan-note").encode()
                ).hexdigest()
                ledger = self.journal / "relationships" / (edge["edge_id"] + ".md")
                self._write(ledger, self.store._ledger_text(current))
                self.store.decide(edge["edge_id"], decision, current["revision"], principal="local")
                moved = self.entries / "Readable title.md"
                moved.write_text(old_raw.decode().replace(
                    "source_id: src_relationship\n",
                    "source_id: src_relationship\ntitle: Readable title\naliases: [src_relationship-v1]\n",
                ))
                moved.chmod(0o600)
                self.store.relocate_journal_projection(
                    "src_relationship", "entries/src_relationship-v1.md", "entries/Readable title.md",
                    original_projection=old_raw, migrated_projection=moved.read_bytes(),
                )
                entry.unlink()
                relocated = ledger.read_bytes()
                self.store.relocate_journal_projection(
                    "src_relationship", "entries/src_relationship-v1.md", "entries/Readable title.md",
                    original_projection=old_raw, migrated_projection=moved.read_bytes(),
                )
                self.assertEqual(ledger.read_bytes(), relocated)
                result = self.store.scan_relationships()
                refreshed = self.store._read_ledger(edge["edge_id"])
                self.assertEqual(result["review"], [])
                self.assertEqual(refreshed["status"], "approved" if decision == "approve" else "rejected")
                self.assertEqual(refreshed["decision_principal"], "local")
                self.assertEqual(refreshed["source_revision"], current["source_revision"])
                self.assertNotEqual(refreshed["evidence_id"], current["evidence_id"])
                self.store.scan_relationships()
                self.assertEqual(self.store._read_ledger(edge["edge_id"]), refreshed)
                moved.unlink()

    def test_same_checksum_body_edit_reopens_decision(self):
        edge = self._edge()
        self.store.decide(edge["edge_id"], "approve", edge["revision"], principal="local")
        entry = self.entries / "src_relationship-v1.md"
        entry.write_text(entry.read_text() + "Changed body without changing checksum.\n")
        refreshed = self.store.scan_relationships()["edges"][0]
        self.assertEqual(refreshed["source_revision"], edge["source_revision"])
        self.assertEqual(refreshed["status"], "proposed")
        self.assertNotIn("decision_principal", refreshed)

    def test_unverified_metadata_edit_reopens_decision(self):
        edge = self._edge()
        self.store.decide(edge["edge_id"], "reject", edge["revision"], principal="local")
        entry = self.entries / "src_relationship-v1.md"
        entry.write_text(entry.read_text().replace("source_id:", "title: New title\nsource_id:"))
        self.assertEqual(self.store.scan_relationships()["edges"][0]["status"], "proposed")

    def test_invalid_migration_bytes_fail_before_ledger_writes(self):
        self._edge()
        entry = self.entries / "src_relationship-v1.md"
        original = entry.read_bytes()
        checksum = hashlib.sha256(b"See [[plan-note#Architecture|Plan]].").hexdigest().encode()
        for migrated in (
            original + b"Changed body.\n",
            original.replace(b"source_id: src_relationship", b"source_id: other"),
            original.replace(b"source_version: 1", b"source_version: 2"),
            original.replace(checksum, b"a" * 64),
            None,
        ):
            with self.subTest(migrated=migrated), patch.object(self.vault, "safe_write_relationship_text") as write:
                with self.assertRaises(RelationshipError):
                    self.store.relocate_journal_projection(
                        "src_relationship", "entries/src_relationship-v1.md", "entries/new.md",
                        original_projection=original, migrated_projection=migrated,
                    )
                write.assert_not_called()

    def test_unknown_evidence_is_not_remapped(self):
        edge = self._edge()
        self.store.decide(edge["edge_id"], "approve", edge["revision"], principal="local")
        current = self.store._read_ledger(edge["edge_id"])
        current["evidence_id"] = "sha256:" + "0" * 64
        ledger = self.journal / "relationships" / (edge["edge_id"] + ".md")
        self._write(ledger, self.store._ledger_text(current))
        original = (self.entries / "src_relationship-v1.md").read_bytes()
        migrated = original.replace(b"source_id:", b"title: New title\nsource_id:")
        self.store.relocate_journal_projection(
            "src_relationship", "entries/src_relationship-v1.md", "entries/new.md",
            original_projection=original, migrated_projection=migrated,
        )
        self.assertEqual(self.store._read_ledger(edge["edge_id"])["evidence_id"], current["evidence_id"])

    def test_changed_journal_checksum_reopens_decision(self):
        edge = self._edge()
        self.store.decide(edge["edge_id"], "approve", edge["revision"], principal="local")
        self._journal_entry("src_relationship", "Updated evidence: [[plan-note#Architecture|Plan]].")
        refreshed = self.store.scan_relationships()["edges"][0]
        self.assertEqual(refreshed["status"], "proposed")
        self.assertNotIn("decision_principal", refreshed)

    def test_legacy_journal_checksums_use_raw_digest_and_original_revision(self):
        entry = self._journal_entry("src_invalid", "No links.")
        raw = entry.read_bytes()
        checksum = hashlib.sha256(b"No links.").hexdigest()
        for invalid in ("not-a-hash", "''", "a" * 63, "123", "null", None):
            with self.subTest(checksum=invalid):
                modified = (
                    raw.replace(checksum.encode(), invalid.encode()) if invalid is not None
                    else raw.replace(b"source_checksum: " + checksum.encode() + b"\n", b"")
                )
                document = self.store._parse_document(
                    modified, "Journal/entries/src_invalid-v1.md", journal=True,
                )
                expected = {"''": "", "null": "None", None: ""}.get(invalid, invalid)
                self.assertEqual(document.revision, "1:" + expected)
                self.assertEqual(document.digest, hashlib.sha256(modified).hexdigest())

    def test_changed_checksumless_journal_evidence_reopens_decision(self):
        edge = self._edge()
        entry = self.entries / "src_relationship-v1.md"
        lines = entry.read_text().splitlines(keepends=True)
        entry.write_text("".join(line for line in lines if not line.startswith("source_checksum:")))
        current = self.store.scan_relationships()["edges"][0]
        self.store.decide(edge["edge_id"], "approve", current["revision"], principal="local")
        entry.write_text(entry.read_text() + "Changed legacy evidence.\n")
        refreshed = self.store.scan_relationships()["edges"][0]
        self.assertEqual(refreshed["status"], "proposed")
        self.assertNotIn("decision_principal", refreshed)

    def test_changed_incoming_user_note_reopens_decision(self):
        self._journal_entry("src_target", "Journal body.")
        note = self.notes / "incoming.md"
        self._write(note, "[[src_target]]\nOriginal evidence.\n")
        edge = self.store.scan_relationships()["edges"][0]
        self.store.decide(edge["edge_id"], "reject", edge["revision"], principal="local")
        self._write(note, "[[src_target]]\nChanged evidence.\n")
        refreshed = self.store.scan_relationships()["edges"][0]
        self.assertEqual(refreshed["status"], "proposed")
        self.assertNotIn("decision_principal", refreshed)

    def test_long_readable_projection_reference_can_be_written_and_decided(self):
        original = self._edge()
        (self.journal / "relationships" / (original["edge_id"] + ".md")).unlink()
        entry = self.entries / "src_relationship-v1.md"
        name = "source text must stay out of argv and default output. See plan-note.md"
        entry.rename(self.entries / name)
        result = self.store.scan_relationships()
        self.assertEqual(result["review"], [])
        edge = result["edges"][0]
        fields = self.store._read_ledger(edge["edge_id"])
        self.assertEqual(fields["source_reference"], "Journal/entries/" + name)
        self.store.decide(edge["edge_id"], "approve", edge["revision"], principal="local")
        self.assertEqual(self.store.scan_relationships()["edges"][0]["status"], "approved")

    def test_approved_relocation_to_long_unicode_readable_path(self):
        edge = self._edge()
        self.store.decide(edge["edge_id"], "approve", edge["revision"], principal="local")
        name = "Щоденник " + "план " * 17 + "end.md"
        self.assertGreater(len(name.encode()), 170)
        self.assertLessEqual(len(name.encode()), 180)
        entry = self.entries / "src_relationship-v1.md"
        entry.rename(self.entries / name)
        self.store.relocate_journal_projection(
            "src_relationship", "entries/src_relationship-v1.md", "entries/" + name
        )
        fields = self.store._read_ledger(edge["edge_id"])
        self.assertEqual(fields["status"], "approved")
        self.assertEqual(fields["source_reference"], "Journal/entries/" + name)
        raw = (self.journal / "relationships" / (edge["edge_id"] + ".md")).read_bytes()
        self.assertEqual(self.store._decode_ledger(raw, edge["edge_id"]), fields)
        self.assertIn(("[[Journal/entries/" + name + "]]").encode(), raw)
        result = self.store.scan_relationships()
        self.assertEqual(result["review"], [])
        self.assertEqual(result["edges"][0]["status"], "approved")


if __name__ == "__main__":
    unittest.main()
