"""``lifecontext init``: idempotent setup of ``~/.lifeweave``, the vault and the model key."""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from lifecontext.config import ConfigError, load_settings, valid_timezone
from lifecontext.credentials import (
    CredentialError,
    _parse_credentials,
    _read_private_file,
)
from lifecontext.hindsight import (
    _PROVIDERS,
    HindsightStartupError,
    _validated_profile_settings,
    llm_key_required,
)
from lifecontext.paths import LifeContextPaths, atomic_write

DEFAULT_PROVIDER = "anthropic"
# Sonnet-class default and the cheaper Haiku-class alternative. The current Claude
# Sonnet 5.5 (and Opus 5.5) reject the forced tool call that Hindsight 0.9.2 uses
# for structured fact extraction, so retain would fail with them.
DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-5"
LOWER_COST_ANTHROPIC_MODEL = "claude-haiku-5-5"
_FORCED_TOOL_REJECTING_MODELS = frozenset({
    "claude-sonnet-5-5", "claude-opus-5-5", "claude-fable-5-1", "claude-mythos-5-1",
})
_API_KEY = re.compile(r"[\x21-\x7e]{1,4096}\Z")
_MAX_KEY_INPUT = 4096 + 2
# Unattended input for install.sh and containers; a flag wins over its variable.
ENVIRONMENT = {"provider": "LIFECONTEXT_PROVIDER", "model": "LIFECONTEXT_MODEL",
               "base_url": "LIFECONTEXT_BASE_URL", "vault": "LIFECONTEXT_VAULT"}
# The key never comes from the environment itself, only from the file this names.
KEY_FILE_VARIABLE = "LIFECONTEXT_API_KEY_FILE"
_LLM_ORDER = ("provider", "model", "base_url", "reasoning_effort")


