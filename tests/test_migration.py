"""Offline migration evidence gate checks for disposable legacy exports."""

from __future__ import annotations

import json
import hashlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from local_paths import scratch_root

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from disposable_migration import (  # noqa: E402
    _GateBlocked,
    MIGRATION_EXPORT_FORMAT,
    convert_migration_export,
    run_migration,
)


def _make_migration_export(root: Path) -> dict:
    owner_namespace = "b" * 32
    active = "jrnl_src_" + "a" * 32
    forgotten = "jrnl_src_" + "c" * 32
    deleted = "jrnl_src_" + "d" * 32
    records = [
        (active, 1, "active", b"active one"),
        (active, 2, "active", b"active two"),
        (deleted, 1, "deleted", None),
        (forgotten, 1, "forgotten", b"forgotten local"),
    ]
    items = []
    for source_id, version, state, content in records:
        items.append({
            "source_id": source_id,
            "source_version": version,
            "lifecycle_state": state,
        })
        if state == "deleted":
            continue
        version_dir = root / "sources" / source_id / f"v{version}"
        version_dir.mkdir(parents=True, mode=0o700)
        manifest = {
            "schema_version": 1,
            "source_id": source_id,
            "source_version": version,
            "sha256": hashlib.sha256(content).hexdigest(),
            "size_bytes": len(content),
            "provenance": {"source": "fixture", "source_ref": f"ref-{version}"},
        }
        (version_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        (version_dir / "original.bin").write_bytes(content)
        (version_dir / "manifest.json").chmod(0o600)
        (version_dir / "original.bin").chmod(0o600)
    items.sort(key=lambda item: (item["source_id"], item["source_version"]))
    return {
        "format": MIGRATION_EXPORT_FORMAT,
        "format_version": 1,
        "inventory_complete": True,
        "owner_namespace": owner_namespace,
        "source_ids": sorted({item["source_id"] for item in items}),
        "tombstone_ids": [deleted],
        "items": items,
    }


class DisposableMigrationTests(unittest.TestCase):
    def test_incomplete_legacy_export_blocks_before_v1_writes(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="lifeweave-migration-test.", dir=scratch_root()
        ) as temporary:
            root = Path(temporary).resolve(strict=True)
            source_home = root / "legacy-home"
            source_home.mkdir(mode=0o700)
            (source_home / "config.yaml").write_text(
                "memory:\n  hindsight:\n    mode: local_embedded\n",
                encoding="utf-8",
            )
            source_text = "must not appear in the migration report"
            owner_namespace = "b" * 32

            def export(command, *, env, cwd, **_kwargs):
                self.assertEqual(
                    [Path(command[0]).name, *command[1:4]],
                    ["hermes", "hindsight", "journal", "export"],
                )
                self.assertEqual(command[-1], "--migration")
                self.assertEqual(env["HERMES_HOME"], str(source_home))
                self.assertEqual(env["HOME"], str(source_home))
                self.assertTrue(Path(env["TMPDIR"]).is_relative_to(root))
                self.assertEqual(cwd, source_home)
                export_root = source_home / "journal" / "exports" / command[-2]
                source_id = "jrnl_src_" + "a" * 32
                source_dir = export_root / "sources" / source_id
                source_dir.mkdir(parents=True, mode=0o700)
                (export_root / "manifest.json").write_text(
                    json.dumps({
                        "schema_version": 2,
                        "owner_namespace": owner_namespace,
                        "source_ids": [source_id],
                    }),
                    encoding="utf-8",
                )
                (source_dir / "manifest.json").write_text(
                    json.dumps({
                        "source_id": source_id,
                        "owner_namespace": owner_namespace,
                        "source_version": 1,
                        "hindsight_state": "forgotten",
                    }),
                    encoding="utf-8",
                )
                (source_dir / "original.md").write_text(
                    source_text, encoding="utf-8"
                )
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=json.dumps({"success": True, "destination": str(export_root)}),
                    stderr="private exporter diagnostics",
                )

            with patch("disposable_migration.subprocess.run", side_effect=export) as run:
                report = run_migration(source_home)

            rendered = json.dumps(report, sort_keys=True)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(report["status"], "BLOCKED")
            self.assertEqual(report["reason_code"], "legacy_lifecycle_evidence_incomplete")
            self.assertEqual(
                report["missing_evidence"],
                ["per_version_lifecycle_state", "tombstone_inventory", "version_inventory"],
            )
            self.assertFalse(report["writes_started"])
            self.assertEqual(report["v1_bundle_roundtrip"], {"status": "NOT_RUN"})
            self.assertEqual(report["converted_bundle"], {"status": "NOT_RUN"})
            self.assertNotIn(source_text, rendered)
            self.assertNotIn(str(root), rendered)
            self.assertNotIn(owner_namespace, rendered)
            self.assertNotIn("private exporter diagnostics", rendered)
            saved_reports = list(source_home.glob("lifeweave-migration-report-*.json"))
            self.assertEqual(len(saved_reports), 1)
            saved = saved_reports[0]
            self.assertEqual(json.loads(saved.read_text(encoding="utf-8")), report)
            self.assertEqual(saved.stat().st_mode & 0o777, 0o600)

    def test_export_root_outside_disposable_home_blocks_before_export(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="lifeweave-migration-test.", dir=scratch_root()
        ) as temporary:
            source_home = Path(temporary).resolve(strict=True) / "legacy-home"
            source_home.mkdir(mode=0o700)
            (source_home / "config.yaml").write_text(
                "memory:\n  hindsight:\n    mode: local_embedded\n"
                "journal_export_root: /home/example/Obsidian/Journal\n",
                encoding="utf-8",
            )

            with patch("disposable_migration.subprocess.run") as run:
                report = run_migration(source_home)

            run.assert_not_called()
            self.assertEqual(report["status"], "BLOCKED")
            self.assertEqual(report["reason_code"], "unsafe_export_root")
            self.assertFalse(report["writes_started"])
            rendered = json.dumps(report, sort_keys=True)
            self.assertNotIn("/home/example", rendered)
            self.assertNotIn("Obsidian", rendered)

    def test_malformed_migration_manifest_is_rejected(self) -> None:
        base = {
            "format": MIGRATION_EXPORT_FORMAT,
            "format_version": 1,
            "inventory_complete": True,
            "owner_namespace": "b" * 32,
            "source_ids": [],
            "tombstone_ids": [],
            "items": [],
        }
        malformed_manifests = (
            ({**base, "owner_namespace": "invalid-owner"}, "FAILED", "legacy_migration_export_invalid"),
            ({**base, "format_version": True}, "FAILED", "legacy_migration_export_invalid"),
            ({**base, "format_version": 2}, "FAILED", "legacy_migration_export_invalid"),
            ({**base, "inventory_complete": False}, "BLOCKED", "legacy_lifecycle_evidence_incomplete"),
        )
        for manifest, expected_status, expected_reason in malformed_manifests:
            with self.subTest(manifest=manifest):
                with tempfile.TemporaryDirectory(
                    prefix="lifeweave-migration-test.", dir=scratch_root()
                ) as temporary:
                    source_home = Path(temporary).resolve(strict=True) / "legacy-home"
                    source_home.mkdir(mode=0o700)
                    (source_home / "config.yaml").write_text(
                        "memory:\n  hindsight:\n    mode: local_embedded\n",
                        encoding="utf-8",
                    )

                    def export(command, **_kwargs):
                        export_root = source_home / "journal" / "exports" / command[-2]
                        (export_root / "sources").mkdir(parents=True, mode=0o700)
                        (export_root / "manifest.json").write_text(
                            json.dumps(manifest),
                            encoding="utf-8",
                        )
                        return subprocess.CompletedProcess(
                            command,
                            0,
                            stdout=json.dumps({"success": True, "destination": str(export_root)}),
                            stderr="",
                        )

                    with patch("disposable_migration.subprocess.run", side_effect=export):
                        report = run_migration(source_home)

                self.assertEqual(report["status"], expected_status)
                self.assertEqual(report["reason_code"], expected_reason)
                self.assertFalse(report["writes_started"])
                self.assertNotIn("invalid-owner", json.dumps(report))

    def test_migration_export_converts_complete_versions_and_lifecycle(self) -> None:
        from lifecontext.bundle import decode_bundle

        with tempfile.TemporaryDirectory(prefix="lifeweave-migration-export-test.", dir=scratch_root()) as temporary:
            export_root = Path(temporary)
            (export_root / "sources").mkdir(mode=0o700)
            manifest = _make_migration_export(export_root)
            (export_root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

            bundle, counts = convert_migration_export(export_root)
            records = decode_bundle(bundle)
            by_identity = {(item.source_id, item.source_version): item for item in records}

            self.assertEqual(counts, {
                "source_count": 3,
                "version_count": 4,
                "forgotten_source_count": 1,
                "deleted_source_count": 1,
            })
            self.assertEqual(by_identity[("jrnl_src_" + "a" * 32, 1)].original_bytes, b"active one")
            self.assertEqual(by_identity[("jrnl_src_" + "a" * 32, 2)].original_bytes, b"active two")
            self.assertEqual(by_identity[("jrnl_src_" + "c" * 32, 1)].lifecycle_state, "forgotten")
            self.assertEqual(by_identity[("jrnl_src_" + "d" * 32, 1)].lifecycle_state, "deleted")
            self.assertIsNone(by_identity[("jrnl_src_" + "d" * 32, 1)].original_bytes)

    def test_migration_export_rejects_incomplete_and_checksum_mismatch(self) -> None:
        with tempfile.TemporaryDirectory(prefix="lifeweave-migration-invalid.", dir=scratch_root()) as temporary:
            export_root = Path(temporary)
            (export_root / "sources").mkdir(mode=0o700)
            manifest = _make_migration_export(export_root)
            (export_root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            missing_tombstone_manifest = {**manifest, "tombstone_ids": []}
            (export_root / "manifest.json").write_text(
                json.dumps(missing_tombstone_manifest), encoding="utf-8",
            )
            with self.assertRaises(_GateBlocked) as missing_tombstones:
                convert_migration_export(export_root)
            self.assertEqual(
                missing_tombstones.exception.reason_code,
                "legacy_lifecycle_evidence_incomplete",
            )

            active_id = "jrnl_src_" + "a" * 32
            original = export_root / "sources" / active_id / "v1" / "original.bin"
            original.write_bytes(b"tampered bytes")
            (export_root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaises(_GateBlocked) as checksum:
                convert_migration_export(export_root)
            self.assertEqual(
                checksum.exception.reason_code,
                "legacy_lifecycle_evidence_incomplete",
            )

    def test_source_home_outside_v1_scratch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="lifeweave-outside.", dir="/tmp") as temporary:
            source_home = Path(temporary).resolve(strict=True)
            with patch("disposable_migration.subprocess.run") as run:
                report = run_migration(source_home)

        run.assert_not_called()
        self.assertEqual(report["status"], "BLOCKED")
        self.assertEqual(report["reason_code"], "source_home_not_disposable")
        self.assertFalse(report["writes_started"])

    def test_cloud_hindsight_mode_blocks_before_export(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="lifeweave-migration-test.", dir=scratch_root()
        ) as temporary:
            source_home = Path(temporary).resolve(strict=True) / "legacy-home"
            source_home.mkdir(mode=0o700)
            (source_home / "config.yaml").write_text(
                "memory:\n  hindsight:\n    mode: cloud\n",
                encoding="utf-8",
            )

            with patch("disposable_migration.subprocess.run") as run:
                report = run_migration(source_home)

        run.assert_not_called()
        self.assertEqual(report["status"], "BLOCKED")
        self.assertEqual(report["reason_code"], "remote_hindsight_not_allowed")
        self.assertFalse(report["writes_started"])

    def test_profile_config_cannot_redirect_export_to_cloud(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="lifeweave-migration-test.", dir=scratch_root()
        ) as temporary:
            source_home = Path(temporary).resolve(strict=True) / "legacy-home"
            profile_config = source_home / "hindsight" / "config.json"
            profile_config.parent.mkdir(mode=0o700, parents=True)
            profile_config.write_text(
                json.dumps({"mode": "cloud", "api_url": "https://example.invalid"}),
                encoding="utf-8",
            )

            with patch("disposable_migration.subprocess.run") as run:
                report = run_migration(source_home)

        run.assert_not_called()
        self.assertEqual(report["status"], "BLOCKED")
        self.assertEqual(report["reason_code"], "remote_hindsight_not_allowed")
        self.assertFalse(report["writes_started"])
        self.assertNotIn("example.invalid", json.dumps(report))


if __name__ == "__main__":
    unittest.main()
