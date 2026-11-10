"""Readiness checks the private vault root and Journal, not every file below them."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lifecontext.api.models import StatusResponse
from lifecontext.application import JournalApplication
from lifecontext.hindsight_adapter import LifeContextHindsightAdapter
from lifecontext.paths import LifeContextPaths
from lifecontext.vault import ManagedVault, VaultError
from local_paths import scratch_root
from test_application import ControlledClient


@unittest.skipUnless(os.name == "posix", "ownership and modes are POSIX properties")
class VaultReadinessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="lifeweave-readiness-test.", dir=scratch_root()
        )
        self.base = Path(self.temporary.name).resolve(strict=True)
        home = self.base / "home"
        home.mkdir(mode=0o700)
        self.home_patch = patch.dict(os.environ, {"HOME": str(home)})
        self.home_patch.start()
        paths = LifeContextPaths.current()
        paths.ensure_private_home()
        self.vault_root = self.base / "vault"
        self.journal = self.vault_root / "Journal"
        self.wiki = self.journal / "wiki"
        self.wiki.mkdir(mode=0o700, parents=True)
        for folder in (self.vault_root, self.journal, self.wiki):
            folder.chmod(0o700)
        self.vault = ManagedVault(self.vault_root, self.journal)
        self.app = JournalApplication(
            paths=paths,
            hindsight=LifeContextHindsightAdapter(ControlledClient(), bank_id="lifeweave"),
            vault=self.vault,
        )

    def tearDown(self) -> None:
        self.app.close()
        self.home_patch.stop()
        self.temporary.cleanup()

    def test_stray_files_and_open_subfolders_keep_the_vault_ready(self) -> None:
        for name in (".DS_Store", "pasted image.png"):
            (self.wiki / name).write_bytes(b"foreign")
            (self.wiki / name).chmod(0o644)
        (self.journal / "documents").mkdir(mode=0o700)
        (self.journal / "documents").chmod(0o777)

        vault = self.app.readiness()["dependencies"]["vault"]

        self.assertTrue(vault["ready"])
        self.assertTrue(vault["permissions"])
        self.assertIsNone(vault["error"])

    def test_an_open_root_or_journal_is_not_ready(self) -> None:
        for folder in (self.vault_root, self.journal):
            with self.subTest(folder=folder.name):
                folder.chmod(0o755)
                try:
                    vault = self.app.readiness()["dependencies"]["vault"]
                finally:
                    folder.chmod(0o700)
                self.assertFalse(vault["ready"])
                self.assertEqual(vault["error"], "unsafe_permissions")
                self.assertEqual(vault["refused_path"], "." if folder == self.vault_root else "Journal")

    def test_status_names_a_refused_file_until_a_write_to_it_succeeds(self) -> None:
        outside = self.base / "outside.md"
        outside.write_text("outside", encoding="utf-8")
        (self.wiki / "Lease.md").symlink_to(outside)
        with self.assertRaises(VaultError) as raised:
            self.vault.safe_write_bytes("wiki/Lease.md", b"page", expected_hash=None, artifact_id="page")
        self.assertEqual((raised.exception.code, str(raised.exception)), ("unsafe_path", "unsafe_path"))
        self.assertEqual(raised.exception.path, "Journal/wiki/Lease.md")

        vault = self.app.readiness()["dependencies"]["vault"]
        self.assertTrue(vault["ready"])
        self.assertEqual(vault["refused_path"], "Journal/wiki/Lease.md")
        self.assertNotIn(str(self.base), repr(vault))
        StatusResponse.model_validate(self.app.readiness())

        # A write elsewhere does not clear it; a successful write to that path does.
        self.vault.safe_write_bytes("wiki/Other.md", b"other", expected_hash=None, artifact_id="other")
        self.assertEqual(self.app.readiness()["dependencies"]["vault"]["refused_path"], "Journal/wiki/Lease.md")
        (self.wiki / "Lease.md").unlink()
        self.vault.safe_write_bytes("wiki/Lease.md", b"page", expected_hash=None, artifact_id="page")
        self.assertIsNone(self.app.readiness()["dependencies"]["vault"]["refused_path"])

    def test_a_refused_request_is_not_named(self) -> None:
        with self.assertRaises(VaultError) as raised:
            self.vault.safe_write_text("../outside.md", "x")
        self.assertIsNone(raised.exception.path)
        with self.assertRaises(VaultError):
            self.vault.safe_write_text(".obsidian/app.json", "x")
        self.assertIsNone(self.app.readiness()["dependencies"]["vault"]["refused_path"])


if __name__ == "__main__":
    unittest.main()
