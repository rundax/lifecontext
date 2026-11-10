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
# The release this install.sh installs, and so the wheel it looks for.
RELEASE = re.search(r'^LIFECONTEXT_RELEASE="([^"]+)"$', INSTALLER.read_text(), re.MULTILINE).group(1)
WHEEL = f"lifecontext-{RELEASE}-py3-none-any.whl"


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
        shown = self.run_installer("--help")
        self.assertEqual(shown.returncode, 0)
        # No model default and no release number that a version bump would leave stale.
        for stale in ("8317", "CLIProxyAPI", "default: anthropic", "default: 1.2.0", "proxy"):
            self.assertNotIn(stale, shown.stdout)
        self.assertIn("--skip-model-probe", shown.stdout)
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
        files = {WHEEL: b"wheel", lock: b"lock"}
        (assets / "SHA256SUMS").write_text("".join(
            f"{hashlib.sha256(data).hexdigest()}  {name}\n" for name, data in files.items()))
        files[WHEEL] = wheel_bytes
        for name, data in files.items():
            (assets / name).write_bytes(data)
        (assets / "SHA256SUMS.sigstore.json").write_text("{}")
        return assets

    def test_assets_with_a_wrong_checksum_are_refused_before_anything_is_installed(self) -> None:
        assets = self._assets(wheel_bytes=b"tampered")
        result = self.run_installer("--assets", str(assets), "--non-interactive")
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"checksum mismatch for {WHEEL}", result.stderr)
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