class InitError(RuntimeError):
    """A stable setup failure that never includes key material."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class InitOptions:
    provider: str | None = None
    model: str | None = None
    base_url: str | None = None
    vault: str | None = None
    timezone: str | None = None
    api_key_stdin: bool = False
    interactive: bool = False
    force: bool = False
    replace_key: bool = False


def detect_timezone(environ: Mapping[str, str] | None = None, etc: Path = Path("/etc")) -> str:
    """The system IANA zone from ``TZ``, ``/etc/timezone`` or the ``/etc/localtime`` link; else UTC."""
    environ = os.environ if environ is None else environ
    candidates = [environ.get("TZ", "").removeprefix(":")]
    try:
        candidates.append((etc / "timezone").read_text(encoding="utf-8").strip())
    except (OSError, UnicodeError):
        pass
    try:
        target = os.readlink(etc / "localtime")
    except OSError:
        target = ""
    if "zoneinfo/" in target:
        candidates.append(target.split("zoneinfo/", 1)[1])
    return next((name for name in candidates if name and valid_timezone(name)), "UTC")


def _valid_key(value: str) -> str:
    if not _API_KEY.fullmatch(value) or any(character in value for character in "\"'\\"):
        raise InitError("invalid_api_key")
    return value


def read_key_from_stream(stream) -> str:
    """One line from ``stream`` (stdin), without its line ending; the key is never echoed."""
    raw = stream.read(_MAX_KEY_INPUT + 1)
    if len(raw) > _MAX_KEY_INPUT:
        raise InitError("invalid_api_key")
    try:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    except UnicodeError:
        raise InitError("invalid_api_key") from None
    lines = text.splitlines()
    if len(lines) != 1:
        raise InitError("invalid_api_key")
    return _valid_key(lines[0].strip())


def read_key_file(path: str) -> tuple[str, bool]:
    """The key from a file such as a container secret: one line, a regular file (links are followed).

    Also says whether group or others may read the file.
    """
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        raise InitError("api_key_file_unreadable") from None
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode):
        os.close(descriptor)
        raise InitError("api_key_file_unreadable")
    with os.fdopen(descriptor, "rb") as stream:
        return read_key_from_stream(stream), bool(info.st_mode & 0o044)


def _directory_state(path: Path) -> str | None:
    """What ``_private_directory`` would do, without changing anything; refuses what it would refuse."""
    if os.path.realpath(path) != os.path.normpath(path):
        # The service opens every vault component without following links.
        raise InitError("vault_path_has_symlink")
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode):
        raise InitError("vault_path_has_symlink")
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        raise InitError("unsafe_vault")
    return "kept" if stat.S_IMODE(info.st_mode) == 0o700 else "tightened"


def _private_directory(path: Path) -> str:
    """Create ``path`` with mode 0700, or tighten an existing directory the user owns."""
    state = _directory_state(path)
    if state is None:
        path.mkdir(mode=0o700, parents=True)
        os.chmod(path, 0o700)
        return "created"
    if state == "tightened":
        os.chmod(path, 0o700)
    return state


def _vault_root(value: str, paths: LifeContextPaths) -> Path:
    if not value or "\x00" in value or "\\" in value:
        raise InitError("invalid_vault")
    expanded = os.path.abspath(os.path.expanduser(value))
    root = Path(os.path.realpath(expanded))
    lifeweave = Path(os.path.realpath(paths.lifeweave_home))
    if root == Path("/") or root.is_relative_to(lifeweave) or lifeweave.is_relative_to(root):
        raise InitError("invalid_vault")
    return root


def render_config(llm: Mapping[str, str], root: Path, journal: Path, timezone: str | None,
                  kept: Mapping[str, Any] | None = None) -> bytes:
    """config.yaml with JSON-quoted scalars, which YAML reads unchanged.

    ``kept`` carries the rest of an existing config (``relationship_roots`` and
    the other top-level blocks such as ``vision`` and ``limits``), written back as is.
    """
    import yaml

    def quoted(value: str) -> str:
        return json.dumps(value, ensure_ascii=False)

    kept = dict(kept or {})
    roots = kept.pop("relationship_roots", None)
    lines = ["# LifeContext settings, written by `lifecontext init`.",
             "# Provider keys belong in .env next to this file, never here.",
             "llm:"]
    lines += [f"  {key}: {quoted(llm[key])}" for key in _LLM_ORDER if llm.get(key)]
    lines += ["vault:", f"  root: {quoted(str(root))}", f"  journal_path: {quoted(str(journal))}"]
    if roots is not None:
        lines += ["  relationship_roots: []"] if not roots else [
            "  relationship_roots:", *(f"    - {quoted(item)}" for item in roots)]
    if timezone:
        lines.append(f"timezone: {quoted(timezone)}")
    text = "\n".join(lines) + "\n"
    if kept:
        text += yaml.safe_dump(kept, sort_keys=False, allow_unicode=True, default_flow_style=False)
    return text.encode("utf-8")


def _existing_credentials(paths: LifeContextPaths) -> dict[str, str] | None:
    try:
        return _parse_credentials(_read_private_file(paths.credentials_path), require_llm_key=False)
    except CredentialError as exc:
        if str(exc) == "missing credentials" and not os.path.lexists(paths.credentials_path):
            return None
        raise InitError("unsafe_credentials" if "unsafe" in str(exc) else "invalid_credentials") from None


def _write_credentials(paths: LifeContextPaths, existing: Mapping[str, str] | None, key: str) -> None:
    values = {**(existing or {}), "LIFEWEAVE_LLM_API_KEY": key}
    body = "".join(f"{name}={values[name]}\n" for name in sorted(values))
    atomic_write(paths.credentials_path, body.encode("ascii"))


def run_init(
    options: InitOptions,
    *,
    ask: Callable[[str, str], str] | None = None,
    ask_secret: Callable[[str], str] | None = None,
    key_stream=None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Set up the current HOME, keeping what exists; never start anything.

    An existing ``config.yaml`` is kept unless ``force``, which applies the given
    values to it and keeps every other setting. A stored key is kept unless
    ``replace_key``. Flags win over the ``LIFECONTEXT_*`` variables in ``environ``.
    ``ask(question, default)`` and ``ask_secret(question)`` are used only when
    ``options.interactive`` is set; ``key_stream`` supplies ``--api-key-stdin``.
    """
    environ = os.environ if environ is None else environ
    paths = LifeContextPaths.current()
    home_state = "kept" if os.path.lexists(paths.lifeweave_home) else "created"
    try:
        paths.ensure_private_home()
    except OSError:
        raise InitError("unsafe_home") from None

    given = {name: getattr(options, name) if getattr(options, name) is not None else environ.get(variable) or None
             for name, variable in ENVIRONMENT.items()}
    given["timezone"] = options.timezone
    # Read a key from stdin or a file first, so a bad one fails before anything is written.
    key = None
    shared_key_file = False
    if options.api_key_stdin:
        if key_stream is None:
            raise InitError("invalid_api_key")
        key = read_key_from_stream(key_stream)
    elif environ.get(KEY_FILE_VARIABLE):
        key, shared_key_file = read_key_file(environ[KEY_FILE_VARIABLE])
    if options.replace_key and key is None and not (options.interactive and ask_secret is not None):
        raise InitError("api_key_required")

    def value(given_value: str | None, question: str, default: str) -> str:
        if given_value is not None or not options.interactive or ask is None:
            return given_value if given_value is not None else default
        return ask(question, default).strip() or default

    report: dict[str, Any] = {"home": str(paths.lifeweave_home), "home_state": home_state, "warnings": []}
    config_exists = os.path.lexists(paths.config_path)
    existing: dict[str, Any] = {}
    if config_exists:
        try:
            existing = load_settings()
        except ConfigError:
            raise InitError("invalid_config") from None
        if "llm" not in existing and not options.force:
            raise InitError("config_incomplete")
    default_root = Path(os.path.realpath(paths.home)) / "LifeContext" / "vault"
    existing_vault = existing.get("vault") or {}
    previous = paths.config_path.read_bytes() if config_exists else None
    if config_exists and not options.force:
        try:
            llm = _validated_profile_settings({"llm": existing["llm"]})
        except HindsightStartupError:
            raise InitError("invalid_llm_settings") from None
        root = Path(existing_vault["root"]) if existing_vault else default_root
        journal = Path(existing_vault["journal_path"]) if existing_vault else root / "Journal"
        timezone = existing.get("timezone")
        current = {"provider": llm["provider"], "model": llm["model"], "base_url": llm.get("base_url"),
                   "vault": str(root), "timezone": timezone}
        ignored = sorted(name for name, wanted in given.items() if wanted is not None and wanted != current[name]
                         and not (name == "vault" and os.path.realpath(os.path.expanduser(wanted)) == str(root)))
        if ignored:
            report["warnings"].append("flags_ignored")
            report["ignored"] = ignored
        report["config_state"] = "kept"
        for directory in (root, journal, journal / "entries"):
            _directory_state(directory)
    else:
        base = dict(existing.get("llm") or {})
        provider = value(given["provider"], "Model provider", base.get("provider") or DEFAULT_PROVIDER)
        if provider not in _PROVIDERS:
            raise InitError("invalid_llm_settings")
        # Same provider: start from its settings. Another one: start from its defaults.
        if provider != base.get("provider"):
            base = {"model": DEFAULT_ANTHROPIC_MODEL if provider == "anthropic" else ""}
        model = value(given["model"], "Model ID" + (
            f" ({LOWER_COST_ANTHROPIC_MODEL} costs less)" if provider == "anthropic" else ""), base.get("model", ""))
        needs_url = provider in {"openai_compatible", "volcano"}
        base_url = value(given["base_url"], "API base URL" + (
            " (required)" if needs_url else " (empty for the default)"), base.get("base_url", ""))
        llm_input = {**base, "provider": provider, "model": model}
        llm_input.pop("base_url", None)
        if base_url:
            llm_input["base_url"] = base_url
        if not model:
            raise InitError("model_required")
        try:
            llm = _validated_profile_settings({"llm": llm_input})
        except HindsightStartupError:
            raise InitError("invalid_llm_settings") from None
        root = _vault_root(value(given["vault"], "Vault folder", existing_vault.get("root") or str(default_root)),
                           paths)
        # A custom journal folder keeps its place inside the vault.
        journal = root / (Path(existing_vault["journal_path"]).relative_to(existing_vault["root"])
                          if existing_vault else Path("Journal"))
        if (config_exists and "timezone" not in existing and given["timezone"] is None
                and not (options.interactive and ask is not None)):
            timezone = None
        else:
            timezone = value(given["timezone"], "Time zone", existing.get("timezone") or detect_timezone(environ))
            if not valid_timezone(timezone):
                raise InitError("invalid_timezone")
        for directory in (root, journal, journal / "entries"):
            _directory_state(directory)
        kept = {name: block for name, block in existing.items() if name not in {"llm", "vault", "timezone"}}
        if "relationship_roots" in existing_vault:
            kept["relationship_roots"] = existing_vault["relationship_roots"]
        atomic_write(paths.config_path, render_config(llm, root, journal, timezone, kept))
        try:
            load_settings()
        except ConfigError:
            _put_back(paths.config_path, previous)
            raise InitError("invalid_config") from None
        report["config_state"] = "updated" if config_exists else "written"
    if llm["provider"] == "anthropic" and llm["model"] in _FORCED_TOOL_REJECTING_MODELS:
        report["warnings"].append("model_rejects_forced_tool_use")

    try:
        vault_state = _private_directory(root)
        journal_state = _private_directory(journal)
        # The service lists entries/ before it writes the first entry.
        _private_directory(journal / "entries")
    except BaseException:
        if report["config_state"] != "kept":
            _put_back(paths.config_path, previous)
        raise
    report.update({
        "provider": llm["provider"], "model": llm["model"], "base_url": llm.get("base_url"),
        "vault_root": str(root), "journal_path": str(journal),
        "vault_state": vault_state if vault_state != "kept" else journal_state,
        "timezone": timezone,
    })

    if shared_key_file:
        report["warnings"].append("api_key_file_readable_by_others")
    required = llm_key_required(llm)
    try:
        credentials = _existing_credentials(paths)
        has_key = bool(credentials and credentials.get("LIFEWEAVE_LLM_API_KEY"))
        if (key is None and options.interactive and ask_secret is not None
                and (not has_key or options.replace_key) and required):
            entered = ask_secret("API key for this provider (input hidden; empty to add it later)").strip()
            key = _valid_key(entered) if entered else None
        if options.replace_key and key is None:
            raise InitError("api_key_required")
    except BaseException:
        # A run that fails on the key leaves config.yaml as it found it.
        if report["config_state"] != "kept":
            _put_back(paths.config_path, previous)
        raise
    if key is not None and (not has_key or options.replace_key):
        _write_credentials(paths, credentials, key)
        report["credentials_state"] = "replaced" if has_key else "stored"
    elif has_key:
        report["credentials_state"] = "kept"
        if key is not None:
            report["warnings"].append("existing_key_kept")
    else:
        report["credentials_state"] = "missing" if required else "not_required"
    report["next_steps"] = _next_steps(report)
    return report


