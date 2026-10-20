"""`lifecontext backup` and `restore` with synthetic homes (no Hindsight or Postgres runs)."""

from __future__ import annotations

import contextlib
import errno
import fcntl
import hashlib
import io
import json
import os
import socket
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from local_paths import scratch_root

from lifecontext import backup as backup_module
from lifecontext import cli
from lifecontext.api.security import local_token
from lifecontext.backup import BackupError, backup, restore, stop_hindsight, verify
from lifecontext.catalog import LifeContextCatalog
from lifecontext.config import load_settings
from lifecontext.hindsight import engine_token
from lifecontext.initialize import InitOptions, run_init
from lifecontext.paths import LifeContextPaths

V1_ROOT = Path(__file__).resolve().parents[1]
KEY = "sk-backup-private-key"
PG_PASSWORD = "a1b2c3d4" * 8


def NEVER(port: int) -> bool:
    return False


@contextmanager
def _homes(count: int = 2):
    with tempfile.TemporaryDirectory(prefix="lifecontext-backup-test.", dir=scratch_root()) as temporary:
        root = Path(temporary).resolve(strict=True)
        homes = [root / f"home{number}" for number in range(count)]
        for home in homes:
            home.mkdir()
        yield root, homes


@contextmanager
def _as(home: Path):
    with patch.dict(os.environ, {"HOME": str(home)}):
        yield LifeContextPaths.current()


def _populate(home: Path) -> LifeContextPaths:
    with _as(home) as paths:
        run_init(InitOptions(timezone="Europe/Vienna", api_key_stdin=True), key_stream=io.BytesIO(KEY.encode()))
        LifeContextCatalog(paths).close()
        local_token(paths, create=True)
        engine_token(paths, create=True)
        paths.postgres_socket_dir.mkdir(mode=0o700)
        (paths.postgres_socket_dir / ".s.PGSQL.5433.lock").write_text("stale")
        original = paths.archive_dir / "sources" / "src1" / "v000001" / "original.bin"
        original.parent.mkdir(parents=True)
        original.write_bytes(b"ORIGINAL BYTES")
        (paths.lifeweave_home / "wiki_revisions" / "source_src1").mkdir(parents=True)
        (paths.lifeweave_home / "wiki_revisions" / "source_src1" / "r1.json").write_text("{}")
        paths.tiktoken_cache_dir.mkdir(parents=True)
        (paths.tiktoken_cache_dir / "abc").write_bytes(b"tokenizer")
        instance = home / ".pg0" / "instances" / "hindsight-embed-lifeweave"
        (instance / "data" / "base").mkdir(parents=True)
        os.chmod(instance / "data", 0o700)
        (instance / "data" / "PG_VERSION").write_text("18\n")
        (instance / "data" / "base" / "16384").write_bytes(os.urandom(4096))
        (instance / "instance.json").write_text(json.dumps({
            "data_dir": str(instance / "data"), "installation_dir": str(home / ".pg0" / "installation"),
            "port": 5433, "pid": None, "username": "hindsight", "password": PG_PASSWORD, "database": "hindsight",
            "version": "18.1.0"}))
        vault = home / "LifeContext" / "vault"
        (vault / "Journal" / "entries").mkdir(parents=True, exist_ok=True)
        os.chmod(vault / "Journal" / "entries", 0o700)
        (vault / "Journal" / "entries" / "2026-10-17 - Lake.md").write_text("Walked to the lake.")
        (vault / ".journal.lock").write_text("")
        (vault / "link-to-elsewhere").symlink_to("/etc/hostname")
        return paths


def _craft(source: Path, target: Path, *, edit=None, extra=(), replace=None) -> Path:
    """A copy of an archive with an edited manifest, replaced member bytes and extra members."""
    replace = replace or {}
    with tarfile.open(source) as original:
        members = [(member, original.extractfile(member).read() if member.isreg() else None)
                   for member in original]
    manifest = json.loads({member.name: data for member, data in members}["manifest.json"])
    for entry in manifest["entries"]:
        if entry["path"] in replace:
            entry["size"] = len(replace[entry["path"]])
            entry["sha256"] = hashlib.sha256(replace[entry["path"]]).hexdigest()
    if edit is not None:
        edit(manifest)
    with tarfile.open(target, "w:gz") as copy:
        for member, data in [*members, *extra]:
            if member.name == "manifest.json":
                continue
            if member.name in replace:
                data = replace[member.name]
                member.size = len(data)
            copy.addfile(member, io.BytesIO(data) if data is not None else None)
        raw = json.dumps(manifest).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size = len(raw)
        copy.addfile(info, io.BytesIO(raw))
    return target


def _member(name: str, data: bytes | None = b"x", kind: bytes = tarfile.REGTYPE, link: str = ""):
    info = tarfile.TarInfo(name)
    info.type, info.linkname, info.mode = kind, link, 0o644
    info.size = len(data) if data is not None and kind == tarfile.REGTYPE else 0
    return info, data if kind == tarfile.REGTYPE else None


