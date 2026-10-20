"""`lifecontext capture`: settled Inbox notes become journal entries once, with the API faked."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import io
import json
import os
import stat
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from local_paths import scratch_root

from lifecontext import cli
from lifecontext.capture import (
    MAX_BYTES,
    SETTLE_SECONDS,
    CaptureError,
    Rejected,
    default_inbox,
    render,
    run_once,
    state_directory,
)
from lifecontext.paths import LifeContextPaths

NOTE = "# Walk by the lake\n\nThe water was calm today.\n"


class FakeApi:
    """Keeps what the real API would: versions by source, replays a key it has already completed."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.sources: dict[str, list[str]] = {}
        self.completed: dict[str, bool] = {}
        self.refuse = False  # the API is unreachable or failing: not done yet
        self.reject: str | None = None  # the API refuses the note for good, with this error code
        self.corrupt = False  # the evidence does not match what was sent

    def _apply(self, key: str, source_id: str, raw: bytes, expected: int) -> bool:
        if self.reject:
            raise Rejected(self.reject)
        if self.refuse:
            return False
        if key not in self.completed:
            versions = self.sources.setdefault(source_id, [])
            if len(versions) != expected:
                return False
            versions.append(hashlib.sha256(raw).hexdigest())
            self.completed[key] = True
        return True

    def retain(self, source_id: str, raw: bytes, key: str) -> bool:
        self.calls.append(("retain", source_id, key))
        return self._apply(key, source_id, raw, 0)

    def correct(self, source_id: str, expected_version: int, raw: bytes, key: str) -> bool:
        self.calls.append(("correct", source_id, expected_version, key))
        return self._apply(key, source_id, raw, expected_version)

    def evidence(self, source_id: str, version: int):
        versions = self.sources.get(source_id, [])
        if not 1 <= version <= len(versions):
            return None
        return {"source_id": source_id, "source_version": version,
                "sha256": "0" * 64 if self.corrupt else versions[version - 1]}


