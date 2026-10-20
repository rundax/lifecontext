"""Managed projection safety checks against disposable vaults only."""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lifecontext.vault import ManagedVault, VaultError, default_journal_paths
from local_paths import scratch_root


class ManagedVaultRootTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="lifeweave-vault-test.", dir=scratch_root()
        )
        self.base = Path(self.temporary.name).resolve(strict=True)
        self.root = self.base / "vault"
        self.journal = self.root / "Journal"
        self.journal.mkdir(mode=0o700, parents=True)
        self.root.chmod(0o700)
        self.journal.chmod(0o700)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_default_path_policy_is_pure_and_portable(self) -> None:
        with (
            patch("lifecontext.vault.os.open") as open_path,
            patch("lifecontext.vault.os.stat") as stat_path,
            patch("pathlib.Path.stat") as path_stat,
            patch("pathlib.Path.exists") as path_exists,
            patch("pathlib.Path.mkdir") as path_mkdir,
        ):
            root, journal = default_journal_paths("/home/example")

        self.assertEqual(root, Path("/home/example/LifeContext/vault"))
        self.assertEqual(journal, Path("/home/example/LifeContext/vault/Journal"))
        open_path.assert_not_called()
        stat_path.assert_not_called()
        path_stat.assert_not_called()
        path_exists.assert_not_called()
        path_mkdir.assert_not_called()

    def test_service_without_a_vault_block_opens_the_portable_default(self) -> None:
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        from lifecontext import cli

        client = MagicMock()
        with (
            patch.dict(os.environ, {"HOME": "/home/example"}),
            patch("lifecontext.config.load_settings", return_value={}),
            patch("lifecontext.hindsight.preflight", return_value={"base_url": "http://127.0.0.1:9"}),
            patch("lifecontext.hindsight.engine_token", return_value="f" * 64),
            patch("lifecontext.hindsight._load_runtime", return_value=SimpleNamespace(Hindsight=lambda **_: client)),
            patch("lifecontext.vision.vision_reader", return_value=None),
            patch("lifecontext.vault.ManagedVault", side_effect=VaultError("unsafe_path")) as vault,
            patch("pathlib.Path.is_dir", return_value=True),
            self.assertRaises(VaultError),
        ):
            cli._build_application()

        vault.assert_called_once_with(
            Path("/home/example/LifeContext/vault"), Path("/home/example/LifeContext/vault/Journal")
        )
        client.close.assert_called_once()

    def test_service_reports_a_missing_vault_before_starting_hindsight(self) -> None:
        from lifecontext import cli

        with (
            patch.dict(os.environ, {"HOME": str(self.base / "fresh-home")}),
            patch("lifecontext.config.load_settings", return_value={}),
            patch("lifecontext.hindsight.preflight") as preflight,
            self.assertRaises(VaultError) as error,
        ):
            cli._build_application()
        self.assertEqual(error.exception.code, "vault_missing")
        preflight.assert_not_called()
        with (
            patch.object(cli, "_build_application", side_effect=VaultError("vault_missing")),
            patch("sys.stderr") as stderr,
        ):
            self.assertEqual(cli.main(["serve"]), 1)
        self.assertIn("Journal folder does not exist", "".join(call.args[0] for call in stderr.write.call_args_list))

    def test_any_absolute_private_vault_is_accepted(self) -> None:
        with ManagedVault(self.root, self.journal) as vault:
            self.assertEqual((vault.vault_root, vault.journal_path), (self.root, self.journal))

    def test_traversal_is_rejected_before_a_lock_or_target_is_created(self) -> None:
        bad_root = f"{self.root}/../vault"

        with self.assertRaises(VaultError):
            ManagedVault(bad_root, self.journal)

        self.assertFalse((self.root / ".journal.lock").exists())
        self.assertEqual(list(self.journal.iterdir()), [])

    def test_journal_outside_root_is_rejected_before_mutation(self) -> None:
        outside = self.base / "outside-journal"
        outside.mkdir(mode=0o700)

        with self.assertRaises(VaultError):
            ManagedVault(self.root, outside)

        self.assertEqual(list(self.journal.iterdir()), [])
        self.assertEqual(list(outside.iterdir()), [])

    def test_symlinked_ancestor_is_rejected_without_touching_its_target(self) -> None:
        real_parent = self.base / "real-parent"
        real_root = real_parent / "vault"
        real_journal = real_root / "Journal"
        real_journal.mkdir(mode=0o700, parents=True)
        real_root.chmod(0o700)
        real_journal.chmod(0o700)
        alias = self.base / "alias"
        alias.symlink_to(real_parent, target_is_directory=True)

        with self.assertRaises(VaultError):
            ManagedVault(alias / "vault", alias / "vault" / "Journal")

        self.assertEqual(list(real_journal.iterdir()), [])
        self.assertFalse((real_root / ".journal.lock").exists())

    def test_symlinked_journal_is_rejected_without_touching_target(self) -> None:
        actual = self.root / "actual-journal"
        actual.mkdir(mode=0o700)
        linked = self.root / "Journal"
        linked.rmdir()
        linked.symlink_to(actual, target_is_directory=True)

        with self.assertRaises(VaultError):
            ManagedVault(self.root, linked)

        self.assertEqual(list(actual.iterdir()), [])
        self.assertFalse((self.root / ".journal.lock").exists())

    @unittest.skipUnless(os.name == "posix", "ownership and modes are POSIX properties")
    def test_insecure_root_mode_is_rejected_before_mutation(self) -> None:
        self.root.chmod(0o750)

        with self.assertRaises(VaultError):
            ManagedVault(self.root, self.journal)

        self.assertFalse((self.root / ".journal.lock").exists())
        self.assertEqual(list(self.journal.iterdir()), [])

    @unittest.skipUnless(os.name == "posix", "ownership is a POSIX property")
    def test_wrong_owner_is_rejected_before_mutation(self) -> None:
        with patch("lifecontext.vault.os.getuid", return_value=os.getuid() + 1):
            with self.assertRaises(VaultError):
                ManagedVault(self.root, self.journal)

        self.assertFalse((self.root / ".journal.lock").exists())
        self.assertEqual(list(self.journal.iterdir()), [])

    @unittest.skipUnless(os.name == "posix", "advisory flock is a POSIX property")
    def test_writer_lock_timeout_does_not_mutate_projection_target(self) -> None:
        vault = ManagedVault(self.root, self.journal)
        try:
            with vault.writer_lock(timeout=0.05):
                with self.assertRaises(VaultError) as raised:
                    with vault.writer_lock(timeout=0.01):
                        self.fail("a contending writer unexpectedly acquired the lock")

            self.assertEqual(raised.exception.code, "lock_timeout")
            self.assertTrue(raised.exception.retryable)
            self.assertFalse((self.journal / "entries" / "never-written.md").exists())
            lock_info = (self.root / ".journal.lock").lstat()
            self.assertTrue(stat.S_ISREG(lock_info.st_mode))
            self.assertEqual(stat.S_IMODE(lock_info.st_mode), 0o600)
        finally:
            vault.close()


class ManagedVaultWriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="lifeweave-vault-write-test.", dir=scratch_root()
        )
        self.base = Path(self.temporary.name).resolve(strict=True)
        self.root = self.base / "vault"
        self.journal = self.root / "Journal"
        self.entries = self.journal / "entries"
        self.entries.mkdir(mode=0o700, parents=True)
        self.root.chmod(0o700)
        self.journal.chmod(0o700)
        self.entries.chmod(0o700)
        self.vault = ManagedVault(self.root, self.journal)

    def tearDown(self) -> None:
        self.vault.close()
        self.temporary.cleanup()

    @staticmethod
    def _owned(content: str) -> str:
        return f"---\nownership_marker: lifeweave-journal\n---\n{content}\n"

    def test_versioned_projection_is_private_and_identical_replays_are_idempotent(self) -> None:
        original = self._owned("version one")
        target = self.entries / "source.md"

        self.assertTrue(self.vault.safe_write_text("entries/source.md", original))
        self.assertEqual(target.read_text(encoding="utf-8"), original)
        self.assertEqual(stat.S_IMODE(target.lstat().st_mode), 0o600)

        original_hash = hashlib.sha256(original.encode("utf-8")).hexdigest()
        self.assertTrue(
            self.vault.safe_write_text(
                "entries/source.md",
                original,
                overwrite=True,
                expected_hash=original_hash,
            )
        )

        with self.assertRaises(VaultError) as raised:
            self.vault.safe_write_text(
                "entries/source.md",
                self._owned("version two"),
                overwrite=True,
                expected_hash="incorrect",
            )
        self.assertEqual(raised.exception.code, "conflict")
        self.assertEqual(target.read_text(encoding="utf-8"), original)

        with self.assertRaises(VaultError) as raised:
            self.vault.safe_write_text(
                "entries/source.md",
                self._owned("version two"),
                overwrite=True,
                expected_hash=original_hash,
            )
        self.assertEqual(raised.exception.code, "conflict")
        self.assertEqual(target.read_text(encoding="utf-8"), original)

    def test_unmanaged_target_and_unsafe_paths_are_rejected_without_changes(self) -> None:
        target = self.entries / "human.md"
        target.write_text("human-owned bytes", encoding="utf-8")
        target.chmod(0o600)

        with self.assertRaises(VaultError) as raised:
            self.vault.safe_write_text(
                "entries/human.md", self._owned("replacement"), overwrite=True
            )
        self.assertEqual(raised.exception.code, "unmanaged_file")
        self.assertEqual(target.read_text(encoding="utf-8"), "human-owned bytes")

        for unsafe in ("../outside.md", ".obsidian/app.json", "expectations/secret.md", "wiki/SCHEMA.md"):
            with self.subTest(path=unsafe):
                with self.assertRaises(VaultError):
                    self.vault.safe_write_text(unsafe, self._owned("data"))
        self.assertEqual(target.read_text(encoding="utf-8"), "human-owned bytes")
        self.assertEqual(list(self.base.glob("outside.md")), [])

    @unittest.skipUnless(os.name == "posix", "ownership and modes are POSIX properties")
    def test_insecure_managed_file_mode_is_rejected_without_replacement(self) -> None:
        original = self._owned("owner-controlled bytes")
        target = self.entries / "unsafe-mode.md"
        target.write_text(original, encoding="utf-8")
        target.chmod(0o644)

        with self.assertRaises(VaultError):
            self.vault.safe_write_text(
                "entries/unsafe-mode.md",
                self._owned("replacement"),
                overwrite=True,
            )

        self.assertEqual(target.read_text(encoding="utf-8"), original)
        self.assertEqual(stat.S_IMODE(target.lstat().st_mode), 0o644)

    def test_symlinked_target_and_parent_are_rejected(self) -> None:
        outside = self.base / "outside.md"
        outside.write_text("outside bytes", encoding="utf-8")
        outside.chmod(0o600)
        (self.entries / "linked.md").symlink_to(outside)

        with self.assertRaises(VaultError):
            self.vault.safe_write_text(
                "entries/linked.md", self._owned("replacement"), overwrite=True
            )

        moved_entries = self.journal / "entries-real"
        self.entries.rename(moved_entries)
        self.entries.symlink_to(self.base, target_is_directory=True)
        try:
            with self.assertRaises(VaultError):
                self.vault.safe_write_text("entries/new.md", self._owned("data"))
        finally:
            self.entries.unlink()
            moved_entries.rename(self.entries)

        self.assertEqual(outside.read_text(encoding="utf-8"), "outside bytes")
        self.assertFalse((self.entries / "new.md").exists())

    def test_failed_atomic_creation_leaves_no_partial_target_or_temp_file(self) -> None:
        import lifecontext.vault as vault_module

        target = self.entries / "atomic.md"

        with (
            patch.object(self.vault, "writer_lock", wraps=self.vault.writer_lock) as lock,
            patch.object(vault_module.os, "link", side_effect=OSError("injected")),
        ):
            with self.assertRaises(VaultError):
                self.vault.safe_write_text(
                    "entries/atomic.md",
                    self._owned("complete content"),
                )

        lock.assert_called_once()
        self.assertFalse(target.exists())
        self.assertEqual(list(self.entries.glob(".atomic.md.tmp.*")), [])

    def test_external_edit_after_hash_read_is_preserved_as_a_conflict(self) -> None:
        import lifecontext.vault as vault_module

        original = self._owned("original projection")
        concurrent = self._owned("concurrent human edit")
        target = self.entries / "race.md"
        target.write_text(original, encoding="utf-8")
        target.chmod(0o600)
        original_hash = hashlib.sha256(original.encode("utf-8")).hexdigest()
        real_read = vault_module._read_target

        def edit_after_read(parent_fd, name):
            result = real_read(parent_fd, name)
            if name == "race.md":
                target.write_text(concurrent, encoding="utf-8")
                target.chmod(0o600)
            return result

        with patch.object(vault_module, "_read_target", side_effect=edit_after_read):
            with self.assertRaises(VaultError) as raised:
                self.vault.safe_write_text(
                    "entries/race.md",
                    self._owned("new projection"),
                    overwrite=True,
                    expected_hash=original_hash,
                )

        self.assertEqual(raised.exception.code, "conflict")
        self.assertEqual(target.read_text(encoding="utf-8"), concurrent)

    def test_parent_swap_during_link_cannot_redirect_write_outside_pinned_directory(self) -> None:
        import lifecontext.vault as vault_module

        outside = self.base / "outside"
        outside.mkdir(mode=0o700)
        sentinel = outside / "swapped.md"
        sentinel.write_text("outside sentinel", encoding="utf-8")
        target = self.entries / "swapped.md"
        moved_entries = self.journal / "entries-pinned"
        real_link = vault_module.os.link

        def swap_parent(src, dst, **kwargs):
            self.entries.rename(moved_entries)
            self.entries.symlink_to(outside, target_is_directory=True)
            try:
                return real_link(src, dst, **kwargs)
            finally:
                self.entries.unlink()
                moved_entries.rename(self.entries)

        with patch.object(vault_module.os, "link", side_effect=swap_parent):
            self.vault.safe_write_text(
                "entries/swapped.md", self._owned("new")
            )

        self.assertEqual(sentinel.read_text(encoding="utf-8"), "outside sentinel")
        self.assertEqual(target.read_text(encoding="utf-8"), self._owned("new"))

    def test_safe_remove_projection_requires_owned_content_and_exact_hash(self) -> None:
        source_checksum = "a" * 64
        content = (
            "---\n"
            "ownership_marker: lifeweave-journal\n"
            "source_id: src_remove\n"
            "source_version: 1\n"
            f"source_checksum: {source_checksum}\n"
            "---\n\nowned projection\n"
        )
        target = self.entries / "src_remove-v1.md"
        self.vault.safe_write_text("entries/src_remove-v1.md", content)
        expected_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        note = self.root / "plan.md"
        note.write_text("ordinary note", encoding="utf-8")

        with self.assertRaises(VaultError) as raised:
            self.vault.safe_remove_projection(
                "entries/src_remove-v1.md",
                expected_hash="0" * 64,
                expected_source_hash=source_checksum,
            )
        self.assertEqual(raised.exception.code, "conflict")
        self.assertEqual(target.read_text(encoding="utf-8"), content)

        self.assertTrue(
            self.vault.safe_remove_projection(
                "entries/src_remove-v1.md",
                expected_hash=expected_hash,
                expected_source_hash=source_checksum,
            )
        )
        self.assertFalse(target.exists())
        with self.assertRaises(VaultError) as missing:
            self.vault.safe_remove_projection(
                "entries/src_remove-v1.md",
                expected_hash=expected_hash,
                expected_source_hash=source_checksum,
            )
        self.assertEqual(missing.exception.code, "conflict")
        self.assertTrue(
            self.vault.safe_remove_projection(
                "entries/src_remove-v1.md",
                expected_hash=expected_hash,
                expected_source_hash=source_checksum,
                allow_missing=True,
            )
        )
        self.assertEqual(note.read_text(encoding="utf-8"), "ordinary note")


if __name__ == "__main__":
    unittest.main()
