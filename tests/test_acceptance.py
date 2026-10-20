"""External-process acceptance on real HTTP/custody with a controlled provider."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib.util
from pathlib import Path
import threading
import sqlite3
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from contextlib import redirect_stdout, redirect_stderr
import io
import json
import stat

import uvicorn

from lifecontext.api.app import create_app
import test_cli_http as fixture

_spec = importlib.util.spec_from_file_location(
    "sync_handoff", Path(__file__).resolve().parents[1] / "scripts" / "sync_handoff.py"
)
_handoff = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_handoff)


class AcceptanceTests(unittest.TestCase):
    setUp = fixture.CliHTTPTests.setUp
    tearDown = fixture.CliHTTPTests.tearDown
    _make_application = fixture.CliHTTPTests._make_application
    _run = fixture.CliHTTPTests._run
    _json_result = staticmethod(fixture.CliHTTPTests._json_result)

    def test_two_external_sessions_correction_restart_and_verified_handoff(self):
        homes = [self.base / "agent-a", self.base / "agent-b"]
        token = (self.api_home / ".lifeweave" / "api.token").read_bytes()
        for home in homes:
            # Each session is a separate shell of the same user, holding the API token.
            (home / ".lifeweave").mkdir(mode=0o700, parents=True)
            home.chmod(0o700)
            (home / ".lifeweave" / "api.token").write_bytes(token)
            (home / ".lifeweave" / "api.token").chmod(0o600)
        source = self.base / "synthetic.md"
        original = b"Synthetic concurrent acceptance source. No private content.\n"
        source.write_bytes(original)
        source.chmod(0o600)
        args = ["retain", "src_acceptance", "1", "--input", str(source),
                "--idempotency-key", "acceptance-two-session", "--json"]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda home: self._json_result(self._run(args, home=home)), homes))
        self.assertEqual(results[0]["operation_id"], results[1]["operation_id"])
        self.assertEqual(len(self.client.retain_calls), 1)
        self.assertEqual(results[0]["sha256"], hashlib.sha256(original).hexdigest())
        def distinct(home):
            suffix = home.name.replace("-", "_")
            return self._json_result(self._run([
                "retain", "src_" + suffix, "1", "--input", str(source),
                "--idempotency-key", "acceptance-" + suffix, "--json",
            ], home=home))
        with ThreadPoolExecutor(max_workers=2) as pool:
            independent = list(pool.map(distinct, homes))
        self.assertNotEqual(independent[0]["operation_id"], independent[1]["operation_id"])
        self.assertEqual(len(self.client.retain_calls), 3)
        (first_projection,) = self.entries.glob("????-??-?? - Synthetic concurrent acceptance source.md")
        self.assertIn(original, first_projection.read_bytes())
        first_bytes = first_projection.read_bytes()
        first_hash = hashlib.sha256(first_bytes).hexdigest()

        source.write_bytes(b"Synthetic corrected acceptance source.\n")
        corrected = self._json_result(self._run([
            "curate", "correct", "src_acceptance", "--expected-version", "1",
            "--input", str(source), "--json",
        ], home=homes[1]))
        self.assertIn("operation_id", corrected)
        self.assertEqual(hashlib.sha256(first_projection.read_bytes()).hexdigest(), first_hash)
        (later_projection,) = self.entries.glob("????-??-?? - Synthetic corrected acceptance source - v2.md")
        self.assertEqual(later_projection.name[:10], first_projection.name[:10])
        self.assertIn(source.read_bytes(), later_projection.read_bytes())
        snapshot = _handoff.snapshot(self.api_home, self.vault_root, self.journal)
        self.assertEqual(len(snapshot["markdown"]), 4)
        for item in snapshot["markdown"]:
            self.assertEqual(set(item), {"path", "sha256", "bytes"})

        self.server.should_exit = True
        self.server_thread.join(timeout=5)
        self.assertFalse(self.server_thread.is_alive())
        self.server = uvicorn.Server(uvicorn.Config(
            create_app(application_factory=self._make_application),
            host="127.0.0.1", port=0, log_level="error", access_log=False,
        ))
        self.server_thread = threading.Thread(target=self.server.run, daemon=True)
        self.server_thread.start()
        deadline = time.monotonic() + 5
        while not self.server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(self.server.started)
        self.port = self.server.servers[0].sockets[0].getsockname()[1]
        operation = self._json_result(self._run([
            "operations", "get", results[0]["operation_id"], "--json"
        ], home=homes[0]))
        self.assertEqual(operation["state"], "completed")
        self.assertEqual(len(self.client.retain_calls), 4)
        self.assertEqual(snapshot["markdown"], _handoff.snapshot(
            self.api_home, self.vault_root, self.journal)["markdown"])
        first_projection.write_bytes(b"external edit")
        with self.assertRaisesRegex(ValueError, "projection_hash_mismatch"):
            _handoff.snapshot(self.api_home, self.vault_root, self.journal)
        first_projection.unlink()
        with self.assertRaisesRegex(ValueError, "missing_projection"):
            _handoff.snapshot(self.api_home, self.vault_root, self.journal)
        first_projection.write_bytes(first_bytes)
        first_projection.chmod(0o600)
        with sqlite3.connect(self.paths.catalog_path) as db:
            db.execute("UPDATE projection_states SET projection_path='relationships/wrong-v1.md' "
                       "WHERE source_id=? AND source_version=1", ("src_acceptance",))
        with self.assertRaisesRegex(ValueError, "projection_identity_mismatch"):
            _handoff.snapshot(self.api_home, self.vault_root, self.journal)
        with sqlite3.connect(self.paths.catalog_path) as db:
            db.execute("UPDATE projection_states SET projection_path=?, "
                       "desired_hash=?, last_projected_hash=? WHERE source_id=? AND source_version=1",
                       (first_projection.relative_to(self.journal).as_posix(), "0" * 64, "0" * 64, "src_acceptance"))
        with self.assertRaisesRegex(ValueError, "projection_provenance_mismatch"):
            _handoff.snapshot(self.api_home, self.vault_root, self.journal)
        for state in ("pending", "conflict"):
            with sqlite3.connect(self.paths.catalog_path) as db:
                db.execute("UPDATE projection_states SET state=? WHERE source_id=? AND source_version=1",
                           (state, "src_acceptance"))
            with self.assertRaisesRegex(ValueError, "unresolved_projection"):
                _handoff.snapshot(self.api_home, self.vault_root, self.journal)

    def test_handoff_output_is_private_and_refuses_unsafe_parent(self):
        scripts = self.base / "package" / "scripts"
        scripts.mkdir(parents=True)
        dev = scripts.parent / ".dev"
        dev.mkdir(mode=0o700)
        payload = {"schema_version": 1, "markdown": [], "purpose": "test source to sync next"}
        ready = SimpleNamespace(returncode=0, stdout=b'{"ready": true}')
        with patch.object(_handoff, "__file__", str(scripts / "sync_handoff.py")), \
             patch.object(_handoff.subprocess, "run", return_value=ready), \
             patch.object(_handoff, "snapshot", return_value=payload), \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(_handoff.main(), 0)
            output = dev / "sync-handoff.json"
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
            self.assertEqual(json.loads(output.read_text()), payload)
            before = output.read_bytes()
            dev.chmod(0o777)
            self.assertEqual(_handoff.main(), 1)
            self.assertEqual(output.read_bytes(), before)
        dev.chmod(0o700)
