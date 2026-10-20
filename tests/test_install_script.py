"""install.sh without network or root: options, refusals and --uninstall in a temporary HOME."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import select
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

try:
    import pty
    import termios
except ImportError:  # no pseudo-terminals on this platform
    pty = None

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "install.sh"


def _stub(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)


def _functions(*names: str) -> str:
    """The shell text of these functions of install.sh, so they run without the rest of the installer."""
    text = INSTALLER.read_text()
    found = []
    for name in names:
        match = re.search(rf"^{name}\(\) \{{(?: [^\n]*\}}\n|\n.*?^\}}\n(?=\n|\Z))", text, re.MULTILINE | re.DOTALL)
        if match is None:
            raise AssertionError(f"install.sh has no function {name}")
        found.append(match.group(0))
    return "\n".join(found)


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
                     "lifecontext-capture.service", "lifecontext-capture.timer",
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


class InstallOcrTests(unittest.TestCase):
    """install_ocr on a system whose only package manager is pacman, with every command stubbed."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="lifecontext-ocr-test.")
        self.addCleanup(self.temp.cleanup)
        self.stubs = Path(self.temp.name) / "stubs"
        self.stubs.mkdir()
        self.calls = Path(self.temp.name) / "calls.log"
        # The stubs are the whole PATH, so the host's apt-get and dnf are not found.
        grep = next(path for path in ("/usr/bin/grep", "/bin/grep") if Path(path).exists())
        os.symlink(grep, self.stubs / "grep")
        _stub(self.stubs / "id", 'if [ "$1" = -u ]; then echo 1000; fi\n')
        # A tesseract that exists once pacman has "installed" it and then lists the three languages.
        self.installed = Path(self.temp.name) / "installed"
        _stub(self.stubs / "pacman", f'echo "pacman $*" >> "{self.calls}"\n: > "{self.installed}"\n')
        _stub(self.stubs / "tesseract", f'[ -e "{self.installed}" ] || exit 1\n'
              'printf "List of available languages (3):\\neng\\nrus\\nukr\\n"\n')

    def sudo(self, works: bool) -> None:
        _stub(self.stubs / "sudo", f'echo "sudo $*" >> "{self.calls}"\n'
              + ('if [ "$2" = true ]; then exit 0; fi\nshift\nexec "$@"\n' if works else "exit 1\n"))

    def run_install_ocr(self):
        harness = Path(self.temp.name) / "harness.sh"
        harness.write_text("set -eu\nos=Linux\n" + _functions(
            "say", "warn", "as_root", "can_run_as_root", "ocr_ready", "install_ocr")
            + '\ninstall_ocr\necho "ocr_state=$ocr_state"\n')
        env = {"PATH": str(self.stubs), "HOME": self.temp.name}
        return subprocess.run(["/bin/sh", str(harness)], text=True, capture_output=True, env=env,
                              stdin=subprocess.DEVNULL, timeout=60, check=False)

    def logged(self) -> list[str]:
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def test_pacman_installs_tesseract_with_the_three_language_packs(self) -> None:
        self.sudo(works=True)
        result = self.run_install_ocr()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Installing Tesseract with pacman", result.stdout)
        packages = "tesseract tesseract-data-eng tesseract-data-rus tesseract-data-ukr"
        self.assertEqual(self.logged(), ["sudo -n true", f"sudo -n pacman -S --needed --noconfirm {packages}",
                                         f"pacman -S --needed --noconfirm {packages}"])
        self.assertIn("ocr_state=ready", result.stdout)

    def test_without_passwordless_sudo_the_exact_command_is_printed_and_the_install_continues(self) -> None:
        self.sudo(works=False)
        result = self.run_install_ocr()
        self.assertEqual(result.returncode, 0, result.stderr)
        command = "sudo pacman -S --needed tesseract tesseract-data-eng tesseract-data-rus tesseract-data-ukr"
        self.assertIn(f"  {command}\n", result.stdout)
        self.assertIn("OCR is not available yet", result.stderr)
        self.assertIn(f"ocr_state=missing: {command}", result.stdout)
        self.assertEqual(self.logged(), ["sudo -n true"])

    def test_without_sudo_at_all_the_command_is_printed_too(self) -> None:
        result = self.run_install_ocr()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("sudo pacman -S --needed tesseract", result.stdout)
        self.assertEqual(self.logged(), [])

    def test_an_existing_installation_is_left_alone(self) -> None:
        self.sudo(works=True)
        self.installed.touch()
        result = self.run_install_ocr()
        self.assertIn("already installed", result.stdout)
        self.assertEqual(self.logged(), [])


