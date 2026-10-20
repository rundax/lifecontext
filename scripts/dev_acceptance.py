#!/usr/bin/env python3
"""Exercise two external shell callers on the stable dev API with synthetic data."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import time
import uuid

from sync_handoff import snapshot


def main():
    dev = Path(__file__).resolve().parents[1] / ".dev"
    launcher = dev.parent / "bin" / "lifecontext-dev"

    retries = []

    def call(cwd, arguments):
        deadline = time.monotonic() + 185
        completed = subprocess.run(
            ["/bin/bash", "-c", 'exec "$@"', "lifeweave-acceptance", str(launcher), *arguments],
            cwd=cwd, capture_output=True, timeout=120,
        )
        if completed.returncode:
            body = json.loads(completed.stdout)
            # Only replay the exact persisted idempotency identity. Live provider
            # verification can briefly be unavailable to a concurrent caller.
            if "--idempotency-key" in arguments and body.get("error") == "provider_unavailable":
                while time.monotonic() < deadline:
                    time.sleep(0.5)
                    retries.append("same_idempotency_key")
                    completed = subprocess.run(
                        ["/bin/bash", "-c", 'exec "$@"', "lifeweave-acceptance", str(launcher), *arguments],
                        cwd=cwd, capture_output=True, timeout=120,
                    )
                    if not completed.returncode:
                        return json.loads(completed.stdout)
            raise ValueError("dev_cli_failed")
        return json.loads(completed.stdout)

    token = uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix="lifeweave-agent-a.") as a, tempfile.TemporaryDirectory(prefix="lifeweave-agent-b.") as b:
        if not call(a, ["status", "--json"])["ready"]:
            raise ValueError("dev_api_not_ready")
        source = Path(a) / "synthetic.md"
        original = b"Synthetic milestone acceptance; no personal data.\n"
        source.write_bytes(original)
        source.chmod(0o600)
        identity = "src_acceptance_" + token
        args = ["retain", identity, "1", "--input", str(source),
                "--idempotency-key", "acceptance-" + token, "--json"]
        with ThreadPoolExecutor(max_workers=2) as pool:
            retained = list(pool.map(lambda cwd: call(cwd, args), (a, b)))
        if retained[0]["operation_id"] != retained[1]["operation_id"]:
            raise ValueError("idempotency_failed")
        if retained[0]["sha256"] != hashlib.sha256(original).hexdigest():
            raise ValueError("source_hash_failed")
        first = dev / "vault" / "Journal" / "entries" / (identity + "-v1.md")
        first_hash = hashlib.sha256(first.read_bytes()).hexdigest()
        source.write_bytes(b"Synthetic corrected milestone acceptance.\n")
        corrected = call(b, ["curate", "correct", identity, "--expected-version", "1",
                             "--input", str(source), "--json"])
        evidence = call(a, ["evidence", identity, "--version", "2", "--json"])
        if evidence["sha256"] != hashlib.sha256(source.read_bytes()).hexdigest():
            raise ValueError("correction_hash_failed")
        if hashlib.sha256(first.read_bytes()).hexdigest() != first_hash:
            raise ValueError("immutable_version_changed")
        handoff = snapshot(dev / "home", dev / "vault", dev / "vault" / "Journal")
        if not any(item["path"] == "entries/" + identity + "-v2.md" for item in handoff["markdown"]):
            raise ValueError("correction_projection_missing")
        print(json.dumps({
            "provider": "configured live dev Hindsight", "source_id": identity,
            "outside_repository_shell_sessions": 2,
            "concurrent_idempotency": "passed", "same_key_transient_retries": len(retries), "capture_and_correction": "passed",
            "retain_operation_id": retained[0]["operation_id"],
            "correction_operation_id": corrected["operation_id"],
            "verified_markdown_count": len(handoff["markdown"]),
        }, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        raise SystemExit("Acceptance failed; inspect dev service safely. No source content printed.")
