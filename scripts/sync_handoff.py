#!/usr/bin/env python3
"""Snapshot verified dev Markdown hashes locally; never enable synchronization."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tempfile

from lifecontext.vault import ManagedVault
from lifecontext.application import JournalApplication
from lifecontext.custody import LifeContextCustody, SourceManifest
from lifecontext.paths import LifeContextPaths


def snapshot(home: Path, root: Path, journal: Path) -> dict:
    """Read a catalog snapshot and descriptor-pinned Markdown under writer lock."""
    catalog = home / ".lifeweave" / "catalog.sqlite3"
    custody = LifeContextCustody(LifeContextPaths(home), max_bytes=67_108_864)
    with ManagedVault(root, journal) as vault:
        with vault.writer_lock(), sqlite3.connect(catalog.as_uri() + "?mode=ro", uri=True) as db:
            db.execute("BEGIN")
            expected = {}
            for source_id, version, manifest_json, path, state, desired, projected in db.execute(
                "SELECT sv.source_id, sv.source_version, sv.manifest_json, "
                "ps.projection_path, ps.state, ps.desired_hash, ps.last_projected_hash "
                "FROM source_versions sv LEFT JOIN projection_states ps "
                "USING(source_id, source_version) "
                "LEFT JOIN tombstones t USING(source_id, source_version) "
                "LEFT JOIN transfer_tombstones tt USING(source_id, source_version) "
                "WHERE COALESCE(t.tombstone_type, tt.tombstone_type, 'active') != 'deleted'"
            ):
                if not path or state != "projected" or not desired or desired != projected:
                    raise ValueError("unresolved_projection")
                if not JournalApplication._journal_entry_path(path):
                    raise ValueError("projection_identity_mismatch")
                evidence = custody.resolve_exact(source_id, version)
                manifest = SourceManifest.from_bytes(manifest_json.encode("utf-8"))
                if manifest.to_bytes() != evidence.manifest.to_bytes():
                    raise ValueError("custody_manifest_mismatch")
                # Earlier releases' renderings stay valid until reconciliation renames the entry.
                renderings = [{}, {"first_line_title": True}]
                if path == f"entries/{source_id}-v{version}.md":
                    renderings.append({"legacy": True})
                if not any(
                    hashlib.sha256(
                        JournalApplication._render_projection(manifest, evidence.original_bytes, **options).encode("utf-8")
                    ).hexdigest() == desired
                    for options in renderings
                ):
                    raise ValueError("projection_provenance_mismatch")
                expected[str(path)] = projected
            inventory = []
            pending = [journal.relative_to(root).as_posix()]
            while pending:
                directory = pending.pop()
                items, truncated = vault.list_vault_directory(directory)
                if truncated:
                    raise ValueError("inventory_limit")
                for name, kind in items:
                    relative = directory + "/" + name
                    if kind == "directory":
                        pending.append(relative)
                    elif name.endswith(".md"):
                        raw = vault.read_vault_file(relative, max_bytes=16_777_216)
                        digest = hashlib.sha256(raw).hexdigest()
                        journal_relative = Path(relative).relative_to(journal.relative_to(root)).as_posix()
                        if journal_relative.startswith("entries/"):
                            if expected.pop(journal_relative, None) != digest:
                                raise ValueError("projection_hash_mismatch")
                        inventory.append({"path": journal_relative, "sha256": digest, "bytes": len(raw)})
            if expected:
                raise ValueError("missing_projection")
    return {
        "schema_version": 1,
        "vault_root": str(root), "journal_path": str(journal), "service_home": str(home),
        "profile": "lifeweave", "api_url": "http://127.0.0.1:18765",
        "identity_basis": "validated dev launcher configuration; API readiness checked separately",
        "purpose": "This is the test source to sync next; synchronization is not enabled.",
        "markdown": sorted(inventory, key=lambda item: item["path"]),
        "remaining_livesync_requirements": [
            "Separate authorization to enable LiveSync and choose the Mac destination vault",
            "Configure CouchDB endpoint and credentials through a private secret mechanism",
            "Configure the Obsidian LiveSync plugin on both devices and verify conflict handling",
            "Verify the first transfer against this inventory before considering production cutover",
        ],
    }


def main() -> int:
    dev = Path(__file__).resolve().parents[1] / ".dev"
    try:
        info = dev.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError("unsafe_dev_directory")
        launcher = dev.parent / "bin" / "lifecontext-dev"
        result = subprocess.run([str(launcher), "status", "--json"], capture_output=True, timeout=30)
        if result.returncode or not json.loads(result.stdout).get("ready"):
            raise ValueError("dev_api_not_ready")
        handoff = snapshot(dev / "home", dev / "vault", dev / "vault" / "Journal")
        descriptor, temporary = tempfile.mkstemp(prefix=".sync-handoff.", dir=dev)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(handoff, stream, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, dev / "sync-handoff.json")
            directory_fd = os.open(dev, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        print("Private handoff written to .dev/sync-handoff.json")
        return 0
    except Exception:
        # Paths, provider output and arbitrary exception text may contain private data.
        print("Handoff refused: verify dev readiness, permissions and all projection hashes.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
