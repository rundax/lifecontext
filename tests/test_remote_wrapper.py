"""bin/lifecontext-remote: command quoting, file mapping and downloads, with a fake ssh.

The fake ssh either records the call and answers from the test, or (gate mode)
runs the gate locally, as sshd would for a forced command, with the stand-in CLI.
"""

from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import agent_gate_stub

ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "bin" / "lifecontext-remote"
STUB = Path(agent_gate_stub.__file__).resolve()
NOT_ALLOWED = b'{"error":"command_not_allowed"}\n'

FAKE_SSH = f"""#!{sys.executable}
import json, os, sys, time
args = sys.argv[1:]
with open(os.environ["FAKE_SSH_LOG"], "a") as log:
    log.write(json.dumps({{"args": args}}) + "\\n")
if os.environ.get("FAKE_SSH_GATE"):
    assert args[:3] == ["-T", "-o", "BatchMode=yes"] and len(args) == 5, args
    env = {{**os.environ, "SSH_ORIGINAL_COMMAND": args[4], "HOME": os.environ["FAKE_REMOTE_HOME"]}}
    os.execve(sys.executable, [sys.executable, {str(STUB)!r}], env)
data = sys.stdin.buffer.read()
with open(os.environ["FAKE_SSH_LOG"] + ".stdin", "ab") as log:
    log.write(data)
if os.environ.get("FAKE_SSH_CREATE"):
    open(os.environ["FAKE_SSH_CREATE"], "w").write("someone else's file")
if os.environ.get("FAKE_SSH_SLEEP"):
    time.sleep(60)
if os.environ.get("FAKE_SSH_STDOUT"):
    sys.stdout.buffer.write(open(os.environ["FAKE_SSH_STDOUT"], "rb").read())
sys.exit(int(os.environ.get("FAKE_SSH_STATUS", "0")))
"""


def frame(content: bytes, result: bytes) -> bytes:
    return b"lifecontext-file 1 %d %d\n" % (len(content), len(result)) + content + result


class RemoteTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="lifecontext-remote-test.")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        tools = self.root / "tools"
        tools.mkdir()
        (tools / "ssh").write_text(FAKE_SSH)
        (tools / "ssh").chmod(0o755)
        # Agents call the wrapper as `lifecontext`, through a link.
        (tools / "lifecontext").symlink_to(WRAPPER)
        self.tools = tools
        self.log = self.root / "ssh.log"
        self.remote_home = self.root / "remote home"
        self.remote_home.mkdir()
        self.work = self.root / "work dir"
        self.work.mkdir()
        self.env = {
            **os.environ,
            "PATH": f"{tools}:{os.environ['PATH']}",
            "PYTHONPATH": os.pathsep.join([str(ROOT / "src"), os.environ.get("PYTHONPATH", "")]),
            "LIFECONTEXT_REMOTE": "agent-host",
            "LIFECONTEXT_SSH": str(tools / "ssh"),
            "FAKE_SSH_LOG": str(self.log),
            "FAKE_REMOTE_HOME": str(self.remote_home),
        }
        for name in ("FAKE_SSH_GATE", "FAKE_SSH_STDOUT", "FAKE_SSH_STATUS", "FAKE_SSH_CREATE", "FAKE_SSH_SLEEP"):
            self.env.pop(name, None)

    def run_wrapper(self, *args: str, stdin: bytes = b"", env: dict[str, str] | None = None):
        return subprocess.run([str(self.tools / "lifecontext"), *args], input=stdin, capture_output=True,
                              env={**self.env, **(env or {})}, cwd=self.work, timeout=300, check=False)

    def calls(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [json.loads(line)["args"] for line in self.log.read_text().splitlines()]

    def sent_stdin(self) -> bytes:
        path = Path(f"{self.log}.stdin")
        return path.read_bytes() if path.exists() else b""

    def last_command(self) -> list[str]:
        (call,) = self.calls()
        self.assertEqual(call[:4], ["-T", "-o", "BatchMode=yes", "agent-host"])
        self.assertEqual(len(call), 5)
        return shlex.split(call[4])

    def write_file(self, name: str, data: bytes, directory: str = "work dir") -> Path:
        path = self.root / directory / name
        path.write_bytes(data)
        return path

    def leftovers(self) -> list[str]:
        return sorted(os.listdir(self.work))


class CommandTests(RemoteTestCase):
    def test_runs_ssh_with_one_quoted_command(self) -> None:
        result = self.run_wrapper("status", "--json", env={"FAKE_SSH_STDOUT": str(self.write_file("out", b"{}\n"))})
        self.assertEqual((result.returncode, result.stdout), (0, b"{}\n"))
        self.assertEqual(self.calls(), [["-T", "-o", "BatchMode=yes", "agent-host", "lifecontext 'status' '--json'"]])

    def test_ssh_is_the_default_command(self) -> None:
        result = self.run_wrapper("status", env={"LIFECONTEXT_SSH": ""})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.last_command(), ["lifecontext", "status"])

    def test_the_remote_is_required(self) -> None:
        for remote in ("", "-oProxyCommand=x"):
            with self.subTest(remote=remote):
                result = self.run_wrapper("status", env={"LIFECONTEXT_REMOTE": remote})
                self.assertEqual((result.returncode, result.stdout), (1, b""))
                self.assertIn(b"LIFECONTEXT_REMOTE", result.stderr)
                result = self.run_wrapper("status", "--json", env={"LIFECONTEXT_REMOTE": remote})
                self.assertEqual((result.returncode, result.stdout), (1, b'{"error":"remote_not_configured"}\n'))
        self.assertEqual(self.calls(), [])

    def test_every_argument_survives_quoting(self) -> None:
        values = [
            "plain", "two words", "it's", "'", "''", 'say "hi"', "$HOME `id` $(id) ; & | > < ( ) { }", "back\\slash\\",
            "Привіт, світ — ☃ 日本語", "", " ", "line one\nline two", "ends with newline\n", "-leading-dash", "*?[x]",
            "tab\there", "~/x", "%s %d \\n", "!bang", "a=b", "@stdin:fake", "--json",
        ]
        for value in values:
            with self.subTest(value=value):
                self.log.unlink(missing_ok=True)
                self.assertEqual(self.run_wrapper("recall", "--query", value, "--max-results", "3").returncode, 0)
                expected = ["lifecontext", "recall", "--query", value, "--max-results", "3"]
                self.assertEqual(self.last_command(), expected)
                (call,) = self.calls()
                _, _, rest = call[4].partition(" ")
                shell = subprocess.run(["sh", "-c", 'printf "%s\\0" ' + rest], capture_output=True, check=True)
                self.assertEqual(shell.stdout.decode().split("\0")[:-1], expected[1:])

    def test_stdin_and_the_exit_status_pass_through(self) -> None:
        for status in (0, 1, 2, 3, 130, 255):
            with self.subTest(status=status):
                self.log.unlink(missing_ok=True)
                Path(f"{self.log}.stdin").unlink(missing_ok=True)
                result = self.run_wrapper("retain", "src_a", "1", "--input", "-", "--json", stdin="Мій запис\n".encode(),
                                          env={"FAKE_SSH_STATUS": str(status)})
                self.assertEqual(result.returncode, status)
                self.assertEqual(self.sent_stdin(), "Мій запис\n".encode())
                self.assertEqual(self.last_command(), ["lifecontext", "retain", "src_a", "1", "--input", "-", "--json"])

    def test_mcp_is_a_plain_stdio_session(self) -> None:
        self.assertEqual(self.run_wrapper("mcp", "--allow-writes", stdin=b'{"jsonrpc":"2.0"}\n').returncode, 0)
        self.assertEqual(self.last_command(), ["lifecontext", "mcp", "--allow-writes"])
        self.assertEqual(self.sent_stdin(), b'{"jsonrpc":"2.0"}\n')


