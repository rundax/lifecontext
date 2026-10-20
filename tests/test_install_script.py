"""install.sh without network or root: options, refusals and --uninstall in a temporary HOME."""

from __future__ import annotations

import hashlib
import os
import platform
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "install.sh"


def _stub(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)


class InstallScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="lifecontext-install-test.")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "home"
        self.home.mkdir()
        self.stubs = Path(self.temp.name) / "stubs"
        self.stubs.mkdir()
        self.calls = Path(self.temp.name) / "calls.log"
        # Never reach the host's user services from a test.
        _stub(self.stubs / "systemctl", f'echo "systemctl $*" >> "{self.calls}"\n')
        _stub(self.stubs / "launchctl", f'echo "launchctl $*" >> "{self.calls}"\n')
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith(("LIFECONTEXT_", "XDG_"))}
        self.env.update(HOME=str(self.home), PATH=f"{self.stubs}:{os.environ['PATH']}")

    def run_installer(self, *args: str):
        return subprocess.run(["sh", str(INSTALLER), *args], text=True, capture_output=True,
                              env=self.env, stdin=subprocess.DEVNULL, timeout=60, check=False)

    def logged(self) -> list[str]:
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def test_help_and_usage_errors(self) -> None:
        self.assertEqual(self.run_installer("--help").returncode, 0)
        for args in (("--bogus",), ("--wheel", "x.whl"), ("--lock", "l.txt"), ("--version",),
                     ("--version", "latest"), ("--version", "1.2.0;id"), ("--version", "1.2.0/../x"),
                     ("--assets", "d", "--wheel", "w.whl", "--lock", "l.txt")):
            with self.subTest(args=args):
                self.assertEqual(self.run_installer(*args).returncode, 2)
        self.assertEqual(os.listdir(self.home), [])

    def test_unsupported_platform_is_refused_before_anything_is_written(self) -> None:
        _stub(self.stubs / "uname", 'case "$1" in -s) echo FreeBSD ;; -m) echo amd64 ;; esac\n')
        result = self.run_installer("--non-interactive")
        self.assertEqual(result.returncode, 1)
        self.assertIn("unsupported system FreeBSD amd64", result.stderr)
        self.assertEqual(os.listdir(self.home), [])

    def test_root_is_refused_unless_allowed(self) -> None:
        _stub(self.stubs / "id", 'if [ "$1" = -u ]; then echo 0; else exec /usr/bin/id "$@"; fi\n')
        _stub(self.stubs / "uname", 'case "$1" in -s) echo FreeBSD ;; -m) echo amd64 ;; esac\n')
        result = self.run_installer()
        self.assertEqual(result.returncode, 1)
        self.assertIn("do not run the installer as root", result.stderr)
        allowed = self.run_installer("--allow-root")
        self.assertEqual(allowed.returncode, 1)
        self.assertIn("unsupported system", allowed.stderr)

    @unittest.skipUnless(platform.system() == "Linux", "glibc check")
    def test_old_glibc_is_refused(self) -> None:
        _stub(self.stubs / "getconf", 'echo "glibc 2.31"\n')
        result = self.run_installer("--non-interactive")
        self.assertEqual(result.returncode, 1)
        self.assertIn("glibc 2.31 is too old", result.stderr)

    def _assets(self, *, wheel_bytes: bytes = b"wheel") -> Path:
        """A release directory whose SHA256SUMS lists the wheel and lock of this platform."""
        machine = "aarch64" if platform.machine() in {"aarch64", "arm64"} else "x86_64"
        lock = "requirements-macos-arm64.txt" if platform.system() == "Darwin" else f"requirements-linux-{machine}.txt"
        assets = Path(self.temp.name) / "assets"
        assets.mkdir()
        files = {"lifecontext-1.2.0-py3-none-any.whl": b"wheel", lock: b"lock"}
        (assets / "SHA256SUMS").write_text("".join(
            f"{hashlib.sha256(data).hexdigest()}  {name}\n" for name, data in files.items()))
        files["lifecontext-1.2.0-py3-none-any.whl"] = wheel_bytes
        for name, data in files.items():
            (assets / name).write_bytes(data)
        (assets / "SHA256SUMS.sigstore.json").write_text("{}")
        return assets

    def test_assets_with_a_wrong_checksum_are_refused_before_anything_is_installed(self) -> None:
        assets = self._assets(wheel_bytes=b"tampered")
        result = self.run_installer("--assets", str(assets), "--non-interactive")
        self.assertEqual(result.returncode, 1)
        self.assertIn("checksum mismatch for lifecontext-1.2.0-py3-none-any.whl", result.stderr)
        self.assertFalse((self.home / ".local").exists())

    def test_an_old_cosign_is_treated_as_absent(self) -> None:
        _stub(self.stubs / "cosign", 'echo "GitVersion:    v2.4.1"\n')
        result = self.run_installer("--assets", str(self._assets(wheel_bytes=b"tampered")), "--non-interactive")
        self.assertIn("older than 3.0", result.stderr)
        self.assertIn("checksum mismatch", result.stderr)

    def test_a_failed_signature_check_shows_why_and_stops(self) -> None:
        _stub(self.stubs / "cosign", 'if [ "$1" = version ]; then echo "GitVersion:    v3.0.6"; '
                                     'else echo "error: no matching signatures: identity mismatch" >&2; exit 1; fi\n')
        result = self.run_installer("--assets", str(self._assets()), "--non-interactive")
        self.assertEqual(result.returncode, 1)
        self.assertIn("no matching signatures: identity mismatch", result.stderr)
        self.assertIn("cosign could not verify SHA256SUMS", result.stderr)
        self.assertFalse((self.home / ".local").exists())

    def test_model_revisions_match_the_dockerfile_and_the_models_hindsight_loads(self) -> None:
        script = INSTALLER.read_text()
        dockerfile = (ROOT / "Dockerfile").read_text()
        hindsight = (ROOT / "src" / "lifecontext" / "hindsight.py").read_text()

        def value(text: str, name: str) -> str:
            return re.search(rf'^(?:ARG )?{name}="?([^"\n]+)"?$', text, re.MULTILINE).group(1)

        for name in ("EMBEDDING_REVISION", "RERANKER_REVISION"):
            self.assertRegex(value(script, name), r"^[0-9a-f]{40}$")
            self.assertEqual(value(script, name), value(dockerfile, name), name)
        self.assertIn(f'EMBEDDING_MODEL = "{value(script, "EMBEDDING_MODEL")}"', hindsight)
        self.assertIn(f'"HINDSIGHT_API_RERANKER_LOCAL_MODEL": "{value(script, "RERANKER_MODEL")}"', hindsight)

    def _data(self) -> list[Path]:
        kept = [self.home / ".lifeweave" / ".env", self.home / ".lifeweave" / "config.yaml",
                self.home / "LifeContext" / "vault" / "Journal" / "entry.md",
                self.home / "LifeContext" / "backups" / "lifecontext-backup.tar.gz"]
        for path in kept:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("data")
        return kept

    def test_uninstall_removes_only_the_installation(self) -> None:
        data_dir = self.home / ".local" / "share" / "lifecontext"
        command = data_dir / "venv" / "bin" / "lifecontext"
        command.parent.mkdir(parents=True)
        _stub(command, f'echo "lifecontext $*" >> "{self.calls}"\n')
        for name in ("bin/uv", "python/cpython/bin/python3", "cache/archive", "installed.sha256"):
            (data_dir / name).parent.mkdir(parents=True, exist_ok=True)
            (data_dir / name).write_text("x")
        link = self.home / ".local" / "bin" / "lifecontext"
        link.parent.mkdir(parents=True)
        link.symlink_to(command)
        kept = self._data()

        result = self.run_installer("--uninstall")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.logged(), ["lifecontext --version", "lifecontext service uninstall",
                                         "lifecontext skills remove"])
        self.assertFalse(data_dir.exists())
        self.assertFalse(os.path.lexists(link))
        for path in kept:
            self.assertEqual(path.read_text(), "data", path)

    @unittest.skipUnless(platform.system() == "Linux", "systemd unit layout")
    def test_uninstall_without_a_working_command_removes_units_and_our_skill_links(self) -> None:
        units = self.home / ".config" / "systemd" / "user"
        (units / "default.target.wants").mkdir(parents=True)
        for name in ("lifecontext.service", "lifecontext-maintain.service", "lifecontext-maintain.timer",
                     "other.service"):
            (units / name).write_text("[Unit]\n")
        (units / "default.target.wants" / "lifecontext.service").symlink_to(units / "lifecontext.service")
        ours = self.home / ".claude" / "skills" / "personal-journal-operations"
        ours.parent.mkdir(parents=True)
        ours.symlink_to("/old/venv/lib/python3.12/site-packages/lifecontext/agent_skill/personal-journal-operations")
        foreign = self.home / ".codex" / "skills" / "personal-journal-operations"
        foreign.parent.mkdir(parents=True)
        foreign.symlink_to("/elsewhere/personal-journal-operations")
        other_command = self.home / ".local" / "bin" / "lifecontext"
        other_command.parent.mkdir(parents=True)
        other_command.symlink_to("/opt/other/lifecontext")
        kept = self._data()

        result = self.run_installer("--uninstall")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sorted(os.listdir(units)), ["default.target.wants", "other.service"])
        self.assertEqual(os.listdir(units / "default.target.wants"), [])
        self.assertIn("systemctl --user disable --now lifecontext.service", self.logged())
        self.assertEqual(self.logged()[-1], "systemctl --user daemon-reload")
        self.assertFalse(os.path.lexists(ours))
        self.assertTrue(os.path.islink(foreign))
        self.assertTrue(os.path.islink(other_command))
        for path in kept:
            self.assertEqual(path.read_text(), "data", path)


if __name__ == "__main__":
    unittest.main()