def _private_vault(source: Path) -> dict[str, tuple[int, bytes | None]]:
    """The source vault as restore writes it: notes 0600, folders 0700, no lock file."""
    expected = _tree(source / "LifeContext" / "vault")
    expected.pop(".journal.lock", None)
    return {name: (0o600 if data is not None else 0o700, data) for name, (_mode, data) in expected.items()}


def _tree(root: Path) -> dict[str, tuple[int, bytes | None]]:
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            continue
        relative = str(path.relative_to(root))
        result[relative] = (stat.S_IMODE(path.stat().st_mode), path.read_bytes() if path.is_file() else None)
    return result


class BackupTests(unittest.TestCase):
    def test_backup_excludes_secrets_and_restore_rebuilds_the_home_elsewhere(self) -> None:
        with _homes() as (root, (source, target)):
            _populate(source)
            with _as(source):
                report = backup(destination=str(root), port=1, probe=NEVER)
            archive = Path(report["archive"])
            self.assertEqual(stat.S_IMODE(archive.stat().st_mode), 0o600)
            self.assertTrue(archive.with_name(archive.name + ".sha256").is_file())
            self.assertEqual(report["skipped_links"], 1)
            names = {entry["path"] for entry in verify(archive)["entries"]}
            for absent in ("home/.lifeweave/.env", "home/.lifeweave/api.token", "home/.lifeweave/cache",
                           "home/.lifeweave/hindsight-api.token", "home/.lifeweave/postgres",
                           "home/.lifeweave/postgres/.s.PGSQL.5433.lock",
                           "home/.lifeweave/service.lock", "vault/.journal.lock"):
                self.assertNotIn(absent, names)
            with _as(source) as source_paths:
                engine = engine_token(source_paths)
            for present in ("home/.lifeweave/catalog.sqlite3", "home/.lifeweave/config.yaml",
                            "home/.lifeweave/archive/sources/src1/v000001/original.bin",
                            "home/.pg0/instances/hindsight-embed-lifeweave/data/base/16384",
                            "vault/Journal/entries/2026-10-17 - Lake.md"):
                self.assertIn(present, names)
            with tarfile.open(archive) as opened:
                content = b"".join(opened.extractfile(member).read() for member in opened if member.isreg())
            self.assertNotIn(KEY.encode(), content)
            self.assertNotIn(engine.encode(), content)

            with _as(target) as paths:
                restored = restore(str(archive), port=1, probe=NEVER)
                settings = load_settings()
                self.assertFalse(paths.hindsight_api_token_path.exists())
                self.assertFalse(paths.postgres_socket_dir.exists())
            self.assertEqual(settings["vault"]["root"], str(target / "LifeContext" / "vault"))
            self.assertEqual(restored["vault_root"], str(target / "LifeContext" / "vault"))
            self.assertFalse(paths.credentials_path.exists())
            self.assertFalse((paths.lifeweave_home / "api.token").exists())
            source_state, target_state = _tree(source / ".lifeweave"), _tree(target / ".lifeweave")
            for name in ("config.yaml", ".env", "api.token", "cache", "cache/tiktoken", "cache/tiktoken/abc",
                         "service.lock", "hindsight-api.token", "postgres", "postgres/.s.PGSQL.5433.lock"):
                source_state.pop(name, None)
                target_state.pop(name, None)
            self.assertEqual(source_state, target_state)
            self.assertEqual(_tree(target / ".pg0/instances/hindsight-embed-lifeweave/data"),
                             _tree(source / ".pg0/instances/hindsight-embed-lifeweave/data"))
            self.assertEqual(_tree(target / "LifeContext/vault"), _private_vault(source))
            self.assertEqual(stat.S_IMODE((target / ".lifeweave").stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((target / "LifeContext" / "vault").stat().st_mode), 0o700)
            instance = json.loads((target / ".pg0/instances/hindsight-embed-lifeweave/instance.json").read_text())
            self.assertEqual(instance["data_dir"], str(target / ".pg0/instances/hindsight-embed-lifeweave/data"))
            self.assertEqual(instance["installation_dir"], str(target / ".pg0" / "installation"))
            # The restored database keeps its password; the socket-only Postgres reads it from here.
            self.assertEqual(instance["password"], PG_PASSWORD)
            with _as(target) as paths:
                from lifecontext.hindsight_postgres import read_instance
                self.assertEqual(read_instance(paths).password, PG_PASSWORD)
            self.assertEqual([path.name for path in target.iterdir() if path.name.startswith(".lifecontext-restore")], [])

    def test_restore_refuses_a_home_with_lifecontext_state(self) -> None:
        with _homes() as (root, (source, target)):
            _populate(source)
            with _as(source):
                archive = backup(destination=str(root), port=1, probe=NEVER)["archive"]
            (target / ".lifeweave").mkdir(mode=0o700)
            with _as(target), self.assertRaises(BackupError) as caught:
                restore(archive, port=1, probe=NEVER)
            self.assertEqual(caught.exception.code, "home_not_empty")
            self.assertEqual(sorted(path.name for path in target.iterdir()), [".lifeweave"])

    def test_a_changed_member_fails_verification_before_anything_is_written(self) -> None:
        with _homes() as (root, (source, target)):
            _populate(source)
            with _as(source):
                archive = Path(backup(destination=str(root), port=1, probe=NEVER)["archive"])
            tampered = root / "tampered.tar.gz"
            with tarfile.open(archive) as original, tarfile.open(tampered, "w:gz") as copy:
                for member in original:
                    data = original.extractfile(member).read() if member.isreg() else None
                    if member.name.endswith("Lake.md"):
                        data = data.replace(b"lake", b"LAKE")
                    copy.addfile(member, io.BytesIO(data) if data is not None else None)
            with _as(target), self.assertRaises(BackupError) as caught:
                restore(str(tampered), port=1, probe=NEVER)
            self.assertEqual(caught.exception.code, "archive_checksum_mismatch")
            self.assertEqual(list(target.iterdir()), [])
            archive.with_name(archive.name + ".sha256").write_text("0" * 64 + "  x\n")
            with _as(target), self.assertRaises(BackupError) as caught:
                restore(str(archive), port=1, probe=NEVER)
            self.assertEqual(caught.exception.code, "archive_checksum_mismatch")

    def test_backup_refuses_while_the_service_or_postgres_runs(self) -> None:
        with _homes(1) as (root, (source,)):
            paths = _populate(source)
            with _as(source):
                with self.assertRaises(BackupError) as caught:
                    backup(destination=str(root), port=1, probe=lambda port: True)
                self.assertEqual(caught.exception.code, "service_running")
                descriptor = os.open(paths.service_lock_path, os.O_RDWR | os.O_CREAT, 0o600)
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX)
                    with self.assertRaises(BackupError) as caught:
                        backup(destination=str(root), port=1, probe=NEVER)
                    self.assertEqual(caught.exception.code, "service_running")
                finally:
                    os.close(descriptor)
                pid_file = source / ".pg0/instances/hindsight-embed-lifeweave/data/postmaster.pid"
                pid_file.write_text(f"{os.getpid()}\n/data\n")
                with self.assertRaises(BackupError) as caught:
                    backup(destination=str(root), port=1, probe=NEVER)
                self.assertEqual(caught.exception.code, "hindsight_running")
                pid_file.unlink()
                with self.assertRaises(BackupError) as caught:
                    backup(destination=str(source / "LifeContext" / "vault"), port=1, probe=NEVER)
                self.assertEqual(caught.exception.code, "destination_inside_backup")
            self.assertEqual([path for path in root.iterdir() if path.name.endswith(".tar.gz")], [])

    def test_a_newer_catalog_schema_is_refused(self) -> None:
        with _homes() as (root, (source, target)):
            paths = _populate(source)
            with sqlite3.connect(paths.catalog_path) as connection:
                connection.execute("PRAGMA user_version=99")
            with _as(source):
                archive = backup(destination=str(root), port=1, probe=NEVER)["archive"]
            with _as(target), self.assertRaises(BackupError) as caught:
                restore(archive, port=1, probe=NEVER)
            self.assertEqual(caught.exception.code, "schema_unsupported")
            self.assertEqual(list(target.iterdir()), [])

    def test_commands_back_up_and_restore_through_the_cli(self) -> None:
        with _homes() as (root, (source, target)):
            _populate(source)
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = str(probe.getsockname()[1])

            def run(home, *arguments):
                env = {"HOME": str(home), "PATH": os.defpath, "PYTHONPATH": str(V1_ROOT / "src")}
                return subprocess.run([sys.executable, "-m", "lifecontext.cli", *arguments, "--port", port, "--json"],
                                      env=env, cwd=home, capture_output=True, text=True, timeout=120, check=False)

            made = run(source, "backup", str(root))
            self.assertEqual(made.returncode, 0, made.stderr)
            archive = json.loads(made.stdout)["archive"]
            again = run(target, "restore", archive)
            self.assertEqual(again.returncode, 0, again.stderr)
            self.assertEqual(json.loads(again.stdout)["vault_root"], str(target / "LifeContext" / "vault"))
            refused = run(target, "restore", archive)
            self.assertEqual((refused.returncode, json.loads(refused.stdout)), (1, {"error": "home_not_empty"}))

    def test_backup_copies_where_hard_links_are_unsupported(self) -> None:
        with _homes(1) as (root, (source,)):
            _populate(source)
            with _as(source), patch.object(backup_module.os, "link", side_effect=PermissionError(errno.EPERM, "")):
                report = backup(destination=str(root), port=1, probe=NEVER)
            archive = Path(report["archive"])
            self.assertEqual(stat.S_IMODE(archive.stat().st_mode), 0o600)
            self.assertEqual(verify(archive)["format"], "lifecontext-backup")
            self.assertEqual([path.name for path in root.iterdir() if path.name.startswith(".lifecontext-backup")],
                             [])


