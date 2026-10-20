"""Model-free daily brief and change log, built from catalog history.

Catalog times are UTC and are shown in the configured top-level ``timezone``, or
in the host's local time (with each date's own offset) when none is set. The
output names documents, pages and counts; quotes appear only for contradictions,
which are the owner's own wiki content. It holds no paths or credentials.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, tzinfo
from typing import Any

MAX_BRIEF_ITEMS = 100
MAX_BRIEF_CONTRADICTIONS = 50
LOG_ENTRIES = 200
# Shown instead of the title of a document the owner asked to forget.
FORGOTTEN = "a forgotten source"
_STAGES = ("custody", "extraction", "indexing", "wiki", "projection")
# Rows read per windowed query; the brief lists the newest of them.
_WINDOW_ROWS = 4096


def _sql_time(value: datetime) -> str:
    """The catalog's CURRENT_TIMESTAMP form, so text comparison orders correctly."""
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


def _local(value: str, zone: tzinfo | None) -> str:
    return datetime.fromisoformat(value).replace(tzinfo=UTC).astimezone(zone).strftime("%Y-%m-%d %H:%M")


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _line(text: str) -> str:
    """One Markdown list line: claims may hold line breaks."""
    return " ".join(text.split())


def _claim_texts(catalog, wiki, page_id: str, revision: int | None) -> set[str] | None:
    if revision is None:
        return set()
    try:
        row = catalog.get_wiki_revision(page_id, revision)
        return {claim.text for claim in wiki.store.read(page_id, revision, row["artifact_sha256"]).claims}
    except (ValueError, TypeError, KeyError, OSError):
        return None


def _change(catalog, wiki, row: Mapping[str, Any], zone: tzinfo | None) -> dict:
    """One wiki revision with claims added and removed against its parent's claim texts."""
    current = _claim_texts(catalog, wiki, row["page_id"], row["revision"])
    previous = _claim_texts(catalog, wiki, row["page_id"], row["parent_revision"])
    known = current is not None and previous is not None
    return {"page_id": row["page_id"], "title": row["title"] or FORGOTTEN, "parent_revision": row["parent_revision"],
            "revision": row["revision"], "claims_added": len(current - previous) if known else None,
            "claims_removed": len(previous - current) if known else None, "at": _local(row["created_at"], zone)}


def _newest(rows: list, limit: int) -> tuple[list, int]:
    """The newest ``limit`` of newest-first rows, oldest first, and how many were left out."""
    return list(reversed(rows[:limit])), max(0, len(rows) - limit)


def build_brief(catalog, wiki, *, since: datetime, until: datetime, timezone: tzinfo | None, timezone_name: str,
                documents: Iterable[Mapping[str, Any]], lint: Mapping[str, Any], day: str | None = None) -> dict:
    """Summarize what the system did in [since, until), and separately what is open now; reads only."""
    window = {"since": _sql_time(since), "until": _sql_time(until), "limit": _WINDOW_ROWS}
    views = {(document["source_id"], document["source_version"]): document for document in documents}
    rows, omitted_documents = _newest(catalog.list_document_imports(**window), MAX_BRIEF_ITEMS)
    imported = []
    for row in rows:
        identity = (row["source_id"], row["source_version"])
        coverage = (views.get(identity) or {}).get("coverage") or {}
        imported.append({
            "source_id": row["source_id"], "source_version": row["source_version"], "title": row["title"] or FORGOTTEN,
            "at": _local(row["accepted_at"], timezone),
            "stages": {name: (catalog.get_document_stage(*identity, name) or {"state": "pending"})["state"] for name in _STAGES},
            "unresolved": len(coverage.get("unresolved") or ()), "partial": len(coverage.get("partial") or ()),
        })
    rows, omitted_changes = _newest(catalog.list_wiki_changes(**window), MAX_BRIEF_ITEMS)
    changes = [_change(catalog, wiki, row, timezone) for row in rows]
    contradictions = []
    for change in changes:
        # Quotes of a forgotten or deleted document version stay out (``WikiService.review_withdrawn``).
        reviews, _ = wiki.find_reviews(page_id=change["page_id"], limit=4096)
        for review in reviews:
            if review["code"] == "contradiction" and review["revision"] == change["revision"]:
                sides = review["details"]["sources"]
                contradictions.append({
                    "review_id": review["review_id"], "page_id": change["page_id"], "title": change["title"],
                    "revision": change["revision"], "state": review["state"],
                    "left": {"text": sides["left"]["text"], "date": sides["left_date"]},
                    "right": {"text": sides["right"]["text"], "date": sides["right_date"]},
                })
    attention = [{"page_id": page["page_id"], "title": page["title"] or FORGOTTEN, "state": page["state"]}
                 for page in catalog.list_wiki_pages_needing_attention()]
    stages = [{"source_id": item["source_id"], "detail": item["detail"], "severity": item["severity"]}
              for item in lint["findings"] if item["code"] in {"stage_failed", "stage_pending"}]
    _, pending = wiki.find_reviews(state="pending", limit=1)
    return {
        "date": day, "since": _iso(since), "until": _iso(until), "timezone": timezone_name,
        "documents": {"items": imported, "omitted": omitted_documents},
        "journal_entries": catalog.count_journal_sources(since=window["since"], until=window["until"]),
        "wiki_changes": {"items": changes, "omitted": omitted_changes},
        "contradictions": {"items": contradictions[:MAX_BRIEF_CONTRADICTIONS],
                           "omitted": max(0, len(contradictions) - MAX_BRIEF_CONTRADICTIONS)},
        # State when the brief is built, not of the day it covers.
        "open_now": {
            "attention": {"items": attention[:MAX_BRIEF_ITEMS], "omitted": max(0, len(attention) - MAX_BRIEF_ITEMS)},
            "stages": {"items": stages[:MAX_BRIEF_ITEMS], "omitted": max(0, len(stages) - MAX_BRIEF_ITEMS)},
            "reviews_pending": pending,
            "lint": {"counts": dict(lint["counts"]), "omitted": lint["omitted"], "documents_omitted": lint["documents_omitted"],
                     "codes": dict(sorted(Counter(item["code"] for item in lint["findings"]).items()))},
        },
    }


