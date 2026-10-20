"""``lifecontext capture``: turn settled Markdown notes in the vault's ``Journal/Inbox`` into journal entries.

A note is captured only after its bytes stopped changing for ``SETTLE_SECONDS``. Each note
keeps one source ID derived from its path: the first capture retains version 1, a later
edit corrects it to the next version. A submission is written to the state directory first
and sent with a fixed idempotency key, so a run that stops half way finishes it on the next
run without a duplicate. When the API cannot be reached or fails (5xx), the submission stays
pending and is sent again. When the API refuses the note for good, the refusal is recorded
against the note's bytes: it is reported on every run but not sent again until the note
changes. The note is never changed or removed. Capture talks to the running API through
``CaptureApi``; it never opens the catalog.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import time
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Protocol

from lifecontext.paths import LifeContextPaths, atomic_write

MAX_BYTES = 1024 * 1024
SETTLE_SECONDS = 15


class CaptureError(RuntimeError):
    """A stable failure that never includes paths or note text."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class Rejected(RuntimeError):
    """The API refused this submission for good, as opposed to being unreachable or failing."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class CaptureApi(Protocol):
    """The API calls capture needs.

    ``retain`` and ``correct`` return True when the mutation completed and False when it is not
    done yet (the API is unreachable or failing: try again later), and raise ``Rejected`` with the
    API's error code when the API refused the note. ``evidence`` returns None when it cannot answer.
    """

    def retain(self, source_id: str, raw: bytes, key: str) -> bool: ...

    def correct(self, source_id: str, expected_version: int, raw: bytes, key: str) -> bool: ...

    def evidence(self, source_id: str, version: int) -> Mapping[str, Any] | None: ...


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def default_inbox(settings: Mapping[str, Any], paths: LifeContextPaths) -> Path:
    """``Journal/Inbox`` of the configured vault (of the default vault when none is configured)."""
    vault = settings.get("vault")
    if vault:
        return Path(vault["journal_path"]) / "Inbox"
    from lifecontext.vault import default_journal_paths

    return default_journal_paths(paths.home)[1] / "Inbox"


def state_directory(paths: LifeContextPaths) -> Path:
    return paths.lifeweave_home / "capture"


def _private_directory(path: Path) -> None:
    try:
        path.mkdir(mode=0o700)
        os.chmod(path, 0o700)
    except FileExistsError:
        pass
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise CaptureError("unsafe_state")


def read_note(path: Path) -> bytes:
    """A regular, non-empty, UTF-8 file of at most ``MAX_BYTES`` that did not change while it was read."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= MAX_BYTES:
            raise ValueError("invalid_note")
        raw = stream.read(MAX_BYTES + 1)
        after = os.fstat(stream.fileno())
    if len(raw) > MAX_BYTES or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("unstable_note")
    raw.decode("utf-8")
    return raw