class CraftedArchiveTests(unittest.TestCase):
    """Archives are trusted input, but restore still keeps every write inside the HOME or --vault."""

    def _archive(self, root: Path, source: Path) -> Path:
        _populate(source)
        with _as(source):
            return Path(backup(destination=str(root), port=1, probe=NEVER)["archive"])

    def _refused(self, archive: Path, home: Path, code: str, **options) -> None:
        with _as(home), self.assertRaises(BackupError) as caught:
            restore(str(archive), port=1, probe=NEVER, **options)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(list(home.iterdir()), [])

    def test_member_names_links_and_secrets_are_refused(self) -> None:
        with _homes() as (root, (source, target)):
            archive = self._archive(root, source)
            members = {
                "dot-dot": _member("../planted/evil.service"),
                "absolute": _member(str(root / "planted" / "evil.service")),
                "dot-dot-inside": _member("vault/../../planted/evil.service"),
                "dot": _member("vault/./Journal/x.md"),
                "other-root": _member("planted/evil.service"),
                "env": _member("home/.lifeweave/.env", b"LIFEWEAVE_LLM_API_KEY=attacker\n"),
                "token": _member("home/.lifeweave/api.token", b"known"),
                "env-upper": _member("home/.lifeweave/.ENV", b"LIFEWEAVE_LLM_API_KEY=attacker\n"),
                "token-upper": _member("home/.lifeweave/API.TOKEN", b"known"),
                "engine-token": _member("home/.lifeweave/hindsight-api.token", b"f" * 64),
                "engine-token-upper": _member("home/.lifeweave/Hindsight-API.token", b"f" * 64),
                "symlink": _member("vault/link", None, tarfile.SYMTYPE, str(root / "planted")),
                "hardlink": _member("vault/hard", None, tarfile.LNKTYPE, "vault/Journal/entries/2026-10-17 - Lake.md"),
                "device": _member("vault/fifo", None, tarfile.FIFOTYPE),
            }
            for label, member in members.items():
                with self.subTest(member=label):
                    crafted = _craft(archive, root / f"{label}.tar.gz", extra=[member])
                    self._refused(crafted, target, "archive_invalid")
                    self.assertFalse((root / "planted").exists())

    def test_vault_root_outside_the_home_needs_an_explicit_vault(self) -> None:
        with _homes() as (root, (source, target)):
            archive = self._archive(root, source)

            def vault(path: str, journal: str | None = None):
                def edit(manifest):
                    manifest["vault"] = {"root": path, "journal_path": journal or path + "/Journal"}
                return edit

            planted = str(root / "planted")
            cases = {
                "outside": (vault(planted), "vault_outside_home"),
                "relative": (vault("planted"), "archive_invalid"),
                "climbing": (vault(str(source) + "/../planted"), "archive_invalid"),
                "journal-climbs": (vault(str(source / "LifeContext/vault"),
                                         str(source / "LifeContext/vault") + "/../../../planted"), "archive_invalid"),
                "journal-outside": (vault(str(source / "LifeContext/vault"), planted), "archive_invalid"),
                "autostart": (vault(str(source / ".config/systemd/user")), "vault_location_refused"),
                "launch-agents": (vault(str(source / "Library/LaunchAgents")), "vault_location_refused"),
                "launch-agents-lower": (vault(str(source / "library/LaunchAgents")), "vault_location_refused"),
                "path-bin": (vault(str(source / "bin")), "vault_location_refused"),
                "lifecontext-itself": (vault(str(source / "LifeContext")), "vault_location_refused"),
                "lifecontext-lower": (vault(str(source / "lifecontext/vault")), "vault_location_refused"),
                "home-itself": (vault(str(source)), "vault_outside_home"),
            }
            for label, (edit, code) in cases.items():
                with self.subTest(case=label):
                    crafted = _craft(archive, root / f"{label}.tar.gz", edit=edit)
                    self._refused(crafted, target, code)
                    self.assertFalse((root / "planted").exists())
            crafted = _craft(archive, root / "explicit.tar.gz", edit=vault(planted))
            with _as(target):
                restored = restore(str(crafted), port=1, probe=NEVER, vault=str(target / "Vault"))
                settings = load_settings()
            self.assertEqual(restored["vault_root"], str(target / "Vault"))
            self.assertEqual(settings["vault"]["journal_path"], str(target / "Vault" / "Journal"))
            self.assertTrue((target / "Vault" / "Journal" / "entries" / "2026-10-17 - Lake.md").is_file())
            self.assertFalse((root / "planted").exists())

    def test_malformed_manifest_config_and_instance_have_stable_codes(self) -> None:
        with _homes() as (root, (source, target)):
            archive = self._archive(root, source)

            def entries(value):
                return lambda manifest: manifest.update(entries=value)

            def first_mode(manifest):
                manifest["entries"][0]["mode"] = "rwx"

            for label, edit in {"entries": entries("x"), "entry": entries([1]), "mode": first_mode,
                                "home": lambda manifest: manifest.update(source_home=7)}.items():
                with self.subTest(manifest=label):
                    self._refused(_craft(archive, root / f"m-{label}.tar.gz", edit=edit), target, "archive_invalid")
            for label, name, data, code in (
                    ("yaml", "home/.lifeweave/config.yaml", b"llm: [unclosed\n", "archive_config_invalid"),
                    ("yaml-list", "home/.lifeweave/config.yaml", b"- 1\n", "archive_config_invalid"),
                    ("instance", "home/.pg0/instances/hindsight-embed-lifeweave/instance.json", b"{not json",
                     "archive_instance_invalid"),
                    ("instance-list", "home/.pg0/instances/hindsight-embed-lifeweave/instance.json", b"[]",
                     "archive_instance_invalid")):
                with self.subTest(member=label):
                    crafted = _craft(archive, root / f"r-{label}.tar.gz", replace={name: data})
                    self._refused(crafted, target, code)

    def test_restored_roots_are_private_whatever_the_archive_says(self) -> None:
        with _homes() as (root, (source, target)):
            archive = self._archive(root, source)

            def loosen(manifest):
                for entry in manifest["entries"]:
                    if entry["path"] in {"home/.lifeweave", "vault", "home/.pg0/instances/hindsight-embed-lifeweave"}:
                        entry["mode"] = "0777"
                    elif entry["path"].startswith("vault/"):
                        entry["mode"] = "0755" if entry["type"] == "dir" else "4755"

            crafted = _craft(archive, root / "loose.tar.gz", edit=loosen)
            with _as(target):
                restore(str(crafted), port=1, probe=NEVER)
            for relative in (".lifeweave", ".pg0", ".pg0/instances", ".pg0/instances/hindsight-embed-lifeweave",
                             "LifeContext/vault", "LifeContext/vault/Journal/entries"):
                self.assertEqual(stat.S_IMODE((target / relative).stat().st_mode), 0o700, relative)
            # Nothing restored into the vault can be executed.
            lake = target / "LifeContext/vault/Journal/entries/2026-10-17 - Lake.md"
            self.assertEqual(stat.S_IMODE(lake.stat().st_mode), 0o600)

    def test_vault_location_is_judged_after_resolving_links(self) -> None:
        with _homes() as (root, (source, target)):
            archive = self._archive(root, source)
            (target / ".local" / "bin").mkdir(parents=True)
            (target / "LifeContext").symlink_to(target / ".local")
            crafted = _craft(archive, root / "linked.tar.gz", edit=lambda manifest: manifest.update(
                vault={"root": str(source / "LifeContext" / "bin"),
                       "journal_path": str(source / "LifeContext" / "bin" / "Journal")}))
            with _as(target), self.assertRaises(BackupError) as caught:
                restore(str(crafted), port=1, probe=NEVER)
            self.assertEqual(caught.exception.code, "vault_location_refused")
            self.assertEqual(list((target / ".local" / "bin").iterdir()), [])
            self.assertEqual(sorted(path.name for path in target.iterdir()), [".local", "LifeContext"])

    def test_a_home_reached_through_a_link_round_trips(self) -> None:
        with _homes(4) as (root, (real_source, real_target, links, _spare)):
            source, target = links / "source", links / "target"
            source.symlink_to(real_source)
            target.symlink_to(real_target)
            _populate(source)
            with _as(source):
                archive = backup(destination=str(root), port=1, probe=NEVER)["archive"]
            manifest = verify(Path(archive))
            self.assertEqual((manifest["source_home"], manifest["source_home_as_given"]),
                             (str(real_source), str(source)))
            self.assertEqual(manifest["vault"]["root"], str(real_source / "LifeContext" / "vault"))
            with _as(target):
                restored = restore(archive, port=1, probe=NEVER)
                settings = load_settings()
            self.assertEqual(restored["vault_root"], str(real_target / "LifeContext" / "vault"))
            self.assertEqual(settings["vault"]["root"], str(real_target / "LifeContext" / "vault"))
            instance = json.loads((real_target / ".pg0/instances/hindsight-embed-lifeweave/instance.json").read_text())
            self.assertEqual(instance["data_dir"], str(target / ".pg0/instances/hindsight-embed-lifeweave/data"))


