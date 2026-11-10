"""A journal retain that failed at the provider is indexed on the next try."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lifecontext.application import ApplicationError, JournalApplication
from lifecontext.hindsight_adapter import LifeContextHindsightAdapter
from lifecontext.paths import LifeContextPaths
from lifecontext.vault import ManagedVault
from local_paths import scratch_root
from test_application import ControlledClient


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