class InputFileTests(RemoteTestCase):
    NAME = "Мій запис (1).txt"

    def test_files_travel_on_stdin_as_tokens(self) -> None:
        path = self.write_file(self.NAME, "Мій запис\n".encode())
        token = f"@stdin:{self.NAME}"
        for args, expected in (
            (["retain", "src_a", "1", "--input", str(path), "--json"], ["retain", "src_a", "1", "--input", token, "--json"]),
            (["retain", "src_a", "1", f"--input={path}"], ["retain", "src_a", "1", f"--input={token}"]),
            (["--json", "retain", "src_a", "1", "--input", self.NAME], ["--json", "retain", "src_a", "1", "--input", token]),
            (["curate", "correct", "src_a", "--input", str(path), "--idempotency-key", "k"],
             ["curate", "correct", "src_a", "--input", token, "--idempotency-key", "k"]),
            (["categories", "propose", "--input", str(path)], ["categories", "propose", "--input", token]),
            (["documents", "import", "--mime", "text/plain", "--filename", "x.txt", str(path)],
             ["documents", "import", "--mime", "text/plain", "--filename", "x.txt", token]),
            (["documents", "import", str(path), "--no-vision"], ["documents", "import", token, "--no-vision"]),
            (["--json", "documents", "import", self.NAME], ["--json", "documents", "import", token]),
        ):
            with self.subTest(args=args):
                self.log.unlink(missing_ok=True)
                Path(f"{self.log}.stdin").unlink(missing_ok=True)
                result = self.run_wrapper(*args, stdin=b"not this")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.last_command(), ["lifecontext", *expected])
                self.assertEqual(self.sent_stdin(), "Мій запис\n".encode())
                self.assertNotIn(str(self.work).encode(), self.log.read_bytes())

    def test_other_arguments_are_not_files(self) -> None:
        path = self.write_file("note.txt", b"file")
        for args in (
            ["recall", "--query", str(path)],
            ["retain", "src_a", "1", "--idempotency-key", str(path), "--input", "-"],
            ["wiki", "show", str(path)],
            ["evidence", "src_a", "--version", "1"],
            ["documents", "import", "-", "--filename", "note.txt"],
            ["documents", "show", "src_a"],
            ["export", str(path)],
            ["import", str(path)],
        ):
            with self.subTest(args=args):
                self.log.unlink(missing_ok=True)
                Path(f"{self.log}.stdin").unlink(missing_ok=True)
                self.assertEqual(self.run_wrapper(*args, stdin=b"stdin text").returncode, 0)
                self.assertEqual(self.last_command(), ["lifecontext", *args])
                self.assertEqual(self.sent_stdin(), b"stdin text")

    def test_an_unusable_input_file_is_reported_without_calling_ssh(self) -> None:
        real = self.write_file("real.txt", b"x")
        (self.work / "link.txt").symlink_to(real)
        (self.work / "folder").mkdir()
        locked = self.write_file("locked.txt", b"x")
        locked.chmod(0)
        self.addCleanup(locked.chmod, 0o600)
        for target in ("missing.txt", "link.txt", "folder", *(["locked.txt"] if os.geteuid() else [])):
            with self.subTest(target=target):
                for args in (["retain", "src_a", "1", "--input", target], ["documents", "import", target]):
                    result = self.run_wrapper(*args)
                    self.assertEqual((result.returncode, result.stdout), (1, b""))
                    self.assertIn(b"input unavailable", result.stderr)
                    result = self.run_wrapper(*args, "--json")
                    self.assertEqual((result.returncode, result.stdout), (1, b'{"error":"input_unavailable"}\n'))
        self.assertEqual(self.calls(), [])


