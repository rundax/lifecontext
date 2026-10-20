"""Run a legacy export in a disposable home and stop at the evidence gate."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml


V1_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = V1_ROOT.parent
REPORT_SCHEMA_VERSION = 1
MIGRATION_EXPORT_FORMAT = "hermes.journal.migration"
MIGRATION_EXPORT_VERSION = 1
MAX_CONFIG_BYTES = 1_048_576
MAX_MANIFEST_BYTES = 4_194_304
MAX_SOURCE_COUNT = 100_000
MAX_MIGRATION_ITEMS = 4_096
MAX_MIGRATION_BYTES = 67_108_864
_SOURCE_ID = re.compile(r"jrnl_src_[0-9a-f]{32}\Z")
_OWNER_NAMESPACE = re.compile(r"[0-9a-f]{32}\Z")


class _GateBlocked(Exception):
    def __init__(self, reason_code: str):
        super().__init__(reason_code)
        self.reason_code = reason_code


def _scratch_root() -> Path:
    dev = V1_ROOT / ".dev"
    if dev.is_symlink():
        raise _GateBlocked("scratch_root_unsafe")
    dev.mkdir(mode=0o700, exist_ok=True)
    root = dev / "tmp"
    if root.is_symlink():
        raise _GateBlocked("scratch_root_unsafe")
    root.mkdir(mode=0o700, exist_ok=True)
    resolved = root.resolve(strict=True)
    if not resolved.is_relative_to(V1_ROOT):
        raise _GateBlocked("scratch_root_unsafe")
    if os.name == "posix":
        info = resolved.stat()
        if info.st_uid != os.geteuid() or not stat.S_ISDIR(info.st_mode):
            raise _GateBlocked("scratch_root_unsafe")
        resolved.chmod(0o700)
    return resolved


def _source_home(path: Path, scratch: Path) -> Path:
    if not path.is_absolute():
        raise _GateBlocked("source_home_not_disposable")
    if path.is_symlink():
        raise _GateBlocked("source_home_not_disposable")
    try:
        lexical = Path(os.path.abspath(path))
        resolved = path.resolve(strict=True)
        relative = resolved.relative_to(scratch)
    except (OSError, ValueError):
        raise _GateBlocked("source_home_not_disposable") from None
    if lexical != resolved or not relative.parts or not resolved.is_dir():
        raise _GateBlocked("source_home_not_disposable")
    current = scratch
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise _GateBlocked("source_home_not_disposable")
    if os.name == "posix":
        info = resolved.stat()
        if info.st_uid != os.geteuid() or not stat.S_ISDIR(info.st_mode):
            raise _GateBlocked("source_home_not_disposable")
        resolved.chmod(0o700)
    return resolved


def _export_roots(value: Any):
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "journal_export_root":
                yield item
            yield from _export_roots(item)
    elif isinstance(value, list):
        for item in value:
            yield from _export_roots(item)


def _check_disposable_configuration(home: Path) -> None:
    config_path = home / "config.yaml"
    config: Any = {}
    if config_path.exists() or config_path.is_symlink():
        if config_path.is_symlink() or not config_path.is_file():
            raise _GateBlocked("legacy_config_unsafe")
        try:
            with config_path.open("rb") as stream:
                raw = stream.read(MAX_CONFIG_BYTES + 1)
            if len(raw) > MAX_CONFIG_BYTES:
                raise _GateBlocked("legacy_config_invalid")
            config = yaml.safe_load(raw) or {}
        except _GateBlocked:
            raise
        except Exception:
            raise _GateBlocked("legacy_config_invalid") from None
    if not isinstance(config, dict):
        raise _GateBlocked("legacy_config_invalid")

    memory = config.get("memory", {})
    if not isinstance(memory, dict):
        raise _GateBlocked("legacy_config_invalid")
    provider = memory.get("hindsight", {})
    if not isinstance(provider, dict):
        raise _GateBlocked("legacy_config_invalid")
    profile_config = _read_json_config(home / "hindsight" / "config.json")
    fallback_config = _read_json_config(home / ".hindsight" / "config.json")
    if "mode" in provider:
        mode = provider["mode"]
    elif profile_config is not None:
        mode = profile_config.get("mode", "cloud")
    elif fallback_config is not None:
        mode = fallback_config.get("mode", "cloud")
    else:
        # The child environment pins local_embedded when no config overrides it.
        mode = "local_embedded"
    api_url = (
        provider.get("api_url")
        or (profile_config or {}).get("api_url")
        or (fallback_config or {}).get("api_url")
        or "http://localhost:8888"
    )
    try:
        endpoint = urlsplit(api_url)
    except (TypeError, ValueError):
        raise _GateBlocked("remote_hindsight_not_allowed") from None
    if (
        not isinstance(mode, str)
        or mode not in {"local", "local_embedded"}
        or endpoint.hostname not in {"localhost", "127.0.0.1", "::1"}
    ):
        raise _GateBlocked("remote_hindsight_not_allowed")

    for configured in _export_roots(config):
        if configured in (None, ""):
            continue
        if not isinstance(configured, str):
            raise _GateBlocked("unsafe_export_root")
        if configured == "~":
            candidate = home
        elif configured.startswith("~/"):
            candidate = home / configured[2:]
        else:
            candidate = Path(configured)
            if not candidate.is_absolute():
                candidate = home / candidate
        try:
            candidate = candidate.resolve(strict=False)
            candidate.relative_to(home)
        except (OSError, ValueError):
            raise _GateBlocked("unsafe_export_root") from None


def _read_json_config(path: Path) -> dict[str, Any] | None:
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink() or not path.is_file():
        raise _GateBlocked("legacy_config_unsafe")
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_CONFIG_BYTES + 1)
        if len(raw) > MAX_CONFIG_BYTES:
            raise _GateBlocked("legacy_config_invalid")
        value = json.loads(raw)
    except _GateBlocked:
        raise
    except Exception:
        raise _GateBlocked("legacy_config_invalid") from None
    if not isinstance(value, dict):
        raise _GateBlocked("legacy_config_invalid")
    return value


def _report(
    status: str,
    reason_code: str,
    *,
    missing_evidence: list[str] | None = None,
    source_count: int = 0,
    forgotten_source_count: int | None = None,
) -> dict[str, Any]:
    report = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "status": status,
        "reason_code": reason_code,
        "missing_evidence": sorted(missing_evidence or []),
        "source_count": source_count,
        "writes_started": False,
        "v1_import_started": False,
        "v1_bundle_roundtrip": {"status": "NOT_RUN"},
        "converted_bundle": {"status": "NOT_RUN"},
    }
    if forgotten_source_count is not None:
        report["forgotten_source_count"] = forgotten_source_count
    return report


def _save_report(home: Path, report: dict[str, Any]) -> None:
    target = home / f"lifeweave-migration-report-{uuid.uuid4().hex}.json"
    fd, temporary = tempfile.mkstemp(prefix=".migration-report-", dir=home)
    try:
        if os.name == "posix":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(report, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, target)
        directory_fd = os.open(home, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _load_export_manifest(
    home: Path,
    result: subprocess.CompletedProcess[str],
    expected_name: str,
) -> tuple[Path, dict[str, Any]]:
    try:
        response = json.loads(result.stdout)
        destination = response.get("destination")
        if response.get("success") is not True or not isinstance(destination, str):
            raise ValueError
        exported = Path(destination)
        if not exported.is_absolute():
            exported = home / exported
        exported = exported.resolve(strict=True)
        relative = exported.relative_to(home)
        if exported.name != expected_name:
            raise ValueError
        current = home
        for part in relative.parts:
            current /= part
            if current.is_symlink():
                raise ValueError
        manifest_path = exported / "manifest.json"
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(manifest_path, flags)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_MANIFEST_BYTES:
                raise ValueError
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                raw = stream.read(MAX_MANIFEST_BYTES + 1)
        finally:
            os.close(descriptor)
        if len(raw) > MAX_MANIFEST_BYTES:
            raise ValueError
        manifest = _strict_json(raw)
        if not isinstance(manifest, dict):
            raise ValueError
        return exported, manifest
    except Exception:
        raise _GateBlocked("legacy_export_invalid") from None


def _safe_migration_file(path: Path, root: Path, max_bytes: int) -> bytes:
    try:
        relative = path.relative_to(root)
        current = root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise ValueError
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError
            if info.st_size > max_bytes:
                raise _GateBlocked("migration_bundle_limit_exceeded")
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                raw = stream.read(max_bytes + 1)
        finally:
            os.close(descriptor)
        if len(raw) > max_bytes:
            raise _GateBlocked("migration_bundle_limit_exceeded")
        return raw
    except _GateBlocked:
        raise
    except Exception:
        raise _GateBlocked("legacy_migration_export_invalid") from None


def _directory_names(path: Path, max_entries: int) -> set[str]:
    names = set()
    try:
        for entry in path.iterdir():
            if len(names) >= max_entries:
                raise _GateBlocked("migration_inventory_limit_exceeded")
            names.add(entry.name)
    except _GateBlocked:
        raise
    except Exception:
        raise _GateBlocked("legacy_migration_export_invalid") from None
    return names


def _strict_json(raw: bytes) -> Any:
    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=object_pairs)
    except (UnicodeError, ValueError, TypeError):
        raise _GateBlocked("legacy_migration_export_invalid") from None


def convert_migration_export(export_root: Path) -> tuple[bytes, dict[str, int]]:
    """Validate an opt-in legacy inventory and encode the existing V1 bundle."""
    try:
        lexical_root = Path(os.path.abspath(export_root))
        current = Path(lexical_root.anchor)
        for part in lexical_root.parts[1:]:
            current = current / part
            if current.is_symlink():
                raise ValueError
        export_root = lexical_root.resolve(strict=True)
        if lexical_root != export_root or not export_root.is_dir():
            raise ValueError
        if _directory_names(export_root, 3) != {"manifest.json", "sources"}:
            raise ValueError
        manifest = _strict_json(
            _safe_migration_file(
                export_root / "manifest.json", export_root, MAX_MANIFEST_BYTES,
            )
        )
        if not isinstance(manifest, dict):
            raise ValueError
        if set(manifest) != {
            "format", "format_version", "inventory_complete", "owner_namespace",
            "source_ids", "tombstone_ids", "items",
        }:
            raise ValueError
        if (
            manifest.get("format") != MIGRATION_EXPORT_FORMAT
            or manifest.get("inventory_complete") is not True
        ):
            raise _GateBlocked("legacy_lifecycle_evidence_incomplete")
        if (
            isinstance(manifest.get("format_version"), bool)
            or manifest.get("format_version") != MIGRATION_EXPORT_VERSION
        ):
            raise ValueError
        owner_namespace = manifest.get("owner_namespace")
        source_ids = manifest.get("source_ids")
        tombstone_ids = manifest.get("tombstone_ids")
        items = manifest.get("items")
        if isinstance(source_ids, list) and len(source_ids) > MAX_MIGRATION_ITEMS:
            raise _GateBlocked("migration_inventory_limit_exceeded")
        if isinstance(items, list) and len(items) > MAX_MIGRATION_ITEMS:
            raise _GateBlocked("migration_inventory_limit_exceeded")
        if (
            not isinstance(owner_namespace, str)
            or not _OWNER_NAMESPACE.fullmatch(owner_namespace)
            or not isinstance(source_ids, list)
            or len(source_ids) > MAX_SOURCE_COUNT
            or source_ids != sorted(set(source_ids))
            or any(not isinstance(value, str) or not _SOURCE_ID.fullmatch(value) for value in source_ids)
            or not isinstance(tombstone_ids, list)
            or tombstone_ids != sorted(set(tombstone_ids))
            or any(not isinstance(value, str) or value not in source_ids for value in tombstone_ids)
            or not isinstance(items, list)
        ):
            raise ValueError

        records_input: list[dict[str, Any]] = []
        identities: set[tuple[str, int]] = set()
        versions_by_source: dict[str, list[int]] = {}
        lifecycle_by_source: dict[str, str] = {}
        for item in items:
            if not isinstance(item, dict) or set(item) != {
                "source_id", "source_version", "lifecycle_state",
            }:
                raise ValueError
            source_id = item.get("source_id")
            version = item.get("source_version")
            state = item.get("lifecycle_state")
            if state in {"forgetting", "deleting"}:
                raise _GateBlocked("legacy_lifecycle_evidence_incomplete")
            if (
                not isinstance(source_id, str)
                or not _SOURCE_ID.fullmatch(source_id)
                or isinstance(version, bool)
                or not isinstance(version, int)
                or version < 1
                or state not in {"active", "forgotten", "deleted"}
            ):
                raise ValueError
            identity = (source_id, version)
            if identity in identities:
                raise ValueError
            identities.add(identity)
            versions_by_source.setdefault(source_id, []).append(version)
            previous = lifecycle_by_source.setdefault(source_id, state)
            if previous != state:
                raise ValueError
            records_input.append(item)
        if records_input != sorted(records_input, key=lambda item: (item["source_id"], item["source_version"])):
            raise ValueError
        if set(versions_by_source) != set(source_ids):
            raise _GateBlocked("legacy_lifecycle_evidence_incomplete")
        if any(
            versions != list(range(1, len(versions) + 1))
            for versions in versions_by_source.values()
        ):
            raise _GateBlocked("legacy_lifecycle_evidence_incomplete")
        declared_deleted = sorted(
            source_id for source_id, state in lifecycle_by_source.items() if state == "deleted"
        )
        if tombstone_ids != declared_deleted:
            raise _GateBlocked("legacy_lifecycle_evidence_incomplete")

        sources_root = export_root / "sources"
        if sources_root.is_symlink() or not sources_root.is_dir():
            raise ValueError
        expected_dirs = {
            source_id for source_id, state in lifecycle_by_source.items() if state != "deleted"
        }
        if _directory_names(sources_root, MAX_MIGRATION_ITEMS + 1) != expected_dirs:
            raise _GateBlocked("legacy_lifecycle_evidence_incomplete")

        if str(V1_ROOT / "src") not in sys.path:
            sys.path.insert(0, str(V1_ROOT / "src"))
        from lifecontext.bundle import BundleError, BundleRecord, encode_bundle
        from lifecontext.config import JournalLimits
        from lifecontext.custody import SourceManifest

        limits = JournalLimits()
        records: list[BundleRecord] = []
        total_input_bytes = 0
        for item in records_input:
            source_id = item["source_id"]
            version = item["source_version"]
            state = item["lifecycle_state"]
            if state == "deleted":
                records.append(BundleRecord(source_id, version, "deleted"))
                continue
            source_dir = sources_root / source_id
            if source_dir.is_symlink() or not source_dir.is_dir():
                raise ValueError
            version_dirs = _directory_names(source_dir, MAX_MIGRATION_ITEMS + 1)
            expected_versions = {
                f"v{number}" for number in versions_by_source[source_id]
            }
            if version_dirs != expected_versions:
                raise _GateBlocked("legacy_lifecycle_evidence_incomplete")
            version_dir = source_dir / f"v{version}"
            if version_dir.is_symlink() or not version_dir.is_dir():
                raise ValueError
            if _directory_names(version_dir, 3) != {"manifest.json", "original.bin"}:
                raise ValueError
            manifest_raw = _safe_migration_file(
                version_dir / "manifest.json", export_root, 65_536,
            )
            total_input_bytes += len(manifest_raw)
            if total_input_bytes > MAX_MIGRATION_BYTES:
                raise _GateBlocked("migration_bundle_limit_exceeded")
            source_manifest_data = _strict_json(manifest_raw)
            if (
                not isinstance(source_manifest_data, dict)
                or set(source_manifest_data) != {
                    "schema_version", "source_id", "source_version", "sha256",
                    "size_bytes", "provenance",
                }
            ):
                raise ValueError
            manifest_bytes = json.dumps(
                source_manifest_data, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"), allow_nan=False,
            ).encode("utf-8")
            manifest = SourceManifest.from_bytes(manifest_bytes)
            if manifest.source_id != source_id or manifest.source_version != version:
                raise ValueError
            remaining = min(MAX_MIGRATION_BYTES - total_input_bytes, limits.max_file_bytes)
            if remaining < 0:
                raise _GateBlocked("migration_bundle_limit_exceeded")
            original = _safe_migration_file(
                version_dir / "original.bin", export_root, remaining,
            )
            total_input_bytes += len(original)
            if total_input_bytes > MAX_MIGRATION_BYTES:
                raise _GateBlocked("migration_bundle_limit_exceeded")
            if len(original) != manifest.size_bytes or hashlib.sha256(original).hexdigest() != manifest.sha256:
                raise _GateBlocked("legacy_lifecycle_evidence_incomplete")
            records.append(BundleRecord(source_id, version, state, manifest, original))

        try:
            bundle = encode_bundle(records, limits=limits)
        except BundleError as exc:
            if str(exc) == "bundle limit exceeded":
                raise _GateBlocked("migration_bundle_limit_exceeded") from None
            raise
        counts = {
            "source_count": len(source_ids),
            "version_count": len(records),
            "forgotten_source_count": sum(state == "forgotten" for state in lifecycle_by_source.values()),
            "deleted_source_count": len(tombstone_ids),
        }
        return bundle, counts
    except _GateBlocked:
        raise
    except Exception:
        raise _GateBlocked("legacy_migration_export_invalid") from None


def _save_converted_bundle(home: Path, name: str, raw: bytes) -> str:
    relative = f".lifeweave-tmp/{name}.bundle"
    target = home / relative
    fd = -1
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "wb") as stream:
            fd = -1
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        directory_fd = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        if fd >= 0:
            os.close(fd)
        try:
            target.unlink()
        except OSError:
            pass
        raise _GateBlocked("disposable_scratch_unavailable") from None
    return relative


def run_migration(source_home: Path) -> dict[str, Any]:
    """Export only from an isolated HERMES_HOME and report before V1 writes."""
    try:
        scratch = _scratch_root()
        home = _source_home(Path(source_home), scratch)
    except _GateBlocked as exc:
        return _report("BLOCKED", exc.reason_code)
    try:
        _check_disposable_configuration(home)
    except _GateBlocked as exc:
        report = _report("BLOCKED", exc.reason_code)
        _save_report(home, report)
        return report

    temporary = home / ".lifeweave-tmp"
    if temporary.is_symlink():
        report = _report("BLOCKED", "source_home_not_disposable")
        _save_report(home, report)
        return report
    try:
        temporary.mkdir(mode=0o700, exist_ok=True)
        temporary.chmod(0o700)
    except OSError:
        report = _report("FAILED", "disposable_scratch_unavailable")
        _save_report(home, report)
        return report

    exporter = shutil.which("hermes")
    if not exporter:
        report = _report("FAILED", "legacy_export_unavailable")
        _save_report(home, report)
        return report

    export_name = "lifeweave-migration-" + uuid.uuid4().hex
    environment = {
        "PATH": os.pathsep.join((str(Path(exporter).parent), os.defpath)),
        "HOME": str(home),
        "HERMES_HOME": str(home),
        "TMPDIR": str(temporary),
        "TEMP": str(temporary),
        "TMP": str(temporary),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "HINDSIGHT_MODE": "local_embedded",
        "HINDSIGHT_API_URL": "http://localhost:8888",
        "PYTHONPATH": str(REPOSITORY_ROOT),
        "PYTHONNOUSERSITE": "1",
    }
    try:
        completed = subprocess.run(
            [exporter, "hindsight", "journal", "export", export_name, "--migration"],
            cwd=home,
            env=environment,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        report = _report("FAILED", "legacy_export_failed")
        _save_report(home, report)
        return report
    if completed.returncode != 0:
        report = _report("FAILED", "legacy_export_failed")
        _save_report(home, report)
        return report

    try:
        export_root, manifest = _load_export_manifest(home, completed, export_name)
    except _GateBlocked as exc:
        report = _report("FAILED", exc.reason_code)
        _save_report(home, report)
        return report
    if "format" not in manifest or manifest.get("format") != MIGRATION_EXPORT_FORMAT:
        report = _report(
            "BLOCKED",
            "legacy_lifecycle_evidence_incomplete",
            missing_evidence=["version_inventory", "tombstone_inventory", "per_version_lifecycle_state"],
        )
    else:
        try:
            bundle, counts = convert_migration_export(export_root)
            bundle_relative_path = _save_converted_bundle(home, export_name, bundle)
        except _GateBlocked as exc:
            status = "BLOCKED" if exc.reason_code in {
                "legacy_lifecycle_evidence_incomplete",
                "migration_bundle_limit_exceeded",
                "migration_inventory_limit_exceeded",
            } else "FAILED"
            report = _report(status, exc.reason_code)
        else:
            report = _report("READY_FOR_V1_IMPORT", "migration_bundle_ready")
            report.update(counts)
            report["converted_bundle"] = {
                "status": "READY",
                "path": bundle_relative_path,
                "sha256": hashlib.sha256(bundle).hexdigest(),
                "size_bytes": len(bundle),
            }
    _save_report(home, report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Audit a legacy Journal export in a disposable V1 scratch home."
    )
    parser.add_argument(
        "--source-home",
        required=True,
        type=Path,
        help="a copied, disposable HERMES_HOME beneath .dev/tmp",
    )
    args = parser.parse_args(argv)
    try:
        report = run_migration(args.source_home)
    except _GateBlocked as exc:
        report = _report("BLOCKED", exc.reason_code)
    print(json.dumps(report, sort_keys=True))
    return 0 if report["status"] == "READY_FOR_V1_IMPORT" else 2


if __name__ == "__main__":
    sys.exit(main())
