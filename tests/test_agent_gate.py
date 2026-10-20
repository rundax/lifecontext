"""lifecontext agent-gate: allowlist, refusals, staged inputs, downloads and cleanup."""

from __future__ import annotations

import contextlib
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import agent_gate_stub

from lifecontext import agent_gate, cli

SRC = Path(__file__).resolve().parents[1] / "src"
STUB = Path(agent_gate_stub.__file__).resolve()
OP_ID = "op_" + "0" * 32
REFUSED = (
    "init", "llm show", "service status", "backup", "restore", "hindsight", "skills check", "maintain",
    "reconcile", "preflight", "serve", "export out.bin", "import bundle.bin", "capture --once", "agent-gate",
)
ALLOWED = (
    "status", "doctor", "retain src_a 1", "recall --query q", "reflect --query q", "evidence src_a --version 1",
    "curate delete src_a", "relationships list", "documents list", "categories list", "wiki list", "brief",
    f"operations get {OP_ID}", "mcp",
)
NOT_ALLOWED = '{"error":"command_not_allowed"}\n'


def parse_frame(output: bytes) -> tuple[bytes, bytes]:
    """The original and the command output of a successful `documents download ID @stdout`."""
    header, _, body = output.partition(b"\n")
    marker, file_length, result_length = header.rsplit(b" ", 2)
    assert marker == b"lifecontext-file 1", header
    assert len(body) == int(file_length) + int(result_length)
    return body[: int(file_length)], body[int(file_length):]


class GateTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="agent-gate-test.")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "home"
        self.home.mkdir()
        self.stage_root = self.home / ".local" / "state" / "lifecontext" / "agent-stage"
        agent_gate_stub.CALLS.clear()

    def leftovers(self) -> list[str]:
        return sorted(os.listdir(self.stage_root)) if self.stage_root.exists() else []

    def gate(self, command: str | None, stdin: bytes = b"") -> tuple[int, bytes]:
        """Run the gate in this process with the stand-in CLI."""
        output = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", write_through=True)
        with (
            mock.patch.dict(os.environ, {"HOME": str(self.home)}),
            mock.patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO(stdin), encoding="utf-8")),
            mock.patch.object(sys, "stdout", output),
        ):
            os.environ.pop("SSH_ORIGINAL_COMMAND", None)
            if command is not None:
                os.environ["SSH_ORIGINAL_COMMAND"] = command
            code = agent_gate.run(main=agent_gate_stub.stub_main)
        return code, output.buffer.getvalue()

    def report(self, command: str, stdin: bytes = b"") -> dict:
        code, output = self.gate(command, stdin)
        self.assertEqual(code, 0, output)
        return json.loads(output)


class AllowlistTests(GateTestCase):
    def test_everyday_commands_run(self) -> None:
        for command in ALLOWED:
            with self.subTest(command=command):
                agent_gate_stub.CALLS.clear()
                code, _ = self.gate(f"lifecontext {command}")
                self.assertEqual(code, 0)
                self.assertEqual(agent_gate_stub.CALLS, [command.split()])

    def test_other_commands_are_refused_without_running_anything(self) -> None:
        for command in REFUSED:
            with self.subTest(command=command):
                self.assertEqual(self.gate(f"lifecontext {command}"), (1, NOT_ALLOWED.encode()))
        self.assertEqual(self.gate("lifecontext --json --port 8765 init"), (1, NOT_ALLOWED.encode()))
        self.assertEqual(self.gate("lifecontext init --help"), (1, NOT_ALLOWED.encode()))
        self.assertEqual(agent_gate_stub.CALLS, [])

    def test_root_options_before_the_command_are_fine(self) -> None:
        self.assertEqual(self.gate("lifecontext --json --port 9000 status")[0], 0)
        self.assertEqual(agent_gate_stub.CALLS, [["--json", "--port", "9000", "status"]])

    def test_help_is_allowed(self) -> None:
        for command in ("lifecontext --help", "lifecontext retain --help", "lifecontext documents import --help"):
            with self.subTest(command=command):
                code, output = self.gate(command)
                self.assertEqual(code, 0)
                self.assertIn(b"usage: lifecontext", output)
        self.assertEqual(agent_gate_stub.CALLS, [])

    def test_a_usage_error_keeps_the_cli_status(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.gate("lifecontext recall")[0], 2)
        self.assertEqual(agent_gate_stub.CALLS, [])

    def test_the_command_line_must_start_with_lifecontext(self) -> None:
        for command in (None, "", "   ", "ls", "status", "/usr/bin/lifecontext status", "lifecontextx status",
                        "sh -c 'lifecontext status'", "lifecontext;status", "LIFECONTEXT status"):
            with self.subTest(command=command):
                self.assertEqual(self.gate(command), (1, NOT_ALLOWED.encode()))
        self.assertEqual(agent_gate_stub.CALLS, [])

    def test_an_unterminated_quote_is_an_invalid_request(self) -> None:
        self.assertEqual(self.gate("lifecontext recall --query 'oops"), (1, b'{"error":"invalid_request"}\n'))

    def test_no_shell_ever_reads_the_command(self) -> None:
        query = "a; rm -rf ~ && $(id) `id` | cat > /tmp/x"
        marker = Path(self.temp.name) / "marker"
        with contextlib.redirect_stderr(io.StringIO()):
            for command in (f"lifecontext recall --query 'x; touch {marker}'", f"lifecontext status; touch {marker}",
                            f"lifecontext status $(touch {marker})"):
                self.gate(command)
        self.gate(f"lifecontext recall --query '{query}'")
        self.assertIn(["recall", "--query", query], agent_gate_stub.CALLS)
        self.assertFalse(marker.exists())

    def test_unicode_and_quotes_arrive_intact(self) -> None:
        query = "it's «Привіт» — ☃ \"q\" $HOME"
        self.assertEqual(self.gate("lifecontext recall --query 'it'\\''s «Привіт» — ☃ \"q\" $HOME'")[0], 0)
        self.assertEqual(agent_gate_stub.CALLS, [["recall", "--query", query]])