def _put_back(path: Path, previous: bytes | None) -> None:
    """Undo a config write: the earlier bytes, or no file when there was none."""
    try:
        if previous is None:
            path.unlink()
        else:
            atomic_write(path, previous)
    except OSError:
        pass


def _next_steps(report: Mapping[str, Any]) -> list[str]:
    steps = []
    if report["credentials_state"] == "missing":
        steps.append("lifecontext init --api-key-stdin < FILE  (or run lifecontext init in a terminal to type the key)")
    steps += [
        ("lifecontext preflight  (checks the model key and starts the local memory engine; "
         "the first start downloads several hundred MB of models)"),
        "lifecontext service install --maintenance  (or keep lifecontext serve running in a terminal)",
        "lifecontext skills install  (links the agent skill into Claude Code, Codex, Gemini CLI, Hermes)",
        "lifecontext doctor",
    ]
    return steps


def render(report: Mapping[str, Any]) -> str:
    credentials = {
        "stored": "stored in .env", "replaced": "replaced in .env", "kept": "already in .env",
        "missing": "missing", "not_required": "not needed for this loopback endpoint",
    }[report["credentials_state"]]
    lines = [
        "LifeContext is set up." if report["credentials_state"] != "missing" else
        "LifeContext is set up, except for the model key.",
        f"- Settings: {report['home']} (config {report['config_state']})",
        f"- Model: {report['provider']} {report['model']}" + (f" at {report['base_url']}" if report.get("base_url") else ""),
        f"- API key: {credentials}",
        f"- Vault: {report['vault_root']} ({report['vault_state']}), journal in {report['journal_path']}",
        f"- Time zone: {report['timezone'] or 'the service local time'}",
    ]
    if "model_rejects_forced_tool_use" in report["warnings"]:
        lines.append(f"Warning: {report['model']} rejects the forced tool call Hindsight uses to extract facts, "
                     f"so retain will fail; use {DEFAULT_ANTHROPIC_MODEL} or {LOWER_COST_ANTHROPIC_MODEL}.")
    if "existing_key_kept" in report["warnings"]:
        lines.append("Warning: the key you gave was not saved because .env already has one; "
                     "add --replace-key to replace it (that changes only .env).")
    if "api_key_file_readable_by_others" in report["warnings"]:
        lines.append("Warning: other users can read the file in LIFECONTEXT_API_KEY_FILE; chmod 600 it "
                     "unless it is a container secret only this service sees.")
    if "flags_ignored" in report["warnings"]:
        names = ", ".join("--" + name.replace("_", "-") for name in report["ignored"])
        lines.append(f"Warning: config.yaml already exists, so {names} did not change it; add --force to apply "
                     "them (every other setting is kept).")
    lines.append("Next steps:")
    lines += [f"  {number}. {step}" for number, step in enumerate(report["next_steps"], 1)]
    return "\n".join(lines)