class VaultMountTests(unittest.TestCase):
    """The vault as its own volume: an existing empty directory, maybe on another filesystem."""

    def _archive(self, root: Path, source: Path) -> Path:
        _populate(source)
        with _as(source):
            return Path(backup(destination=str(root), port=1, probe=NEVER)["archive"])

    def _expected(self, source: Path) -> dict:
        return _private_vault(source)

    def test_an_empty_existing_vault_directory_is_filled(self) -> None:
        with _homes(3) as (root, (source, target, volumes)):
            archive = self._archive(root, source)
            mount = volumes / "vault"
            mount.mkdir(mode=0o755)
            with _as(target):
                report = restore(str(archive), port=1, probe=NEVER, vault=str(mount))
                settings = load_settings()
            self.assertEqual(report["vault_root"], str(mount))
            self.assertEqual(settings["vault"]["root"], str(mount))
            self.assertEqual(_tree(mount), self._expected(source))
            self.assertEqual(stat.S_IMODE(mount.stat().st_mode), 0o700)

    def test_a_vault_on_another_filesystem_is_copied_and_checked(self) -> None:
        real_rename = os.rename

        def cross_device(source, target, *args, **kwargs):
            if Path(source).name == "vault":
                raise OSError(errno.EXDEV, "cross-device")
            return real_rename(source, target, *args, **kwargs)

        for existing in (False, True):
            with self.subTest(existing=existing), _homes(3) as (root, (source, target, volumes)):
                archive = self._archive(root, source)
                mount = volumes / "vault"
                if existing:
                    mount.mkdir(mode=0o700)
                with _as(target), patch.object(backup_module.os, "rename", side_effect=cross_device), \
                        patch.object(backup_module.os, "link", side_effect=OSError(errno.EXDEV, "")):
                    restore(str(archive), port=1, probe=NEVER, vault=str(mount))
                self.assertEqual(_tree(mount), self._expected(source))
                self.assertEqual([path.name for path in volumes.iterdir()], ["vault"])

    def test_a_non_empty_foreign_or_linked_target_is_refused(self) -> None:
        with _homes(3) as (root, (source, target, volumes)):
            archive = self._archive(root, source)
            full = volumes / "full"
            full.mkdir()
            (full / "note.md").write_text("mine")
            linked = volumes / "linked"
            (volumes / "empty").mkdir()
            linked.symlink_to(volumes / "empty")
            for vault, code in ((full, "vault_not_empty"), (linked, "unsafe_vault")):
                with self.subTest(vault=vault.name), _as(target), self.assertRaises(BackupError) as caught:
                    restore(str(archive), port=1, probe=NEVER, vault=str(vault))
                self.assertEqual(caught.exception.code, code)
            self.assertEqual([path.name for path in full.iterdir()], ["note.md"])
            self.assertEqual(list((volumes / "empty").iterdir()), [])
            foreign = os.geteuid() + 1
            with _as(target), patch.object(backup_module.os, "geteuid", return_value=foreign), \
                    self.assertRaises(BackupError) as caught:
                restore(str(archive), port=1, probe=NEVER, vault=str(volumes / "empty"))
            self.assertEqual(caught.exception.code, "unsafe_vault")
            self.assertEqual(list(target.iterdir()), [])

    def test_a_failed_restore_leaves_the_existing_vault_empty(self) -> None:
        real_extract = backup_module._extract

        def tampered(path, staging, manifest):
            real_extract(path, staging, manifest)
            lake = staging / "vault" / "Journal" / "entries" / "2026-10-17 - Lake.md"
            lake.write_text("changed after the check")

        with _homes(3) as (root, (source, target, volumes)):
            archive = self._archive(root, source)
            mount = volumes / "vault"
            mount.mkdir(mode=0o700)
            with self.subTest(failure="later move"), _as(target), \
                    patch.object(backup_module, "_move", side_effect=OSError(errno.EIO, "")), \
                    self.assertRaises(OSError):
                restore(str(archive), port=1, probe=NEVER, vault=str(mount))
            self.assertEqual(list(mount.iterdir()), [])
            self.assertEqual(list(target.iterdir()), [])
            with self.subTest(failure="changed bytes"), _as(target), \
                    patch.object(backup_module, "_extract", side_effect=tampered), \
                    patch.object(backup_module.os, "link", side_effect=OSError(errno.EXDEV, "")), \
                    self.assertRaises(BackupError) as caught:
                restore(str(archive), port=1, probe=NEVER, vault=str(mount))
            self.assertEqual(caught.exception.code, "archive_changed")
            self.assertEqual(list(mount.iterdir()), [])
            self.assertEqual(list(target.iterdir()), [])