class PathTests(GateTestCase):
    def test_server_paths_are_refused(self) -> None:
        for command in (
            "retain src_a 1 --input /etc/hostname", "retain src_a 1 --input=relative.txt",
            "curate correct src_a --input ../secret", "categories propose --input /tmp/p.json",
            "documents import /etc/hostname", "documents download src_a /tmp/out", "documents download src_a out.bin",
            "mcp --allow-writes --import-dir /tmp",
        ):
            with self.subTest(command=command):
                self.assertEqual(self.gate(f"lifecontext {command}"), (1, b'{"error":"path_not_allowed"}\n'))
        self.assertEqual(agent_gate_stub.CALLS, [])
        self.assertEqual(self.leftovers(), [])

    def test_stdin_stays_stdin(self) -> None:
        output = self.report("lifecontext retain src_a 1 --input - --json", b"text\n")
        self.assertEqual(output["args"], ["retain", "src_a", "1", "--input", "-", "--json"])
        self.assertEqual(output["stdin"], "text\n")
        self.assertEqual(self.leftovers(), [])

    def test_mcp_and_documents_import_read_stdin_too(self) -> None:
        self.assertEqual(self.report("lifecontext mcp", b"{}\n")["stdin"], "{}\n")
        self.assertEqual(self.report("lifecontext documents import - --filename a.txt", b"x")["stdin"], "x")


