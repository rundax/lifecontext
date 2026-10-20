"""Safe loading and structural validation for LifeContext settings."""

from __future__ import annotations

import importlib.metadata
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from lifecontext.paths import LifeContextPaths


class ConfigError(ValueError):
    """A stable, redacted configuration prerequisite failure."""


_SECRET_KEY_WORDS = {
    "auth",
    "authorization",
    "credential",
    "credentials",
    "key",
    "password",
    "secret",
    "token",
}
_KEY_SEPARATOR = re.compile(r"[^a-z0-9]+")
_MAX_CONFIG_BYTES = 64 * 1024
ROOT_SETTINGS = frozenset({"llm", "limits", "vault", "timezone", "vision"})
_LLM_SETTINGS = {"provider", "model", "base_url", "reasoning_effort"}
_VISION_SETTINGS = {"provider", "model", "base_url", "max_pages_per_document"}
_VAULT_SETTINGS = {"root", "journal_path", "relationship_roots"}
_MAX_RELATIONSHIP_ROOTS = 128

# These are V1-owned operational ceilings, independent of Hermes config.
LIMIT_CEILINGS = {
    "category_budget": 100,
    "max_text_bytes": 1_048_576,
    "max_file_bytes": 67_108_864,
    "max_query_bytes": 65_536,
    "max_result_count": 100,
    "max_response_bytes": 4_194_304,
    "max_relationship_files": 20_000,
    "max_relationship_file_bytes": 16_777_216,
    "max_relationship_bytes": 67_108_864,
    "max_relationship_scan_ms": 15_000,
}
_LIMIT_DEFAULTS = {
    "category_budget": 10,
    "max_text_bytes": 1_048_576,
    "max_file_bytes": 16_777_216,
    "max_query_bytes": 8_192,
    "max_result_count": 20,
    "max_response_bytes": 1_048_576,
    "max_relationship_files": 2_000,
    "max_relationship_file_bytes": 1_048_576,
    "max_relationship_bytes": 16_777_216,
    "max_relationship_scan_ms": 2_000,
}


@dataclass(frozen=True)
class JournalLimits:
    """Bounded source and request limits accepted by the V1 config."""

    category_budget: int = _LIMIT_DEFAULTS["category_budget"]
    max_text_bytes: int = _LIMIT_DEFAULTS["max_text_bytes"]
    max_file_bytes: int = _LIMIT_DEFAULTS["max_file_bytes"]
    max_query_bytes: int = _LIMIT_DEFAULTS["max_query_bytes"]
    max_result_count: int = _LIMIT_DEFAULTS["max_result_count"]
    max_response_bytes: int = _LIMIT_DEFAULTS["max_response_bytes"]
    max_relationship_files: int = _LIMIT_DEFAULTS["max_relationship_files"]
    max_relationship_file_bytes: int = _LIMIT_DEFAULTS["max_relationship_file_bytes"]
    max_relationship_bytes: int = _LIMIT_DEFAULTS["max_relationship_bytes"]
    max_relationship_scan_ms: int = _LIMIT_DEFAULTS["max_relationship_scan_ms"]

    @classmethod
    def from_config(cls, settings: Mapping[str, Any]) -> "JournalLimits":
        configured = settings.get("limits", {})
        if not isinstance(configured, Mapping):
            raise ConfigError("invalid LifeContext config: limits must be a mapping")
        if any(not isinstance(key, str) or key not in LIMIT_CEILINGS for key in configured):
            raise ConfigError("invalid LifeContext config: unknown limit")

        values: dict[str, int] = {}
        for name, ceiling in LIMIT_CEILINGS.items():
            value = configured.get(name, _LIMIT_DEFAULTS[name])
            if isinstance(value, bool) or not isinstance(value, int):
                raise ConfigError("invalid LifeContext config: limit must be an integer")
            if not 1 <= value <= ceiling:
                raise ConfigError("invalid LifeContext config: limit out of range")
            values[name] = value
        return cls(**values)