def show_llm() -> dict[str, Any]:
    """The configured provider, model and endpoint, and whether a key is stored; never the key."""
    paths = LifeContextPaths.current()
    try:
        settings = load_settings()
    except ConfigError:
        raise InitError("invalid_config") from None
    if "llm" not in settings:
        raise InitError("config_missing")
    try:
        llm = _validated_profile_settings({"llm": settings["llm"]})
    except HindsightStartupError:
        raise InitError("invalid_llm_settings") from None
    credentials = _existing_credentials(paths)
    if credentials and credentials.get("LIFEWEAVE_LLM_API_KEY"):
        key = "set"
    else:
        key = "missing" if llm_key_required(llm) else "not_required"
    return {"provider": llm["provider"], "model": llm["model"], "base_url": llm.get("base_url"), "api_key": key}


def set_llm(options: InitOptions, *, key_stream=None, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Change only the given provider, model and base URL of an initialized HOME, and the key if one is given.

    The key comes from ``key_stream`` (``options.api_key_stdin``) or the file named by
    ``LIFECONTEXT_API_KEY_FILE``. Every other setting stays as it is; the other
    ``LIFECONTEXT_*`` variables are ignored here. Validation is ``run_init``'s.
    """
    environ = os.environ if environ is None else environ
    if not os.path.lexists(LifeContextPaths.current().config_path):
        raise InitError("config_missing")
    before = show_llm()
    key_file = environ.get(KEY_FILE_VARIABLE)
    key_given = options.api_key_stdin or bool(key_file)
    run_init(
        replace(options, vault=None, timezone=None, force=True, interactive=False, replace_key=key_given),
        key_stream=key_stream, environ={KEY_FILE_VARIABLE: key_file} if key_file else {},
    )
    report = {**show_llm(), "api_key_updated": key_given, "warnings": []}
    if report["provider"] != before["provider"] and not key_given and report["api_key"] == "set":
        report["warnings"].append("provider_changed_key_kept")
    if report["provider"] == "anthropic" and report["model"] in _FORCED_TOOL_REJECTING_MODELS:
        report["warnings"].append("model_rejects_forced_tool_use")
    return report


def render_llm(report: Mapping[str, Any]) -> str:
    key = {"set": "set", "missing": "MISSING (this provider needs one)",
           "not_required": "not set (not needed for this loopback endpoint)"}[report["api_key"]]
    lines = ["Model settings updated." if "api_key_updated" in report else "Model settings:",
             f"- Provider: {report['provider']}",
             f"- Model: {report['model']}",
             f"- Base URL: {report['base_url'] or 'the provider default'}",
             f"- API key: {key}"]
    if report["api_key"] == "missing":
        lines.append("Add it with: lifecontext llm set --api-key-stdin < FILE  "
                     "(or run lifecontext llm set with LIFECONTEXT_API_KEY_FILE=FILE)")
    if "provider_changed_key_kept" in report.get("warnings", ()):
        lines.append("Warning: the provider changed but the stored key was kept; if it belongs to the old "
                     "provider, replace it with: lifecontext llm set --api-key-stdin < FILE")
    if "model_rejects_forced_tool_use" in report.get("warnings", ()):
        lines.append(f"Warning: {report['model']} rejects the forced tool call Hindsight uses to extract facts, "
                     f"so retain will fail; use {DEFAULT_ANTHROPIC_MODEL} or {LOWER_COST_ANTHROPIC_MODEL}.")
    return "\n".join(lines)