class Capture:
    def __init__(self, inbox: Path, directory: Path, api: CaptureApi, *,
                 now: Callable[[], float] = time.time, settle: float = SETTLE_SECONDS):
        self.inbox, self.directory, self.api, self.now, self.settle = Path(inbox), Path(directory), api, now, settle
        if self.inbox.name != "Inbox":
            raise CaptureError("capture_requires_inbox")
        _private_directory(self.directory)
        self.path = self.directory / "state.json"
        try:
            self.data: dict[str, dict[str, Any]] = json.loads(self.path.read_text())
        except FileNotFoundError:
            self.data = {}
        except (OSError, ValueError):
            raise CaptureError("unsafe_state") from None
        # Notes the API refused, by path inside the inbox, as of this run.
        self.failures: dict[str, str] = {}

    def save(self) -> None:
        atomic_write(self.path, json.dumps(self.data, sort_keys=True).encode())

    def sweep(self) -> None:
        """Remove what no pending submission needs: note text left by a run that stopped between steps."""
        keep = {"state.json", "lock", *(item["pending"]["snapshot"] for item in self.data.values()
                                        if "pending" in item)}
        for entry in self.directory.iterdir():
            if entry.name not in keep and entry.is_file():
                entry.unlink(missing_ok=True)

    def reject(self, relative: str, item: dict[str, Any], code: str) -> str:
        """Record that the API refused these bytes: no more sends until the note changes."""
        pending = item.pop("pending")
        item["rejected"] = {"hash": pending["hash"], "code": code}
        self.failures[relative] = code
        self.save()
        (self.directory / pending["snapshot"]).unlink(missing_ok=True)
        return "failed"

    def submit(self, relative: str, item: dict[str, Any]) -> str:
        """Send the pending submission.

        Returns ``captured``; ``pending`` (try again later); ``failed`` (the API refused it); or
        ``dropped`` (its snapshot is gone or damaged, so the submission is forgotten and the note,
        if it still exists, is snapshotted again).
        """
        pending = item["pending"]
        snapshot = self.directory / pending["snapshot"]
        try:
            raw = read_note(snapshot)
            if digest(raw) != pending["hash"]:
                raise ValueError("snapshot_mismatch")
        except (FileNotFoundError, ValueError):
            del item["pending"]
            self.save()
            snapshot.unlink(missing_ok=True)
            return "dropped"
        try:
            if pending["version"] == 1:
                done = self.api.retain(item["source"], raw, pending["key"])
            else:
                done = self.api.correct(item["source"], pending["version"] - 1, raw, pending["key"])
            evidence = self.api.evidence(item["source"], pending["version"]) if done else None
        except Rejected as exc:
            return self.reject(relative, item, exc.code)
        if not evidence:
            return "pending"
        if any(evidence.get(field) != expected for field, expected in (
                ("source_id", item["source"]), ("source_version", pending["version"]), ("sha256", pending["hash"]))):
            # The mutation did complete, so the source is at this version now; a later edit corrects from it.
            item["version"] = pending["version"]
            return self.reject(relative, item, "evidence_mismatch")
        item.update(version=pending["version"], imported_hash=pending["hash"])
        item.pop("rejected", None)
        del item["pending"]
        self.save()
        snapshot.unlink(missing_ok=True)
        return "captured"

    def scan(self) -> dict[str, Any]:
        counts = {"captured": 0, "pending": 0, "skipped": 0}
        self.sweep()
        # Finish submissions that stopped half way, even when the note was deleted since.
        for relative, item in self.data.items():
            if "pending" in item:
                try:
                    outcome = self.submit(relative, item)
                except (OSError, ValueError):
                    outcome = "pending"
                if outcome in counts:
                    counts[outcome] += 1
        if self.inbox.is_dir() and not self.inbox.is_symlink():
            self.scan_inbox(counts)
        failures = [{"note": note, "code": code} for note, code in sorted(self.failures.items())]
        return {**counts, "failed": len(failures), "failures": failures}

    def scan_inbox(self, counts: dict[str, int]) -> None:
        for folder, directories, files in os.walk(self.inbox, followlinks=False):
            if Path(folder).is_symlink():
                directories[:] = []
                continue
            directories[:] = [name for name in directories if not name.startswith(".")
                              and not (Path(folder) / name).is_symlink()]
            for name in files:
                if not name.endswith(".md") or name.startswith("."):
                    continue
                path = Path(folder) / name
                try:
                    raw = read_note(path)
                    relative = str(path.relative_to(self.inbox))
                    # A renamed note is a new note: its source ID follows its path.
                    item = self.data.setdefault(
                        relative, {"source": "obsidian_" + digest(relative.encode()), "version": 0})
                    checksum = digest(raw)
                    if item.get("observed_hash") != checksum:
                        item.update(observed_hash=checksum, observed_at=self.now())
                        self.save()
                        continue
                    rejected = item.get("rejected")
                    if rejected and rejected["hash"] == checksum:
                        self.failures[relative] = rejected["code"]
                        continue
                    if "pending" in item or checksum == item.get("imported_hash") \
                            or self.now() - item["observed_at"] < self.settle:
                        continue
                    snapshot = uuid.uuid4().hex + ".md"
                    atomic_write(self.directory / snapshot, raw)
                    item["pending"] = {"hash": checksum, "version": item["version"] + 1,
                                       "snapshot": snapshot, "key": "obsidian:" + uuid.uuid4().hex}
                    self.save()
                    outcome = self.submit(relative, item)
                    if outcome in counts:
                        counts[outcome] += 1
                except (OSError, ValueError):
                    counts["skipped"] += 1


def run_once(inbox: Path, paths: LifeContextPaths, api: CaptureApi, *,
             now: Callable[[], float] = time.time, settle: float = SETTLE_SECONDS) -> dict[str, Any]:
    """One scan of ``inbox`` under a lock; ``busy`` when another capture run holds it."""
    directory = state_directory(paths)
    try:
        paths.ensure_private_home()
        _private_directory(directory)
        descriptor = os.open(directory / "lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError:
        raise CaptureError("unsafe_state") from None
    with os.fdopen(descriptor, "w"):
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"inbox": str(inbox), "inbox_found": inbox.is_dir(), "busy": True, "captured": 0,
                    "pending": 0, "skipped": 0, "failed": 0, "failures": []}
        try:
            counts = Capture(inbox, directory, api, now=now, settle=settle).scan()
        except OSError:
            raise CaptureError("capture_unavailable") from None
    return {"inbox": str(inbox), "inbox_found": inbox.is_dir() and not inbox.is_symlink(), "busy": False, **counts}


def render(report: Mapping[str, Any]) -> str:
    if report["busy"]:
        return "Another capture run is in progress; nothing to do."
    lines = [(f"Captured {report['captured']}, still pending {report['pending']}, "
              f"failed {report['failed']}, skipped {report['skipped']}.")]
    lines += [(f"Refused: {Path(report['inbox']) / failure['note']} ({failure['code']}); "
               "it is sent again when the note changes.") for failure in report["failures"]]
    if not report["inbox_found"]:
        lines.append(f"The inbox folder does not exist yet: {report['inbox']} (create it in Obsidian, "
                     "or pass --inbox DIR).")
    if report["pending"]:
        lines.append("Pending notes are sent again on the next run; the API was unreachable or failing.")
    return "\n".join(lines)
