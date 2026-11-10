"""A stray file in a managed folder never blocks vault writes (OPS-1)."""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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


class _ForeignStat:
    """An fstat result that claims another owner, for one chosen inode."""

    def __init__(self, real: os.stat_result):
        self._real = real
        self.st_uid = real.st_uid + 1

    def __getattr__(self, name: str):
        return getattr(self._real, name)


@unittest.skipUnless(os.name == "posix", "ownership and modes are POSIX properties")
class TargetPathTests(unittest.TestCase):
    """Only the target and its folders are checked, and the old guarantees hold there."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="lifeweave-vault-target-test.", dir=scratch_root()
        )
        self.base = Path(self.temporary.name).resolve(strict=True)
        self.root = self.base / "vault"
        self.journal = self.root / "Journal"
        self.wiki = self.journal / "wiki"
        self.entries = self.journal / "entries"
        for folder in (self.wiki, self.entries):
            folder.mkdir(mode=0o700, parents=True)
        for folder in (self.root, self.journal, self.wiki, self.entries):
            folder.chmod(0o700)
        self.outside = self.base / "outside.md"
        self.outside.write_text("outside bytes", encoding="utf-8")
        self.outside.chmod(0o644)
        self.vault = ManagedVault(self.root, self.journal)

    def tearDown(self) -> None:
        self.vault.close()
        self.temporary.cleanup()

    def _write_page(self, data: bytes = b"page", expected_hash: str | None = None) -> bool:
        return self.vault.safe_write_bytes(
            "wiki/Lease.md", data, expected_hash=expected_hash, artifact_id="page"
        )

    def _as_foreign(self, path: Path):
        inode = (path.lstat().st_dev, path.lstat().st_ino)
        real_fstat = os.fstat

        def fstat(descriptor):
            info = real_fstat(descriptor)
            return _ForeignStat(info) if (info.st_dev, info.st_ino) == inode else info

        return patch("lifecontext.vault.os.fstat", side_effect=fstat)

    def test_symlinked_target_is_refused_and_never_followed_or_chmodded(self) -> None:
        (self.wiki / "Lease.md").symlink_to(self.outside)
        digest = hashlib.sha256(b"outside bytes").hexdigest()
        for call in (
            lambda: self._write_page(b"outside bytes", digest),
            lambda: self._write_page(b"new", digest),
            lambda: self.vault.read_artifact("wiki/Lease.md"),
            lambda: self.vault.safe_remove_artifact("wiki/Lease.md", expected_hash=digest, artifact_id="page"),
        ):
            with self.assertRaises(VaultError) as raised:
                call()
            self.assertEqual(raised.exception.code, "unsafe_path")
        self.assertEqual(self.outside.read_text(encoding="utf-8"), "outside bytes")
        self.assertEqual(stat.S_IMODE(self.outside.stat().st_mode), 0o644)
        self.assertTrue((self.wiki / "Lease.md").is_symlink())

    def test_symlinked_parent_is_refused_without_writing_through_it(self) -> None:
        elsewhere = self.base / "elsewhere"
        elsewhere.mkdir(mode=0o700)
        (self.wiki / "history").symlink_to(elsewhere, target_is_directory=True)
        with self.assertRaises(VaultError) as raised:
            self.vault.safe_write_bytes(
                "wiki/history/Lease.md", b"page", expected_hash=None, artifact_id="page"
            )
        self.assertEqual(raised.exception.code, "unsafe_path")
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_group_or_other_writable_parent_is_refused(self) -> None:
        for mode in (0o770, 0o702, 0o777):
            with self.subTest(mode=oct(mode)):
                self.wiki.chmod(mode)
                try:
                    with self.assertRaises(VaultError) as raised:
                        self._write_page()
                    self.assertEqual(raised.exception.code, "unsafe_permissions")
                    with self.assertRaises(VaultError):
                        self.vault.read_artifact("wiki/Lease.md")
                finally:
                    self.wiki.chmod(0o700)
                self.assertFalse((self.wiki / "Lease.md").exists())

    def test_readable_but_not_writable_parent_is_accepted(self) -> None:
        self.wiki.chmod(0o755)
        self.assertTrue(self._write_page())
        self.assertEqual(stat.S_IMODE((self.wiki / "Lease.md").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.wiki.stat().st_mode), 0o755)

    def test_target_owned_by_another_user_is_refused_unchanged(self) -> None:
        self.assertTrue(self._write_page())
        target = self.wiki / "Lease.md"
        target.chmod(0o644)
        digest = hashlib.sha256(b"page").hexdigest()
        with self._as_foreign(target):
            for call in (
                lambda: self._write_page(b"page", digest),
                lambda: self._write_page(b"new", digest),
                lambda: self.vault.read_artifact("wiki/Lease.md"),
                lambda: self.vault.safe_remove_artifact("wiki/Lease.md", expected_hash=digest, artifact_id="page"),
            ):
                with self.assertRaises(VaultError) as raised:
                    call()
                self.assertEqual(raised.exception.code, "unsafe_permissions")
        self.assertEqual(target.read_bytes(), b"page")
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)

    def test_parent_owned_by_another_user_is_refused(self) -> None:
        with self._as_foreign(self.wiki):
            with self.assertRaises(VaultError) as raised:
                self._write_page()
            self.assertEqual(raised.exception.code, "unsafe_permissions")
        self.assertFalse((self.wiki / "Lease.md").exists())

    def test_own_file_with_extra_bits_is_tightened_on_a_matching_write(self) -> None:
        self.assertTrue(self._write_page())
        page = self.wiki / "Lease.md"
        page.chmod(0o644)
        self.assertTrue(self._write_page(b"page", hashlib.sha256(b"page").hexdigest()))
        self.assertEqual(stat.S_IMODE(page.stat().st_mode), 0o600)

        entry = _owned("entry")
        self.assertTrue(self.vault.safe_write_text("entries/source-v1.md", entry))
        written = self.entries / "source-v1.md"
        written.chmod(0o664)
        self.assertTrue(self.vault.safe_write_text("entries/source-v1.md", entry, overwrite=True))
        self.assertEqual(stat.S_IMODE(written.stat().st_mode), 0o600)

    def test_own_file_with_extra_bits_is_replaced_by_a_private_one(self) -> None:
        self.assertTrue(self._write_page())
        page = self.wiki / "Lease.md"
        page.chmod(0o644)
        self.assertTrue(self._write_page(b"updated", hashlib.sha256(b"page").hexdigest()))
        self.assertEqual(page.read_bytes(), b"updated")
        self.assertEqual(stat.S_IMODE(page.stat().st_mode), 0o600)

    def test_foreign_file_at_the_target_is_never_changed(self) -> None:
        human = self.wiki / "Lease.md"
        human.write_bytes(b"typed by hand")
        human.chmod(0o644)
        with self.assertRaises(VaultError) as raised:
            self._write_page()
        self.assertEqual(raised.exception.code, "conflict")
        with self.assertRaises(VaultError) as raised:
            self._write_page(b"typed by hand", hashlib.sha256(b"other").hexdigest())
        self.assertEqual(raised.exception.code, "conflict")
        note = self.entries / "note.md"
        note.write_text("my own note", encoding="utf-8")
        note.chmod(0o644)
        with self.assertRaises(VaultError) as raised:
            self.vault.safe_write_text("entries/note.md", _owned("x"), overwrite=True)
        self.assertEqual(raised.exception.code, "unmanaged_file")
        for path, data in ((human, b"typed by hand"), (note, b"my own note")):
            self.assertEqual(path.read_bytes(), data)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)

    def test_lock_file_with_extra_bits_is_tightened(self) -> None:
        with self.vault.writer_lock():
            pass
        lock = self.root / ".journal.lock"
        lock.chmod(0o644)
        with self.vault.writer_lock():
            pass
        self.assertEqual(stat.S_IMODE(lock.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
