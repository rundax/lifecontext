"""lifecontext agent-gate: allowlist, refusals, staged inputs, downloads and cleanup."""

from __future__ import annotations

import contextlib
import fcntl
import io
import json
import os
import re
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
    "curate reindex src_a", "relationships list", "documents list", "categories list", "wiki list", "brief",
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
        """Staging directories still there (the slot files stay)."""
        if not self.stage_root.exists():
            return []
        return sorted(name for name in os.listdir(self.stage_root) if not name.startswith("slot-"))

    def gate(self, command: str | None, stdin: bytes | io.BufferedReader = b"", **options) -> tuple[int, bytes]:
        """Run the gate in this process with the stand-in CLI; options are those of the forced command."""
        output = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", write_through=True)
        stream = io.BytesIO(stdin) if isinstance(stdin, bytes) else stdin
        with (
            mock.patch.dict(os.environ, {"HOME": str(self.home)}),
            mock.patch.object(sys, "stdin", io.TextIOWrapper(stream, encoding="utf-8")),
            mock.patch.object(sys, "stdout", output),
        ):
            os.environ.pop("SSH_ORIGINAL_COMMAND", None)
            if command is not None:
                os.environ["SSH_ORIGINAL_COMMAND"] = command
            code = agent_gate.run(main=agent_gate_stub.stub_main, **options)
        return code, output.buffer.getvalue()

    def audit_log(self) -> list[str]:
        path = self.home / ".local" / "state" / "lifecontext" / "agent-gate.log"
        return path.read_text().splitlines() if path.exists() else []

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
        self.assertEqual(self.gate("lifecontext --json --port 8765 status")[0], 0)
        self.assertEqual(agent_gate_stub.CALLS, [["--json", "--port", "8765", "status"]])

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


class DeleteTests(GateTestCase):
    def test_deleting_and_forgetting_are_refused_by_default(self) -> None:
        for action in sorted(agent_gate.DELETING_ACTIONS):
            for command in (f"lifecontext curate {action} src_a", f"lifecontext --json curate {action} src_a --expected-version 1"):
                with self.subTest(command=command):
                    self.assertEqual(self.gate(command), (1, NOT_ALLOWED.encode()))
        self.assertEqual(agent_gate_stub.CALLS, [])

    def test_the_other_curate_actions_still_run(self) -> None:
        for action in ("correct", "reindex"):
            self.assertEqual(self.gate(f"lifecontext curate {action} src_a")[0], 0)
        self.assertEqual(agent_gate_stub.CALLS, [["curate", "correct", "src_a"], ["curate", "reindex", "src_a"]])

    def test_the_forced_command_can_allow_them(self) -> None:
        for action in sorted(agent_gate.DELETING_ACTIONS):
            with self.subTest(action=action):
                agent_gate_stub.CALLS.clear()
                self.assertEqual(self.gate(f"lifecontext curate {action} src_a", allow_delete=True)[0], 0)
                self.assertEqual(agent_gate_stub.CALLS, [["curate", action, "src_a"]])

    def test_the_actions_listed_are_the_ones_the_cli_has(self) -> None:
        actions = next(a for a in cli._build_parser()._subparsers._group_actions[0].choices["curate"]._actions
                       if a.dest == "action").choices
        self.assertEqual(set(actions) - agent_gate.DELETING_ACTIONS, {"correct", "reindex"})
        self.assertLessEqual(agent_gate.DELETING_ACTIONS, set(actions))


