"""`lifecontext doctor` lists vault files a write would refuse, and changes nothing."""

from __future__ import annotations

import os
import stat
import unittest
from pathlib import Path
from unittest.mock import patch

from lifecontext.doctor import render, run_doctor
from test_doctor import _service_down, _set_up, _temporary_home, _tesseract_run


@unittest.skipUnless(os.name == "posix", "ownership and modes are POSIX properties")
class DoctorVaultScanTests(unittest.TestCase):
    def _journal(self, home: Path) -> Path:
        journal = home / "LifeContext" / "vault" / "Journal"
        self.assertTrue(journal.is_dir())
        return journal

    def test_foreign_files_with_open_modes_are_not_reported(self) -> None:
        with _temporary_home() as home:
            _set_up(home)
            wiki = self._journal(home) / "wiki"
            wiki.mkdir(mode=0o755, exist_ok=True)
            for name in (".DS_Store", "pasted image.png"):
                (wiki / name).write_bytes(b"foreign")
                (wiki / name).chmod(0o644)
            report, _ = run_doctor(port=8765, fetch=_service_down, run=_tesseract_run)
            self.assertEqual(report["vault_refusals"], {"paths": [], "count": 0, "truncated": False})
            self.assertNotIn("vault_files_refused", report["warnings"])
            self.assertNotIn("vault_files_refused", report["problems"])
            self.assertNotIn("would refuse", render(report))

    def test_symlinks_writable_folders_and_special_files_are_listed_read_only(self) -> None:
        with _temporary_home() as home:
            _set_up(home)
            journal = self._journal(home)
            outside = home / "outside.md"
            outside.write_text("outside", encoding="utf-8")
            outside.chmod(0o644)
            wiki = journal / "wiki"
            wiki.mkdir(mode=0o700, exist_ok=True)
            (wiki / "link.md").symlink_to(outside)
            (journal / "documents").mkdir(mode=0o700, exist_ok=True)
            (journal / "documents").chmod(0o700)
            shared = journal / "documents" / "inbox"
            shared.mkdir(mode=0o700, exist_ok=True)
            shared.chmod(0o775)
            os.mkfifo(wiki / "pipe")
            (wiki / "plain.md").write_text("fine", encoding="utf-8")
            (wiki / "plain.md").chmod(0o644)
            before = {path: path.lstat() for path in (outside, wiki / "link.md", shared, wiki / "plain.md")}

            report, code = run_doctor(port=8765, fetch=_service_down, run=_tesseract_run)

            refusals = report["vault_refusals"]
            self.assertEqual(
                sorted(refusals["paths"]),
                ["Journal/documents/inbox", "Journal/wiki/link.md", "Journal/wiki/pipe"],
            )
            self.assertEqual((refusals["count"], refusals["truncated"]), (3, False))
            self.assertIn("vault_files_refused", report["warnings"])
            self.assertNotIn("vault_files_refused", report["problems"])
            self.assertEqual(code, 1 if report["problems"] else 0)
            text = render(report)
            self.assertIn("Journal/wiki/link.md", text)
            self.assertNotIn("outside", text)
            for path, info in before.items():
                with self.subTest(path=path.name):
                    after = path.lstat()
                    self.assertEqual(stat.S_IMODE(after.st_mode), stat.S_IMODE(info.st_mode))
                    self.assertEqual(after.st_mtime_ns, info.st_mtime_ns)
            self.assertEqual(outside.read_text(encoding="utf-8"), "outside")

    def test_entries_of_another_owner_are_listed(self) -> None:
        with _temporary_home() as home:
            _set_up(home)
            journal = self._journal(home)
            entries = journal / "entries"
            entries.mkdir(mode=0o700, exist_ok=True)
            foreign = entries / "theirs.md"
            foreign.write_text("x", encoding="utf-8")
            real_lstat = os.lstat
            inode = real_lstat(foreign).st_ino

            class _Foreign:
                def __init__(self, real):
                    self._real = real
                    self.st_uid = real.st_uid + 1

                def __getattr__(self, name):
                    return getattr(self._real, name)

            def lstat(path, *args, **kwargs):
                info = real_lstat(path, *args, **kwargs)
                return _Foreign(info) if info.st_ino == inode else info

            with patch("lifecontext.doctor.os.lstat", side_effect=lstat):
                report, _ = run_doctor(port=8765, fetch=_service_down, run=_tesseract_run)
            self.assertIn("Journal/entries/theirs.md", report["vault_refusals"]["paths"])


if __name__ == "__main__":
    unittest.main()