class _Writers:
    """Stands in for the user service and the Hindsight profile; records what was asked."""

    def __init__(self, instance: Path, *, units=("lifecontext.service",), start_fails: bool = False,
                 stop_fails: bool = False):
        self.instance, self.units = instance, list(units)
        self.start_fails, self.stop_fails = start_fails, stop_fails
        self.api_up = bool(units)
        self.calls: list = []

    def probe(self, port: int) -> bool:
        return self.api_up

    def active_service(self) -> list[str]:
        self.calls.append("active_service")
        return list(self.units)

    def stop_service(self, units: list[str]) -> None:
        self.calls.append(("stop_service", units))
        if self.stop_fails:
            raise BackupError("systemctl_failed")
        self.api_up = False

    def start_service(self, units: list[str]) -> None:
        self.calls.append(("start_service", units))
        if self.start_fails:
            raise BackupError("systemctl_failed")

    def stop_hindsight(self, paths: LifeContextPaths) -> str:
        self.calls.append("stop_hindsight")
        (self.instance / "data" / "postmaster.pid").unlink(missing_ok=True)
        return "stopped"


class StopServiceTests(unittest.TestCase):
    def _running(self, source: Path) -> Path:
        _populate(source)
        instance = source / ".pg0/instances/hindsight-embed-lifeweave"
        (instance / "data" / "postmaster.pid").write_text(f"{os.getpid()}\n/data\n")
        return instance

    def test_stop_service_stops_backs_up_and_starts_again(self) -> None:
        with _homes(1) as (root, (source,)):
            writers = _Writers(self._running(source))
            with _as(source):
                report = backup(destination=str(root), port=1, probe=writers.probe, stop_service=True,
                                writers=writers)
            self.assertEqual(writers.calls, ["active_service", ("stop_service", ["lifecontext.service"]),
                                             "stop_hindsight", ("start_service", ["lifecontext.service"])])
            self.assertEqual(report["stopped"], {"service": ["lifecontext.service"], "hindsight": "stopped"})
            self.assertEqual((report["restarted"], report["warnings"]), (["lifecontext.service"], []))
            verify(Path(report["archive"]))
            self.assertIn("Started again: lifecontext.service", backup_module.render(report))

    def test_a_service_that_does_not_start_again_is_a_warning(self) -> None:
        with _homes(1) as (root, (source,)):
            writers = _Writers(self._running(source), start_fails=True)
            with _as(source):
                report = backup(destination=str(root), port=1, probe=writers.probe, stop_service=True,
                                writers=writers)
            self.assertTrue(Path(report["archive"]).is_file())
            self.assertEqual((report["restarted"], report["warnings"]), ([], ["service_restart_failed"]))
            self.assertIn("did not start again", backup_module.render(report))

    def test_a_foreground_serve_and_a_bad_destination_stop_nothing(self) -> None:
        with _homes(1) as (root, (source,)):
            writers = _Writers(self._running(source), units=())
            writers.api_up = True
            with _as(source), self.assertRaises(BackupError) as caught:
                backup(destination=str(root), port=1, probe=writers.probe, stop_service=True, writers=writers)
            self.assertEqual(caught.exception.code, "service_running")
            self.assertEqual(writers.calls, ["active_service"])
            writers = _Writers(source / ".pg0/instances/hindsight-embed-lifeweave")
            with _as(source), self.assertRaises(BackupError) as caught:
                backup(destination=str(source / "LifeContext" / "vault"), port=1, probe=writers.probe,
                       stop_service=True, writers=writers)
            self.assertEqual(caught.exception.code, "destination_inside_backup")
            self.assertEqual(writers.calls, [])

    def test_a_failed_backup_still_starts_the_service_again(self) -> None:
        with _homes(1) as (root, (source,)):
            instance = self._running(source)
            writers = _Writers(instance)
            writers.stop_hindsight = lambda paths: writers.calls.append("stop_hindsight") or "stopped"
            with _as(source), patch.object(backup_module, "_STOP_TIMEOUT", 0.1), \
                    self.assertRaises(BackupError) as caught:
                backup(destination=str(root), port=1, probe=writers.probe, stop_service=True, writers=writers)
            self.assertEqual(caught.exception.code, "hindsight_running")
            self.assertEqual(writers.calls[-1], ("start_service", ["lifecontext.service"]))
            self.assertEqual([path for path in root.iterdir() if path.name.endswith(".tar.gz")], [])

    def test_a_failing_stop_or_an_interrupt_still_starts_the_service_again(self) -> None:
        for failure in ("stop", "interrupt"):
            with self.subTest(failure=failure), _homes(1) as (root, (source,)):
                writers = _Writers(self._running(source), stop_fails=failure == "stop")
                if failure == "interrupt":
                    def interrupted(paths):
                        raise KeyboardInterrupt
                    writers.stop_hindsight = interrupted
                expected = BackupError if failure == "stop" else KeyboardInterrupt
                with _as(source), self.assertRaises(expected):
                    backup(destination=str(root), port=1, probe=writers.probe, stop_service=True, writers=writers)
                self.assertEqual(writers.calls[-1], ("start_service", ["lifecontext.service"]))
                self.assertEqual([path for path in root.iterdir() if path.name.endswith(".tar.gz")], [])

    def test_command_exits_1_when_the_service_does_not_start_again(self) -> None:
        with _homes(1) as (root, (source,)):
            writers = _Writers(self._running(source), units=(), start_fails=True)
            output = io.StringIO()
            with _as(source), patch.object(backup_module, "_Writers", return_value=writers), \
                    contextlib.redirect_stdout(output):
                code = cli.main(["backup", str(root), "--stop-service", "--json", "--port", "1"])
            report = json.loads(output.getvalue())
            self.assertEqual((code, report["warnings"], report["stopped"]["hindsight"]), (0, [], "stopped"))
            writers = _Writers(source / ".pg0/instances/hindsight-embed-lifeweave", start_fails=True)
            output = io.StringIO()
            with _as(source), patch.object(backup_module, "_Writers", return_value=writers), \
                    contextlib.redirect_stdout(output):
                code = cli.main(["backup", str(root / "second.tar.gz"), "--stop-service", "--json", "--port", "1"])
            self.assertEqual((code, json.loads(output.getvalue())["warnings"]), (1, ["service_restart_failed"]))


