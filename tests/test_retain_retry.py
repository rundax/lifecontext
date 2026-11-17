"""A journal retain that failed at the provider is indexed on the next try."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lifecontext.api.app import _error_details
from lifecontext.application import ApplicationError, JournalApplication
from lifecontext.capture import SETTLE_SECONDS, Rejected, digest, run_once
from lifecontext.cli import _TRANSIENT_API_ERRORS
from lifecontext.hindsight_adapter import LifeContextHindsightAdapter
from lifecontext.paths import LifeContextPaths
from lifecontext.vault import ManagedVault
from local_paths import scratch_root
from test_application import ControlledClient


class InProcessCaptureApi:
    """What capture sees through the HTTP API (``cli._CaptureApi``), served by the application in process."""

    def __init__(self, application: JournalApplication) -> None:
        self.application = application

    def _done(self, mutation) -> bool:
        try:
            asyncio.run(mutation)
        except ApplicationError as exc:
            status, _, code = _error_details(exc.code)
            if code in _TRANSIENT_API_ERRORS or status >= 500:
                return False
            raise Rejected(code) from None
        return True

    def retain(self, source_id: str, raw: bytes, key: str) -> bool:
        return self._done(self.application.retain(
            source_id, 1, raw, provenance={"source": "capture"}, idempotency_key=key
        ))

    def correct(self, source_id: str, expected_version: int, raw: bytes, key: str) -> bool:
        return self._done(self.application.curate({
            "action": "correct", "source_id": source_id,
            "expected_version": expected_version, "content": raw.decode("utf-8"),
        }, idempotency_key=key))

    def evidence(self, source_id: str, version: int):
        try:
            manifest = self.application.exact_evidence(source_id, version).manifest
        except ApplicationError:
            return None
        return {"source_id": manifest.source_id, "source_version": manifest.source_version,
                "sha256": manifest.sha256}


class RetainRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="lifeweave-retain-retry-test.", dir=scratch_root()
        )
        self.base = Path(self.temporary.name).resolve(strict=True)
        self.home = self.base / "home"
        self.home.mkdir(mode=0o700)
        self.home_patch = patch.dict(os.environ, {"HOME": str(self.home)})
        self.home_patch.start()
        self.paths = LifeContextPaths.current()
        self.paths.ensure_private_home()

        self.vault_root = self.base / "vault"
        self.journal = self.vault_root / "Journal"
        (self.journal / "entries").mkdir(mode=0o700, parents=True)
        self.vault_root.chmod(0o700)
        self.journal.chmod(0o700)
        (self.journal / "entries").chmod(0o700)

        self.client = ControlledClient()
        self.adapter = LifeContextHindsightAdapter(self.client, bank_id="lifeweave")
        self.app = self._open()

    def tearDown(self) -> None:
        self.app.close()
        self.home_patch.stop()
        self.temporary.cleanup()

    def _open(self) -> JournalApplication:
        return JournalApplication(
            paths=self.paths,
            hindsight=self.adapter,
            vault=ManagedVault(self.vault_root, self.journal),
        )

    def _operation_state(self) -> tuple[str, str | None]:
        with sqlite3.connect(self.paths.catalog_path) as connection:
            return connection.execute(
                "SELECT state, error_code FROM operations"
            ).fetchone()

    def _fail_once(self, source_id: str, original: bytes, key: str) -> None:
        self.client.error = RuntimeError("rate limited")
        with self.assertRaises(ApplicationError) as caught:
            asyncio.run(self.app.retain(
                source_id, 1, original, provenance={"source": "test"}, idempotency_key=key
            ))
        self.assertEqual(caught.exception.code, "retain_failed")
        self.assertEqual(self._operation_state(), ("retryable_failed", "retain_failed"))
        self.client.error = None

    def test_same_key_after_provider_error_indexes_and_projects_the_note(self) -> None:
        source_id = "src_retry_same_key"
        original = b"note captured during a rate limit"
        key = "retain-retry-key"
        self._fail_once(source_id, original, key)

        result = asyncio.run(self.app.retain(
            source_id, 1, original, provenance={"source": "test"}, idempotency_key=key
        ))

        self.assertEqual(result["submission_state"], "submitted")
        self.assertEqual(len(self.client.retain_calls), 2)
        self.assertEqual(self._operation_state(), ("completed", None))
        self.assertEqual(
            self.app.catalog.get_provider_state(source_id, 1)["submission_state"], "submitted"
        )
        projection = self.app.catalog.get_projection_state(source_id, 1)
        self.assertEqual(projection["state"], "projected")
        self.assertTrue((self.journal / projection["projection_path"]).is_file())

        # A completed operation still replays its stored result without a new call.
        replay = asyncio.run(self.app.retain(
            source_id, 1, original, provenance={"source": "test"}, idempotency_key=key
        ))
        self.assertEqual(replay, result)
        self.assertEqual(len(self.client.retain_calls), 2)

    def test_restart_resubmits_a_failed_note_from_custody(self) -> None:
        source_id = "src_retry_restart"
        original = b"note recovered at startup"
        self._fail_once(source_id, original, "retain-retry-restart-key")
        self.app.close()
        self.app = self._open()

        report = asyncio.run(self.app.recover_startup())

        self.assertEqual(report["resumed"], 1)
        self.assertEqual(report["completed"], 1)
        self.assertEqual(len(self.client.retain_calls), 2)
        self.assertEqual(self.client.retain_calls[-1][1], original.decode("utf-8"))
        self.assertEqual(self._operation_state(), ("completed", None))
        self.assertEqual(
            self.app.catalog.get_provider_state(source_id, 1)["submission_state"], "submitted"
        )
        projection = self.app.catalog.get_projection_state(source_id, 1)
        self.assertEqual(projection["state"], "projected")
        self.assertTrue((self.journal / projection["projection_path"]).is_file())

        # Nothing is left to recover on the next start.
        again = asyncio.run(self.app.recover_startup())
        self.assertEqual(again["resumed"], 0)
        self.assertEqual(len(self.client.retain_calls), 2)

    def test_restart_while_provider_still_fails_stays_retryable(self) -> None:
        source_id = "src_retry_restart_fails"
        self._fail_once(source_id, b"note still refused", "retain-retry-restart-fail-key")
        self.client.error = RuntimeError("still rate limited")

        report = asyncio.run(self.app.recover_startup())

        self.assertEqual(report["completed"], 0)
        self.assertEqual(len(self.client.retain_calls), 2)
        self.assertEqual(self._operation_state(), ("retryable_failed", "retain_failed"))

    def test_retry_that_fails_again_stays_retryable(self) -> None:
        source_id = "src_retry_fails_again"
        original = b"note that fails twice"
        key = "retain-retry-twice-key"
        self._fail_once(source_id, original, key)

        self.client.error = RuntimeError("still rate limited")
        with self.assertRaises(ApplicationError) as caught:
            asyncio.run(self.app.retain(
                source_id, 1, original, provenance={"source": "test"}, idempotency_key=key
            ))
        self.assertEqual(caught.exception.code, "retain_failed")
        self.assertEqual(len(self.client.retain_calls), 2)
        self.assertEqual(self._operation_state(), ("retryable_failed", "retain_failed"))

    def test_retry_never_indexes_an_older_version_over_a_newer_one(self) -> None:
        source_id = "src_retry_older_version"
        key = "retain-retry-old-version-key"
        self._fail_once(source_id, b"first version", key)
        asyncio.run(self.app.retain(
            source_id, 2, b"second version", provenance={"source": "test"},
            idempotency_key="retain-retry-second-version",
        ))
        calls = len(self.client.retain_calls)

        with self.assertRaises(ApplicationError) as caught:
            asyncio.run(self.app.retain(
                source_id, 1, b"first version", provenance={"source": "test"},
                idempotency_key=key,
            ))
        self.assertEqual(caught.exception.code, "source_conflict")
        self.assertEqual(len(self.client.retain_calls), calls)

    def _correct(self, source_id: str, content: str, key: str, expected_version: int = 1):
        return asyncio.run(self.app.curate({
            "action": "correct", "source_id": source_id,
            "expected_version": expected_version, "content": content,
        }, idempotency_key=key))

    def _fail_correction(self, source_id: str, content: str, key: str) -> None:
        held = dict(self.client.document_metadata)
        self.client.error = RuntimeError("rate limited")
        with self.assertRaises(ApplicationError) as caught:
            self._correct(source_id, content, key)
        self.assertEqual(caught.exception.code, "retain_failed")
        # The provider kept what it had: the refused version never reached it.
        self.client.document_metadata = held
        self.client.error = None

    def _edited_note(self, source_id: str) -> None:
        asyncio.run(self.app.retain(
            source_id, 1, b"first version of the note", provenance={"source": "test"},
            idempotency_key=source_id + "-v1",
        ))

    def test_same_key_after_provider_error_indexes_an_edited_note(self) -> None:
        source_id = "src_retry_correct_same_key"
        key = "retain-retry-correct-key"
        self._edited_note(source_id)
        self._fail_correction(source_id, "second version of the note", key)

        result = self._correct(source_id, "second version of the note", key)

        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["summary"]["sources"][0]["source_version"], 2)
        self.assertEqual(len(self.client.retain_calls), 3)
        self.assertEqual(self.client.retain_calls[-1][1], "second version of the note")
        self.assertEqual(
            self.app.catalog.get_provider_state(source_id, 2)["submission_state"], "submitted"
        )
        projection = self.app.catalog.get_projection_state(source_id, 2)
        self.assertEqual(projection["state"], "projected")
        self.assertTrue((self.journal / projection["projection_path"]).is_file())

        replay = self._correct(source_id, "second version of the note", key)
        self.assertEqual(replay, result)
        self.assertEqual(len(self.client.retain_calls), 3)

    def test_restart_resubmits_a_failed_correction_from_custody(self) -> None:
        source_id = "src_retry_correct_restart"
        self._edited_note(source_id)
        self._fail_correction(source_id, "corrected at startup", "retain-retry-correct-restart")
        self.app.close()
        self.app = self._open()

        report = asyncio.run(self.app.recover_startup())

        self.assertEqual((report["resumed"], report["completed"]), (1, 1))
        self.assertEqual(len(self.client.retain_calls), 3)
        self.assertEqual(self.client.retain_calls[-1][1], "corrected at startup")
        self.assertEqual(
            self.app.catalog.get_provider_state(source_id, 2)["submission_state"], "submitted"
        )
        projection = self.app.catalog.get_projection_state(source_id, 2)
        self.assertEqual(projection["state"], "projected")
        self.assertTrue((self.journal / projection["projection_path"]).is_file())
        again = asyncio.run(self.app.recover_startup())
        self.assertEqual(again["resumed"], 0)
        self.assertEqual(len(self.client.retain_calls), 3)

    def test_correction_retry_never_indexes_an_older_version_over_a_newer_one(self) -> None:
        source_id = "src_retry_correct_older"
        key = "retain-retry-correct-older"
        self._edited_note(source_id)
        self._fail_correction(source_id, "second version", key)
        self._correct(source_id, "third version", "retain-retry-correct-third", expected_version=2)
        calls = len(self.client.retain_calls)

        with self.assertRaises(ApplicationError) as caught:
            self._correct(source_id, "second version", key)
        self.assertEqual(caught.exception.code, "conflict")
        asyncio.run(self.app.recover_startup())
        self.assertEqual(len(self.client.retain_calls), calls)
        self.assertEqual(self.client.retain_calls[-1][1], "third version")

    def _entry(self, source_id: str, version: int) -> tuple[dict, str]:
        projection = self.app.catalog.get_projection_state(source_id, version)
        return projection, (self.journal / projection["projection_path"]).read_text(encoding="utf-8")

    def test_a_failed_note_is_in_the_vault_right_after_custody_and_written_once(self) -> None:
        source_id = "src_retry_early_entry"
        original = b"note written before it was indexed"
        key = "retain-retry-early-entry"
        # The entry is in the vault right after custody, before the provider sees it.
        seen: list[str] = []
        self.client.on_retain = lambda *_: seen.append(
            self.app.catalog.get_projection_state(source_id, 1)["state"]
        )
        self._fail_once(source_id, original, key)
        self.client.on_retain = None
        self.assertEqual(seen, ["projected"])

        pending, text = self._entry(source_id, 1)
        self.assertEqual(pending["state"], "projected")
        self.assertTrue(text.endswith(original.decode()))
        self.assertEqual(self.app.reconcile_projections()["already_projected"], 1)
        self.assertEqual(self._entry(source_id, 1)[1], text)

        # Indexing it later never rewrites or replaces the entry.
        with patch.object(self.app.vault, "safe_write_text", side_effect=AssertionError), \
                patch.object(self.app.vault, "safe_remove_projection", side_effect=AssertionError):
            asyncio.run(self.app.retain(
                source_id, 1, original, provenance={"source": "test"}, idempotency_key=key
            ))

        indexed, after = self._entry(source_id, 1)
        self.assertEqual(indexed["state"], "projected")
        self.assertEqual(indexed["projection_path"], pending["projection_path"])
        self.assertEqual(after, text)
        self.assertEqual(len(list((self.journal / "entries").iterdir())), 1)

    def test_a_note_indexed_on_the_first_try_is_written_once(self) -> None:
        source_id = "src_retry_written_once"
        with patch.object(self.app.vault, "safe_remove_projection", side_effect=AssertionError):
            self._edited_note(source_id)
        projection, text = self._entry(source_id, 1)
        self.assertEqual(projection["state"], "projected")
        self.assertTrue(text.endswith("first version of the note"))

    def test_a_failed_correction_is_in_the_vault_before_it_is_indexed(self) -> None:
        source_id = "src_retry_correct_early_entry"
        key = "retain-retry-correct-early-entry"
        self._edited_note(source_id)
        self._fail_correction(source_id, "second version, not indexed yet", key)
        pending, text = self._entry(source_id, 2)
        self.assertEqual(pending["state"], "projected")
        self.assertTrue(text.endswith("second version, not indexed yet"))

        self._correct(source_id, "second version, not indexed yet", key)

        projection, after = self._entry(source_id, 2)
        self.assertEqual(projection["state"], "projected")
        self.assertEqual(after, text)

    def test_a_capture_run_after_a_failure_completes_the_note(self) -> None:
        inbox = self.base / "notes" / "Inbox"
        inbox.mkdir(parents=True)
        note = inbox / "Lake.md"
        source_id = "obsidian_" + digest(b"Lake.md")
        clock = [1_000_000.0]

        def scan() -> dict:
            return run_once(inbox, self.paths, InProcessCaptureApi(self.app), now=lambda: clock[0])

        for version, text in ((1, "# Walk by the lake\n\nThe water was calm.\n"),
                              (2, "# Walk by the lake\n\nThe water was calm, then it rained.\n")):
            note.write_text(text, encoding="utf-8")
            scan()
            clock[0] += SETTLE_SECONDS + 1
            held = dict(self.client.document_metadata)
            self.client.error = RuntimeError("rate limited")
            self.assertEqual(scan()["pending"], 1)
            self.client.document_metadata = held
            self.client.error = None
            clock[0] += 30

            report = scan()

            self.assertEqual((report["captured"], report["pending"], report["failed"]), (1, 0, 0))
            self.assertEqual(
                self.app.catalog.get_provider_state(source_id, version)["submission_state"], "submitted"
            )
            projection, entry = self._entry(source_id, version)
            self.assertEqual(projection["state"], "projected")
            self.assertTrue(entry.endswith(text))
        self.assertEqual(self.client.retain_calls[-1][1], text)

    def test_different_payload_under_failed_key_is_still_a_conflict(self) -> None:
        source_id = "src_retry_conflict"
        original = b"original failed note"
        key = "retain-retry-conflict-key"
        self._fail_once(source_id, original, key)

        with self.assertRaises(ApplicationError) as caught:
            asyncio.run(self.app.retain(
                source_id, 1, b"changed note", provenance={"source": "test"},
                idempotency_key=key,
            ))
        self.assertEqual(caught.exception.code, "conflict")
        self.assertEqual(len(self.client.retain_calls), 1)
        self.assertEqual(self._operation_state(), ("retryable_failed", "retain_failed"))


if __name__ == "__main__":
    unittest.main()