class FetchReleaseTests(unittest.TestCase):
    """fetch_release with curl stubbed: a release that does not exist is named, with where to look."""

    def run_fetch(self, head_status: str) -> subprocess.CompletedProcess[str]:
        temp = tempfile.TemporaryDirectory(prefix="lifecontext-fetch-test.")
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        stubs = root / "stubs"
        stubs.mkdir()
        # The download fails as curl -f does on an HTTP error; the HEAD request reports the status.
        _stub(stubs / "curl", 'for argument in "$@"; do [ "$argument" = -I ] && { printf %s '
              f'"{head_status}"; exit 0; }}; done\necho "curl: (22) The requested URL returned error" >&2\nexit 22\n')
        harness = root / "harness.sh"
        harness.write_text(f'set -eu\nREPOSITORY="rundax/lifecontext"\nversion=9.9.9\nplatform=linux-x86_64\n'
                           f'tmp="{root}"\n' + _functions("say", "warn", "die", "download", "download_failed",
                                                           "fetch_release") + "\nfetch_release\n")
        return subprocess.run(["/bin/sh", str(harness)], text=True, capture_output=True,
                              env={"PATH": f"{stubs}:{os.defpath}"}, stdin=subprocess.DEVNULL, timeout=60,
                              check=False)

    def test_a_missing_release_says_where_the_releases_are(self) -> None:
        result = self.run_fetch("404")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Error: release v9.9.9 not found; see https://github.com/rundax/lifecontext/releases "
                      "or pass --version\n", result.stderr)

    def test_another_download_failure_names_the_file(self) -> None:
        result = self.run_fetch("503")
        self.assertEqual(result.returncode, 1)
        self.assertIn("could not download SHA256SUMS from github.com/rundax/lifecontext", result.stderr)
        self.assertNotIn("not found", result.stderr)


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

    def harness(self, interactive: str) -> Path:
        harness = Path(self.temp.name) / "harness.sh"
        harness.write_text(f"set -eu\nos=Linux\ninteractive={interactive}\n" + _functions(
            "say", "warn", "tty_available", "ask_yes", "as_root", "can_run_as_root", "may_install_as_root",
            "ocr_ready", "install_ocr") + '\ninstall_ocr\necho "ocr_state=$ocr_state"\n')
        return harness

    def run_install_ocr(self):
        env = {"PATH": str(self.stubs), "HOME": self.temp.name}
        return subprocess.run(["/bin/sh", str(self.harness("no"))], text=True, capture_output=True, env=env,
                              stdin=subprocess.DEVNULL, timeout=60, check=False)

    @unittest.skipIf(pty is None, "needs pseudo-terminals")
    def test_an_interactive_run_asks_before_using_sudo_with_a_password(self) -> None:
        # sudo -n fails (a password is needed); plain sudo "asks" and runs the command.
        _stub(self.stubs / "sudo", f'echo "sudo $*" >> "{self.calls}"\n[ "$1" = -n ] && exit 1\nexec "$@"\n')
        for answer, expected in (("", "ocr_state=ready"), ("n", "ocr_state=missing: sudo pacman")):
            with self.subTest(answer=answer):
                self.calls.unlink(missing_ok=True)
                self.installed.unlink(missing_ok=True)
                pid, descriptor = pty.fork()
                if pid == 0:
                    os.execve("/bin/sh", ["sh", str(self.harness("auto"))],
                              {"PATH": str(self.stubs), "HOME": self.temp.name})
                transcript = b""
                deadline = time.monotonic() + 60
                answered = False
                try:
                    while time.monotonic() < deadline:
                        if not answered and b"Install Tesseract with sudo now?" in transcript \
                                and transcript.endswith(b"[Y/n]: "):
                            os.write(descriptor, (answer + "\n").encode())
                            answered = True
                        if not select.select([descriptor], [], [], 1)[0]:
                            continue
                        try:
                            data = os.read(descriptor, 4096)
                        except OSError:
                            break
                        if not data:
                            break
                        transcript += data
                finally:
                    os.close(descriptor)
                    os.waitpid(pid, 0)
                text = transcript.decode(errors="replace")
                self.assertTrue(answered, text)
                self.assertIn(expected, text)
                packages = "tesseract tesseract-data-eng tesseract-data-rus tesseract-data-ukr"
                self.assertEqual(self.logged(), ["sudo -n true"] + (
                    [f"sudo pacman -S --needed --noconfirm {packages}", f"pacman -S --needed --noconfirm {packages}"]
                    if answer == "" else []))

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
              f'[ -z "${{LIFECONTEXT_BASE_URL:-}}" ] || echo "LIFECONTEXT_BASE_URL reached init" >> "{self.calls}"\n'
              'if [ -n "${LIFECONTEXT_API_KEY_FILE:-}" ]; then\n'
              f'  echo "key=$(cat "$LIFECONTEXT_API_KEY_FILE") $(ls -l "$LIFECONTEXT_API_KEY_FILE" | cut -c1-10) '
              f'$(dirname "$LIFECONTEXT_API_KEY_FILE")" >> "{self.calls}"\nfi\n'
              f"printf '%s' '{report}'\n")
        self.harness = root / "harness.sh"
        self.harness.write_text(
            f'set -eu\nHOME="{self.home}"\ntmp="{self.tmp}"\nvenv="{root / "venv"}"\nlc="$venv/bin/lifecontext"\n'
            + _functions("say", "warn", "die", "restore_tty", "ask", "ask_required", "ask_secret", "ask_no",
                         "choose_provider", "ask_anthropic_model", "print_init_report", "configure_interactive")
            + '\nconfigure_interactive\necho "capture=$capture"\n')

    def converse(self, answers: list[tuple[str, str]], **environ: str) -> tuple[int, str]:
        """Answer each prompt once it appears; the hidden key only after the terminal stopped echoing."""
        env = {"PATH": os.defpath, "HOME": str(self.home), **environ}
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

    def test_the_menu_preselects_nothing_and_a_local_server_suggests_its_url(self) -> None:
        from lifecontext.initialize import model_question, provider_menu

        code, transcript = self.converse([
            ("Choose 1-6: ", ""), ("Choose 1-6: ", "5"), ("Base URL of the server [http://127.0.0.1:11434/v1]", ""),
            ("Model ID", "llama3.3"), ("API key", ""), ("Vault folder", ""), ("Turn notes", "n")])
        self.assertEqual(code, 0, transcript)
        # The same menu, word for word, as lifecontext init.
        for line in provider_menu().splitlines()[:-1]:
            self.assertIn(line + "\r\n", transcript)
        self.assertIn("type a number from 1 to 6", transcript)
        self.assertIn(model_question("openai_compatible", 5) + ": ", transcript)
        self.assertEqual(self.calls.read_text().splitlines(), [
            (f"init --non-interactive --json --provider openai_compatible --model llama3.3 "
             f"--vault {self.home}/LifeContext/vault --base-url http://127.0.0.1:11434/v1")])
        self.assertIn("capture=0", transcript)

    def test_the_key_is_hidden_reaches_init_only_through_a_private_file_and_is_removed(self) -> None:
        key = "sk-typed-secret-0123456789"
        code, transcript = self.converse([
            ("Choose 1-6: ", "6"), ("API base URL", "http://127.0.0.1:9999/v1"),
            ("Model ID (exactly as your endpoint names it)", "other-model"), ("API key", key),
            ("Vault folder", str(self.home / "Notes")),
            ("Turn notes you put in Journal/Inbox into journal entries automatically? [y/N]", "")])
        self.assertEqual(code, 0, transcript)
        self.assertNotIn(key, transcript)
        recorded = self.calls.read_text().splitlines()
        self.assertEqual(recorded[0], f"init --non-interactive --json --provider openai_compatible "
                                      f"--model other-model --vault {self.home}/Notes "
                                      f"--base-url http://127.0.0.1:9999/v1")
        self.assertEqual(recorded[1], f"key={key} -rw------- {self.tmp}")
        self.assertEqual([path.name for path in self.tmp.iterdir()], ["next-steps"])
        # Inbox capture is off unless asked for.
        self.assertIn("capture=0", transcript)

    def test_a_rerun_that_finds_no_key_asks_for_it_hidden_at_a_terminal(self) -> None:
        key = "sk-typed-again-0123456789"
        (self.home / ".lifeweave").mkdir()
        (self.home / ".lifeweave" / "config.yaml").write_text("llm: {}\n")
        common = {"home": "/h/.lifeweave", "config_state": "kept", "provider": "p", "model": "m", "vault_root": "/v",
                  "timezone": "UTC", "warnings": []}
        missing = json.dumps({**common, "credentials_state": "missing"})
        replaced = json.dumps({**common, "credentials_state": "replaced"})
        venv = Path(self.temp.name) / "venv"
        _stub(venv / "bin" / "lifecontext",
              f'echo "$*" >> "{self.calls}"\n'
              'if [ -n "${LIFECONTEXT_API_KEY_FILE:-}" ]; then\n'
              f'  echo "key=$(cat "$LIFECONTEXT_API_KEY_FILE") $(ls -l "$LIFECONTEXT_API_KEY_FILE" | cut -c1-10) '
              f'$(dirname "$LIFECONTEXT_API_KEY_FILE")" >> "{self.calls}"\n'
              f"  printf '%s' '{replaced}'\nelse\n  printf '%s' '{missing}'\nfi\n")
        self.harness.write_text(
            f'set -eu\nHOME="{self.home}"\ntmp="{self.tmp}"\nvenv="{venv}"\nlc="$venv/bin/lifecontext"\n'
            'interactive=auto\nkey_changed=0\n'
            + _functions("say", "warn", "die", "sha256_of", "tty_available", "restore_tty", "ask_secret",
                         "print_init_report", "key_missing", "ask_key_again", "configure")
            + '\nconfigure\necho "key_changed=$key_changed"\n')
        code, transcript = self.converse([("API key for this provider (none is stored yet", key)])
        self.assertEqual(code, 0, transcript)
        self.assertNotIn(key, transcript)
        self.assertEqual(self.calls.read_text().splitlines(), [
            "init --non-interactive --json", "init --non-interactive --replace-key --json",
            f"key={key} -rw------- {self.tmp}"])
        self.assertEqual([path.name for path in self.tmp.iterdir()], ["next-steps"])
        self.assertIn("API key: replaced in .env", transcript)
        self.assertIn("key_changed=1", transcript)

    def test_anthropic_suggests_a_model_and_refuses_the_ones_that_cannot_work(self) -> None:
        from lifecontext.initialize import model_question

        # A LIFECONTEXT_BASE_URL left over in the environment must not reach init.
        code, transcript = self.converse([
            ("Choose 1-6: ", "1"), (f"{model_question('anthropic')} [claude-sonnet-5]", "claude-opus-5-5"),
            # Enter at the model prompt asked again takes the suggestion.
            ("does not work with LifeContext yet", ""), ("API key", ""), ("Vault folder", ""), ("Turn notes", "y")],
            LIFECONTEXT_BASE_URL="http://127.0.0.1:8000/v1")
        self.assertEqual(code, 0, transcript)
        self.assertNotIn("URL", transcript)
        self.assertEqual(self.calls.read_text().splitlines(), [
            (f"init --non-interactive --json --provider anthropic --model claude-sonnet-5 "
             f"--vault {self.home}/LifeContext/vault")])
        self.assertIn("capture=1", transcript)

    def test_the_refused_models_and_the_model_lists_match_lifecontext_init(self) -> None:
        from lifecontext.initialize import (
            FORCED_TOOL_REJECTING_MODELS,
            MODEL_LISTS,
            model_question,
        )

        script = INSTALLER.read_text()
        refused = re.search(r"^\t\t(claude-[^)]+)\)$", script, re.MULTILINE).group(1)
        self.assertEqual(set(refused.split(" | ")), FORCED_TOOL_REJECTING_MODELS)
        for provider in MODEL_LISTS:
            self.assertIn(f'ask_required "{model_question(provider)}" ""', script)