class PortTests(GateTestCase):
    def test_the_port_is_the_gates_own(self) -> None:
        for command in ("--port 9000 status", "status --port 9000", "mcp --port 9000", "--port=9000 status",
                        "--port 8765 --port 9000 status", "recall --query q --port 9001"):
            with self.subTest(command=command):
                self.assertEqual(self.gate(f"lifecontext {command}"), (1, NOT_ALLOWED.encode()))
        self.assertEqual(agent_gate_stub.CALLS, [])
        for command in ("--port 8765 status", "status --port 8765", "mcp --port 8765"):
            with self.subTest(command=command):
                self.assertEqual(self.gate(f"lifecontext {command}")[0], 0)

    def test_a_gate_with_another_port_runs_every_command_against_it(self) -> None:
        self.assertEqual(self.gate("lifecontext status", port=9000)[0], 0)
        self.assertEqual(self.gate("lifecontext --port 9000 status", port=9000)[0], 0)
        self.assertEqual(self.gate("lifecontext mcp --port 9000", port=9000)[0], 0)
        self.assertEqual(agent_gate_stub.CALLS, [
            ["--port", "9000", "status"], ["--port", "9000", "--port", "9000", "status"],
            ["--port", "9000", "mcp", "--port", "9000"],
        ])
        agent_gate_stub.CALLS.clear()
        for command in ("status --port 8765", "--port 8765 status", "status --port 9001", "--port 9001 mcp"):
            with self.subTest(command=command):
                self.assertEqual(self.gate(f"lifecontext {command}", port=9000), (1, NOT_ALLOWED.encode()))
        self.assertEqual(agent_gate_stub.CALLS, [])


class LoadTests(GateTestCase):
    def hold(self, *numbers: int) -> None:
        """Hold these slots, as other running calls would."""
        self.stage_root.mkdir(parents=True, mode=0o700, exist_ok=True)
        for number in numbers:
            descriptor = os.open(self.stage_root / f"slot-{number}", os.O_RDWR | os.O_CREAT, 0o600)
            self.addCleanup(os.close, descriptor)
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_the_default_is_four_calls_at_a_time(self) -> None:
        self.hold(0, 1, 2)
        self.assertEqual(self.gate("lifecontext status")[0], 0)
        self.hold(3)
        self.assertEqual(self.gate("lifecontext status"), (1, b'{"error":"busy"}\n'))
        self.assertEqual(len(agent_gate_stub.CALLS), 1)

    def test_every_kind_of_call_needs_a_slot(self) -> None:
        self.hold(0, 1)
        for command in ("status", "mcp", "retain src_a 1 --input @stdin:a.txt", "documents download src_a @stdout"):
            with self.subTest(command=command):
                self.assertEqual(self.gate(f"lifecontext {command}", b"x", max_calls=2), (1, b'{"error":"busy"}\n'))
        self.assertEqual(agent_gate_stub.CALLS, [])
        self.assertEqual(self.leftovers(), [])

    def test_help_and_refusals_need_no_slot(self) -> None:
        self.hold(0)
        self.assertEqual(self.gate("lifecontext --help", max_calls=1)[0], 0)
        self.assertEqual(self.gate("lifecontext init", max_calls=1), (1, NOT_ALLOWED.encode()))

    def test_a_finished_call_gives_its_slot_back(self) -> None:
        for _ in range(6):
            self.assertEqual(self.gate("lifecontext status", max_calls=1)[0], 0)
        with (
            mock.patch.object(agent_gate_stub, "stub_main", side_effect=RuntimeError("boom")),
            contextlib.suppress(RuntimeError),
        ):
            self.gate("lifecontext status", max_calls=1)
        self.assertEqual(self.gate("lifecontext status", max_calls=1)[0], 0)

    def test_an_mcp_session_holds_its_slot_while_it_lasts(self) -> None:
        held = []

        def session(argv):
            for number in (0, 1):
                descriptor = os.open(self.stage_root / f"slot-{number}", os.O_RDWR | os.O_CREAT, 0o600)
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    held.append(False)
                except BlockingIOError:
                    held.append(True)
                finally:
                    os.close(descriptor)
            return 0

        with mock.patch.object(agent_gate_stub, "stub_main", session):
            self.assertEqual(self.gate("lifecontext mcp", max_calls=2)[0], 0)
        self.assertEqual(held, [True, False])  # slot-0 was in use; slot-1 never was
        self.assertEqual(self.gate("lifecontext status", max_calls=1)[0], 0)

    def test_a_stalled_input_times_out_and_is_cleaned_up(self) -> None:
        read, write = os.pipe()
        self.addCleanup(os.close, write)
        stalled = os.fdopen(read, "rb")
        self.addCleanup(stalled.close)
        started = time.monotonic()
        with mock.patch.object(agent_gate, "INPUT_DEADLINE_SECONDS", 1):
            result = self.gate("lifecontext retain src_a 1 --input @stdin:a.txt", stalled)
        self.assertEqual(result, (1, b'{"error":"input_timeout"}\n'))
        self.assertLess(time.monotonic() - started, 30)
        self.assertEqual((agent_gate_stub.CALLS, self.leftovers()), ([], []))
        self.assertEqual(signal.getsignal(signal.SIGALRM), signal.SIG_DFL)
        self.assertEqual(signal.alarm(0), 0)

    def test_input_that_arrives_in_time_is_taken(self) -> None:
        with mock.patch.object(agent_gate, "INPUT_DEADLINE_SECONDS", 30):
            self.assertEqual(self.gate("lifecontext retain src_a 1 --input @stdin:a.txt", b"x")[0], 0)
        self.assertEqual(signal.alarm(0), 0)