@contextmanager
def _home():
    with tempfile.TemporaryDirectory(prefix="lifecontext-capture-test.", dir=scratch_root()) as temporary:
        home = Path(temporary).resolve(strict=True)
        yield home


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class CaptureTests(unittest.TestCase):
    def setUp(self) -> None:
        manager = _home()
        self.home = manager.__enter__()
        self.addCleanup(manager.__exit__, None, None, None)
        self.paths = LifeContextPaths(self.home)
        self.inbox = self.home / "vault" / "Journal" / "Inbox"
        self.inbox.mkdir(parents=True)
        self.api = FakeApi()
        self.clock = Clock()

    def scan(self) -> dict:
        return run_once(self.inbox, self.paths, self.api, now=self.clock)

    def write(self, name: str, text: str = NOTE) -> Path:
        path = self.inbox / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_a_settled_note_is_captured_once(self) -> None:
        self.write("Lake.md")
        first = self.scan()
        self.assertEqual((first["captured"], first["pending"], first["skipped"]), (0, 0, 0))
        self.assertEqual(self.api.calls, [])
        self.clock.advance(SETTLE_SECONDS + 1)
        second = self.scan()
        self.assertEqual(second["captured"], 1)
        self.assertEqual(len(self.api.calls), 1)
        kind, source_id, key = self.api.calls[0]
        self.assertEqual(kind, "retain")
        self.assertEqual(source_id, "obsidian_" + hashlib.sha256(b"Lake.md").hexdigest())
        self.assertTrue(key.startswith("obsidian:"))
        self.assertEqual(self.api.sources[source_id], [hashlib.sha256(NOTE.encode()).hexdigest()])
        # Nothing further happens on later runs, and the note itself is untouched.
        self.clock.advance(60)
        third = self.scan()
        self.assertEqual((third["captured"], third["pending"], third["skipped"]), (0, 0, 0))
        self.assertEqual(len(self.api.calls), 1)
        self.assertEqual((self.inbox / "Lake.md").read_text(encoding="utf-8"), NOTE)

    def test_a_note_still_changing_is_not_captured(self) -> None:
        note = self.write("Draft.md", "first")
        self.scan()
        self.clock.advance(SETTLE_SECONDS - 5)
        self.scan()
        self.assertEqual(self.api.calls, [])
        note.write_text("first, then more", encoding="utf-8")
        self.clock.advance(SETTLE_SECONDS - 5)
        self.scan()  # the new bytes are seen now; the settle time starts over
        self.clock.advance(SETTLE_SECONDS - 5)
        self.scan()
        self.assertEqual(self.api.calls, [])
        self.clock.advance(10)
        self.assertEqual(self.scan()["captured"], 1)
        self.assertEqual(len(self.api.calls), 1)

    def test_oversized_empty_binary_hidden_and_linked_notes_are_skipped(self) -> None:
        self.write("Huge.md", "x" * (MAX_BYTES + 1))
        self.write("Empty.md", "")
        (self.inbox / "Binary.md").write_bytes(b"\xff\xfe\x00bad")
        self.write(".hidden.md")
        self.write("notes.txt")
        self.write(".obsidian/Config.md")
        outside = self.home / "outside.md"
        outside.write_text(NOTE)
        (self.inbox / "Link.md").symlink_to(outside)
        (self.inbox / "LinkedDir").symlink_to(self.home)
        exact = self.write("Exact.md", "y" * MAX_BYTES)
        first = self.scan()
        self.clock.advance(SETTLE_SECONDS + 1)
        second = self.scan()
        self.assertEqual(first["skipped"], 4)  # huge, empty, binary and the link; Exact.md is only observed
        self.assertEqual(second["captured"], 1)
        self.assertEqual([call[1] for call in self.api.calls],
                         ["obsidian_" + hashlib.sha256(exact.name.encode()).hexdigest()])

    def test_a_note_in_a_folder_and_an_edit_become_versions_of_one_source(self) -> None:
        note = self.write("2026/Lake.md")
        self.scan()
        self.clock.advance(SETTLE_SECONDS + 1)
        self.assertEqual(self.scan()["captured"], 1)
        source_id = "obsidian_" + hashlib.sha256(str(Path("2026/Lake.md")).encode()).hexdigest()
        note.write_text(NOTE + "More.\n", encoding="utf-8")
        self.clock.advance(60)
        self.scan()
        self.clock.advance(SETTLE_SECONDS + 1)
        self.assertEqual(self.scan()["captured"], 1)
        self.assertEqual(self.api.calls[1][:3], ("correct", source_id, 1))
        self.assertEqual(len(self.api.sources[source_id]), 2)
        self.clock.advance(60)
        self.assertEqual(self.scan()["captured"], 0)

    def test_a_refused_submission_is_retried_with_the_same_key_and_never_doubled(self) -> None:
        self.write("Lake.md")
        self.scan()
        self.clock.advance(SETTLE_SECONDS + 1)
        self.api.refuse = True
        refused = self.scan()
        self.assertEqual((refused["captured"], refused["pending"]), (0, 1))
        self.clock.advance(30)
        self.assertEqual(self.scan()["pending"], 1)
        self.api.refuse = False
        self.clock.advance(30)
        recovered = self.scan()
        self.assertEqual((recovered["captured"], recovered["pending"]), (1, 0))
        keys = {call[2] for call in self.api.calls}
        self.assertEqual(len(keys), 1)
        self.assertEqual(len(self.api.sources), 1)
        self.assertEqual(len(next(iter(self.api.sources.values()))), 1)
        # The snapshot used for the retry is gone once the submission completed.
        self.assertEqual(sorted(path.name for path in state_directory(self.paths).iterdir()),
                         ["lock", "state.json"])

    def test_a_refusal_for_good_is_reported_not_retried_and_cleared_by_a_change(self) -> None:
        note = self.write("2026/Lake.md")
        self.scan()
        self.clock.advance(SETTLE_SECONDS + 1)
        self.api.reject = "source_conflict"
        refused = self.scan()
        self.assertEqual((refused["captured"], refused["pending"], refused["failed"]), (0, 0, 1))
        self.assertEqual(refused["failures"], [{"note": "2026/Lake.md", "code": "source_conflict"}])
        text = render(refused)
        self.assertIn("failed 1", text)
        self.assertIn(f"Refused: {self.inbox / '2026' / 'Lake.md'} (source_conflict)", text)
        calls = len(self.api.calls)
        # No snapshot of the note is kept, and later runs report the refusal without sending again.
        self.assertEqual(sorted(path.name for path in state_directory(self.paths).iterdir()), ["lock", "state.json"])
        for _ in range(3):
            self.clock.advance(30)
            again = self.scan()
            self.assertEqual((again["pending"], again["failed"]), (0, 1))
            self.assertEqual(again["failures"], refused["failures"])
        self.assertEqual(len(self.api.calls), calls)
        # A changed note is tried again, with a new key, and a success clears the refusal.
        self.api.reject = None
        note.write_text(NOTE + "More.\n", encoding="utf-8")
        self.clock.advance(30)
        self.scan()
        self.clock.advance(SETTLE_SECONDS + 1)
        retried = self.scan()
        self.assertEqual((retried["captured"], retried["failed"], retried["failures"]), (1, 0, []))
        self.assertEqual(self.api.calls[-1][0], "retain")
        self.assertNotEqual(self.api.calls[-1][2], self.api.calls[0][2])
        self.clock.advance(30)
        self.assertEqual(self.scan()["failed"], 0)

    def test_a_refusal_survives_the_note_being_deleted_in_the_same_run(self) -> None:
        note = self.write("Lake.md")
        self.scan()
        self.clock.advance(SETTLE_SECONDS + 1)
        self.api.refuse = True
        self.scan()
        note.unlink()
        self.api.refuse = False
        self.api.reject = "conflict"
        refused = self.scan()
        self.assertEqual((refused["pending"], refused["failures"]), (0, [{"note": "Lake.md", "code": "conflict"}]))
        self.clock.advance(30)
        self.assertEqual(self.scan()["failed"], 0)  # the note is gone: nothing left to report

    def test_evidence_that_does_not_match_what_was_sent_is_a_refusal(self) -> None:
        self.write("Lake.md")
        self.scan()
        self.clock.advance(SETTLE_SECONDS + 1)
        self.api.corrupt = True
        report = self.scan()
        self.assertEqual(report["failures"], [{"note": "Lake.md", "code": "evidence_mismatch"}])
        # The source did reach version 1, so the next edit corrects it instead of retaining it again.
        self.api.corrupt = False
        note = self.inbox / "Lake.md"
        note.write_text(NOTE + "More.\n", encoding="utf-8")
        self.clock.advance(30)
        self.scan()
        self.clock.advance(SETTLE_SECONDS + 1)
        fixed = self.scan()
        self.assertEqual((fixed["captured"], fixed["failed"]), (1, 0))
        self.assertEqual(self.api.calls[-1][0], "correct")
        self.assertEqual(self.api.calls[-1][2], 1)

    def test_a_pending_submission_whose_snapshot_is_gone_or_damaged_is_dropped_and_redone(self) -> None:
        for damage in ("missing", "changed"):
            with self.subTest(damage=damage):
                name = f"{damage}.md"
                self.write(name)
                self.scan()
                self.clock.advance(SETTLE_SECONDS + 1)
                self.api.refuse = True
                self.assertEqual(self.scan()["pending"], 1)
                first_key = self.api.calls[-1][2]
                directory = state_directory(self.paths)
                snapshot = next(path for path in directory.glob("*.md"))
                if damage == "missing":
                    snapshot.unlink()
                else:
                    snapshot.write_text("not what was snapshotted", encoding="utf-8")
                self.api.refuse = False
                self.clock.advance(30)
                recovered = self.scan()
                self.assertEqual((recovered["captured"], recovered["pending"], recovered["failed"]), (1, 0, 0))
                self.assertNotEqual(self.api.calls[-1][2], first_key)
                source_id = "obsidian_" + hashlib.sha256(name.encode()).hexdigest()
                self.assertEqual(self.api.sources[source_id], [hashlib.sha256(NOTE.encode()).hexdigest()])
                self.assertEqual(sorted(path.name for path in directory.iterdir()), ["lock", "state.json"])
                self.clock.advance(30)
                self.assertEqual(self.scan()["captured"], 0)

    def test_note_text_left_in_the_state_folder_by_an_interrupted_run_is_removed(self) -> None:
        self.write("Lake.md")
        self.scan()
        directory = state_directory(self.paths)
        (directory / ("f" * 32 + ".md")).write_text(NOTE)
        (directory / ".state.json.partial").write_text("{")
        self.clock.advance(SETTLE_SECONDS + 1)
        self.api.refuse = True
        self.scan()  # now one submission is pending: its snapshot must stay
        self.assertEqual(len(list(directory.glob("*.md"))), 1)
        (directory / ("e" * 32 + ".md")).write_text(NOTE)
        self.api.refuse = False
        self.clock.advance(30)
        self.assertEqual(self.scan()["captured"], 1)
        self.assertEqual(sorted(path.name for path in directory.iterdir()), ["lock", "state.json"])

    def test_a_pending_submission_finishes_after_the_note_was_deleted(self) -> None:
        note = self.write("Lake.md")
        self.scan()
        self.clock.advance(SETTLE_SECONDS + 1)
        self.api.refuse = True
        self.scan()
        note.unlink()
        self.api.refuse = False
        self.clock.advance(30)
        self.assertEqual(self.scan()["captured"], 1)

    def test_state_is_private_and_an_unsafe_directory_is_refused(self) -> None:
        self.write("Lake.md")
        self.scan()
        directory = state_directory(self.paths)
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((directory / "state.json").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((directory / "lock").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.paths.lifeweave_home.stat().st_mode), 0o700)
        directory.chmod(0o755)
        with self.assertRaises(CaptureError) as caught:
            self.scan()
        self.assertEqual(caught.exception.code, "unsafe_state")

    def test_a_second_run_at_the_same_time_does_nothing(self) -> None:
        self.write("Lake.md")
        self.scan()
        directory = state_directory(self.paths)
        with open(directory / "lock", "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.clock.advance(SETTLE_SECONDS + 1)
            busy = self.scan()
        self.assertTrue(busy["busy"])
        self.assertEqual(self.api.calls, [])
        self.assertIn("in progress", render(busy))

    def test_only_a_folder_named_inbox_is_captured(self) -> None:
        folder = self.home / "vault" / "Journal"
        with self.assertRaises(CaptureError) as caught:
            run_once(folder, self.paths, self.api, now=self.clock)
        self.assertEqual(caught.exception.code, "capture_requires_inbox")

    def test_a_missing_inbox_is_reported_not_created(self) -> None:
        missing = self.home / "elsewhere" / "Inbox"
        report = run_once(missing, self.paths, self.api, now=self.clock)
        self.assertFalse(report["inbox_found"])
        self.assertEqual(report["captured"], 0)
        self.assertFalse(missing.exists())
        self.assertIn("does not exist yet", render(report))

    def test_default_inbox_follows_the_configured_vault(self) -> None:
        journal = self.home / "Notes" / "Journal"
        settings = {"vault": {"root": str(journal.parent), "journal_path": str(journal)}}
        self.assertEqual(default_inbox(settings, self.paths), journal / "Inbox")
        self.assertEqual(default_inbox({}, self.paths), self.home / "LifeContext" / "vault" / "Journal" / "Inbox")


class _Http:
    """Stands in for the CLI's HTTP layer and records the requests."""

    OPERATION = "op_" + "0" * 32

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict | None, dict]] = []

    def __call__(self, args, method, target, *, body=None, headers=None, **_):
        headers = headers or {}
        self.requests.append((method, target, json.loads(body) if headers.get("Content-Type") == "application/json"
                              else body, headers))
        if target.startswith("/v1/operations/"):
            return {"operation_id": self.OPERATION, "state": "completed"}, None
        if target.startswith("/v1/evidence/"):
            return {"source_id": "s", "source_version": 1, "sha256": "0" * 64}, None
        return {"submission_state": "submitted"}, self.OPERATION


