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
from lifecontext.catalog import CatalogError
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
        self.clock = [1_000_000.0]
        self.app = self._open()

    def tearDown(self) -> None:
        self.app.close()
        self.home_patch.stop()
        self.temporary.cleanup()

    def _open(self) -> JournalApplication:
        application = JournalApplication(
            paths=self.paths,
            hindsight=self.adapter,
            vault=ManagedVault(self.vault_root, self.journal),
        )
        application._clock = lambda: self.clock[0]
        return application

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
        self.clock[0] += 61  # past the first retry delay

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

    def test_same_key_retries_wait_before_the_provider_is_asked_again(self) -> None:
        source_id = "src_retry_backoff"
        original = b"note refused while the provider is down"
        key = "retain-retry-backoff"
        start = self.clock[0]
        self.client.error = RuntimeError("provider down")

        def retry() -> str | None:
            try:
                asyncio.run(self.app.retain(
                    source_id, 1, original, provenance={"source": "test"}, idempotency_key=key
                ))
            except ApplicationError as exc:
                return exc.code
            return None

        self.assertEqual(retry(), "retain_failed")
        self.client.document_metadata.clear()  # the provider never held the refused note
        for _ in range(10):
            self.clock[0] += 5
            self.assertEqual(retry(), "retain_failed")
        self.assertEqual(len(self.client.retain_calls), 1)
        # A restart within the delay does not ask the provider either.
        self.app.close()
        self.app = self._open()
        asyncio.run(self.app.recover_startup())
        self.assertEqual(len(self.client.retain_calls), 1)

        # Past the delay the next retry asks again; refused again, the next delay is 5 minutes.
        self.clock[0] = start + 60
        self.assertEqual(retry(), "retain_failed")
        self.assertEqual(len(self.client.retain_calls), 2)
        self.clock[0] += 299
        self.assertEqual(retry(), "retain_failed")
        self.assertEqual(len(self.client.retain_calls), 2)
        self.client.error = None
        self.clock[0] += 1
        self.assertIsNone(retry())
        self.assertEqual(len(self.client.retain_calls), 3)
        self.assertEqual(self._operation_state(), ("completed", None))

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
        self.clock[0] += 61  # past the first retry delay

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

    def test_a_deleted_note_s_refused_correction_is_never_sent_again(self) -> None:
        source_id = "src_retry_correct_deleted"
        key = "retain-retry-correct-deleted"
        self._edited_note(source_id)
        self._fail_correction(source_id, "PRIVATE second version", key)
        prepared = asyncio.run(self.app.curate({
            "action": "prepare_delete", "source_id": source_id, "expected_version": 2,
        }, idempotency_key="retain-retry-prepare-delete"))
        try:
            asyncio.run(self.app.curate({
                "action": "delete", "source_id": source_id, "expected_version": 2,
                "prepared_operation_id": prepared["operation_id"],
            }, idempotency_key="retain-retry-delete"))
        except ApplicationError:
            pass  # the provider never held version 2; the tombstones are what matter here
        self.assertEqual(len(self.app.catalog.list_tombstones(source_id)), 2)
        calls = len(self.client.retain_calls)

        self.app.close()
        self.app = self._open()
        asyncio.run(self.app.recover_startup())
        with self.assertRaises(ApplicationError) as caught:
            self._correct(source_id, "PRIVATE second version", key)

        self.assertEqual(caught.exception.code, "conflict")
        self.assertEqual(len(self.client.retain_calls), calls)
        self.assertNotIn("PRIVATE second version", [call[1] for call in self.client.retain_calls[calls:]])

    def _edit_entry(self, source_id: str, version: int) -> Path:
        path = self.journal / self.app.catalog.get_projection_state(source_id, version)["projection_path"]
        path.write_text(path.read_text(encoding="utf-8") + "\nAdded in Obsidian before indexing.\n",
                        encoding="utf-8")
        return path

    def _assert_indexed_and_edit_kept(self, source_id: str, version: int, path: Path) -> None:
        self.assertEqual(
            self.app.catalog.get_provider_state(source_id, version)["submission_state"], "submitted"
        )
        self.assertEqual(self.app.catalog.get_projection_state(source_id, version)["state"], "conflict")
        self.assertIn("Added in Obsidian before indexing.", path.read_text(encoding="utf-8"))
        facts = self.app.catalog.list_reconciliation_facts(source_id, version)
        self.assertIn("stale_projection", [fact["fact"] for fact in facts])
        status = self.app.readiness()
        self.assertGreaterEqual(status["reconciliation"]["facts"]["stale_projection"], 1)
        self.assertEqual(status["indexing_pending"]["count"], 0)

    def test_an_entry_edited_before_a_retry_indexed_it_completes_and_keeps_the_edit(self) -> None:
        source_id = "src_retry_edited_entry"
        original = b"note the owner edited in the vault"
        key = "retain-retry-edited-entry"
        self._fail_once(source_id, original, key)
        path = self._edit_entry(source_id, 1)

        result = asyncio.run(self.app.retain(
            source_id, 1, original, provenance={"source": "test"}, idempotency_key=key
        ))

        self.assertEqual(result["submission_state"], "submitted")
        self.assertEqual(self._operation_state(), ("completed", None))
        self._assert_indexed_and_edit_kept(source_id, 1, path)

    def test_a_correction_edited_before_its_retry_completes_and_keeps_the_edit(self) -> None:
        source_id = "src_retry_edited_correction"
        key = "retain-retry-edited-correction"
        self._edited_note(source_id)
        self._fail_correction(source_id, "second version, edited in the vault", key)
        path = self._edit_entry(source_id, 2)

        result = self._correct(source_id, "second version, edited in the vault", key)

        self.assertEqual(result["state"], "completed")
        self._assert_indexed_and_edit_kept(source_id, 2, path)

    def test_an_entry_edited_before_startup_recovery_indexed_it_completes(self) -> None:
        source_id = "src_retry_edited_recovered"
        self._fail_once(source_id, b"note recovered after an edit", "retain-retry-edited-recovered")
        path = self._edit_entry(source_id, 1)
        self.app.close()
        self.app = self._open()

        report = asyncio.run(self.app.recover_startup())

        self.assertEqual(report["completed"], 1)
        self.assertEqual(self._operation_state(), ("completed", None))
        self._assert_indexed_and_edit_kept(source_id, 1, path)

    def _states(self, kind: str) -> list[str]:
        with sqlite3.connect(self.paths.catalog_path) as connection:
            return [row[0] for row in connection.execute(
                "SELECT state FROM operations WHERE operation_kind = ? ORDER BY created_at", (kind,)
            )]

    def test_a_recovered_correction_never_stays_running(self) -> None:
        source_id = "src_retry_correct_never_running"
        self._edited_note(source_id)
        self._fail_correction(source_id, "corrected, then the catalog fails", "retain-retry-never-running")
        self.app.close()
        self.app = self._open()

        with patch.object(self.app.catalog, "record_curation_history",
                          side_effect=CatalogError("catalog operation failed")):
            first = asyncio.run(self.app.recover_startup())

        self.assertEqual(first["resumed"], 1)
        self.assertEqual(self._states("curation"), ["permanent_failed"])
        calls = len(self.client.retain_calls)
        self.app.close()
        self.app = self._open()
        second = asyncio.run(self.app.recover_startup())
        self.assertEqual((second["resumed"], second["unresolved"]), (0, 0))
        self.assertEqual(len(self.client.retain_calls), calls)

    def test_a_recovered_correction_whose_entry_was_edited_completes_once(self) -> None:
        source_id = "src_retry_correct_edited_restart"
        self._edited_note(source_id)
        self._fail_correction(source_id, "corrected while the entry was edited", "retain-retry-edited-restart")
        path = self._edit_entry(source_id, 2)
        for expected in ((1, 1), (0, 0)):
            self.app.close()
            self.app = self._open()
            report = asyncio.run(self.app.recover_startup())
            self.assertEqual((report["resumed"], report["completed"]), expected)
            self.assertEqual(self._states("curation"), ["completed"])
        self.assertEqual(len(self.client.retain_calls), 3)
        self._assert_indexed_and_edit_kept(source_id, 2, path)

    def _unindexed(self) -> dict:
        return self.app.readiness()["indexing_pending"]

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
        # Status, not the file, says the entry is waiting for indexing.
        self.assertEqual(self._unindexed(), {"count": 1, "entries": ["Journal/" + pending["projection_path"]]})
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
        self.assertEqual(self._unindexed(), {"count": 0, "entries": []})

    def test_status_counts_every_unindexed_entry_and_lists_twenty(self) -> None:
        self.client.error = RuntimeError("provider down")
        for number in range(22):
            with self.assertRaises(ApplicationError):
                asyncio.run(self.app.retain(
                    f"src_retry_many_{number}", 1, f"# Note {number}\n\nbody".encode(),
                    provenance={"source": "test"}, idempotency_key=f"retain-retry-many-{number}",
                ))
        pending = self._unindexed()
        self.assertEqual(pending["count"], 22)
        self.assertEqual(len(pending["entries"]), 20)
        self.assertTrue(all(entry.startswith("Journal/entries/") for entry in pending["entries"]))

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
        self.assertEqual(self._unindexed(), {"count": 1, "entries": ["Journal/" + pending["projection_path"]]})

        self._correct(source_id, "second version, not indexed yet", key)

        projection, after = self._entry(source_id, 2)
        self.assertEqual(projection["state"], "projected")
        self.assertEqual(after, text)
        self.assertEqual(self._unindexed(), {"count": 0, "entries": []})

    def test_a_capture_run_after_a_failure_completes_the_note(self) -> None:
        inbox = self.base / "notes" / "Inbox"
        inbox.mkdir(parents=True)
        note = inbox / "Lake.md"
        source_id = "obsidian_" + digest(b"Lake.md")
        clock = self.clock

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
            # Within the first retry delay the pending note is not sent again.
            calls = len(self.client.retain_calls)
            self.assertEqual(scan()["pending"], 1)
            self.assertEqual(len(self.client.retain_calls), calls)
            clock[0] += 31

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
