"""Readable wiki names retain the artifact writer's trust boundaries."""

import hashlib
import os
import stat
import tempfile
import unittest
from pathlib import Path

from lifecontext.vault import ManagedVault, VaultError


class WikiVaultNamesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name) / "vault"
        root.mkdir(mode=0o700)
        self.journal = root / "Journal"
        self.journal.mkdir(mode=0o700)
        self.vault = ManagedVault(root, self.journal)
        self.addCleanup(self.vault.close)

    def write(self, path, content=b"wiki", expected_hash=None):
        return self.vault.safe_write_bytes(
            path, content, expected_hash=expected_hash, artifact_id="wiki_test")

    def test_readable_names_round_trip_and_private_permissions(self):
        for directory in ("wiki", "wiki/history"):
            for name in ("ASCII-v1.md", "Café notes.md", "生活 — 🐈.md",
                         "e\u0301 -- arbitrary collision suffix.md", "界" * 79 + ".md"):
                path = f"{directory}/{name}"
                with self.subTest(path=path):
                    self.assertTrue(self.write(path))
                    self.assertEqual(self.vault.read_artifact(path), b"wiki")
                    self.assertEqual(stat.S_IMODE((self.journal / path).stat().st_mode), 0o600)
                    self.assertEqual(stat.S_IMODE((self.journal / directory).stat().st_mode), 0o700)

    def test_invalid_names_and_paths(self):
        names = [".hidden.md", "name.txt", "name.MD", "name.md ", "name.md.",
                 "界" * 80 + ".md", "a" * 238 + ".md", "CON.md", "con.notes.md",
                 "NUL.md", "PRN.md", "AUX.md", "COM1.md", "lpt9.md", "COM¹.md",
                 "CONIN$.md", "CONOUT$.md", "CON .md", "\ud800.md"]
        names += [f"bad{character}name.md" for character in '\\:*?"<>|#[]^']
        names += [f"bad{character}name.md" for character in
                  ("\x00", "\n", "\x7f", "\x85", "\u200b", "\u202e")]
        paths = [f"{directory}/{name}" for directory in ("wiki", "wiki/history")
                 for name in names]
        paths += ["wiki/../escape.md", "/wiki/name.md", "wiki//name.md",
                  "wiki/hístory/name.md", "wiki/history space/name.md"]
        for path in paths:
            with self.subTest(path=repr(path)):
                with self.assertRaises(VaultError) as caught:
                    self.write(path)
                self.assertEqual(caught.exception.code, "unsafe_path")

    def test_exact_byte_boundary(self):
        self.assertTrue(self.write("wiki/" + "a" * 237 + ".md"))
        self.assertTrue(self.write("wiki/" + "界" * 78 + "abc.md"))

    def test_document_and_index_restrictions_unchanged(self):
        for directory in ("documents/inbox/attachments", "documents/inbox", "indices"):
            self.assertTrue(self.write(f"{directory}/ASCII-v1.bin"))
            with self.assertRaises(VaultError) as caught:
                self.write(f"{directory}/Café.md")
            self.assertEqual(caught.exception.code, "unsafe_path")

    def test_schema_remains_read_only(self):
        with self.assertRaises(VaultError) as caught:
            self.write("wiki/SCHEMA.md")
        self.assertEqual(caught.exception.code, "read_only_path")

    def test_unicode_cas_and_identity_protection(self):
        path = "wiki/history/生活.md"
        self.write(path)
        digest = hashlib.sha256(b"wiki").hexdigest()
        _, identity = self.vault.read_artifact_identity(path)
        with self.assertRaises(VaultError):
            self.write(path, b"overwrite")
        with self.assertRaises(VaultError):
            self.write(path, b"overwrite", "0" * 64)
        with self.assertRaises(VaultError):
            self.vault.safe_remove_artifact(path, expected_hash=digest,
                artifact_id="wiki_test", expected_identity=(identity[0], identity[1] + 1))
        self.assertEqual(self.vault.read_artifact(path), b"wiki")
        self.assertTrue(self.write(path, b"updated", digest))
        self.assertTrue(self.vault.safe_remove_artifact(path,
            expected_hash=hashlib.sha256(b"updated").hexdigest(), artifact_id="wiki_test"))

    def test_symlinks_and_nonprivate_files_rejected(self):
        self.write("wiki/生活.md")
        target = self.journal / "wiki/生活.md"
        os.symlink(target, self.journal / "wiki/リンク.md")
        with self.assertRaises(VaultError):
            self.vault.read_artifact("wiki/リンク.md")
        os.symlink(self.journal / "wiki", self.journal / "wiki/history")
        with self.assertRaises(VaultError):
            self.vault.read_artifact("wiki/history/生活.md")
        target.chmod(0o644)
        with self.assertRaises(VaultError) as caught:
            self.vault.read_artifact("wiki/生活.md")
        self.assertEqual(caught.exception.code, "unsafe_permissions")


if __name__ == "__main__":
    unittest.main()
