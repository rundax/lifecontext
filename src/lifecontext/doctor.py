"""``lifecontext doctor``: safe diagnostics that read state and never change it.

Reports versions, paths and modes, a config summary without secrets, service
readiness, OCR, vision, the model cache and counts; never document or journal
content. ``problems`` are broken prerequisites (non-zero exit); ``warnings`` are not.
"""

from __future__ import annotations

import importlib.metadata
import os
import platform
import shutil
import sqlite3
import stat
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from lifecontext import __version__
from lifecontext.paths import LifeContextPaths

OCR_LANGUAGES = ("eng", "rus", "ukr")
LOW_DISK_BYTES = 2 * 1024**3
_HINDSIGHT_PACKAGES = ("hindsight-all", "hindsight-embed", "hindsight-client")


def _entry(path: Path, *, directory: bool) -> dict[str, Any]:
    """Existence, mode and whether it is a private (0700/0600) entry owned by this user."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {"path": str(path), "exists": False, "mode": None, "private": False}
    except OSError:
        return {"path": str(path), "exists": True, "mode": None, "private": False}
    kind_ok = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    mode = stat.S_IMODE(info.st_mode)
    private = kind_ok and info.st_uid == os.geteuid() and mode == (0o700 if directory else 0o600)
    return {"path": str(path), "exists": True, "mode": f"{mode:04o}", "private": private}


def _tesseract(run: Callable[..., Any]) -> dict[str, Any]:
    path = shutil.which("tesseract")
    if path is None:
        return {"path": None, "version": None, "languages": [], "missing_languages": list(OCR_LANGUAGES)}
    try:
        version = run([path, "--version"], capture_output=True, text=True, timeout=10, check=False)
        listed = run([path, "--list-langs"], capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return {"path": path, "version": None, "languages": [], "missing_languages": list(OCR_LANGUAGES)}
    first = (version.stdout or version.stderr or "").splitlines()[:1]
    languages = sorted(line.strip() for line in (listed.stdout or "").splitlines()[1:] if line.strip())
    return {"path": path, "version": first[0].strip() if first else None, "languages": languages,
            "missing_languages": [name for name in OCR_LANGUAGES if name not in languages]}


def _operation_counts(catalog: Path) -> dict[str, int] | None:
    """Unfinished and failed operations, from a read-only catalog connection."""
    if not catalog.is_file():
        return None
    try:
        connection = sqlite3.connect(f"{catalog.as_uri()}?mode=ro", uri=True, timeout=5)
        try:
            rows = connection.execute("SELECT state, COUNT(*) FROM operations GROUP BY state").fetchall()
        finally:
            connection.close()
    except sqlite3.Error:
        return None
    counts = dict(rows)
    return {"pending": counts.get("pending", 0) + counts.get("running", 0),
            "retryable_failed": counts.get("retryable_failed", 0),
            "permanent_failed": counts.get("permanent_failed", 0)}


def _settings_report(paths: LifeContextPaths, home: Mapping[str, Any]) -> tuple[dict[str, Any], dict | None]:
    from lifecontext.config import ConfigError, load_settings
    from lifecontext.hindsight import HindsightStartupError, _validated_profile_settings

    if not os.path.lexists(paths.config_path):
        return {"state": "missing"}, None
    if not home["private"]:
        return {"state": "unreadable"}, None
    try:
        settings = load_settings()
    except ConfigError:
        return {"state": "invalid"}, None
    report: dict[str, Any] = {"state": "ok", "timezone": settings.get("timezone"),
                              "vault_configured": "vault" in settings,
                              "limits_customized": sorted(settings.get("limits") or {})}
    try:
        llm = _validated_profile_settings(settings)
    except HindsightStartupError:
        report["state"] = "invalid"
        return report, None
    report["llm"] = {"provider": llm["provider"], "model": llm["model"], "base_url": llm.get("base_url"),
                     "reasoning_effort": llm.get("reasoning_effort")}
    return report, settings


def _credentials_report(settings: Mapping[str, Any] | None) -> dict[str, Any]:
    from lifecontext.credentials import CredentialError, load_credentials
    from lifecontext.hindsight import _validated_profile_settings, llm_key_required

    try:
        credentials = load_credentials(require_llm_key=False)
    except CredentialError as exc:
        return {"state": "unsafe" if "unsafe" in str(exc) else "invalid"}
    # Without a readable config, assume the default provider, which needs a key.
    required = settings is None or llm_key_required(_validated_profile_settings(settings))
    llm = "present" if credentials.get("LIFEWEAVE_LLM_API_KEY") else ("missing" if required else "not_required")
    return {"state": "ok", "llm_key": llm,
            "vision_key": "present" if credentials.get("LIFEWEAVE_VISION_API_KEY") else "absent"}


def _vision_report(settings: Mapping[str, Any], credentials: Mapping[str, Any]) -> dict[str, Any]:
    """Configuration only: doctor never sends the probe image (``preflight --vision-probe`` does)."""
    from lifecontext.credentials import load_credentials
    from lifecontext.hindsight import HindsightStartupError
    from lifecontext.vision import load_vision_config, validated_vision_settings

    try:
        validated = validated_vision_settings(settings)
        if validated is None:
            return {"configured": False, "status": "off"}
        if credentials.get("state") != "ok":
            return {"configured": True, "status": "blocked", "error": "invalid_credentials"}
        load_vision_config(settings, load_credentials(require_llm_key=False))
    except HindsightStartupError as exc:
        return {"configured": True, "status": "blocked", "error": exc.code}
    return {"configured": True, "status": "not_probed", "provider": validated["provider"],
            "model": validated["model"], "max_pages_per_document": validated["max_pages_per_document"]}


def _service_report(fetch: Callable[[str], dict]) -> dict[str, Any]:
    try:
        health = fetch("/healthz")
    except Exception as exc:  # noqa: BLE001 -- report a stable code only
        return {"state": "not_running", "error": getattr(exc, "code", "api_unavailable")}
    if health.get("status") != "ok":
        return {"state": "not_running", "error": "invalid_api_response"}
    report: dict[str, Any] = {"state": "running"}
    try:
        status = fetch("/v1/status")
    except Exception as exc:  # noqa: BLE001
        report["status_error"] = getattr(exc, "code", "api_error")
        return report
    dependencies = status.get("dependencies") if isinstance(status.get("dependencies"), dict) else {}
    report["ready"] = status.get("ready") is True
    report["dependencies"] = {name: bool(value.get("ready")) for name, value in dependencies.items()
                              if isinstance(value, dict)}
    reconciliation = status.get("reconciliation")
    if isinstance(reconciliation, dict):
        report["reconciliation_facts"] = reconciliation.get("fact_count")
    try:
        lint = fetch("/v1/wiki/lint")
        report["lint"] = {**dict(lint.get("counts") or {}), "omitted": lint.get("omitted")}
    except Exception as exc:  # noqa: BLE001
        report["lint_error"] = getattr(exc, "code", "api_error")
    return report


class _FetchError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def http_fetcher(port: int, *, timeout: float = 30.0) -> Callable[[str], dict]:
    """GET a loopback API route, with the local token for ``/v1``; short timeout, JSON only."""
    import http.client
    import json

    from lifecontext.api.security import local_token, protected_path

    def fetch(target: str) -> dict:
        headers = {"Accept": "application/json"}
        if protected_path(target.split("?", 1)[0]):
            try:
                headers["Authorization"] = "Bearer " + local_token(LifeContextPaths.current(), create=False)
            except OSError:
                raise _FetchError("unauthorized") from None
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        try:
            connection.request("GET", target, headers=headers)
            response = connection.getresponse()
            raw = response.read(4 * 1024 * 1024)
        except (http.client.HTTPException, OSError):
            raise _FetchError("api_unavailable") from None
        finally:
            connection.close()
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError):
            raise _FetchError("invalid_api_response") from None
        if not 200 <= response.status < 300:
            code = value.get("error_code") if isinstance(value, dict) else None
            raise _FetchError(code if isinstance(code, str) and code.isidentifier() else "api_error")
        if not isinstance(value, dict):
            raise _FetchError("invalid_api_response")
        return value

    return fetch


def _engine_report(paths: LifeContextPaths) -> dict[str, Any]:
    from lifecontext.hindsight import engine_report

    try:
        return engine_report(paths)
    except Exception:  # noqa: BLE001 -- diagnostics report a state, never a raw error
        return {"profile": "unreadable"}


def _engine_problems(engine: Mapping[str, Any]) -> list[str]:
    """Hindsight engine settings that leave the memory or its database open to other local users."""
    if engine.get("profile") == "unsafe":
        return ["engine_profile_unsafe"]
    postgres = engine.get("postgres") or {}
    files = (["postgres_files_unsafe"] if postgres.get("socket_dir_mode") not in (None, "0700")
             or postgres.get("instance_file_private") is False or postgres.get("socket_dir_is_private") is False
             else [])
    if engine.get("profile") != "present":
        return (["postgres_tcp"] if postgres.get("listen") == "tcp" else []) + files
    problems = files
    if (not engine.get("auth_configured") or engine.get("token_file") != "private"
            or not engine.get("token_matches_profile") or engine.get("auth_enforced") is False):
        problems.append("engine_auth_off")
    if engine.get("daemon") == "other_owner":
        problems.append("engine_port_taken")
    if postgres.get("listen") == "tcp" or not postgres.get("configured_socket_only"):
        problems.append("postgres_tcp")
    return problems


def _disk(path: Path) -> int | None:
    while not path.exists() and path != path.parent:
        path = path.parent
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


def run_doctor(
    *, port: int, fetch: Callable[[str], dict] | None = None, run: Callable[..., Any] = subprocess.run,
    environ: Mapping[str, str] | None = None,
) -> tuple[dict[str, Any], int]:
    from lifecontext.hindsight import model_cache_status, read_daemon_start_record
    from lifecontext.vault import default_journal_paths

    fetch = fetch or http_fetcher(port)
    paths = LifeContextPaths.current()
    problems: list[str] = []
    warnings: list[str] = []
    python_ok = (3, 11) <= sys.version_info[:2] < (3, 14)
    if not python_ok:
        problems.append("python_unsupported")
    packages = {}
    for name in _HINDSIGHT_PACKAGES:
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    if any(version != "0.9.2" for version in packages.values()):
        problems.append("hindsight_packages")

    home = _entry(paths.lifeweave_home, directory=True)
    if not home["exists"]:
        problems.append("home_missing")
    elif not home["private"]:
        problems.append("home_unsafe")
    config, settings = _settings_report(paths, home)
    if config["state"] in {"missing", "invalid"}:
        problems.append(f"config_{config['state']}")
    credentials = _credentials_report(settings) if home["private"] else {"state": "unreadable"}
    if credentials["state"] in {"unsafe", "invalid"}:
        problems.append(f"credentials_{credentials['state']}")
    elif credentials.get("llm_key") == "missing":
        problems.append("credentials_missing")

    vault = (settings or {}).get("vault")
    root, journal = (Path(vault["root"]), Path(vault["journal_path"])) if vault else default_journal_paths(paths.home)
    vault_entries = {"root": _entry(root, directory=True), "journal": _entry(journal, directory=True)}
    if not all(item["exists"] for item in vault_entries.values()):
        problems.append("vault_missing")
    elif not all(item["private"] for item in vault_entries.values()):
        problems.append("vault_unsafe")

    vision = _vision_report(settings, credentials) if settings is not None else {"configured": None, "status": "unknown"}
    if vision["status"] == "blocked":
        problems.append("vision_blocked")

    service = _service_report(fetch)
    if service["state"] != "running":
        warnings.append("service_not_running")
    elif not service.get("ready"):
        warnings.append("service_not_ready")
    engine = _engine_report(paths) if home["private"] else {"profile": "unreadable"}
    problems.extend(_engine_problems(engine))
    tesseract = _tesseract(run)
    if tesseract["path"] is None:
        warnings.append("tesseract_missing")
    elif tesseract["missing_languages"]:
        warnings.append("tesseract_languages_missing")
    models = model_cache_status(paths, environ)
    record = read_daemon_start_record(paths)
    models["offline_next_start"] = models["complete"]
    models["last_daemon_start"] = record
    if not models["complete"]:
        warnings.append("models_not_cached")
    disk = {"home_free_bytes": _disk(paths.home), "vault_free_bytes": _disk(root)}
    if any(value is not None and value < LOW_DISK_BYTES for value in disk.values()):
        warnings.append("disk_low")

    report = {
        "ok": not problems, "problems": problems, "warnings": warnings,
        "version": __version__,
        "platform": {"system": platform.system(), "release": platform.release(), "machine": platform.machine()},
        "python": {"version": platform.python_version(), "executable": sys.executable, "supported": python_ok},
        "command": {"lifecontext": shutil.which("lifecontext"), "port": port},
        "hindsight_packages": packages,
        "paths": {"home": home, "config": _entry(paths.config_path, directory=False),
                  "credentials": _entry(paths.credentials_path, directory=False), **{
                      f"vault_{name}": value for name, value in vault_entries.items()}},
        "config": config, "credentials": credentials, "vision": vision,
        "service": service, "engine": engine,
        "operations": _operation_counts(paths.catalog_path) if home["private"] else None,
        "tesseract": tesseract, "models": models, "disk": disk,
    }
    return report, 1 if problems else 0


def _render_engine(engine: Mapping[str, Any]) -> str:
    postgres = engine.get("postgres") or {}
    listen = {"socket_only": "socket only", "tcp": "on TCP"}.get(postgres.get("listen"), None)
    database = (f"Postgres {postgres.get('state', 'unknown')}" + (f", {listen}" if listen else "")
                + ("" if postgres.get("configured_socket_only") or engine.get("profile") != "present"
                   else ", configured for TCP"))
    if engine.get("profile") != "present":
        return f"Hindsight engine: profile {engine.get('profile', 'unknown')}; {database}"
    auth = "on" if engine.get("auth_configured") and engine.get("token_matches_profile") else "off"
    if engine.get("auth_enforced") is not None:
        auth += ", enforced" if engine["auth_enforced"] else ", NOT enforced by the running daemon"
    daemon = {"this_home": "this HOME's daemon", "other_owner": "held by another user or HOME",
              "not_running": "not running"}.get(engine.get("daemon"), "unknown")
    return f"Hindsight engine: auth {auth}; port {engine.get('port')} ({daemon}); {database}"


def render(report: Mapping[str, Any]) -> str:
    def mode(entry: Mapping[str, Any]) -> str:
        if not entry["exists"]:
            return f"{entry['path']} (missing)"
        return f"{entry['path']} ({entry['mode']}{'' if entry['private'] else ', not private'})"

    config, service, models = report["config"], report["service"], report["models"]
    llm = config.get("llm") or {}
    lines = [
        (f"LifeContext {report['version']} on {report['platform']['system']} {report['platform']['machine']}, "
         f"Python {report['python']['version']}"),
        f"Command: {report['command']['lifecontext'] or 'lifecontext is not on PATH'}",
        f"Settings: {mode(report['paths']['home'])}; config {config['state']}"
        + (f", {llm['provider']} {llm['model']}" if llm else ""),
        f"Credentials: {report['credentials'].get('llm_key', report['credentials']['state'])}",
        f"Vault: {mode(report['paths']['vault_root'])}; journal {mode(report['paths']['vault_journal'])}",
        f"Service on port {report['command']['port']}: {service['state']}"
        + (f", ready: {'yes' if service.get('ready') else 'no'}" if service["state"] == "running" else ""),
    ]
    lines.append(_render_engine(report.get("engine") or {}))
    if "lint" in service:
        lint = service["lint"]
        lines.append(f"Wiki lint: {lint.get('error')} errors, {lint.get('warning')} warnings, {lint.get('info')} info")
    operations = report["operations"]
    if operations is not None:
        lines.append(f"Operations: {operations['pending']} unfinished, "
                     f"{operations['retryable_failed']} retryable failed, {operations['permanent_failed']} failed")
    tesseract = report["tesseract"]
    lines.append("OCR: " + ("Tesseract not installed" if tesseract["path"] is None else
                            f"{tesseract['version']}, languages {', '.join(tesseract['languages']) or 'none'}"))
    vision = report["vision"]
    lines.append(f"Vision: {vision['status']}" + (f" ({vision.get('error')})" if vision.get("error") else ""))
    cached = ", ".join(f"{role} {'cached' if item['cached'] else 'missing'}" for role, item in models["models"].items())
    record = models.get("last_daemon_start") or {}
    lines.append(f"Models: {cached}; next start offline: {'yes' if models['offline_next_start'] else 'no'}; "
                 f"last start offline: {record.get('hf_hub_offline', 'unknown')}; tokenizer "
                 f"{'cached' if models['tiktoken']['cached'] else 'not cached'}")
    free = report["disk"]["home_free_bytes"]
    if free is not None:
        lines.append(f"Disk free: {free / 1024**3:.1f} GB")
    lines.append("Problems: " + (", ".join(report["problems"]) or "none"))
    lines.append("Warnings: " + (", ".join(report["warnings"]) or "none"))
    return "\n".join(lines)
