"""Readable journal names; callers are responsible for resolving collisions.

Every entry is named ``YYYY-MM-DD - Title``. The date and title come from the
source itself; only when it states no date does the caller's fallback apply.

The opening-sentence title is written into the projection, so its rule is part
of what reconciliation recognises: change it together with
``first_line_journal_title`` and its use in ``JournalApplication``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date, datetime, timedelta

from lifecontext.custody import SourceManifest
from lifecontext.projection_names import _finish_basename, _sanitize_title

_DATE_FIELDS = ("date", "entry_date", "journal_date", "occurred_at", "timestamp", "captured_at", "created_at")
_ISO_DATE = re.compile(r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)")
_FALLBACK_TITLE = "Journal entry"
_OPENING_LINES = 3
_MAX_DATE_LINE = 200
_MAX_SENTENCE = 400
_MAX_SENTENCE_TITLE = 100
# Leaves room in the 180-byte basename for the date prefix and a version suffix.
_MAX_SENTENCE_TITLE_BYTES = 150
_MIN_SENTENCE_TITLE = 12
# A date mentioned in the opening sentence counts only this close before the retained
# day, or one day after it: the retained day may be read in another timezone.
_MENTION_WINDOW = timedelta(days=31)
_MENTION_SKEW = timedelta(days=-1)
_TEXT, _ATX, _SETEXT = "text", "atx", "setext"
# A period after these, after any one- or two-letter word, or between a month name and a
# number does not end a sentence.
_ABBREVIATIONS = frozenset({
    "вул", "див", "грн", "год", "тис", "обл", "буд", "тел", "рис", "стр", "руб", "сек", "млн", "млрд",
    "проф", "напр", "стор", "табл", "прим", "акад", "англ", "укр",
    "mrs", "etc", "inc", "fig", "vol", "prof", "approx", "capt", "dept", "chap",
})

# English, Ukrainian and Russian month names: abbreviated, nominative and genitive.
_MONTHS = tuple(
    re.compile(names)
    for names in (
        r"jan(?:uary)?|січ(?:ня|ень)?|янв(?:ар[яь])?",
        r"feb(?:ruary)?|лют(?:ого|ий)?|фев(?:рал[яь])?",
        r"mar(?:ch)?|бер(?:ез(?:ня|ень)?)?|мар(?:та?)?",
        r"apr(?:il)?|квіт(?:ня|ень)?|апр(?:ел[яь])?",
        r"may|трав(?:ня|ень)?|ма[яй]",
        r"june?|черв(?:ня|ень)?|июн[яь]?",
        r"july?|лип(?:ня|ень)?|июл[яь]?",
        r"aug(?:ust)?|серп(?:ня|ень)?|авг(?:уста?)?",
        r"sep(?:t(?:ember)?)?|вер(?:ес(?:ня|ень)?)?|сен(?:т(?:ябр[яь])?)?",
        r"oct(?:ober)?|жовт(?:ня|ень)?|окт(?:ябр[яь])?",
        r"nov(?:ember)?|лист(?:опада?)?|ноя(?:бр[яь])?",
        r"dec(?:ember)?|груд(?:ня|ень)?|дек(?:абр[яь])?",
    )
)
_WRITTEN_DATES = (
    re.compile(r"(?<!\d)(?P<y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})(?!\d)"),
    re.compile(r"(?<![\d.])(?P<d>\d{1,2})\.(?P<m>\d{1,2})\.(?P<y>\d{4})(?!\d|\.\d)"),
    re.compile(r"(?<![\d.])(?P<d>\d{1,2})(?:st|nd|rd|th|-го)?\s+(?P<month>[^\W\d_]+)\.?,?\s+(?P<y>\d{4})(?!\d)"),
    re.compile(r"(?<![^\W\d_])(?P<month>[^\W\d_]+)\.?\s+(?P<d>\d{1,2})(?:st|nd|rd|th)?,?\s+(?P<y>\d{4})(?!\d)"),
)
# Full names come before the abbreviations they start with: the possessive loop below takes the first match.
_WEEKDAY = (
    r"(?:mon(?:day)?|tue(?:s(?:day)?)?|wed(?:nesday)?|thu(?:r(?:s(?:day)?)?)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?"
    r"|понеділок|вівторок|серед[ауи]|сред[аыу]|четверг?|п[’'ʼ]?ятниц[яюіаиу]"
    r"|субот[аиу]|неділ[яюі]|понедельник|вторник|суббот[аыу]|воскресенье|пн|вт|ср|чт|пт|сб|нд|вс)"
)
_DATE_LABEL = r"(?:entry date|journal date|date|дата запису|дата записи|дата)"
# What may surround the date on a line that states nothing but the date. The
# possessive loop keeps a long line from being matched in more than one way.
_DATE_LINE_BEFORE = re.compile(rf"(?:{_DATE_LABEL}\s*:)?\s*(?:{_WEEKDAY}\.?,?)?\s*", re.IGNORECASE)
_DATE_LINE_AFTER = re.compile(
    r"(?>\([^()]*\)|\d{1,2}:\d{2}(?::\d{2})?|" + _WEEKDAY
    + r"|[A-Za-z_]+(?:/[A-Za-z_]+(?:-[A-Za-z_]+)*(?:[+-]?\d{1,2}(?![\d:]))?)+|UTC|GMT|[+-]\d{2}:?\d{2}"
    r"|року|р\.|года|г\.|[\s.,;:\-–—])*+",
    re.IGNORECASE,
)
# Capture headers that describe the entry instead of saying what it is about.
_HEADER_LABEL = re.compile(
    rf"(?:{_DATE_LABEL}|time ?zone|provenance|source"
    r"|часовий пояс|часовой пояс|походження|происхождение|джерело|источник)\s*:",
    re.IGNORECASE,
)
_SENTENCE_BREAK = re.compile(r"(?P<word>(?:[^\W\d_]|['’ʼ])*)[.!?…]+\s+")


def _text(value: str) -> str:
    return " ".join(value.split())


def _content(original_bytes: bytes) -> tuple[list[str], list[str], list[str]]:
    """Return headings, meaningful lines in document order, and what each of those lines is."""
    try:
        lines = original_bytes.decode("utf-8-sig").splitlines()
    except UnicodeDecodeError:
        raise ValueError("journal original bytes must be valid UTF-8") from None
    if lines and lines[0].strip() == "---":
        end = next((index for index in range(1, len(lines)) if lines[index].strip() in ("---", "...")), None)
        if end is not None:
            lines = lines[end + 1:]
    meaningful = []
    kinds = []
    headings = []
    fence = None
    for line in lines:
        stripped = line.strip()
        marker = re.match(r"(`{3,}|~{3,})", stripped)
        if marker:
            if fence is None:
                fence = marker[1]
            elif marker[1][0] == fence[0] and len(marker[1]) >= len(fence):
                fence = None
            continue
        if fence is not None or not stripped:
            continue
        if re.fullmatch(r"(?:=+|-+)", stripped):
            if meaningful:
                headings.append(meaningful[-1])
                if kinds[-1] == _TEXT:
                    kinds[-1] = _SETEXT
            continue
        if re.fullmatch(r"(?:\*\s*){3,}|(?:_\s*){3,}|(?:-\s*){3,}", stripped):
            continue
        heading = re.match(r"^#{1,6}\s+(.+?)\s*#*\s*$", stripped)
        if heading:
            title = _text(heading[1])
            if title:
                headings.append(title)
                meaningful.append(title)
                kinds.append(_ATX)
        elif any(character.isalnum() for character in stripped):
            meaningful.append(_text(stripped))
            kinds.append(_TEXT)
    return headings, meaningful, kinds


def _written_date(line: str) -> tuple[str, int, int] | None:
    """Return the first valid calendar date written in a line and its span."""
    found: tuple[str, int, int] | None = None
    for pattern in _WRITTEN_DATES:
        for match in pattern.finditer(line):
            if found is not None and match.start() >= found[1]:
                break
            month = match.groupdict().get("m")
            if month is None:
                word = match["month"].casefold()
                month = next((number for number, names in enumerate(_MONTHS, 1) if names.fullmatch(word)), None)
                if month is None:
                    continue
            try:
                value = date(int(match["y"]), int(month), int(match["d"])).isoformat()
            except ValueError:
                continue
            found = (value, match.start(), match.end())
            break
    return found


def _is_date_line(line: str) -> bool:
    """True for a line that only states a date, such as ``Entry date: 2026-10-12 (UTC).``"""
    if len(line) > _MAX_DATE_LINE:
        return False
    found = _written_date(line)
    return (
        found is not None
        and _DATE_LINE_BEFORE.fullmatch(line, 0, found[1]) is not None
        and _DATE_LINE_AFTER.fullmatch(line, found[2]) is not None
    )


def _is_header_line(line: str, kind: str) -> bool:
    """True for a line that carries the date, timezone or provenance, not the subject.

    A ``#`` heading is the author's own title unless it only states the date.
    """
    return _is_date_line(line) or (kind != _ATX and _HEADER_LABEL.match(line) is not None)


def _first_sentence(line: str) -> str:
    """Return the first sentence of a line, looking no further than its opening."""
    line = line[:_MAX_SENTENCE]
    for match in _SENTENCE_BREAK.finditer(line):
        word = match["word"].casefold()
        following = line[match.end():match.end() + 1]
        abbreviation = word and (
            len(word) <= 2
            or word in _ABBREVIATIONS
            or following.isdigit() and any(names.fullmatch(word) for names in _MONTHS)
        )
        if (
            match.end("word") >= _MIN_SENTENCE_TITLE
            and (following.isupper() or following.isdigit())
            and not abbreviation
        ):
            return line[:match.end("word") + 1]
    return line


def _sentence_title(line: str) -> str:
    """Return the first sentence of a line, short enough to read in a file list."""
    title = _first_sentence(line)

    def fits(text: str) -> bool:
        return len(text) <= _MAX_SENTENCE_TITLE and len(text.encode("utf-8")) <= _MAX_SENTENCE_TITLE_BYTES

    if not fits(title):
        words = title.split(" ")
        count = next((count for count in range(len(words), 0, -1) if fits(" ".join(words[:count]))), 0)
        title = " ".join(words[:count]) if count else (
            title.encode("utf-8")[:_MAX_SENTENCE_TITLE_BYTES].decode("utf-8", errors="ignore")[:_MAX_SENTENCE_TITLE]
        )
    return title.rstrip(" .…,;:-–—")


def _stated_title(manifest: SourceManifest) -> str | None:
    metadata = manifest.provenance.get("journal_source_metadata")
    for provenance in (manifest.provenance, metadata):
        if isinstance(provenance, Mapping):
            title = provenance.get("title")
            if isinstance(title, str) and _text(title):
                return _text(title)[:256].rstrip()
    return None


def journal_title(manifest: SourceManifest, original_bytes: bytes) -> str:
    """Prefer root then legacy provenance title, then a heading, then the opening sentence.

    Lines that only carry the date, timezone or provenance are not titles. When the
    first heading is such a line, the title is the next heading or sentence after it.
    """
    _, meaningful, kinds = _content(original_bytes)
    title = _stated_title(manifest)
    if title is not None:
        return title
    lines = list(zip(meaningful, kinds, strict=True))
    heading = next((item for item in lines if item[1] != _TEXT), None)
    if heading is not None and not _is_header_line(*heading):
        return heading[0][:256].rstrip()
    for line, kind in lines:
        if not _is_header_line(line, kind):
            return (_sentence_title(line) or _FALLBACK_TITLE) if kind == _TEXT else line[:256].rstrip()
    return _FALLBACK_TITLE


def first_line_journal_title(manifest: SourceManifest, original_bytes: bytes) -> str:
    """Return the title as written before opening sentences were used.

    Reconciliation needs it to recognise projections it wrote under that rule.
    """
    headings, meaningful, _ = _content(original_bytes)
    title = _stated_title(manifest)
    if title is not None:
        return title
    return next(iter(headings or meaningful), _FALLBACK_TITLE)[:256].rstrip()


def _entry_date(
    manifest: SourceManifest, headings: list[str], meaningful: list[str], retained: date | None
) -> str | None:
    metadata = manifest.provenance.get("journal_source_metadata")
    for provenance in (manifest.provenance, metadata):
        if not isinstance(provenance, Mapping):
            continue
        for field in _DATE_FIELDS:
            value = provenance.get(field)
            if not isinstance(value, str):
                continue
            value = value.strip()
            try:
                if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                    return date.fromisoformat(value).isoformat()
                if re.match(r"^\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}", value):
                    return datetime.fromisoformat(value.replace("z", "Z")).date().isoformat()
            except ValueError:
                continue
    opening = meaningful[:_OPENING_LINES]
    for line in opening:
        if _is_date_line(line):
            return _written_date(line)[0]
    for heading in headings:
        for match in _ISO_DATE.finditer(heading):
            try:
                return date.fromisoformat(match[0]).isoformat()
            except ValueError:
                continue
    # A date only mentioned in the opening sentence may be about another day entirely.
    if opening:
        found = _written_date(_first_sentence(opening[0]))
        if found is not None and (
            retained is None or _MENTION_SKEW <= retained - date.fromisoformat(found[0]) <= _MENTION_WINDOW
        ):
            return found[0]
    return None


def journal_basename(manifest: SourceManifest, original_bytes: bytes, *, fallback_date: str | None = None) -> str:
    """Return a safe ``YYYY-MM-DD - Title`` basename (no extension) with a later-version suffix.

    The date is the one the source states: provenance, a date line among its
    opening lines, or a dated heading, kept as the supplied calendar day without
    timezone conversion. ``fallback_date`` (an ISO date, normally the day the
    source was retained) applies only when the source states none; a date merely
    mentioned in the opening sentence counts when it falls in the month before
    that day or on the day after it. The 180-byte budget includes the version suffix and leaves room
    for collision suffixes, ``.md``, and ManagedVault temporary filename overhead.
    """
    title = journal_title(manifest, original_bytes)
    headings, meaningful, _ = _content(original_bytes)
    retained = date.fromisoformat(fallback_date) if fallback_date is not None else None
    entry_date = _entry_date(manifest, headings, meaningful, retained)
    if entry_date is None and retained is not None:
        entry_date = retained.isoformat()
    title = _sanitize_title(title, fallback=_FALLBACK_TITLE)
    if entry_date:
        if title.startswith(entry_date):
            title = title[len(entry_date):].lstrip(" .,;-–—") or _FALLBACK_TITLE
        title = f"{entry_date} - {title}"
    suffix = f" - v{manifest.source_version}" if manifest.source_version > 1 else ""
    return _finish_basename(title, fallback=_FALLBACK_TITLE, suffix=suffix)
