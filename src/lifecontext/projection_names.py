"""Readable projection names; callers are responsible for resolving collisions."""

from __future__ import annotations

import re
import unicodedata

_MAX_BASENAME_BYTES = 180
_UNSAFE = frozenset('/\\:*?"<>|#[]^')


def _sanitize_title(title: str, *, fallback: str) -> str:
    title = unicodedata.normalize("NFC", title)
    title = "".join(character for character in title if character not in _UNSAFE and not unicodedata.category(character).startswith("C"))
    return " ".join(title.split()).strip(" .") or fallback


def readable_basename(title: str, *, fallback: str, suffix: str = "") -> str:
    """Return a safe basename within 180 UTF-8 bytes, including the suffix.

    Callers supply a safe fallback and suffix; neither is sanitized.
    """
    title = _sanitize_title(title, fallback=fallback)
    return _finish_basename(title, fallback=fallback, suffix=suffix)


def _finish_basename(title: str, *, fallback: str, suffix: str) -> str:
    if re.fullmatch(r"(?i:CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[1-9¹²³]|LPT[1-9¹²³])(?:\..*)?", title):
        title = f"{fallback} - {title}"
    budget = _MAX_BASENAME_BYTES - len(suffix.encode("utf-8"))
    if budget < 0:
        raise ValueError("suffix exceeds the 180-byte basename budget")
    title = title.encode("utf-8")[:budget].decode("utf-8", errors="ignore").rstrip(" .")
    return title + suffix