class CaptureApiTests(unittest.TestCase):
    def test_retain_and_correct_go_through_the_http_api_with_the_stored_key(self) -> None:
        http = _Http()
        api = cli._CaptureApi(cli._build_parser().parse_args(["capture", "--once"]))
        with patch.object(cli, "_http_request", http):
            self.assertTrue(api.retain("obsidian_a", b"note", "obsidian:k1"))
            self.assertTrue(api.correct("obsidian_a", 1, "second é".encode(), "obsidian:k2"))
            self.assertEqual(api.evidence("obsidian_a", 2)["source_version"], 1)
        retain, operation, correct, _, evidence = http.requests
        self.assertEqual(retain[:2], ("POST", "/v1/retain?source_id=obsidian_a&source_version=1"))
        self.assertEqual(retain[3]["Idempotency-Key"], "obsidian:k1")
        self.assertNotIn("X-Source-Basename", retain[3])
        self.assertEqual(operation[:2], ("GET", f"/v1/operations/{_Http.OPERATION}"))
        self.assertEqual(correct[:2], ("POST", "/v1/curation"))
        self.assertEqual(correct[2], {"action": "correct", "source_id": "obsidian_a", "expected_version": 1,
                                      "content": "second é"})
        self.assertEqual(correct[3]["Idempotency-Key"], "obsidian:k2")
        self.assertEqual(evidence[:2], ("GET", "/v1/evidence/obsidian_a?version=2&include_content=false"))

    def test_an_unreachable_or_failing_api_means_try_again_later(self) -> None:
        api = cli._CaptureApi(cli._build_parser().parse_args(["capture", "--once"]))
        for error in (cli.CLIError("api_unavailable"), cli.CLIError("token_unavailable"),
                      cli.CLIError("unauthorized"), cli.CLIError("provider_unavailable", 503),
                      cli.CLIError("retain_failed", 503), cli.CLIError("catalog_busy", 503),
                      cli.CLIError("internal_error", 500), cli.CLIError("api_error", 400),
                      cli.CLIError("invalid_api_response"), cli.CLIError("host_not_allowed", 403),
                      cli.CLIError("origin_not_allowed", 403), cli.CLIError("unsafe_path", 403),
                      cli.CLIError("unsafe_permissions", 403)):
            def down(*args, error=error, **kwargs):
                raise error

            with self.subTest(code=error.code), patch.object(cli, "_http_request", down):
                self.assertFalse(api.retain("obsidian_a", b"note", "k"))
                self.assertFalse(api.correct("obsidian_a", 1, b"note", "k"))
                self.assertIsNone(api.evidence("obsidian_a", 1))

        def running(args, method, target, **kwargs):
            return ({"operation_id": _Http.OPERATION, "state": "running"} if target.startswith("/v1/operations/")
                    else {}), _Http.OPERATION

        with patch.object(cli, "_http_request", running):
            self.assertFalse(api.retain("obsidian_a", b"note", "k"))

    def test_any_other_refusal_is_reported_with_its_code(self) -> None:
        api = cli._CaptureApi(cli._build_parser().parse_args(["capture", "--once"]))
        for error in (cli.CLIError("source_conflict", 409), cli.CLIError("conflict", 409),
                      cli.CLIError("invalid_source_text", 422), cli.CLIError("body_too_large", 413),
                      cli.CLIError("body_too_large"), cli.CLIError("invalid_request", 422)):
            def refusing(*args, error=error, **kwargs):
                raise error

            with self.subTest(code=error.code), patch.object(cli, "_http_request", refusing):
                with self.assertRaises(Rejected) as caught:
                    api.retain("obsidian_a", b"note", "k")
                self.assertEqual(caught.exception.code, error.code)
                with self.assertRaises(Rejected):
                    api.correct("obsidian_a", 1, b"note", "k")
                # The mutation already completed by then, so the evidence check never refuses the note.
                self.assertIsNone(api.evidence("obsidian_a", 1))

        for state in ("permanent_failed", "cancelled"):
            def finished(args, method, target, state=state, **kwargs):
                return ({"operation_id": _Http.OPERATION, "state": state} if target.startswith("/v1/operations/")
                        else {}), _Http.OPERATION

            with self.subTest(state=state), patch.object(cli, "_http_request", finished):
                with self.assertRaises(Rejected) as caught:
                    api.retain("obsidian_a", b"note", "k")
                self.assertEqual(caught.exception.code, f"operation_{state}")

    def test_the_http_layer_keeps_the_status_of_an_api_error(self) -> None:
        args = cli._build_parser().parse_args(["capture", "--once"])
        body = json.dumps({"request_id": "a" * 16, "error_code": "source_conflict", "retryable": False}).encode()
        with patch.object(cli, "_http_exchange", return_value=(409, {}, body)), \
                self.assertRaises(cli.CLIError) as caught:
            cli._http_request(args, "GET", "/v1/status")
        self.assertEqual((caught.exception.code, caught.exception.status), ("source_conflict", 409))