class StagingTests(GateTestCase):
    def test_stdin_becomes_a_private_file_that_is_removed_afterwards(self) -> None:
        data = "Мій запис\n".encode()
        output = self.report("lifecontext retain src_a 1 --input @stdin:note.txt --json", data)
        (path, staged), = output["files"].items()
        self.assertEqual(output["args"], ["retain", "src_a", "1", "--input", path, "--json"])
        self.assertEqual(Path(path).name, "note.txt")
        self.assertTrue(path.startswith(f"{self.stage_root}{os.sep}"))
        self.assertEqual(staged["text"], "Мій запис\n")
        self.assertEqual(staged["mode"], "0o600")
        self.assertEqual(staged["dir_mode"], "0o700")
        self.assertEqual(oct(os.stat(self.stage_root).st_mode & 0o777), "0o700")
        self.assertEqual(self.leftovers(), [])

    def test_every_input_position(self) -> None:
        for command, expect in (
            ("retain src_a 1 --input=@stdin:a.txt", lambda a, p: a[3] == f"--input={p}"),
            ("curate correct src_a --input @stdin:a.txt --idempotency-key k", lambda a, p: p in a),
            ("categories propose --input @stdin:a.txt", lambda a, p: a[-1] == p),
            ("documents import @stdin:a.txt --mime text/plain", lambda a, p: a[2] == p),
            ("documents import --mime text/plain @stdin:a.txt", lambda a, p: a[-1] == p),
        ):
            with self.subTest(command=command):
                output = self.report(f"lifecontext {command}", b"body")
                (path, staged), = output["files"].items()
                self.assertTrue(expect(output["args"], path), output["args"])
                self.assertEqual((Path(path).name, staged["text"]), ("a.txt", "body"))
        self.assertEqual(self.leftovers(), [])

    def test_the_token_is_only_replaced_where_the_cli_reads_an_input(self) -> None:
        output = self.report("lifecontext recall --query @stdin:note.txt")
        self.assertEqual(output["args"], ["recall", "--query", "@stdin:note.txt"])
        self.assertEqual(self.leftovers(), [])

    def test_only_one_staged_input_per_call(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.gate("lifecontext documents import @stdin:a.txt @stdin:b.txt", b"x")[0], 2)
        self.assertEqual(agent_gate_stub.CALLS, [])
        self.assertEqual(self.leftovers(), [])

    def test_names_are_checked(self) -> None:
        for name in ("", ".hidden", "..", "../x", "a/b", "/abs", "x" * 201, "й" * 101, "a\nb", "a\x00b", "tab\there"):
            with self.subTest(name=name):
                self.assertFalse(agent_gate.valid_name(name))
        for name in ("note.txt", "Мій файл (1).txt", "a b", "x" * 200, "й" * 100, "-dash", "a'b\"c$d", "a.b."):
            with self.subTest(name=name):
                self.assertTrue(agent_gate.valid_name(name))
        for name in (".hidden", "a/b", "x" * 201):
            with self.subTest(refused=name):
                self.assertEqual(self.gate(f"lifecontext retain s 1 --input '@stdin:{name}'", b"x"),
                                 (1, b'{"error":"invalid_request"}\n'))
        self.assertEqual(agent_gate_stub.CALLS, [])
        self.assertFalse(self.stage_root.exists())

    def test_unusual_valid_names_arrive_whole(self) -> None:
        output = self.report("lifecontext documents import '@stdin:Мій файл (1)'\\''s.pdf'", b"x")
        (path,) = output["files"]
        self.assertEqual(Path(path).name, "Мій файл (1)'s.pdf")

    def test_input_is_limited_like_the_cli(self) -> None:
        for command, limit in (
            ("retain src_a 1 --input @stdin:a.txt", cli.MAX_TEXT_BYTES),
            ("curate correct src_a --input @stdin:a.txt", cli.MAX_TEXT_BYTES),
            ("categories propose --input @stdin:a.json", cli.MAX_REQUEST_BYTES),
        ):
            with self.subTest(command=command):
                self.assertEqual(self.gate(f"lifecontext {command}", b"x" * limit)[0], 0)
                self.assertEqual(self.gate(f"lifecontext {command}", b"x" * (limit + 1)),
                                 (1, b'{"error":"body_too_large"}\n'))
        self.assertEqual(len(agent_gate_stub.CALLS), 3)
        self.assertEqual(self.leftovers(), [])

    def test_documents_may_be_larger(self) -> None:
        self.assertGreater(cli.MAX_DOCUMENT_BYTES, cli.MAX_REQUEST_BYTES)
        output = self.report("lifecontext documents import @stdin:big.bin", b"x" * (cli.MAX_REQUEST_BYTES + 1))
        (staged,) = output["files"].values()
        self.assertEqual(staged["size"], cli.MAX_REQUEST_BYTES + 1)

    def test_cleanup_after_a_failing_or_crashing_command(self) -> None:
        self.assertEqual(self.gate("lifecontext wiki lint --fail-on error")[0], 3)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.gate("lifecontext retain src_a 1 --input @stdin:a.txt --fail-on x", b"x")[0], 2)
        with (
            mock.patch.object(agent_gate_stub, "stub_main", side_effect=RuntimeError("boom")),
            self.assertRaises(RuntimeError),
        ):
            self.gate("lifecontext retain src_a 1 --input @stdin:a.txt", b"x")
        with mock.patch.object(agent_gate_stub, "stub_main", side_effect=SystemExit(1)):
            self.assertEqual(self.gate("lifecontext retain src_a 1 --input @stdin:a.txt", b"x")[0], 1)
        self.assertEqual(self.leftovers(), [])

    def test_the_exit_status_of_the_command_is_kept(self) -> None:
        with mock.patch.object(agent_gate_stub, "stub_main", return_value=7):
            self.assertEqual(self.gate("lifecontext retain src_a 1 --input @stdin:a.txt", b"x")[0], 7)
            self.assertEqual(self.gate("lifecontext status")[0], 7)
        self.assertEqual(self.leftovers(), [])

    def test_stale_directories_are_swept(self) -> None:
        old = self.stage_root / "stage-old"
        fresh = self.stage_root / "stage-fresh"
        for path in (old, fresh):
            path.mkdir(parents=True, mode=0o700)
            (path / "leftover.txt").write_text("note")
        long_ago = time.time() - 3 * 24 * 3600
        os.utime(old, (long_ago, long_ago))
        self.report("lifecontext retain src_a 1 --input @stdin:a.txt", b"x")
        self.assertEqual(self.leftovers(), ["stage-fresh"])

    def test_a_linked_stage_root_is_refused(self) -> None:
        elsewhere = Path(self.temp.name) / "elsewhere"
        elsewhere.mkdir()
        self.stage_root.parent.mkdir(parents=True)
        self.stage_root.symlink_to(elsewhere)
        self.assertEqual(self.gate("lifecontext retain src_a 1 --input @stdin:a.txt", b"x"),
                         (1, b'{"error":"input_unavailable"}\n'))
        self.assertEqual(os.listdir(elsewhere), [])


