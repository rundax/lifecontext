"""Filesystem custody contracts for the standalone LifeContext core."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lifecontext.custody import (
    CustodyConflict,
    CustodyError,
    LifeContextCustody,
)
from lifecontext.config import LIMIT_CEILINGS
from lifecontext.paths import LifeContextPaths
from local_paths import scratch_root


class LifeContextCustodyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="lifeweave-custody-test.", dir=scratch_root()
        )
        self.home = Path(self.temporary.name).resolve(strict=True)
        self.home_patch = patch.dict(os.environ, {"HOME": str(self.home)})
        self.home_patch.start()
        self.paths = LifeContextPaths.current()
        self.custody = LifeContextCustody(self.paths, max_bytes=64)

    def tearDown(self) -> None:
        self.home_patch.stop()
        self.temporary.cleanup()

    def test_round_trip_preserves_original_bytes_manifest_and_private_modes(self) -> None:
        original = b"\x00raw\xff\r\ncontent"
        provenance = {
            "source": "mail",
            "details": {"source_ref": "message-17", "labels": ["inbox", "urgent"]},
        }

        manifest = self.custody.write_version(
            "src_mail_17", 1, original, provenance=provenance
        )

        evidence = self.custody.resolve_exact("src_mail_17", 1)
        self.assertEqual(evidence.original_bytes, original)
        self.assertEqual(evidence.manifest, manifest)
        self.assertEqual(manifest.sha256, hashlib.sha256(original).hexdigest())
        self.assertEqual(manifest.size_bytes, len(original))
        self.assertEqual(manifest.to_dict()["provenance"], provenance)
        manifest_path = self.custody.manifest_path("src_mail_17", 1)
        persisted = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["source_id"], "src_mail_17")
        self.assertEqual(persisted["source_version"], 1)
        self.assertEqual(persisted["sha256"], hashlib.sha256(original).hexdigest())
        self.assertEqual(persisted["size_bytes"], len(original))
        self.assertEqual(persisted["provenance"], provenance)

        if os.name == "posix":
            for path in (
                self.custody.original_path("src_mail_17", 1),
                manifest_path,
            ):
                info = path.lstat()
                self.assertTrue(stat.S_ISREG(info.st_mode))
                self.assertEqual(info.st_uid, os.geteuid())
                self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)

    def test_same_source_version_cannot_replace_original_or_manifest(self) -> None:
        first = self.custody.write_version(
            "src_mail_18", 1, b"first", provenance={"source": "mail"}
        )
        before = self.custody.manifest_path("src_mail_18", 1).read_bytes()

        with self.assertRaises(CustodyConflict):
            self.custody.write_version(
                "src_mail_18", 1, b"replacement", provenance={"source": "other"}
            )

        self.assertEqual(
            self.custody.resolve_exact("src_mail_18", 1).manifest, first
        )
        self.assertEqual(self.custody.manifest_path("src_mail_18", 1).read_bytes(), before)
        self.assertEqual(self.custody.original_path("src_mail_18", 1).read_bytes(), b"first")

    def test_over_limit_input_fails_before_archive_files_are_visible(self) -> None:
        with self.assertRaisesRegex(CustodyError, "size limit"):
            self.custody.write_version(
                "src_large", 1, b"x" * 65, provenance={"source": "test"}
            )

        self.assertFalse(self.paths.archive_dir.exists())

    def test_invalid_source_ids_and_versions_are_rejected(self) -> None:
        for source_id, version in (("../escape", 1), ("src_safe", 0), ("src_safe", True)):
            with self.subTest(source_id=source_id, version=version):
                with self.assertRaises(CustodyError):
                    self.custody.write_version(
                        source_id, version, b"data", provenance={"source": "test"}
                    )
        self.assertFalse(self.paths.archive_dir.exists())

    def test_explicit_size_limit_cannot_exceed_v1_ceiling(self) -> None:
        with self.assertRaises(CustodyError):
            LifeContextCustody(
                self.paths,
                max_bytes=LIMIT_CEILINGS["max_file_bytes"] + 1,
            )

    def test_symlinked_source_directory_is_rejected_without_outside_writes(self) -> None:
        self.paths.ensure_private_home()
        sources = self.paths.archive_dir / "sources"
        sources.mkdir(parents=True, mode=0o700)
        outside = self.home / "outside"
        outside.mkdir(mode=0o700)
        (sources / "src_linked").symlink_to(outside, target_is_directory=True)

        with self.assertRaises(CustodyError):
            self.custody.write_version(
                "src_linked", 1, b"data", provenance={"source": "test"}
            )

        self.assertEqual(list(outside.iterdir()), [])

    def test_insecure_archive_directory_mode_is_rejected(self) -> None:
        self.paths.ensure_private_home()
        self.paths.archive_dir.mkdir(mode=0o700)
        if os.name == "posix":
            self.paths.archive_dir.chmod(0o755)
            with self.assertRaises(CustodyError):
                self.custody.write_version(
                    "src_mode", 1, b"data", provenance={"source": "test"}
                )
        else:
            self.skipTest("directory permission modes are POSIX-specific")

    def test_exact_resolution_returns_requested_version_and_detects_corruption(self) -> None:
        first = self.custody.write_version(
            "src_versions", 1, b"one", provenance={"revision": 1}
        )
        second = self.custody.write_version(
            "src_versions", 2, b"two", provenance={"revision": 2}
        )

        self.assertEqual(self.custody.resolve_exact("src_versions", 1).manifest, first)
        self.assertEqual(self.custody.resolve_exact("src_versions", 2).manifest, second)
        self.assertEqual(self.custody.resolve_exact("src_versions", 1).original_bytes, b"one")

        self.custody.original_path("src_versions", 1).write_bytes(b"tampered")
        with self.assertRaisesRegex(CustodyError, "checksum"):
            self.custody.resolve_exact("src_versions", 1)

    def test_remove_source_erases_only_verified_manifest_versions(self) -> None:
        first = self.custody.write_version(
            "src_delete", 1, b"first exact bytes", provenance={"source": "test"}
        )
        second = self.custody.write_version(
            "src_delete", 2, b"second exact bytes", provenance={"source": "test"}
        )
        unrelated = self.custody.write_version(
            "src_keep", 1, b"preserve other source", provenance={"source": "test"}
        )

        self.custody.remove_source(
            "src_delete",
            {1: first.sha256, 2: second.sha256},
        )

        self.assertFalse(self.custody.original_path("src_delete", 1).exists())
        self.assertFalse(self.custody.manifest_path("src_delete", 2).exists())
        self.assertEqual(self.custody.resolve_exact("src_keep", 1).manifest, unrelated)

    def test_remove_source_refuses_unexpected_archive_entries_before_deleting_any_version(self) -> None:
        first = self.custody.write_version(
            "src_delete_conflict", 1, b"first exact bytes", provenance={"source": "test"}
        )
        second = self.custody.write_version(
            "src_delete_conflict", 2, b"second exact bytes", provenance={"source": "test"}
        )
        (self.paths.archive_dir / "sources" / "src_delete_conflict" / "notes.txt").write_text(
            "unmanaged content", encoding="utf-8"
        )

        with self.assertRaises(CustodyError):
            self.custody.remove_source(
                "src_delete_conflict",
                {1: first.sha256, 2: second.sha256},
            )

        self.assertEqual(self.custody.resolve_exact("src_delete_conflict", 1).original_bytes, b"first exact bytes")
        self.assertTrue((self.paths.archive_dir / "sources" / "src_delete_conflict" / "notes.txt").exists())


if __name__ == "__main__":
    unittest.main()
