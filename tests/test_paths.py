"""Private HOME path and unsafe-root checks for V1."""

from __future__ import annotations

import os
import stat
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from lifecontext.paths import LifeContextPaths
from local_paths import scratch_root


@contextmanager
def _temporary_home():
    with tempfile.TemporaryDirectory(
        prefix="lifeweave-paths-test.", dir=scratch_root()
    ) as temporary:
        home = Path(temporary).resolve(strict=True)
        with patch.dict(os.environ, {"HOME": str(home)}):
            yield home


class LifeContextPathTests(unittest.TestCase):
    def test_storage_paths_are_below_the_active_process_home(self) -> None:
        with _temporary_home() as home:
            paths = LifeContextPaths.current()

            self.assertEqual(paths.home, home)
            self.assertEqual(paths.archive_dir, home / ".lifeweave" / "archive")
            self.assertEqual(
                paths.catalog_path, home / ".lifeweave" / "catalog.sqlite3"
            )
            self.assertEqual(paths.lock_path, home / ".lifeweave" / "lifeweave.lock")
            for path in (paths.archive_dir, paths.catalog_path, paths.lock_path):
                self.assertTrue(path.is_relative_to(paths.lifeweave_home))

            paths.ensure_private_home()

            info = paths.lifeweave_home.lstat()
            self.assertTrue(stat.S_ISDIR(info.st_mode))
            self.assertFalse(paths.lifeweave_home.is_symlink())
            if os.name == "posix":
                self.assertEqual(info.st_uid, os.geteuid())
                self.assertEqual(stat.S_IMODE(info.st_mode), 0o700)

    def test_renamed_package_keeps_existing_storage_and_profile(self) -> None:
        from lifecontext.hindsight import PROFILE

        with _temporary_home() as home:
            paths = LifeContextPaths.current()
            paths.ensure_private_home()
            self.assertEqual(paths.lifeweave_home, home / ".lifeweave")
            self.assertEqual(paths.lock_path.name, "lifeweave.lock")
            self.assertEqual(PROFILE, "lifeweave")
            self.assertFalse((home / ".life-context").exists())
            self.assertFalse((home / ".lifecontext").exists())

    def test_symlinked_home_is_rejected(self) -> None:
        with _temporary_home() as home:
            paths = LifeContextPaths.current()
            target = home / "other"
            target.mkdir()
            paths.lifeweave_home.symlink_to(target, target_is_directory=True)

            with self.assertRaisesRegex(OSError, "unsafe LifeContext home"):
                paths.ensure_private_home()

    def test_non_directory_home_is_rejected(self) -> None:
        with _temporary_home():
            paths = LifeContextPaths.current()
            paths.lifeweave_home.write_text("not a directory", encoding="utf-8")

            with self.assertRaisesRegex(OSError, "unsafe LifeContext home"):
                paths.ensure_private_home()

    @unittest.skipUnless(os.name == "posix", "ownership is a POSIX property")
    def test_wrong_owner_home_is_rejected_before_open(self) -> None:
        with _temporary_home():
            paths = LifeContextPaths.current()
            paths.lifeweave_home.mkdir(mode=0o700)

            with (
                patch("lifecontext.paths.os.geteuid", return_value=os.geteuid() + 1),
                patch("lifecontext.paths.os.open") as open_path,
            ):
                with self.assertRaisesRegex(OSError, "unsafe LifeContext home"):
                    paths.ensure_private_home()
            open_path.assert_not_called()


if __name__ == "__main__":
    unittest.main()