class DownloadTests(GateTestCase):
    def test_the_original_and_the_result_come_back_in_one_frame(self) -> None:
        code, output = self.gate("lifecontext documents download src_a @stdout --json")
        self.assertEqual(code, 0)
        original, result = parse_frame(output)
        self.assertEqual(original, agent_gate_stub.ORIGINAL)
        self.assertEqual(json.loads(result), {"source_id": "src_a", "size_bytes": len(agent_gate_stub.ORIGINAL)})
        (call,) = agent_gate_stub.CALLS
        self.assertTrue(call[3].startswith(f"{self.stage_root}{os.sep}"))
        self.assertEqual(self.leftovers(), [])

    def test_a_failure_sends_only_the_error_output(self) -> None:
        self.assertEqual(self.gate("lifecontext documents download fail_download @stdout --json"),
                         (1, b'{"error":"api_unavailable"}\n'))
        self.assertEqual(self.leftovers(), [])

    def test_a_missing_download_is_an_output_error(self) -> None:
        self.assertEqual(self.gate("lifecontext documents download no_file @stdout"),
                         (1, b'{"error":"output_unavailable"}\n'))
        self.assertEqual(self.leftovers(), [])

    def test_an_empty_result_is_a_valid_frame(self) -> None:
        def silent(argv):
            Path(argv[3]).write_bytes(b"data")
            return 0

        with mock.patch.object(agent_gate_stub, "stub_main", silent):
            self.assertEqual(self.gate("lifecontext documents download src_a @stdout")[1],
                             b"lifecontext-file 1 4 0\ndata")


class ProcessTests(unittest.TestCase):
    """The gate as a real process: stdin through a pipe, signals and the CLI wiring."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="agent-gate-test.")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "home"
        self.home.mkdir()
        self.stage_root = self.home / ".local" / "state" / "lifecontext" / "agent-stage"
        self.env = {
            **os.environ,
            "HOME": str(self.home),
            "PYTHONPATH": os.pathsep.join([str(SRC), os.environ.get("PYTHONPATH", "")]),
        }

    def command(self, ssh_command: str | None, *argv: str, **extra: str) -> subprocess.Popen:
        env = {**self.env, **extra}
        env.pop("SSH_ORIGINAL_COMMAND", None)
        if ssh_command is not None:
            env["SSH_ORIGINAL_COMMAND"] = ssh_command
        return subprocess.Popen([sys.executable, *argv], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=env)

    def test_binary_stdin_through_a_pipe(self) -> None:
        data = bytes(range(256)) * 1500
        process = self.command("lifecontext documents import @stdin:blob.bin", str(STUB))
        stdout, stderr = process.communicate(data, timeout=120)
        self.assertEqual(process.returncode, 0, stderr)
        (staged,) = json.loads(stdout)["files"].values()
        self.assertEqual(staged["size"], len(data))
        self.assertEqual(os.listdir(self.stage_root), [])

    def test_signals_remove_the_directory(self) -> None:
        for number in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            with self.subTest(signal=number.name):
                ready = Path(self.temp.name) / f"ready-{number.name}"
                process = self.command("lifecontext retain src_a 1 --input @stdin:note.txt", str(STUB),
                                       STUB_READY=str(ready))
                self.addCleanup(process.kill)
                process.stdin.write(b"private note")
                process.stdin.close()
                deadline = time.monotonic() + 120
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(ready.exists())
                self.assertEqual(len(os.listdir(self.stage_root)), 1)
                process.send_signal(number)
                process.wait(timeout=30)
                with process.stdout, process.stderr:
                    stdout, stderr = process.stdout.read(), process.stderr.read()
                self.assertEqual(process.returncode, 128 + number, stderr)
                self.assertEqual((stdout, os.listdir(self.stage_root)), (b"", []))

    def test_the_cli_runs_the_gate(self) -> None:
        for ssh_command, status, expected in (
            (None, 1, NOT_ALLOWED),
            ("lifecontext init", 1, NOT_ALLOWED),
            ("lifecontext agent-gate", 1, NOT_ALLOWED),
            ("lifecontext --version", 0, None),
        ):
            with self.subTest(command=ssh_command):
                process = self.command(ssh_command, "-m", "lifecontext.cli", "agent-gate")
                stdout, stderr = process.communicate(timeout=120)
                self.assertEqual((process.returncode, stderr), (status, b""), stdout)
                if expected is not None:
                    self.assertEqual(stdout.decode(), expected)
                else:
                    self.assertTrue(stdout.startswith(b"lifecontext "))
        self.assertFalse(self.stage_root.exists())


if __name__ == "__main__":
    unittest.main()
