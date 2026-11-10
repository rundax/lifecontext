"""A stray file in a managed folder never blocks vault writes (OPS-1)."""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
import unittest
from pathlib import Path

from lifecontext.vault import ManagedVault, VaultError
from local_paths import scratch_root


def _owned(content: str) -> str:
    return f"---\nownership_marker: lifeweave-journal\n---\n{content}\n"


@unittest.skipUnless(os.name == "posix", "ownership and modes are POSIX properties")
class StrayFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="lifeweave-vault-scope-test.", dir=scratch_root()
        )
        self.base = Path(self.temporary.name).resolve(strict=True)
        self.root = self.base / "vault"
        self.journal = self.root / "Journal"
        self.wiki = self.journal / "wiki"
        self.wiki.mkdir(mode=0o700, parents=True)
        for folder in (self.root, self.journal, self.wiki):
            folder.chmod(0o700)
        self.vault = ManagedVault(self.root, self.journal)

    def tearDown(self) -> None:
        self.vault.close()
        self.temporary.cleanup()

    def _stray_files(self) -> list[Path]:
        strays = [self.wiki / ".DS_Store", self.wiki / "pasted image.png"]
        for path in strays:
            path.write_bytes(b"\x00foreign bytes")
            path.chmod(0o644)
        return strays

    def test_ds_store_and_image_with_0644_do_not_block_writes(self) -> None:
        strays = self._stray_files()
        before = {path: (path.read_bytes(), path.stat()) for path in strays}

        self.assertTrue(self.vault.verify_permissions())
        self.assertTrue(self.vault.safe_write_text("entries/source-v1.md", _owned("entry")))
        page = b"# Lease\n"
        self.assertTrue(
            self.vault.safe_write_bytes("wiki/Lease.md", page, expected_hash=None, artifact_id="page")
        )

        self.assertEqual((self.wiki / "Lease.md").read_bytes(), page)
        self.assertEqual(stat.S_IMODE((self.wiki / "Lease.md").stat().st_mode), 0o600)
        for path, (data, info) in before.items():
            with self.subTest(path=path.name):
                after = path.stat()
                self.assertEqual(path.read_bytes(), data)
                self.assertEqual(stat.S_IMODE(after.st_mode), 0o644)
                self.assertEqual(after.st_mtime_ns, info.st_mtime_ns)
                self.assertEqual(after.st_atime_ns, info.st_atime_ns)

    def test_stray_symlink_and_fifo_elsewhere_do_not_block_writes(self) -> None:
        outside = self.base / "outside.md"
        outside.write_text("outside bytes", encoding="utf-8")
        (self.wiki / "linked note.md").symlink_to(outside)
        os.mkfifo(self.wiki / "pipe")

        self.assertTrue(self.vault.safe_write_text("entries/source-v1.md", _owned("entry")))
        self.assertTrue(
            self.vault.safe_write_bytes("wiki/Lease.md", b"page", expected_hash=None, artifact_id="page")
        )
        self.assertEqual(outside.read_text(encoding="utf-8"), "outside bytes")

    def test_root_and_journal_stay_owner_only(self) -> None:
        self._stray_files()
        self.journal.chmod(0o755)
        try:
            with self.assertRaises(VaultError) as raised:
                self.vault.safe_write_text("entries/source-v1.md", _owned("entry"))
            self.assertEqual(raised.exception.code, "unsafe_permissions")
            with self.assertRaises(VaultError):
                self.vault.verify_permissions()
        finally:
            self.journal.chmod(0o700)
        self.assertFalse((self.journal / "entries").exists())


if __name__ == "__main__":
    unittest.main()
