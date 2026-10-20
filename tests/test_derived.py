"""Real-filesystem immutability and adversarial derived-store tests."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lifecontext.derived import DerivedConflict, DerivedError, DerivedStore
from lifecontext.paths import LifeContextPaths


class DerivedStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.paths = LifeContextPaths(self.home)
        self.store = DerivedStore(self.paths)
        self.payload = {"text": "héllo", "segments": [{"page": 1}], "warnings": []}

    def write(self):
        return self.store.write("source-1", 1, 1, self.payload)

    def test_write_read_reopen_replay(self) -> None:
        stored = self.write()
        self.assertEqual(stored.path, self.store.artifact_path("source-1", 1, 1))
        canonical = json.dumps(
            self.payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode()
        self.assertEqual(stored.sha256, hashlib.sha256(canonical).hexdigest())
        before = stored.path.stat()
        reopened = DerivedStore(self.paths)
        self.assertEqual(
            reopened.read("source-1", 1, 1, expected_sha256=stored.sha256), self.payload
        )
        self.assertEqual(
            reopened.write(
                "source-1", 1, 1, dict(reversed(list(self.payload.items())))
            ),
            stored,
        )
        self.assertEqual(stored.path.stat().st_ino, before.st_ino)
        self.assertEqual(stored.path.stat().st_mtime_ns, before.st_mtime_ns)
        self.assertFalse(self.paths.archive_dir.exists())
        self.assertEqual(list(stored.path.parent.iterdir()), [stored.path])

    def test_conflict_does_not_overwrite(self) -> None:
        stored = self.write()
        original = stored.path.read_bytes()
        with self.assertRaises(DerivedConflict):
            self.store.write("source-1", 1, 1, {"text": "different"})
        self.assertEqual(stored.path.read_bytes(), original)

    def test_independent_revision_and_version(self) -> None:
        self.write()
        self.store.write("source-1", 1, 2, {"text": "next extraction"})
        self.store.write("source-1", 2, 1, {"text": "next original"})
        self.assertEqual(
            self.store.list_inventory(),
            [("source-1", 1, 1), ("source-1", 1, 2), ("source-1", 2, 1)],
        )

    def test_empty_inventory(self) -> None:
        self.assertEqual(self.store.list_inventory(), [])
        self.assertFalse((self.paths.lifeweave_home / "derived").exists())

    def test_invalid_identities(self) -> None:
        for identity in ("../private", "x/y", "", ".hidden", "a" * 129, "secret\n"):
            with self.subTest(identity=identity), self.assertRaises(DerivedError):
                self.store.write(identity, 1, 1, self.payload)
        for number in (0, -1, True, 1.0, 2_147_483_648):
            with self.subTest(number=number):
                with self.assertRaises(DerivedError):
                    self.store.read("safe", number, 1)
                with self.assertRaises(DerivedError):
                    self.store.write("safe", 1, number, self.payload)
        self.assertFalse(self.paths.lifeweave_home.exists())

    def test_invalid_json_payloads(self) -> None:
        cyclic = {}
        cyclic["self"] = cyclic
        for payload in (
            [],
            {1: "value"},
            {"nested": {1: "value"}},
            {"n": float("nan")},
            {"n": float("inf")},
            {"raw": b"secret"},
            {"tuple": (1,)},
            cyclic,
        ):
            with (
                self.subTest(payload=type(payload).__name__),
                self.assertRaises(DerivedError),
            ):
                self.store.write("safe", 1, 1, payload)

    def test_bounded_payload_and_envelope(self) -> None:
        with patch("lifecontext.derived.MAX_ARTIFACT_BYTES", 100):
            with self.assertRaises(DerivedError):
                self.store.write("safe", 1, 1, {"text": "x" * 101})
            with self.assertRaises(DerivedError):
                self.store.write("safe", 1, 1, {})
        self.assertFalse(self.paths.lifeweave_home.exists())

    def test_checksum_tampering(self) -> None:
        stored = self.write()
        value = json.loads(stored.path.read_bytes())
        value["payload"]["text"] = "tampered"
        stored.path.write_text(json.dumps(value))
        with self.assertRaisesRegex(DerivedError, "checksum"):
            self.store.read("source-1", 1, 1)
        with self.assertRaises(DerivedError):
            self.store.list_inventory()

    def test_external_checksum_pin(self) -> None:
        self.write()
        with self.assertRaisesRegex(DerivedError, "checksum"):
            self.store.read("source-1", 1, 1, expected_sha256="0" * 64)

    def test_identity_tampering(self) -> None:
        stored = self.write()
        value = json.loads(stored.path.read_bytes())
        value["source_version"] = True
        stored.path.write_text(json.dumps(value))
        with self.assertRaisesRegex(DerivedError, "identity"):
            self.store.read("source-1", 1, 1)

    def test_noncanonical_and_duplicate_json_rejected(self) -> None:
        stored = self.write()
        original = stored.path.read_bytes()
        for raw in (original + b"\n", b'{"schema_version":9,' + original[1:]):
            stored.path.write_bytes(raw)
            with self.assertRaises(DerivedError):
                self.store.read("source-1", 1, 1)

    def test_file_permissions(self) -> None:
        stored = self.write()
        self.assertEqual(stored.path.stat().st_mode & 0o777, 0o600)
        stored.path.chmod(0o644)
        with self.assertRaises(DerivedError):
            self.store.read("source-1", 1, 1)
        self.assertEqual(stored.path.stat().st_mode & 0o777, 0o644)

    def test_directory_permissions(self) -> None:
        stored = self.write()
        for directory in (self.paths.lifeweave_home, *stored.path.parents[:3]):
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
            directory.chmod(0o755)
            with self.assertRaises(DerivedError):
                self.store.read("source-1", 1, 1)
            self.assertEqual(directory.stat().st_mode & 0o777, 0o755)
            directory.chmod(0o700)

    def test_symlink_file_preserved(self) -> None:
        stored = self.write()
        outside = self.home / "outside"
        outside.write_text("private outside data")
        stored.path.unlink()
        stored.path.symlink_to(outside)
        for action in (
            lambda: self.store.read("source-1", 1, 1),
            self.write,
            self.store.list_inventory,
        ):
            with self.assertRaises(DerivedError) as raised:
                action()
            self.assertNotIn(str(outside), str(raised.exception))
        self.assertTrue(stored.path.is_symlink())
        self.assertEqual(outside.read_text(), "private outside data")

    def test_symlink_directories_preserved(self) -> None:
        outside = self.home / "outside"
        outside.mkdir(mode=0o700)
        self.paths.lifeweave_home.mkdir(mode=0o700)
        (self.paths.lifeweave_home / "derived").symlink_to(
            outside, target_is_directory=True
        )
        with self.assertRaises(DerivedError):
            self.write()
        self.assertEqual(list(outside.iterdir()), [])
        self.assertTrue((self.paths.lifeweave_home / "derived").is_symlink())

    def test_symlink_home_and_ancestor_rejected(self) -> None:
        outside = self.home / "outside"
        outside.mkdir(mode=0o700)
        self.paths.lifeweave_home.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(DerivedError):
            self.write()
        alias = self.home / "alias"
        alias.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(DerivedError):
            DerivedStore(LifeContextPaths(alias)).write("safe", 1, 1, {})
        self.assertEqual(list(outside.iterdir()), [])

    def test_unexpected_inventory_entries_preserved(self) -> None:
        stored = self.write()
        unexpected = stored.path.parent / "unexpected.txt"
        unexpected.write_text("do not delete")
        with self.assertRaises(DerivedError):
            self.store.list_inventory()
        self.assertEqual(unexpected.read_text(), "do not delete")

    def test_inventory_limit(self) -> None:
        self.write()
        self.store.write("source-1", 1, 2, {})
        with self.assertRaises(DerivedError):
            self.store.list_inventory(limit=1)
        for limit in (0, True, 4097):
            with self.assertRaises(DerivedError):
                self.store.list_inventory(limit=limit)

    def test_oversized_file_rejected(self) -> None:
        stored = self.write()
        with (
            patch("lifecontext.derived.MAX_ARTIFACT_BYTES", 10),
            self.assertRaises(DerivedError),
        ):
            self.store.read("source-1", 1, 1)
        self.assertTrue(stored.path.exists())

    def test_directory_replacement_during_publication(self) -> None:
        stored = self.write()
        version = stored.path.parent
        moved = version.with_name("moved")
        outside = self.home / "outside"
        outside.mkdir(mode=0o700)
        real_link = os.link

        def replace_and_link(*args, **kwargs):
            version.rename(moved)
            version.symlink_to(outside, target_is_directory=True)
            return real_link(*args, **kwargs)

        with (
            patch("lifecontext.derived.os.link", side_effect=replace_and_link),
            self.assertRaisesRegex(DerivedError, "unsafe"),
        ):
            self.store.write("source-1", 1, 2, {"text": "new"})
        self.assertEqual(list(outside.iterdir()), [])
        self.assertTrue((moved / "r2.json").exists())
        self.assertTrue(version.is_symlink())

    def test_file_replacement_between_stat_and_open(self) -> None:
        stored = self.write()
        outside = self.home / "outside"
        outside.write_text("do not read")
        real_open = os.open

        def replace_and_open(name, flags, *args, **kwargs):
            if name == "r1.json":
                stored.path.unlink()
                stored.path.symlink_to(outside)
            return real_open(name, flags, *args, **kwargs)

        with (
            patch("lifecontext.custody.os.open", side_effect=replace_and_open),
            self.assertRaises(DerivedError),
        ):
            self.store.read("source-1", 1, 1)
        self.assertEqual(outside.read_text(), "do not read")
        self.assertTrue(stored.path.is_symlink())

    def test_staging_replacement_is_preserved(self) -> None:
        self.write()
        version = self.store.artifact_path("source-1", 1, 1).parent
        real_link = os.link
        replacement = []

        def replace_staging(name, destination, *args, **kwargs):
            staging = version / name
            staging.unlink()
            staging.symlink_to(self.home / "nonexistent-private-file")
            replacement.append(staging)
            return real_link(name, destination, *args, **kwargs)

        with (
            patch("lifecontext.derived.os.link", side_effect=replace_staging),
            self.assertRaises(DerivedError),
        ):
            self.store.write("source-1", 1, 2, {"text": "new"})
        self.assertTrue(replacement[0].is_symlink())
        self.assertTrue((version / "r2.json").is_symlink())

    def test_write_failure_cleans_only_owned_staging(self) -> None:
        self.write()
        version = self.store.artifact_path("source-1", 1, 1).parent
        real_write = os.write

        def failing_write(descriptor, raw):
            real_write(descriptor, raw[:3])
            raise OSError("private details must not leak")

        with (
            patch("lifecontext.derived.os.write", side_effect=failing_write),
            self.assertRaises(DerivedError) as raised,
        ):
            self.store.write("source-1", 1, 2, {"text": "new"})
        self.assertNotIn("private details", str(raised.exception))
        self.assertEqual([entry.name for entry in version.iterdir()], ["r1.json"])

    def test_foreign_owner_rejected_without_chmod(self) -> None:
        stored = self.write()
        mode = stored.path.stat().st_mode
        with (
            patch("lifecontext.custody.os.geteuid", return_value=os.geteuid() + 1),
            self.assertRaises(DerivedError),
        ):
            self.store.read("source-1", 1, 1)
        self.assertEqual(stored.path.stat().st_mode, mode)

    def test_simultaneous_identical_publication(self) -> None:
        real_link = os.link
        entered = False

        def competing_link(*args, **kwargs):
            nonlocal entered
            if not entered:
                entered = True
                self.store.write("source-1", 1, 1, self.payload)
            return real_link(*args, **kwargs)

        with patch("lifecontext.derived.os.link", side_effect=competing_link):
            stored = self.write()
        self.assertEqual(self.store.read("source-1", 1, 1), self.payload)
        self.assertEqual(list(stored.path.parent.iterdir()), [stored.path])

    def test_simultaneous_conflicting_publication(self) -> None:
        real_link = os.link
        entered = False

        def competing_link(*args, **kwargs):
            nonlocal entered
            if not entered:
                entered = True
                self.store.write("source-1", 1, 1, {"text": "winner"})
            return real_link(*args, **kwargs)

        with (
            patch("lifecontext.derived.os.link", side_effect=competing_link),
            self.assertRaises(DerivedConflict),
        ):
            self.write()
        self.assertEqual(self.store.read("source-1", 1, 1), {"text": "winner"})
        self.assertEqual(
            len(list(self.store.artifact_path("source-1", 1, 1).parent.iterdir())), 1
        )


if __name__ == "__main__":
    unittest.main()