def valid_timezone(zone: Any) -> bool:
    """An IANA name such as ``Europe/Vienna`` that the system time zone database knows."""
    try:
        if not isinstance(zone, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_+-]*(?:/[A-Za-z0-9_+-]+)*", zone):
            return False
        ZoneInfo(zone)
    except (KeyError, OSError, ValueError):
        return False
    return True


def _validate_mapping(value: Any, *, seen: set[int] | None = None) -> None:
    if seen is None:
        seen = set()
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in seen:
            return
        seen.add(identity)
        for key, child in value.items():
            if not isinstance(key, str):
                raise ConfigError("invalid LifeContext config: mapping keys must be string keys")
            words = set(_KEY_SEPARATOR.sub(" ", key.casefold()).split())
            if words & _SECRET_KEY_WORDS:
                raise ConfigError("invalid LifeContext config: credential-shaped setting")
            _validate_mapping(child, seen=seen)
    elif isinstance(value, (list, tuple)):
        identity = id(value)
        if identity in seen:
            return
        seen.add(identity)
        for child in value:
            _validate_mapping(child, seen=seen)


def _read_config(path: Path) -> str | None:
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        raise ConfigError("invalid LifeContext config: unreadable config file") from None
    if not stat.S_ISREG(before.st_mode):
        raise ConfigError("invalid LifeContext config: unsafe config file")
    if os.name == "posix" and (
        before.st_uid != os.geteuid() or stat.S_IMODE(before.st_mode) != 0o600
    ):
        raise ConfigError("invalid LifeContext config: unsafe config file")

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise ConfigError("invalid LifeContext config: unsafe config file") from None
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise ConfigError("invalid LifeContext config: unsafe config file")
        if os.name == "posix" and (
            opened.st_uid != os.geteuid() or stat.S_IMODE(opened.st_mode) != 0o600
        ):
            raise ConfigError("invalid LifeContext config: unsafe config file")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            raw = handle.read(_MAX_CONFIG_BYTES + 1)
    except OSError:
        raise ConfigError("invalid LifeContext config: unreadable config file") from None
    finally:
        os.close(descriptor)
    if len(raw) > _MAX_CONFIG_BYTES:
        raise ConfigError("invalid LifeContext config: config too large")
    try:
        return raw.decode("utf-8")
    except UnicodeError:
        raise ConfigError("invalid LifeContext config: unreadable config file") from None