@unittest.skipIf(pty is None, "needs pseudo-terminals")
class InstallEndpointPromptTests(unittest.TestCase):
    """configure_interactive on a pseudo-terminal, with `lifecontext init` replaced by a recorder."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="lifecontext-prompt-test.")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.home = root / "home"
        self.home.mkdir()
        self.tmp = root / "tmp"
        self.tmp.mkdir(mode=0o700)
        self.calls = root / "calls.log"
        venv_bin = root / "venv" / "bin"
        venv_bin.mkdir(parents=True)
        os.symlink(sys.executable, venv_bin / "python")
        report = json.dumps({"home": "/h/.lifeweave", "config_state": "written", "provider": "p", "model": "m",
                             "credentials_state": "stored", "vault_root": "/v", "timezone": "UTC", "warnings": []})
        _stub(venv_bin / "lifecontext",
              f'echo "$*" >> "{self.calls}"\n'
              'if [ -n "${LIFECONTEXT_API_KEY_FILE:-}" ]; then\n'
              f'  echo "key=$(cat "$LIFECONTEXT_API_KEY_FILE") $(ls -l "$LIFECONTEXT_API_KEY_FILE" | cut -c1-10) '
              f'$(dirname "$LIFECONTEXT_API_KEY_FILE")" >> "{self.calls}"\nfi\n'
              f"printf '%s' '{report}'\n")
        self.harness = root / "harness.sh"
        self.harness.write_text(
            f'set -eu\nHOME="{self.home}"\ntmp="{self.tmp}"\nvenv="{root / "venv"}"\nlc="$venv/bin/lifecontext"\n'
            + _functions("say", "warn", "die", "restore_tty", "ask", "ask_required", "ask_secret",
                         "print_init_report", "configure_interactive")
            + '\nconfigure_interactive\n')

    def converse(self, answers: list[tuple[str, str]]) -> tuple[int, str]:
        """Answer each prompt once it appears; the hidden key only after the terminal stopped echoing."""
        env = {"PATH": os.defpath, "HOME": str(self.home)}
        pid, descriptor = pty.fork()
        if pid == 0:  # the child: a shell with this pseudo-terminal as its controlling terminal
            os.execve("/bin/sh", ["sh", str(self.harness)], env)
        transcript = b""
        position = 0
        deadline = time.monotonic() + 60

        def read_more() -> bool:
            nonlocal transcript
            if time.monotonic() > deadline:
                return False
            if not select.select([descriptor], [], [], 1)[0]:
                return True
            try:
                data = os.read(descriptor, 4096)
            except OSError:
                return False
            transcript += data
            return bool(data)

        try:
            for prompt, reply in answers:
                while prompt.encode() not in transcript[position:]:
                    self.assertTrue(read_more(), f"no prompt {prompt!r} in {transcript!r}")
                position = transcript.index(prompt.encode(), position) + len(prompt)
                if prompt.startswith("API key"):
                    while termios.tcgetattr(descriptor)[3] & termios.ECHO:
                        self.assertLess(time.monotonic(), deadline)
                        time.sleep(0.01)
                os.write(descriptor, (reply + "\n").encode())
            while read_more():
                pass
        finally:
            os.close(descriptor)
            _, status = os.waitpid(pid, 0)
        return os.waitstatus_to_exitcode(status), transcript.decode(errors="replace")

    def test_defaults_are_a_local_openai_compatible_proxy_and_an_empty_key_is_allowed(self) -> None:
        code, transcript = self.converse([
            ("Provider (", ""), ("API base URL [http://127.0.0.1:8317/v1]", ""), ("Model ID", "my-model"),
            ("API key", ""), ("Vault folder", "")])
        self.assertEqual(code, 0, transcript)
        self.assertIn("[openai_compatible]", transcript)
        self.assertEqual(self.calls.read_text().splitlines(), [
            (f"init --non-interactive --json --provider openai_compatible --model my-model "
             f"--vault {self.home}/LifeContext/vault --base-url http://127.0.0.1:8317/v1")])

    def test_the_key_is_hidden_reaches_init_only_through_a_private_file_and_is_removed(self) -> None:
        key = "sk-typed-secret-0123456789"
        code, transcript = self.converse([
            ("Provider (", "openai_compatible"), ("API base URL", "http://127.0.0.1:9999/v1"),
            ("Model ID", "other-model"), ("API key", key), ("Vault folder", str(self.home / "Notes"))])
        self.assertEqual(code, 0, transcript)
        self.assertNotIn(key, transcript)
        recorded = self.calls.read_text().splitlines()
        self.assertEqual(recorded[0], f"init --non-interactive --json --provider openai_compatible "
                                      f"--model other-model --vault {self.home}/Notes "
                                      f"--base-url http://127.0.0.1:9999/v1")
        self.assertEqual(recorded[1], f"key={key} -rw------- {self.tmp}")
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_anthropic_needs_no_base_url_and_proposes_a_model(self) -> None:
        code, transcript = self.converse([
            ("Provider (", "anthropic"), ("API base URL", ""),
            ("Model ID (exactly as your endpoint names it) [claude-sonnet-5]", ""),
            ("API key", ""), ("Vault folder", "")])
        self.assertEqual(code, 0, transcript)
        self.assertEqual(self.calls.read_text().splitlines(), [
            (f"init --non-interactive --json --provider anthropic --model claude-sonnet-5 "
             f"--vault {self.home}/LifeContext/vault")])


if __name__ == "__main__":
    unittest.main()