class InstallEndingTests(unittest.TestCase):
    """configure on a re-run, service_command and finish, with `lifecontext` stubbed."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="lifecontext-ending-test.")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.home = root / "home"
        self.home.mkdir()
        self.tmp = root / "tmp"
        self.tmp.mkdir()
        self.calls = root / "calls.log"
        self.root = root
        venv_bin = root / "venv" / "bin"
        venv_bin.mkdir(parents=True)
        os.symlink(sys.executable, venv_bin / "python")
        report = json.dumps({"home": "/h/.lifeweave", "config_state": "kept", "provider": "p", "model": "m",
                             "credentials_state": "kept", "vault_root": "/v", "timezone": "UTC", "warnings": [],
                             "next_steps": ["Open /v in Obsidian as a vault (\"Open folder as vault\").",
                                            "Import a document: lifecontext documents import ~/path/to/lease.pdf"]})
        _stub(venv_bin / "lifecontext", f'echo "$*" >> "{self.calls}"\n'
              f"""case "$1" in --version) echo "lifecontext 9.9" ;; init) printf '%s' '{report}' ;; esac\n""")

    def run_functions(self, body: str, *names: str, **environ: str) -> subprocess.CompletedProcess[str]:
        harness = self.root / "harness.sh"
        harness.write_text(
            f'set -eu\nHOME="{self.home}"\ntmp="{self.tmp}"\nvenv="{self.root / "venv"}"\n'
            'lc="$venv/bin/lifecontext"\nlink="$HOME/.local/bin/lifecontext"\nos=Linux\n'
            'interactive=no\ncapture=${CAPTURE:-0}\nkey_changed=0\nservice_state="running"\nocr_state="ready"\n'
            'skills_state="linked for Claude Code"\n'
            'backup_archive=""\nmodel_state=${MODEL:-passed}\ndoctor_status=${DOCTOR:-0}\n'
            + _functions("say", "warn", "die", "sha256_of", *names) + "\n" + body)
        return subprocess.run(["/bin/sh", str(harness)], text=True, capture_output=True,
                              env={"PATH": os.defpath, **environ}, stdin=subprocess.DEVNULL, timeout=60, check=False)

    def test_a_rerun_runs_init_on_the_kept_settings_for_the_next_steps(self) -> None:
        (self.home / ".lifeweave").mkdir()
        (self.home / ".lifeweave" / "config.yaml").write_text("llm: {}\n")
        result = self.run_functions("configure\n", "print_init_report", "configure")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls.read_text(), "init --non-interactive --json\n")
        self.assertIn("Existing settings in ~/.lifeweave kept", result.stdout)
        self.assertEqual((self.tmp / "next-steps").read_text().splitlines()[1],
                         "  2. Import a document: lifecontext documents import ~/path/to/lease.pdf")

    def test_capture_is_added_when_chosen_or_already_installed(self) -> None:
        body = "service_command\n"
        self.assertEqual(self.run_functions(body, "capture_installed", "service_command").stdout,
                         "service install --maintenance\n")
        self.assertEqual(self.run_functions(body, "capture_installed", "service_command", CAPTURE="1").stdout,
                         "service install --maintenance --capture\n")
        timer = self.home / ".config" / "systemd" / "user" / "lifecontext-capture.timer"
        timer.parent.mkdir(parents=True)
        timer.write_text("[Timer]\n")
        self.assertEqual(self.run_functions(body, "capture_installed", "service_command").stdout,
                         "service install --maintenance --capture\n")

    def finish(self, **environ: str) -> subprocess.CompletedProcess[str]:
        (self.tmp / "next-steps").write_text('  1. Open /v in Obsidian as a vault ("Open folder as vault").\n')
        (self.tmp / "model-check").write_text("The model check failed: the provider rejected the key.\n"
                                              "Fix it with: lifecontext llm set --api-key-stdin < FILE\n")
        return self.run_functions("finish\n", "finish", **environ)

    def test_a_good_install_ends_with_the_next_steps_and_exit_0(self) -> None:
        result = self.finish()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Model check: passed\n", result.stdout)
        self.assertIn("Agent skill: linked for Claude Code\n", result.stdout)
        self.assertTrue(result.stdout.endswith('Next steps:\n  1. Open /v in Obsidian as a vault '
                                               '("Open folder as vault").\n'))
        self.assertEqual(result.stderr, "")

    def test_a_failed_model_check_comes_last_without_next_steps_and_exits_3(self) -> None:
        result = self.finish(MODEL="failed")
        self.assertEqual(result.returncode, 3)
        self.assertNotIn("Next steps", result.stdout)
        self.assertTrue(result.stdout.endswith("The model check failed: the provider rejected the key.\n"
                                               "Fix it with: lifecontext llm set --api-key-stdin < FILE\n"))
        self.assertNotIn("doctor reported a problem", result.stderr)

    def test_a_doctor_problem_keeps_the_next_steps_and_exits_3(self) -> None:
        result = self.finish(DOCTOR="1")
        self.assertEqual(result.returncode, 3)
        self.assertIn("Next steps:", result.stdout)
        self.assertIn("lifecontext doctor reported a problem", result.stderr)


class InstallWaitTests(unittest.TestCase):
    """wait_for_api with the health check, sleep and journalctl stubbed."""

    def run_wait(self, answers_after: int, wait: int) -> subprocess.CompletedProcess[str]:
        temp = tempfile.TemporaryDirectory(prefix="lifecontext-wait-test.")
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        stubs = root / "stubs"
        stubs.mkdir()
        _stub(stubs / "sleep", "exit 0\n")
        _stub(stubs / "journalctl", 'echo "journalctl $*"\necho "a log line"\n')
        harness = root / "harness.sh"
        harness.write_text(
            f"set -eu\nos=Linux\nHEALTH_WAIT_SECONDS={wait}\ncount=0\n"
            f'api_answers() {{ count=$((count + 1)); [ "$count" -gt {answers_after} ]; }}\n'
            + _functions("say", "warn", "log_command", "show_log_tail", "wait_for_api")
            + '\nif wait_for_api; then echo answered; else show_log_tail; fi\n')
        return subprocess.run(["/bin/sh", str(harness)], text=True, capture_output=True,
                              env={"PATH": f"{stubs}:{os.defpath}"}, stdin=subprocess.DEVNULL, timeout=60,
                              check=False)

    def test_a_long_wait_says_how_long_and_where_the_log_is_every_20_seconds(self) -> None:
        result = self.run_wait(answers_after=9, wait=900)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [
            ("Still waiting for the API (20 s of up to 900 s); follow the log with: "
             "journalctl --user -u lifecontext.service"),
            ("Still waiting for the API (40 s of up to 900 s); follow the log with: "
             "journalctl --user -u lifecontext.service"),
            "answered"])

    def test_a_timeout_shows_the_last_20_log_lines(self) -> None:
        result = self.run_wait(answers_after=99, wait=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Still waiting", result.stdout)
        self.assertIn("The last 20 lines of the service log:\n"
                      "journalctl --user -u lifecontext.service -n 20 --no-pager\na log line\n", result.stdout)


class InstallSkillsAndPathTests(unittest.TestCase):
    """link_skills and link_command with `lifecontext` stubbed: what the summary and the PATH hint say."""

    def run_function(self, name: str, check_output: str = "", shell: str = "/bin/bash",
                     os_name: str = "Linux") -> subprocess.CompletedProcess[str]:
        temp = tempfile.TemporaryDirectory(prefix="lifecontext-skills-test.")
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        venv_bin = root / "venv" / "bin"
        venv_bin.mkdir(parents=True)
        os.symlink(sys.executable, venv_bin / "python")
        _stub(venv_bin / "lifecontext", f"""case "$*" in "skills check --json") printf '%s' '{check_output}' ;; esac\n""")
        harness = root / "harness.sh"
        harness.write_text(
            f'set -eu\nHOME="{root}"\nvenv="{root / "venv"}"\nlc="$venv/bin/lifecontext"\nno_skills=0\n'
            f'os={os_name}\nbin_dir="$HOME/.local/bin"\nlink="$bin_dir/lifecontext"\n'
            + _functions("say", "warn", name) + f'\n{name}\necho "skills_state=${{skills_state:-}}"\n')
        return subprocess.run(["/bin/sh", str(harness)], text=True, capture_output=True,
                              env={"PATH": os.defpath, "SHELL": shell}, stdin=subprocess.DEVNULL, timeout=60,
                              check=False)

    def test_the_summary_names_the_agents_that_got_the_skill_or_says_none_was_found(self) -> None:
        report = json.dumps({"agents": [{"agent": "claude", "state": "linked"}, {"agent": "codex", "state": "linked"},
                                        {"agent": "gemini", "state": "missing"}]})
        self.assertIn("skills_state=linked for Claude Code, Codex\n", self.run_function("link_skills", report).stdout)
        result = self.run_function("link_skills", json.dumps({"agents": []}))
        self.assertIn("skills_state=no agent found", result.stdout)
        result = self.run_function("link_skills", json.dumps({"error": "link_failed"}))
        self.assertIn("skills_state=unknown; see lifecontext skills check", result.stdout)

    def test_the_path_hint_names_the_file_the_shell_reads(self) -> None:
        for shell, os_name, expected in (("/bin/zsh", "Darwin", "~/.zprofile"), ("/bin/bash", "Linux", "~/.profile"),
                                         ("/bin/bash", "Darwin", "~/.bash_profile"),
                                         ("/usr/bin/fish", "Linux", "fish_add_path ~/.local/bin")):
            with self.subTest(shell=shell, os=os_name):
                result = self.run_function("link_command", shell=shell, os_name=os_name)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(expected, result.stdout)


class InstallModelCheckTests(unittest.TestCase):
    """check_model with `lifecontext preflight --model-probe` replaced by a stub."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="lifecontext-probe-test.")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.home = root / "home"
        (self.home / ".lifeweave").mkdir(parents=True)
        (self.home / ".lifeweave" / "config.yaml").write_text("llm: {}\n")
        self.tmp = root / "tmp"
        self.tmp.mkdir()
        self.calls = root / "calls.log"
        venv_bin = root / "venv" / "bin"
        venv_bin.mkdir(parents=True)
        os.symlink(sys.executable, venv_bin / "python")
        self.lc = venv_bin / "lifecontext"
        self.harness = root / "harness.sh"
        self.harness.write_text(
            f'set -eu\nHOME="{self.home}"\ntmp="{self.tmp}"\nvenv="{root / "venv"}"\nlc="$venv/bin/lifecontext"\n'
            'skip_model_probe=${SKIP:-0}\n' + _functions("say", "check_model")
            + '\ncheck_model\necho "model_state=$model_state"\n')

    def check(self, output: str, status: int = 0, **environ: str) -> str:
        _stub(self.lc, f'echo "$*" >> "{self.calls}"\necho "provider text on stderr" >&2\n'
                       f"printf '%s' '{output}'\nexit {status}\n")
        env = {"PATH": os.defpath, **environ}
        result = subprocess.run(["/bin/sh", str(self.harness)], text=True, capture_output=True, env=env,
                                stdin=subprocess.DEVNULL, timeout=60, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("provider text", result.stdout + result.stderr)
        return result.stdout

    def test_a_passing_model(self) -> None:
        out = self.check(json.dumps({"model_probe": {"status": "pass", "cause": None, "reflect": {"status": "pass"}}}))
        self.assertIn("The model answered", out)
        self.assertTrue(out.endswith("model_state=passed\n"))
        self.assertEqual(self.calls.read_text(), "preflight --model-probe\n")

    def test_a_rejected_key_names_the_cause_and_the_fix_and_the_install_goes_on(self) -> None:
        probe = {"status": "fail", "cause": "key_rejected", "message": "the provider rejected the key",
                 "fix": "lifecontext llm set --api-key-stdin < FILE", "reflect": {"status": "skipped"}}
        out = self.check(json.dumps({"model_probe": probe}), status=3)
        self.assertIn("The model check failed: the provider rejected the key.\n"
                      "Fix it with: lifecontext llm set --api-key-stdin < FILE\n", out)
        self.assertTrue(out.endswith("model_state=failed\n"))
        self.assertIn("the provider rejected the key", (self.tmp / "model-check").read_text())

    def test_a_reflect_warning_and_a_check_that_did_not_run(self) -> None:
        out = self.check(json.dumps({"model_probe": {"status": "pass", "cause": None, "reflect": {
            "status": "warn", "message": "the model refused a forced tool call"}}}))
        self.assertIn("Warning: the model refused a forced tool call.", out)
        out = self.check("Traceback: not JSON", status=1)
        self.assertIn("did not run", out)
        self.assertTrue(out.endswith("model_state=failed\n"))

    def test_the_check_can_be_skipped(self) -> None:
        for environ in ({"SKIP": "1"}, {"LIFECONTEXT_SKIP_MODEL_PROBE": "1"}):
            with self.subTest(environ=environ):
                out = self.check("{}", **environ)
                self.assertIn("Skipped", out)
                self.assertTrue(out.endswith("model_state=not checked\n"))
        self.assertFalse(self.calls.exists())


if __name__ == "__main__":
    unittest.main()