def load_settings() -> dict[str, Any]:
    """Load behavioral settings from the current process HOME's config only."""
    paths = LifeContextPaths.current()
    try:
        paths.ensure_private_home()
    except OSError:
        raise ConfigError("invalid LifeContext config: unsafe LifeContext home") from None

    try:
        version = importlib.metadata.version("PyYAML")
    except importlib.metadata.PackageNotFoundError:
        raise ConfigError("invalid LifeContext config: PyYAML 6.0.3 is required") from None
    if version != "6.0.3":
        raise ConfigError("invalid LifeContext config: PyYAML 6.0.3 is required")

    raw = _read_config(paths.config_path)
    if raw is None:
        return {}

    try:
        import yaml

        loaded = yaml.safe_load(raw)
    except Exception as exc:
        # PyYAML errors can include source lines, which may contain secrets.
        if exc.__class__.__module__.startswith("yaml"):
            raise ConfigError("invalid LifeContext config: yaml") from None
        raise ConfigError("invalid LifeContext config: yaml") from None

    if loaded is None:
        return {}
    if not isinstance(loaded, Mapping):
        raise ConfigError("invalid LifeContext config: root must be a mapping")
    if any(not isinstance(key, str) for key in loaded):
        raise ConfigError("invalid LifeContext config: root mapping keys must be string keys")
    _validate_mapping(loaded)
    if any(key not in ROOT_SETTINGS for key in loaded):
        raise ConfigError("invalid LifeContext config: unknown setting")
    if "llm" in loaded and not isinstance(loaded["llm"], Mapping):
        raise ConfigError("invalid LifeContext config: llm must be a mapping")
    if "llm" in loaded:
        llm = loaded["llm"]
        if any(not isinstance(key, str) or key not in _LLM_SETTINGS for key in llm):
            raise ConfigError("invalid LifeContext config: unknown llm setting")
        if any(not isinstance(value, str) for value in llm.values()):
            raise ConfigError("invalid LifeContext config: invalid llm setting")
    if "vision" in loaded:
        # Semantic checks (provider, exact model ID, URL rules) run at service start.
        vision = loaded["vision"]
        if not isinstance(vision, Mapping):
            raise ConfigError("invalid LifeContext config: vision must be a mapping")
        if any(not isinstance(key, str) or key not in _VISION_SETTINGS for key in vision):
            raise ConfigError("invalid LifeContext config: unknown vision setting")
        if any(
            not isinstance(value, int) or isinstance(value, bool)
            if key == "max_pages_per_document"
            else not isinstance(value, str)
            for key, value in vision.items()
        ):
            raise ConfigError("invalid LifeContext config: invalid vision setting")

    if "timezone" in loaded and not valid_timezone(loaded["timezone"]):
        raise ConfigError("invalid LifeContext config: invalid timezone")

    if "vault" in loaded:
        vault = loaded["vault"]
        if not isinstance(vault, Mapping):
            raise ConfigError("invalid LifeContext config: vault must be a mapping")
        if (
            not {"root", "journal_path"} <= set(vault)
            or any(not isinstance(key, str) or key not in _VAULT_SETTINGS for key in vault)
        ):
            raise ConfigError("invalid LifeContext config: invalid vault settings")
        values = []
        for key in ("root", "journal_path"):
            value = vault[key]
            if (
                not isinstance(value, str)
                or not value
                or "\x00" in value
                or not Path(value).is_absolute()
                or ".." in value.split("/")
            ):
                raise ConfigError("invalid LifeContext config: invalid absolute path")
            values.append(Path(os.path.normpath(value)))
        root, journal = values
        if journal == root or not journal.is_relative_to(root):
            raise ConfigError("invalid LifeContext config: invalid absolute path")
        allowlist = vault.get("relationship_roots", [])
        if not isinstance(allowlist, list):
            raise ConfigError("invalid LifeContext config: invalid relationship roots")
        if len(allowlist) > _MAX_RELATIONSHIP_ROOTS:
            raise ConfigError("invalid LifeContext config: too many relationship roots")
        normalized_roots: list[str] = []
        for relative in allowlist:
            if (
                not isinstance(relative, str)
                or not relative
                or "\x00" in relative
                or "\\" in relative
                or relative.startswith("/")
                or any(
                    ord(char) < 32
                    or 0x7F <= ord(char) <= 0x9F
                    or 0xD800 <= ord(char) <= 0xDFFF
                    for char in relative
                )
            ):
                raise ConfigError("invalid LifeContext config: invalid relationship root")
            components = relative.split("/")
            if (
                any(part in {"", ".", ".."} for part in components)
                or any(part.startswith(".") for part in components)
                or components[0].casefold() == "journal"
            ):
                raise ConfigError("invalid LifeContext config: invalid relationship root")
            normalized_roots.append("/".join(components))
        if len(set(normalized_roots)) != len(normalized_roots):
            raise ConfigError("invalid LifeContext config: duplicate relationship root")
        for left in normalized_roots:
            for right in normalized_roots:
                if left != right and right.startswith(left + "/"):
                    raise ConfigError("invalid LifeContext config: overlapping relationship roots")
        if "relationship_roots" in vault:
            vault["relationship_roots"] = sorted(normalized_roots)

    JournalLimits.from_config(loaded)
    return dict(loaded)
