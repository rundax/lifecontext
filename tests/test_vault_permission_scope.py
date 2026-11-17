"""A stray file in a managed folder never blocks vault writes (OPS-1)."""

from __future__ import annotations

import hashlib
import json
import os
import socket
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

    def test_hard_linked_file_is_never_chmodded(self) -> None:
        self.assertTrue(self._write_page())
        page = self.wiki / "Lease.md"
        page.chmod(0o644)
        elsewhere = self.base / "elsewhere.md"
        os.link(page, elsewhere)
        self.assertTrue(self._write_page(b"page", hashlib.sha256(b"page").hexdigest()))
        self.assertEqual(stat.S_IMODE(elsewhere.stat().st_mode), 0o644)

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


def _ledger(edge_from: str = "a", edge_to: str = "b") -> tuple[str, str]:
    digest = hashlib.sha256(
        json.dumps([edge_from, edge_to, "references"], ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    text = (
        "---\nownership_marker: lifeweave-relationship\nschema_version: 1\n"
        f"edge_id: edge_{digest}\nidentity: sha256:{digest}\nfrom: {edge_from}\nto: {edge_to}\n"
        "relation: references\nstatus: proposed\nrevision: 1\n---\n"
    )
    return f"edge_{digest}", text


@unittest.skipUnless(os.name == "posix", "ownership and modes are POSIX properties")
class RefusedComponentTests(unittest.TestCase):
    """The refused folder or file is named; special files and bad folders are refused."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="lw-scope.", dir=scratch_root())
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

    def _page(self) -> bool:
        return self.vault.safe_write_bytes("wiki/Lease.md", b"page", expected_hash=None, artifact_id="page")

    def test_fifo_socket_and_directory_at_the_target_are_named(self) -> None:
        target = self.wiki / "Lease.md"
        listener = socket.socket(socket.AF_UNIX)
        self.addCleanup(listener.close)
        # A short path through the folder's descriptor: the full path exceeds AF_UNIX's limit.
        wiki_fd = os.open(self.wiki, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, wiki_fd)
        makers = {
            "fifo": lambda: os.mkfifo(target),
            "directory": lambda: target.mkdir(mode=0o700),
            "socket": lambda: listener.bind(f"/proc/self/fd/{wiki_fd}/Lease.md"),
        }
        for kind, make in makers.items():
            with self.subTest(kind=kind):
                if kind == "socket" and not os.path.isdir("/proc/self/fd"):
                    self.skipTest("needs /proc for a short socket path")
                make()
                with self.assertRaises(VaultError) as raised:
                    self._page()
                self.assertEqual(raised.exception.code, "unsafe_path")
                self.assertTrue(raised.exception.on_disk)
                self.assertEqual(raised.exception.path, "Journal/wiki/Lease.md")
                if target.is_dir():
                    target.rmdir()
                else:
                    target.unlink()

    def test_symlinked_and_group_writable_folders_are_refused_and_named(self) -> None:
        edge_id, ledger = _ledger()
        source_hash = hashlib.sha256(b"source").hexdigest()
        calls = {
            "entries": (
                lambda: self.vault.safe_write_text("entries/source-v1.md", _owned("x")),
                lambda: self.vault.safe_remove_projection(
                    "entries/source-v1.md", expected_hash=source_hash,
                    expected_source_hash=source_hash, allow_missing=True),
            ),
            "relationships": (
                lambda: self.vault.safe_write_relationship_text(edge_id, ledger),
            ),
        }
        elsewhere = self.base / "elsewhere"
        elsewhere.mkdir(mode=0o700)
        for folder, folder_calls in calls.items():
            path = self.journal / folder
            for how in ("symlink", "group-writable"):
                if how == "symlink":
                    path.symlink_to(elsewhere, target_is_directory=True)
                    code = "unsafe_path"
                else:
                    path.mkdir(mode=0o700)
                    path.chmod(0o770)
                    code = "unsafe_permissions"
                for index, call in enumerate(folder_calls):
                    with self.subTest(folder=folder, how=how, call=index):
                        with self.assertRaises(VaultError) as raised:
                            call()
                        self.assertEqual(raised.exception.code, code)
                        self.assertEqual(raised.exception.path, f"Journal/{folder}")
                        self.assertEqual(self.vault.last_refusal["path"], f"Journal/{folder}")
                if how == "symlink":
                    path.unlink()
                else:
                    path.rmdir()
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_refusal_record_is_dropped_once_the_folder_is_acceptable(self) -> None:
        self.wiki.chmod(0o777)
        with self.assertRaises(VaultError):
            self._page()
        self.assertEqual(self.vault.last_refusal["path"], "Journal/wiki")
        self.wiki.chmod(0o700)
        self.assertIsNone(self.vault.last_refusal)

    def test_reads_apply_the_write_side_folder_and_file_checks(self) -> None:
        self.assertTrue(self._page())
        self.assertEqual(self.vault.read_vault_file("Journal/wiki/Lease.md", max_bytes=100), b"page")
        self.assertEqual(self.vault.list_vault_directory("Journal/wiki")[0], [("Lease.md", "file")])
        elsewhere = self.base / "elsewhere"
        elsewhere.mkdir(mode=0o700)
        (elsewhere / "Lease.md").write_bytes(b"outside")
        self.wiki.chmod(0o770)
        for call in (
            lambda: self.vault.read_vault_file("Journal/wiki/Lease.md", max_bytes=100),
            lambda: self.vault.list_vault_directory("Journal/wiki"),
        ):
            with self.assertRaises(VaultError) as raised:
                call()
            self.assertEqual(raised.exception.code, "unsafe_permissions")
        self.wiki.chmod(0o700)
        (self.journal / "documents").symlink_to(elsewhere, target_is_directory=True)
        with self.assertRaises(VaultError) as raised:
            self.vault.read_vault_file("Journal/documents/Lease.md", max_bytes=100)
        self.assertEqual(raised.exception.code, "unsafe_path")
        with self.assertRaises(VaultError) as raised:
            self.vault.read_vault_file("Journal/missing/Lease.md", max_bytes=100)
        self.assertEqual(raised.exception.code, "not_found")
        os.mkfifo(self.wiki / "pipe.md")
        with self.assertRaises(VaultError) as raised:
            self.vault.read_vault_file("Journal/wiki/pipe.md", max_bytes=100)
        self.assertEqual(raised.exception.code, "unsafe_path")
        page = self.wiki / "Lease.md"
        inode = (page.stat().st_dev, page.stat().st_ino)
        real_fstat = os.fstat

        def fstat(descriptor):
            info = real_fstat(descriptor)
            return _ForeignStat(info) if (info.st_dev, info.st_ino) == inode else info

        with patch("lifecontext.vault.os.fstat", side_effect=fstat):
            with self.assertRaises(VaultError) as raised:
                self.vault.read_vault_file("Journal/wiki/Lease.md", max_bytes=100)
        self.assertEqual(raised.exception.code, "unsafe_permissions")

    def test_folders_and_files_outside_journal_keep_the_plain_checks(self) -> None:
        areas = self.root / "Areas"
        (areas / "Sub").mkdir(parents=True)
        for folder in (areas, areas / "Sub"):
            folder.chmod(0o775)
        note = areas / "note.md"
        note.write_text("[[x]]", encoding="utf-8")
        note.chmod(0o664)
        self.assertEqual(
            self.vault.list_vault_directory("Areas")[0], [("Sub", "directory"), ("note.md", "file")]
        )
        self.assertEqual(self.vault.read_vault_file("Areas/note.md", max_bytes=100), b"[[x]]")
        (areas / "link.md").symlink_to(note)
        with self.assertRaises(VaultError) as raised:
            self.vault.read_vault_file("Areas/link.md", max_bytes=100)
        self.assertEqual(raised.exception.code, "unsafe_path")

    @unittest.skipIf(os.geteuid() == 0, "root can open a mode 0000 file")
    def test_refusal_of_an_unopenable_own_file_stands_after_the_recheck(self) -> None:
        locked = self.wiki / "Locked.md"
        locked.write_bytes(b"x")
        locked.chmod(0)
        with self.assertRaises(VaultError) as raised:
            self.vault.read_artifact("wiki/Locked.md")
        self.assertEqual(raised.exception.code, "unsafe_permissions")
        self.assertEqual(self.vault.last_refusal["path"], "Journal/wiki/Locked.md")
        locked.chmod(0o600)
        self.assertIsNone(self.vault.last_refusal)

    def test_last_refusal_never_raises_when_the_recheck_cannot_run(self) -> None:
        self.wiki.chmod(0o777)
        with self.assertRaises(VaultError):
            self._page()
        with patch("lifecontext.vault.os.dup", side_effect=OSError(24, "too many open files")):
            self.assertEqual(self.vault.last_refusal["path"], "Journal/wiki")
        self.wiki.chmod(0o700)

    def test_lock_file_with_a_second_hard_link_is_refused_and_named(self) -> None:
        with self.vault.writer_lock():
            pass
        lock = self.root / ".journal.lock"
        os.link(lock, self.base / "other-link")
        lock.chmod(0o644)
        with self.assertRaises(VaultError) as raised:
            with self.vault.writer_lock():
                pass
        self.assertEqual(raised.exception.code, "unsafe_permissions")
        self.assertEqual((raised.exception.on_disk, raised.exception.path), (True, ".journal.lock"))
        self.assertEqual(stat.S_IMODE((self.base / "other-link").stat().st_mode), 0o644)
        with self.assertRaises(VaultError) as raised:
            self._page()
        self.assertEqual(self.vault.last_refusal["path"], ".journal.lock")

    def test_new_lock_file_is_private_whatever_the_umask(self) -> None:
        old = os.umask(0o277)
        try:
            with self.vault.writer_lock():
                pass
        finally:
            os.umask(old)
        self.assertEqual(stat.S_IMODE((self.root / ".journal.lock").stat().st_mode), 0o600)

    def test_keyword_calls_keep_working_and_name_the_target(self) -> None:
        (self.wiki / "Lease.md").symlink_to(self.base)
        with self.assertRaises(VaultError) as raised:
            self.vault.safe_write_bytes(
                relative_path="wiki/Lease.md", content=b"page", expected_hash=None, artifact_id="page"
            )
        self.assertEqual(raised.exception.path, "Journal/wiki/Lease.md")
        edge_id, ledger = _ledger()
        self.assertTrue(self.vault.safe_write_relationship_text(edge_id=edge_id, content=ledger))


if __name__ == "__main__":
    unittest.main()