class DownloadTests(RemoteTestCase):
    ORIGINAL = b"\x00original\xff bytes\r\n\n"

    def download(self, canned: bytes, *args: str, status: int = 0, env: dict[str, str] | None = None):
        out = self.write_file("canned", canned, directory="")
        result = self.run_wrapper("documents", "download", "src_a", *args, env={
            "FAKE_SSH_STDOUT": str(out), "FAKE_SSH_STATUS": str(status), **(env or {})})
        return result

    def test_the_original_is_written_and_the_result_printed(self) -> None:
        for result_text in (b'{"size_bytes":19,"source_id":"src_a"}\n', b"", b"line\n\n\n", b"no newline"):
            with self.subTest(result=result_text):
                self.log.unlink(missing_ok=True)
                destination = self.work / "Копія файлу.bin"
                destination.unlink(missing_ok=True)
                result = self.download(frame(self.ORIGINAL, result_text), str(destination), "--json")
                self.assertEqual((result.returncode, result.stdout), (0, result_text), result.stderr)
                self.assertEqual(destination.read_bytes(), self.ORIGINAL)
                self.assertEqual(oct(destination.stat().st_mode & 0o777), "0o600")
                self.assertEqual(self.last_command(), ["lifecontext", "documents", "download", "src_a", "@stdout", "--json"])
                self.assertEqual(self.leftovers(), ["Копія файлу.bin"])

    def test_options_and_a_relative_destination(self) -> None:
        result = self.download(frame(b"data", b"ok\n"), "--version", "2", "out.bin")
        self.assertEqual((result.returncode, result.stdout), (0, b"ok\n"))
        self.assertEqual(self.last_command(), ["lifecontext", "documents", "download", "src_a", "--version", "2", "@stdout"])
        self.assertEqual((self.work / "out.bin").read_bytes(), b"data")

    def test_an_existing_destination_is_never_overwritten(self) -> None:
        (self.work / "taken").write_text("mine")
        (self.work / "dangling").symlink_to(self.work / "nowhere")
        for name in ("taken", "dangling"):
            with self.subTest(name=name):
                result = self.download(frame(b"data", b""), name, "--json")
                self.assertEqual((result.returncode, result.stdout), (1, b'{"error":"destination_exists"}\n'))
        self.assertEqual((self.work / "taken").read_text(), "mine")
        self.assertFalse((self.work / "nowhere").exists())
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.leftovers(), ["dangling", "taken"])

    def test_a_destination_that_appears_meanwhile_is_kept(self) -> None:
        result = self.download(frame(b"data", b"ok\n"), "late.bin", "--json", env={"FAKE_SSH_CREATE": str(self.work / "late.bin")})
        self.assertEqual((result.returncode, result.stdout), (1, b'{"error":"destination_exists"}\n'))
        self.assertEqual((self.work / "late.bin").read_text(), "someone else's file")
        self.assertEqual(self.leftovers(), ["late.bin"])

    def test_a_failure_prints_the_error_output_and_keeps_the_status(self) -> None:
        for status, canned in ((1, b'{"error":"api_unavailable"}\n'), (1, NOT_ALLOWED), (255, b""), (3, b"text\n")):
            with self.subTest(status=status, canned=canned):
                result = self.download(canned, "out.bin", "--json", status=status)
                self.assertEqual((result.returncode, result.stdout), (status, canned))
                self.assertEqual(self.leftovers(), [])

    def test_a_damaged_frame_is_an_output_error(self) -> None:
        good = frame(b"abcdef", b"ok\n")
        for canned in (
            b'{"size_bytes":1}\n', b"", b"lifecontext-file 1 6 3\n", good[:-1], good + b"x", good[:30],
            b"lifecontext-file 1 6\nabcdefok\n", b"lifecontext-file 1 x 3\nabcdefok\n", b"lifecontext-file 2 6 3\nabcdefok\n",
            b"lifecontext-file 1 6 3 9\nabcdefok\n", b"lifecontext-file 1 -6 3\nabcdefok\n",
        ):
            with self.subTest(canned=canned):
                result = self.download(canned, "out.bin", "--json")
                self.assertEqual((result.returncode, result.stdout), (1, b'{"error":"output_unavailable"}\n'))
                self.assertEqual(self.leftovers(), [])

    def test_an_interrupt_removes_the_partial_files(self) -> None:
        process = subprocess.Popen([str(self.tools / "lifecontext"), "documents", "download", "src_a", "out.bin"],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=self.work, start_new_session=True,
                                   env={**self.env, "FAKE_SSH_SLEEP": "1"})
        deadline = time.monotonic() + 60
        while not [name for name in self.leftovers() if "lifecontext-remote" in name] and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(self.leftovers())
        os.killpg(process.pid, signal.SIGTERM)
        process.communicate(timeout=30)
        self.assertEqual(process.returncode, 130)
        self.assertEqual(self.leftovers(), [])