class AuditLogTests(GateTestCase):
    LINE = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00 command=(\S+) action=(\S+) refused=(\S+) status=(\d+)")

    def entries(self) -> list[tuple[str, ...]]:
        return [self.LINE.fullmatch(line).groups() for line in self.audit_log()]

    def test_only_the_command_the_action_the_refusal_and_the_status_are_logged(self) -> None:
        secrets = ("SECRET-QUERY", "secret_src", "SECRET-NAME", "SECRET-WORD", "SECRET BODY", "secret-key", "secret.pdf")
        self.gate("lifecontext retain secret_src 1 --input - --idempotency-key secret-key --json", b"SECRET BODY")
        self.gate("lifecontext recall --query SECRET-QUERY")
        self.gate("lifecontext retain secret_src 1 --input @stdin:SECRET-NAME.txt", b"SECRET BODY")
        self.gate("lifecontext documents import @stdin:secret.pdf --filename secret.pdf", b"SECRET BODY")
        self.gate("lifecontext curate delete secret_src --expected-version 3")
        self.gate("lifecontext curate correct secret_src --input @stdin:secret.pdf", b"SECRET BODY")
        self.gate("lifecontext documents download secret_src @stdout")
        self.gate("lifecontext init --vault /home/SECRET-WORD")
        self.gate("lifecontext SECRET-WORD --query SECRET-QUERY")
        self.gate("lifecontext retain secret_src 1 --input /etc/SECRET-WORD")
        self.gate("lifecontext retain secret_src 1 --input '@stdin:.SECRET-NAME'", b"SECRET BODY")
        self.gate("SECRET-WORD status")
        self.gate("lifecontext recall --query 'SECRET-QUERY")
        self.gate(None)
        text = "\n".join(self.audit_log())
        for secret in secrets:
            self.assertNotIn(secret, text)
        self.assertEqual(self.entries(), [
            ("retain", "-", "-", "0"), ("recall", "-", "-", "0"), ("retain", "-", "-", "0"), ("documents", "-", "-", "0"),
            ("curate", "delete", "command_not_allowed", "1"), ("curate", "correct", "-", "0"), ("documents", "-", "-", "0"),
            ("init", "-", "command_not_allowed", "1"), ("other", "-", "command_not_allowed", "1"),
            ("retain", "-", "path_not_allowed", "1"), ("retain", "-", "invalid_request", "1"),
            ("-", "-", "command_not_allowed", "1"), ("-", "-", "invalid_request", "1"), ("-", "-", "command_not_allowed", "1"),
        ])

    def test_the_status_and_busy_are_logged(self) -> None:
        with mock.patch.object(agent_gate_stub, "stub_main", return_value=3):
            self.gate("lifecontext wiki lint --fail-on error")
        descriptor = os.open(self.stage_root / "slot-0", os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, descriptor)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.gate("lifecontext status", max_calls=1)
        self.assertEqual(self.entries(), [("wiki", "-", "-", "3"), ("status", "-", "busy", "1")])

    def test_the_file_is_private_and_rotates(self) -> None:
        path = self.home / ".local" / "state" / "lifecontext" / "agent-gate.log"
        with mock.patch.object(agent_gate, "LOG_LIMIT_BYTES", 300):
            for _ in range(12):
                self.gate("lifecontext init")
        rotated = path.with_name("agent-gate.log.1")
        self.assertTrue(rotated.exists())
        self.assertLess(path.stat().st_size, 600)
        for logged in (path, rotated):
            self.assertEqual(oct(logged.stat().st_mode & 0o777), "0o600")
        self.assertEqual(oct(path.parent.stat().st_mode & 0o777), "0o700")
        self.assertTrue(0 < len(self.entries()) + len(rotated.read_text().splitlines()) <= 12)

    def test_a_log_that_cannot_be_written_does_not_stop_the_call(self) -> None:
        path = self.home / ".local" / "state" / "lifecontext" / "agent-gate.log"
        path.mkdir(parents=True)
        self.assertEqual(self.gate("lifecontext status")[0], 0)

    def test_every_command_the_log_names_is_one_the_cli_has(self) -> None:
        choices = cli._build_parser()._subparsers._group_actions[0].choices
        self.assertEqual(set(choices), agent_gate.KNOWN_COMMANDS)


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

    def stage_dirs(self) -> list[str]:
        return sorted(name for name in os.listdir(self.stage_root) if name.startswith("stage-"))

    def wait_for(self, path: Path) -> None:
        deadline = time.monotonic() + 120
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(path.exists())

    def test_binary_stdin_through_a_pipe(self) -> None:
        data = bytes(range(256)) * 1500
        process = self.command("lifecontext documents import @stdin:blob.bin", str(STUB))
        stdout, stderr = process.communicate(data, timeout=120)
        self.assertEqual(process.returncode, 0, stderr)
        (staged,) = json.loads(stdout)["files"].values()
        self.assertEqual(staged["size"], len(data))
        self.assertEqual(self.stage_dirs(), [])

    def test_a_killed_call_frees_its_slot(self) -> None:
        options = {"GATE_OPTIONS": json.dumps({"max_calls": 1})}
        ready = Path(self.temp.name) / "ready-busy"
        holder = self.command("lifecontext mcp", str(STUB), STUB_READY=str(ready), **options)
        self.addCleanup(holder.kill)
        self.wait_for(ready)
        busy = self.command("lifecontext status", str(STUB), **options)
        stdout, stderr = busy.communicate(timeout=120)
        self.assertEqual((busy.returncode, stdout), (1, b'{"error":"busy"}\n'), stderr)
        holder.kill()
        holder.wait(timeout=30)
        for stream in (holder.stdin, holder.stdout, holder.stderr):
            stream.close()
        free = self.command("lifecontext status", str(STUB), **options)
        stdout, stderr = free.communicate(timeout=120)
        self.assertEqual(free.returncode, 0, stderr)
        self.assertEqual(json.loads(stdout)["args"], ["status"])

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
                self.assertEqual(len(self.stage_dirs()), 1)
                process.send_signal(number)
                process.wait(timeout=30)
                with process.stdout, process.stderr:
                    stdout, stderr = process.stdout.read(), process.stderr.read()
                self.assertEqual(process.returncode, 128 + number, stderr)
                self.assertEqual((stdout, self.stage_dirs()), (b"", []))

    def test_the_cli_runs_the_gate(self) -> None:
        for ssh_command, options, status, expected in (
            (None, [], 1, NOT_ALLOWED),
            ("lifecontext init", [], 1, NOT_ALLOWED),
            ("lifecontext init", ["--allow-delete", "--max-calls", "2", "--port", "9000"], 1, NOT_ALLOWED),
            ("lifecontext agent-gate", [], 1, NOT_ALLOWED),
            ("lifecontext --version", [], 0, None),
        ):
            with self.subTest(command=ssh_command, options=options):
                process = self.command(ssh_command, "-m", "lifecontext.cli", "agent-gate", *options)
                stdout, stderr = process.communicate(timeout=120)
                self.assertEqual((process.returncode, stderr), (status, b""), stdout)
                if expected is not None:
                    self.assertEqual(stdout.decode(), expected)
                else:
                    self.assertTrue(stdout.startswith(b"lifecontext "))
        self.assertFalse(self.stage_root.exists())

    def test_the_options_of_the_gate_are_checked(self) -> None:
        process = self.command(None, "-m", "lifecontext.cli", "agent-gate", "--max-calls", "0")
        stdout, stderr = process.communicate(timeout=120)
        self.assertEqual((process.returncode, stdout), (2, b""))
        self.assertIn(b"--max-calls", stderr)


if __name__ == "__main__":
    unittest.main()