def _change_line(change: Mapping[str, Any]) -> str:
    revision = (f"r{change['revision']}" if change["parent_revision"] is None
                else f"r{change['parent_revision']} → r{change['revision']}")
    claims = ("claims unreadable" if change["claims_added"] is None
              else f"+{change['claims_added']} / -{change['claims_removed']} claims")
    return f"{_line(change['title'])} ({change['page_id']}) {revision}: {claims}"


def _section(lines: list[str], heading: str, section: Mapping[str, Any], render, *, level: str = "##") -> None:
    lines.extend(["", f"{level} {heading} ({len(section['items']) + section['omitted']})", ""])
    lines.extend(f"- {render(item)}" for item in section["items"])
    if section["omitted"]:
        lines.append(f"- and {section['omitted']} more")
    if not section["items"]:
        lines.append("None.")


def render_brief(brief: Mapping[str, Any]) -> str:
    """Deterministic Markdown for one brief: the same catalog state gives the same bytes."""
    lines = [f"# Brief {brief['date'] or 'since ' + brief['since']}", "",
             (f"From {brief['since']} to {brief['until']} (UTC); times in {brief['timezone']}. Generated from "
              "the catalog without a model; an edited copy is kept and never overwritten.")]
    _section(lines, "Documents imported", brief["documents"], lambda item: (
        f"{item['at']} {_line(item['title'])} ({item['source_id']} v{item['source_version']}): "
        + ", ".join(f"{name} {state}" for name, state in item["stages"].items())
        + (f"; {item['unresolved']} unresolved" if item["unresolved"] else "")
        + (f"; {item['partial']} partly unread" if item["partial"] else "")))
    lines.extend(["", "## Journal", "", f"{brief['journal_entries']} entries retained."])
    _section(lines, "Wiki changes", brief["wiki_changes"], lambda item: f"{item['at']} {_change_line(item)}")
    _section(lines, "New contradictions", brief["contradictions"], lambda item: (
        f"{_line(item['title'])} ({item['review_id']}, {item['state']}): "
        f"\"{_line(item['left']['text'])}\" ({item['left']['date']}) / "
        f"\"{_line(item['right']['text'])}\" ({item['right']['date']})"))
    now = brief["open_now"]
    lines.extend(["", "## Open now", "", "The state when this brief was generated, not of the day it covers."])
    _section(lines, "Pages needing attention", now["attention"],
             lambda item: f"{_line(item['title'])} ({item['page_id']}): {item['state']}", level="###")
    _section(lines, "Unfinished document stages", now["stages"],
             lambda item: f"{item['source_id']}: {item['detail']}", level="###")
    counts = now["lint"]["counts"]
    lines.extend(["", "### Reviews and lint", "",
                  f"- Pending reviews: {now['reviews_pending']} (lifecontext wiki reviews --json)",
                  f"- Lint: {counts['error']} errors, {counts['warning']} warnings, {counts['info']} info"
                  + "".join(f"; {code} {count}" for code, count in now["lint"]["codes"].items())
                  + " (lifecontext wiki lint --json)"])
    return "\n".join(lines) + "\n"


def render_log(catalog, wiki, *, timezone: tzinfo | None, timezone_name: str) -> str:
    """Newest first: the last 200 wiki revisions and document imports, from immutable history."""
    events = [(row["created_at"], "wiki", row["page_id"], row["revision"],
               f"{_local(row['created_at'], timezone)} · wiki · {_change_line(_change(catalog, wiki, row, timezone))}")
              for row in catalog.list_wiki_changes(limit=LOG_ENTRIES)]
    events.extend((row["accepted_at"], "document", row["source_id"], row["source_version"],
                   (f"{_local(row['accepted_at'], timezone)} · document · {_line(row['title'] or FORGOTTEN)} "
                    f"({row['source_id']} v{row['source_version']}) imported"))
                  for row in catalog.list_document_imports(limit=LOG_ENTRIES))
    events.sort(reverse=True)
    lines = ["# Log", "", (f"Newest first, times in {timezone_name}: the last {LOG_ENTRIES} wiki revisions and "
                           "document imports, generated from the catalog; an edited copy is kept and never overwritten."), ""]
    lines.extend(f"- {event[-1]}" for event in events[:LOG_ENTRIES])
    if not events:
        lines.append("Nothing yet.")
    return "\n".join(lines) + "\n"