class EndToEndTests(RemoteTestCase):
    """The wrapper, a fake ssh and the real gate, with the stand-in CLI."""

    def setUp(self) -> None:
        super().setUp()
        self.env["FAKE_SSH_GATE"] = "1"
        self.stage_root = self.remote_home / ".local" / "state" / "lifecontext" / "agent-stage"

    def run_ok(self, *args: str, stdin: bytes = b"") -> bytes:
        result = self.run_wrapper(*args, stdin=stdin)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_a_host_file_reaches_the_command_and_is_removed(self) -> None:
        path = self.write_file("Мій запис.txt", "Мій запис\n".encode())
        output = json.loads(self.run_ok("retain", "src_a", "1", "--input", str(path), "--json"))
        (staged_path, staged), = output["files"].items()
        self.assertEqual(output["args"], ["retain", "src_a", "1", "--input", staged_path, "--json"])
        self.assertEqual((Path(staged_path).name, staged["text"], staged["mode"]), ("Мій запис.txt", "Мій запис\n", "0o600"))
        self.assertTrue(staged_path.startswith(f"{self.stage_root}{os.sep}"))
        document = self.write_file("Звіт, кв. 3.pdf", b"%PDF-\x00\xff")
        output = json.loads(self.run_ok("documents", "import", str(document), "--mime", "application/pdf"))
        (staged_path, staged), = output["files"].items()
        self.assertEqual((output["args"][:2], Path(staged_path).name, staged["size"]), (["documents", "import"], "Звіт, кв. 3.pdf", 7))
        self.assertEqual(os.listdir(self.stage_root), [])

    def test_a_download_is_written_on_the_host(self) -> None:
        result = self.run_ok("documents", "download", "src_a", "Копія.bin", "--json")
        self.assertEqual(json.loads(result), {"source_id": "src_a", "size_bytes": len(agent_gate_stub.ORIGINAL)})
        self.assertEqual((self.work / "Копія.bin").read_bytes(), agent_gate_stub.ORIGINAL)
        self.assertEqual(os.listdir(self.stage_root), [])
        failed = self.run_wrapper("documents", "download", "fail_download", "other.bin", "--json")
        self.assertEqual((failed.returncode, failed.stdout), (1, b'{"error":"api_unavailable"}\n'))
        missing = self.run_wrapper("documents", "download", "no_file", "other.bin", "--json")
        self.assertEqual((missing.returncode, missing.stdout), (1, b'{"error":"output_unavailable"}\n'))
        self.assertEqual(self.leftovers(), ["Копія.bin"])
        self.assertEqual(os.listdir(self.stage_root), [])

    def test_commands_outside_the_allowlist_are_refused(self) -> None:
        for args in (["init"], ["export", "bundle.out"], ["backup", "--json"], ["--json", "service", "status"]):
            with self.subTest(args=args):
                result = self.run_wrapper(*args)
                self.assertEqual((result.returncode, result.stdout), (1, NOT_ALLOWED))
        bundle = self.write_file("bundle.in", b"bundle")
        self.assertEqual(self.run_wrapper("import", str(bundle)).stdout, NOT_ALLOWED)
        result = self.run_wrapper("mcp", "--allow-writes", "--import-dir", "/tmp")
        self.assertEqual((result.returncode, result.stdout), (1, b'{"error":"path_not_allowed"}\n'))
        self.assertEqual(self.leftovers(), ["bundle.in"])
        self.assertFalse(self.stage_root.exists())

    def test_stdin_status_and_quoting_pass_through(self) -> None:
        output = json.loads(self.run_ok("retain", "src_a", "1", "--input", "-", stdin="Привіт\n".encode()))
        self.assertEqual((output["args"], output["stdin"]), (["retain", "src_a", "1", "--input", "-"], "Привіт\n"))
        self.assertEqual(json.loads(self.run_ok("mcp", stdin=b'{"id":1}\n'))["stdin"], '{"id":1}\n')
        self.assertEqual(self.run_wrapper("wiki", "lint", "--fail-on", "error").returncode, 3)
        query = "it's «Привіт» — ☃ \"q\" $HOME `id` ; & \n end\n"
        output = json.loads(self.run_ok("recall", "--query", query, "--json"))
        self.assertEqual(output["args"], ["recall", "--query", query, "--json"])


if __name__ == "__main__":
    unittest.main()