class CaptureCommandTests(unittest.TestCase):
    def run_command(self, home: Path, *arguments: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {"HOME": str(home)}), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            code = cli.main(["capture", *arguments])
        return code, out.getvalue(), err.getvalue()

    def test_once_is_required_and_json_reports_the_counts(self) -> None:
        with _home() as home:
            inbox = home / "Notes" / "Inbox"
            inbox.mkdir(parents=True)
            (inbox / "Lake.md").write_text(NOTE, encoding="utf-8")
            with patch.object(cli, "_CaptureApi", return_value=FakeApi()):
                code, out, _ = self.run_command(home, "--once", "--inbox", str(inbox), "--json")
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out), {"inbox": str(inbox), "inbox_found": True, "busy": False,
                                               "captured": 0, "pending": 0, "skipped": 0, "failed": 0,
                                               "failures": []})
            with self.assertRaises(SystemExit) as caught, contextlib.redirect_stderr(io.StringIO()):
                cli.main(["capture"])
            self.assertEqual(caught.exception.code, 2)

    def test_the_default_inbox_is_the_configured_vaults_and_a_wrong_folder_fails(self) -> None:
        with _home() as home:
            paths = LifeContextPaths(home)
            paths.ensure_private_home()
            vault = home / "Vault"
            paths.config_path.write_text(
                "llm:\n  provider: anthropic\n  model: claude-sonnet-5\n"
                f"vault:\n  root: {json.dumps(str(vault))}\n  journal_path: {json.dumps(str(vault / 'Journal'))}\n")
            paths.config_path.chmod(0o600)
            with patch.object(cli, "_CaptureApi", return_value=FakeApi()):
                code, out, _ = self.run_command(home, "--once", "--json")
                self.assertEqual(code, 0)
                self.assertEqual(json.loads(out)["inbox"], str(vault / "Journal" / "Inbox"))
                code, out, _ = self.run_command(home, "--once", "--inbox", str(vault), "--json")
            self.assertEqual((code, json.loads(out)), (1, {"error": "capture_requires_inbox"}))


if __name__ == "__main__":
    unittest.main()