class StopHindsightTests(unittest.TestCase):
    def test_stop_profile_addresses_only_the_lifeweave_profile(self) -> None:
        from lifecontext import hindsight

        with _homes(1) as (_root, (home,)), _as(home) as paths:
            self.assertEqual(hindsight.stop_profile(paths), "not_configured")
            paths.hindsight_profiles_dir.mkdir(mode=0o700, parents=True)
            os.chmod(paths.hindsight_home, 0o700)
            paths.hindsight_profile_env_path.write_text("HINDSIGHT_API_PORT=24001\n")
            asked = []
            client = SimpleNamespace(stop_daemon=lambda profile=None: asked.append(profile) or True,
                                     get_daemon_url=lambda profile=None: "http://127.0.0.1:9855")
            owners = {24001: "here", 9855: "elsewhere"}
            with patch.object(hindsight, "_load_runtime"), \
                    patch.object(hindsight, "_daemon_owner", side_effect=lambda port, paths: owners[port]), \
                    patch.object(hindsight, "_listener_pids", return_value=set()), \
                    patch.object(hindsight, "_port_answers", return_value=True), \
                    patch.object(hindsight, "_DAEMON_STOP_TIMEOUT", 0.01), \
                    patch.dict(sys.modules, {"hindsight_embed.daemon_client": client}):
                self.assertEqual(hindsight.stop_profile(paths), "stopped")
                client.stop_daemon = lambda profile=None: asked.append(profile) and False
                with self.assertRaises(hindsight.HindsightStartupError) as caught:
                    hindsight.stop_profile(paths)
                # Without a port in the profile, hindsight-embed's name-derived port is only
                # stopped when its daemon is this HOME's; here it is another's.
                paths.hindsight_profile_env_path.write_text("HINDSIGHT_API_PORT=1\n")
                self.assertEqual(hindsight.stop_profile(paths), "stopped")
            self.assertEqual(caught.exception.code, "daemon_stop_failed")
            self.assertEqual(asked, ["lifeweave", "lifeweave"])

    def test_an_orphaned_postgres_is_stopped_by_its_instance_name(self) -> None:
        from lifecontext import hindsight_postgres

        with _homes(1) as (_root, (home,)):
            instance = home / ".pg0/instances/hindsight-embed-lifeweave"
            (instance / "data").mkdir(parents=True)
            pid_file = instance / "data" / "postmaster.pid"
            pid_file.write_text(f"{os.getpid()}\n")
            stopped = []
            with _as(home) as paths, patch("lifecontext.hindsight.stop_profile", return_value="stopped"), \
                    patch.object(backup_module, "_ORPHAN_GRACE", 0.1), \
                    patch.object(hindsight_postgres, "stop", side_effect=lambda p: stopped.append(p) or pid_file.unlink()):
                self.assertEqual(backup_module._Writers().stop_hindsight(paths), "stopped")
                self.assertEqual(stopped, [paths])
                pid_file.write_text(f"{os.getpid()}\n")
                with patch.object(hindsight_postgres, "stop",
                                  side_effect=hindsight_postgres.PostgresError("postgres_stop_failed")), \
                        self.assertRaises(BackupError) as caught:
                    backup_module._Writers().stop_hindsight(paths)
                self.assertEqual(caught.exception.code, "postgres_stop_failed")

    def test_command_refuses_while_the_api_runs(self) -> None:
        with _homes(1) as (_root, (home,)):
            writers = _Writers(home / ".pg0/instances/hindsight-embed-lifeweave")
            with _as(home) as paths:
                with self.assertRaises(BackupError) as caught:
                    stop_hindsight(port=1, probe=lambda port: True, writers=writers)
                self.assertEqual(caught.exception.code, "service_running")
                paths.hindsight_profiles_dir.mkdir(mode=0o700, parents=True)
                paths.hindsight_profile_env_path.write_text("HINDSIGHT_API_PORT=1\n")
                report = stop_hindsight(port=1, probe=NEVER, writers=writers)
            self.assertEqual(writers.calls, ["stop_hindsight"])
            self.assertEqual(report, {"command": "hindsight stop", "profile": "lifeweave", "state": "stopped"})

    def test_command_leaves_a_fresh_home_untouched(self) -> None:
        with _homes(1) as (_root, (home,)):
            writers = _Writers(home / ".pg0/instances/hindsight-embed-lifeweave")
            with _as(home):
                report = stop_hindsight(port=1, probe=NEVER, writers=writers)
            self.assertEqual(report["state"], "not_configured")
            self.assertEqual((writers.calls, list(home.iterdir())), ([], []))


if __name__ == "__main__":
    unittest.main()
