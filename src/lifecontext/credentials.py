"""Strict reader for the private LifeContext credential file."""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

from lifecontext.paths import LifeContextPaths


class CredentialError(ValueError):
    """A stable, redacted credential prerequisite failure."""


# The vision key is optional: without it, the LLM key is reused only for the same endpoint.
_ALLOWED_CREDENTIALS = {"LIFEWEAVE_LLM_API_KEY", "LIFEWEAVE_VISION_API_KEY"}
_ENV_NAME = re.compile(r"[A-Z_][A-Z0-9_]*\Z")
_MAX_CREDENTIAL_BYTES = 64 * 1024
_LINE_SEPARATORS = "\r\n\v\f\x1c\x1d\x1e\x85\u2028\u2029"
_NONSTANDARD_LINE_SEPARATORS = "\v\f\x1c\x1d\x1e\x85\u2028\u2029"


def _has_unsafe_line_character(value: str) -> bool:
    return any(
        character in _LINE_SEPARATORS
        or ord(character) < 32
        or 0x7F <= ord(character) <= 0x9F
        for character in value
    )


def _read_private_file(path: Path) -> str:
    try:
        before = path.lstat()
    except FileNotFoundError:
        raise CredentialError("missing credentials") from None
    except OSError:
        raise CredentialError("unsafe credential file") from None
    if not stat.S_ISREG(before.st_mode):
        raise CredentialError("unsafe credential file")

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        raise CredentialError("missing credentials") from None
    except OSError:
        raise CredentialError("unsafe credential file") from None

    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or (info.st_dev, info.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise CredentialError("unsafe credential file")
        if os.name == "posix":
            if stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.geteuid():
                raise CredentialError("unsafe credential file")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            raw = handle.read(_MAX_CREDENTIAL_BYTES + 1)
    except OSError:
        raise CredentialError("unsafe credential file") from None
    finally:
        os.close(descriptor)

    if len(raw) > _MAX_CREDENTIAL_BYTES:
        raise CredentialError("malformed credentials")
    try:
        return raw.decode("utf-8")
    except UnicodeError:
        raise CredentialError("malformed credentials") from None


def _parse_credentials(raw: str, *, require_llm_key: bool = True) -> dict[str, str]:
    if any(character in _NONSTANDARD_LINE_SEPARATORS for character in raw):
        raise CredentialError("malformed credentials")
    credentials: dict[str, str] = {}
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        if not separator or name != name.strip() or not _ENV_NAME.fullmatch(name):
            raise CredentialError("malformed credentials")
        if name in credentials:
            raise CredentialError("duplicate credentials")
        if name not in _ALLOWED_CREDENTIALS:
            raise CredentialError("unsupported credential name")
        value = value.strip()
        if not value:
            raise CredentialError("missing credentials")
        if _has_unsafe_line_character(value):
            raise CredentialError("malformed credentials")
        if value[0] in "\"'":
            if len(value) < 2 or value[-1] != value[0]:
                raise CredentialError("malformed credentials")
            value = value[1:-1]
            if not value or _has_unsafe_line_character(value):
                raise CredentialError("malformed credentials")
        elif value[-1:] in "\"'":
            raise CredentialError("malformed credentials")
        credentials[name] = value

    if require_llm_key and "LIFEWEAVE_LLM_API_KEY" not in credentials:
        raise CredentialError("missing credentials")
    return credentials


def load_credentials(*, require_llm_key: bool = True) -> dict[str, str]:
    """Return the selected provider's key from ``~/.lifeweave/.env`` only.

    ``require_llm_key=False`` accepts a missing file or a file without the LLM key,
    for a loopback endpoint that needs none; an unsafe or malformed file still fails.
    """
    paths = LifeContextPaths.current()
    try:
        paths.ensure_private_home()
    except OSError:
        raise CredentialError("unsafe LifeContext home") from None
    try:
        raw = _read_private_file(paths.credentials_path)
    except CredentialError as exc:
        if require_llm_key or str(exc) != "missing credentials":
            raise
        return {}
    return _parse_credentials(raw, require_llm_key=require_llm_key)
