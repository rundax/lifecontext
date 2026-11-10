"""Plain-text renderers for the CLI's everyday commands.

Each renderer reads only fields the API returns and treats newer optional fields
(``outcome``, ``problems``, ``last_operation_id``, ``wiki_page_id``,
``vault_path`` on documents; ``title``, ``filename`` on citations) as optional.
A renderer returns ``None`` when the answer does not have the expected shape;
the caller then prints its generic fallback. Output never depends on the
terminal width and carries no colour, so it can be compared in tests.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any

STAGES = ("custody", "extraction", "indexing", "wiki", "projection")
_FAILED_STAGE_STATES = frozenset({"failed", "blocked", "conflict"})
_MARKS = {"completed": "x", "failed": "!", "blocked": "!", "conflict": "!"}
_HEX_ID = re.compile(r"(.*_)?([0-9a-f]{32})\Z")
CELL_WIDTH = 60


def short_id(source_id: object) -> str:
    """``doc_af7084b8`` for ``doc_af7084b8…`` (32 hex digits); other IDs unchanged."""
    text = str(source_id)
    match = _HEX_ID.fullmatch(text)
    return f"{match.group(1) or ''}{match.group(2)[:8]}" if match else text


def _cell(value: object, width: int = CELL_WIDTH) -> str:
    text = " ".join(str("" if value is None else value).split())
    return text if len(text) <= width else text[: width - 3] + "..."


def table(headers: Sequence[str], rows: Sequence[Sequence[object]]) -> list[str]:
    cells = [[_cell(value) for value in row] for row in rows]
    widths = [max([len(header), *(len(row[index]) for row in cells)]) for index, header in enumerate(headers)]
    return ["  ".join(value.ljust(width) for value, width in zip(row, widths, strict=True)).rstrip()
            for row in [list(headers), *cells]]


def _citation(evidence: Mapping[str, Any]) -> str:
    name = evidence.get("title") or evidence.get("filename")
    label = f"{short_id(evidence.get('source_id'))} v{evidence.get('source_version')}"
    return f"{_cell(name, 120)}, {label}" if isinstance(name, str) and name.strip() else label


def _stage_entries(stages: Mapping[str, Any]) -> list[tuple[str, Mapping[str, Any]]]:
    names = [*STAGES, *sorted(name for name in stages if name not in STAGES)]
    return [(name, stages[name]) for name in names if isinstance(stages.get(name), Mapping)]


def _failed_stage(document: Mapping[str, Any]) -> tuple[str, Mapping[str, Any]] | None:
    stages = document.get("stages")
    if not isinstance(stages, Mapping):
        return None
    return next(((name, stage) for name, stage in _stage_entries(stages)
                 if stage.get("state") in _FAILED_STAGE_STATES), None)


def document_state(document: Mapping[str, Any]) -> str:
    outcome = document.get("outcome")
    if isinstance(outcome, str) and outcome:
        return outcome
    stages = document.get("stages")
    if not isinstance(stages, Mapping):
        return "unknown"
    entries = _stage_entries(stages)
    failed = _failed_stage(document)
    if failed:
        return f"{failed[0]} {failed[1].get('state')}"
    waiting = next(((name, stage) for name, stage in entries if stage.get("state") != "completed"), None)
    return f"{waiting[0]} {waiting[1].get('state')}" if waiting else "complete"


def _pages(document: Mapping[str, Any]) -> str:
    coverage = document.get("coverage")
    if isinstance(coverage, Mapping) and isinstance(coverage.get("total"), int):
        return f"{coverage.get('resolved')}/{coverage['total']}"
    return "-"


def _problem(item: object) -> str:
    if isinstance(item, Mapping):
        parts = [str(item[key]) for key in ("code", "message", "detail", "hint") if item.get(key)]
        return _cell(": ".join(parts) or "problem", 200)
    return _cell(item, 200)


def _next_for_failure(document: Mapping[str, Any], operation_id: object) -> str | None:
    failed = _failed_stage(document)
    if failed is None:
        return None
    operation = document.get("last_operation_id") or operation_id
    retry = (f"`lifecontext documents retry {operation}`" if isinstance(operation, str) and operation
             else "`lifecontext documents retry OPERATION_ID` with the operation ID the import printed")
    if failed[1].get("error_code") == "provider_unavailable":
        return f"check the model with `lifecontext doctor --model-probe`, then run {retry}"
    return f"run {retry}"


def document_lines(document: Mapping[str, Any], *, operation_id: object = None) -> list[str]:
    from lifecontext.cli import _coverage_line

    title = document.get("title") or document.get("filename") or "Untitled document"
    lines = [f"{_cell(title, 200)} ({document.get('source_id')} v{document.get('source_version')})"]
    if document.get("filename"):
        mime = f" ({document['mime_type']})" if document.get("mime_type") else ""
        lines.append(f"File: {_cell(document['filename'], 255)}{mime}")
    if document.get("category_id"):
        lines.append(f"Category: {document['category_id']}")
    if isinstance(document.get("outcome"), str):
        lines.append(f"Outcome: {document['outcome']}")
    stages = document.get("stages")
    if isinstance(stages, Mapping):
        lines.append("Stages:")
        for name, stage in _stage_entries(stages):
            state = str(stage.get("state"))
            detail = state + (f" ({stage['error_code']})" if stage.get("error_code") else "")
            attempt = stage.get("attempt")
            if isinstance(attempt, int) and not isinstance(attempt, bool) and attempt > 1:
                detail += f", attempt {attempt}"
            lines.append(f"  [{_MARKS.get(state, ' ')}] {name:<10}  {detail}")
    if isinstance(document.get("coverage"), Mapping):
        lines.append(_coverage_line(document["coverage"]))
    warnings = document.get("extraction_warnings")
    if isinstance(warnings, list) and warnings:
        lines.append(f"Extraction warnings: {len(warnings)} (listed with --json)")
    page = document.get("wiki_page_id")
    if isinstance(page, str) and page:
        vault_path = document.get("vault_path")
        lines.append(f"Wiki page: {page}" + (f" ({vault_path})" if isinstance(vault_path, str) and vault_path else ""))
    problems = document.get("problems")
    if isinstance(problems, list) and problems:
        lines.append("Problems:")
        lines.extend(f"- {_problem(item)}" for item in problems)
    following = _next_for_failure(document, operation_id)
    if following:
        lines.append(f"Next: {following}.")
    elif document_state(document) not in {"complete", "completed"} and not isinstance(document.get("outcome"), str):
        lines.append(f"Next: check again with `lifecontext documents show {document.get('source_id')}`.")
    return lines


def render_document_show(value: Mapping[str, Any]) -> str | None:
    if not isinstance(value.get("source_id"), str) or not isinstance(value.get("stages"), Mapping):
        return None
    return "\n".join(document_lines(value))


def render_document_operation(value: Mapping[str, Any]) -> str | None:
    """The import and retry result: the operation, then the document it produced."""
    document = value.get("document")
    if not isinstance(value.get("operation_id"), str) or not isinstance(document, Mapping):
        return None
    lines = [f"Durable operation ID: {value['operation_id']}", f"State: {value.get('state')}"]
    summary = value.get("summary")
    if isinstance(summary, Mapping) and summary.get("error_code"):
        lines.append(f"Error: {summary['error_code']}")
    lines.extend(document_lines(document, operation_id=value["operation_id"]))
    if value.get("state") == "retryable_failed" and not _failed_stage(document):
        lines.append(f"Next: run `lifecontext documents retry {value['operation_id']}`.")
    return "\n".join(lines)


def render_document_list(value: Mapping[str, Any]) -> str | None:
    documents = value.get("documents")
    if not isinstance(documents, list) or not all(isinstance(item, Mapping) for item in documents):
        return None
    if not documents:
        return "No documents yet. Import one with `lifecontext documents import FILE`."
    rows = [(item.get("source_id"), item.get("source_version"), item.get("title"), item.get("category_id"),
             _pages(item), document_state(item)) for item in documents]
    lines = table(("SOURCE ID", "V", "TITLE", "CATEGORY", "PAGES", "STATE"), rows)
    lines.append(f"{len(documents)} document{'s' if len(documents) != 1 else ''}.")
    if any(_failed_stage(item) for item in documents):
        lines.append("Next: `lifecontext documents show SOURCE_ID` says what failed and how to retry it.")
    return "\n".join(lines)


def render_wiki_list(value: Mapping[str, Any]) -> str | None:
    pages = value.get("pages")
    if not isinstance(pages, list) or not all(isinstance(item, Mapping) for item in pages):
        return None
    lines = (table(("PAGE ID", "TITLE", "STATE", "REVISION"),
                   [(item.get("page_id"), item.get("title"), item.get("state"), item.get("current_revision"))
                    for item in pages]) if pages else ["No wiki pages yet."])
    for key, label in (("semantic_support_verified", "Semantic support verified"),
                       ("summary_model_generated", "Summary written by the model")):
        if isinstance(value.get(key), bool):
            lines.append(f"{label}: {'yes' if value[key] else 'no'}")
    if any(item.get("state") in {"stale", "conflict"} for item in pages):
        lines.append("Next: `lifecontext wiki lint` says what to do about stale or conflicting pages.")
    return "\n".join(lines)


def render_wiki_show(value: Mapping[str, Any]) -> str | None:
    managed = value.get("managed")
    return managed.rstrip("\n") if isinstance(managed, str) else None


def render_categories(value: Mapping[str, Any]) -> str | None:
    categories = value.get("categories")
    if not isinstance(categories, list) or not all(isinstance(item, Mapping) for item in categories):
        return None
    if not categories:
        return "No categories yet."
    return "\n".join(table(("CATEGORY ID", "NAME", "REVISION", "ALIASES"), [
        (item.get("category_id"), item.get("name"), item.get("revision"),
         ", ".join(map(str, item.get("aliases") or [])) or "-") for item in categories]))


def render_status(value: Mapping[str, Any]) -> str | None:
    dependencies = value.get("dependencies")
    if not isinstance(value.get("ready"), bool) or not isinstance(dependencies, Mapping):
        return None
    lines = [f"Ready: {'yes' if value['ready'] else 'no'}"]
    order = [name for name in ("catalog", "hindsight", "vault") if name in dependencies]
    order += sorted(name for name in dependencies if name not in order)
    for name in order:
        entry = dependencies[name]
        if not isinstance(entry, Mapping):
            continue
        line = f"- {name}: {'ready' if entry.get('ready') else 'not ready'}"
        if entry.get("error"):
            line += f" ({entry['error']})"
        capabilities = entry.get("capabilities")
        if isinstance(capabilities, Mapping):
            missing = sorted(key for key, enabled in capabilities.items() if not enabled)
            if missing:
                line += "; without " + ", ".join(missing)
        if entry.get("permissions") is False:
            line += "; permissions too open"
        lines.append(line)
    reconciliation = value.get("reconciliation")
    if isinstance(reconciliation, Mapping) and isinstance(reconciliation.get("fact_count"), int):
        facts = reconciliation.get("facts") if isinstance(reconciliation.get("facts"), Mapping) else {}
        found = ", ".join(f"{count} {name.replace('_', ' ')}" for name, count in sorted(facts.items()) if count)
        if reconciliation["fact_count"]:
            lines.append(f"Reconciliation: {reconciliation['fact_count']} to repair ({found or 'see --json'})")
        else:
            lines.append("Reconciliation: nothing to repair")
    maximum = value.get("max_file_bytes")
    if isinstance(maximum, int) and not isinstance(maximum, bool):
        lines.append(f"Largest accepted file: {maximum // (1024 * 1024)} MiB")
    if not value["ready"]:
        lines.append("Next: `lifecontext doctor` says what is wrong and how to fix it.")
    elif isinstance(reconciliation, Mapping) and reconciliation.get("fact_count"):
        lines.append("Next: `lifecontext reconcile` repairs the vault copies.")
    return "\n".join(lines)


def render_brief_result(value: Mapping[str, Any]) -> str | None:
    from lifecontext.brief import render_brief

    try:
        text = render_brief(value).rstrip("\n")
    except (KeyError, TypeError, AttributeError, IndexError):
        return None
    projection = value.get("projection")
    if isinstance(projection, Mapping):
        text += "\n\nWritten to the vault: " + ", ".join(f"{name} {state}" for name, state in sorted(projection.items()))
    return text


def render_recall(value: Mapping[str, Any]) -> str | None:
    results = value.get("results")
    if not isinstance(results, list) or not all(isinstance(item, Mapping) for item in results):
        return None
    lines = []
    for item in results:
        evidence = item.get("evidence")
        source = f" ({_citation(evidence)})" if isinstance(evidence, Mapping) else ""
        text = str(item.get("text", "")).strip().replace("\n", "\n  ")
        lines.append(f"- {text}{source}")
    if not results:
        lines.append("Nothing found.")
    omitted = value.get("omitted_unresolvable")
    if isinstance(omitted, int) and not isinstance(omitted, bool) and omitted:
        lines.append(f"{omitted} result{'s' if omitted != 1 else ''} without a source left out.")
    if value.get("truncated") is True:
        lines.append("More matched than fit; raise --max-results or --max-tokens to see more.")
    return "\n".join(lines)


def render_reflect(value: Mapping[str, Any]) -> str | None:
    text, evidence = value.get("text"), value.get("evidence")
    if not isinstance(text, str) or not isinstance(evidence, list):
        return None
    lines = [text.strip(), "", "Sources:"]
    lines.extend(f"- {_citation(item)}" for item in evidence if isinstance(item, Mapping))
    return "\n".join(lines)


_DOCTOR = "`lifecontext doctor` checks the service, the engine and the model"
_SAME_VERSION = "`lifecontext doctor` checks that this command and the service are the same version"
_MODEL = "check the model with `lifecontext doctor --model-probe`, then re-run the command"


class ErrorText:
    """What one failure code means, what to do next, and whether the same call may work later."""

    __slots__ = ("message", "next_step", "retryable")

    def __init__(self, message: str, next_step: str | None, retryable: bool = False):
        self.message = message
        self.next_step = next_step
        self.retryable = retryable


# Every code the CLI prints as {"error": ...}: the API's codes, the CLI's own and the MCP server's.
# docs/operations.md "Failure codes" is generated from this table. {port}, {tool} and {start} are filled in.
ERRORS: dict[str, ErrorText] = {
    # The API's codes (cli._API_ERROR_CODES).
    "invalid_request": ErrorText("The request was refused: an argument or the input is not valid.",
                                 "check the arguments with `lifecontext COMMAND --help`"),
    "unauthorized": ErrorText("The service refused the API token.",
                              "run the command as the same user, with the same HOME, as the service"),
    "body_too_large": ErrorText("The input or the answer is larger than the configured limit.",
                                "send a smaller input; for recall and reflect lower --max-results or --max-tokens"),
    "response_too_large": ErrorText("The service's answer is larger than this command accepts.",
                                    "ask for less: lower --limit, --max-results or --max-tokens"),
    "host_not_allowed": ErrorText("The service accepts requests only for 127.0.0.1.",
                                  "connect to 127.0.0.1 directly, not through a proxy or another host name"),
    "origin_not_allowed": ErrorText("The service refuses requests from a web page.",
                                    "call it from the CLI or an agent, not from a browser"),
    "source_not_found": ErrorText("No source or page has that ID.",
                                  "`lifecontext documents list` and `lifecontext wiki list` show the IDs; "
                                  "a document's wiki page is `source_` followed by its ID"),
    "source_conflict": ErrorText("That source ID and version already hold different content.",
                                 "use the next version number, or look at the stored one with "
                                 "`lifecontext documents show ID`"),
    "evidence_unavailable": ErrorText("The stored original of that source cannot be read or no longer matches "
                                      "its checksum.", "run `lifecontext reconcile`, then " + _DOCTOR, True),
    "invalid_source_text": ErrorText("The input must be UTF-8 text.",
                                     "convert it to UTF-8, or import the file with `lifecontext documents import`"),
    "invalid_provider_response": ErrorText("The model answered, but not in the expected form.", _MODEL, True),
    "provider_unavailable": ErrorText("The model did not answer.", _MODEL, True),
    "retain_failed": ErrorText("The memory engine did not store the text.",
                               "re-run with the same --idempotency-key; " + _DOCTOR, True),
    "recall_failed": ErrorText("The memory engine could not search.",
                               "re-run in a minute; " + _DOCTOR, True),
    "reflect_failed": ErrorText("The memory engine could not write an answer.",
                                "re-run in a minute, or use `lifecontext recall`; " + _DOCTOR, True),
    "unattributed_evidence": ErrorText("The answer could not be tied to a stored source, so it was withheld.",
                                       "rephrase the question, or read the passages with `lifecontext recall`"),
    "result_limit_exceeded": ErrorText("More results were asked for than the configured maximum.",
                                       "lower --max-results"),
    "operation_status_unavailable": ErrorText("The state of that operation cannot be read now.",
                                              "check the operation ID and try again; " + _DOCTOR, True),
    "curation_unavailable": ErrorText("That change to the source cannot be made now.",
                                      "look at the current version with `lifecontext documents show ID`, "
                                      "then re-run", True),
    "transfer_unavailable": ErrorText("The export or import bundle cannot be processed.",
                                      "check the bundle file; " + _DOCTOR, True),
    "conflict": ErrorText("It changed first: the revision or version you gave is no longer the current one.",
                          "read the current revision with the matching list or show command and re-run with it"),
    "relationship_unavailable": ErrorText("That relationship cannot be read or decided now.",
                                          "see the current edges with `lifecontext relationships list`, "
                                          "then re-run", True),
    "unsafe_path": ErrorText("The service refused a path outside the vault or through a symbolic link.",
                             "`lifecontext doctor` names the path to fix"),
    "unsafe_permissions": ErrorText("Files or folders of LifeContext are open to other users.",
                                    "`lifecontext doctor` names them; make folders 0700 and files 0600"),
    "vault_unavailable": ErrorText("The vault cannot be read or written.",
                                   "check that the vault folder exists and belongs to you; " + _DOCTOR, True),
    "catalog_busy": ErrorText("The catalog is busy with another change.", "wait a few seconds and re-run", True),
    "internal_error": ErrorText("The service failed unexpectedly.",
                                "re-run once; if it fails again, " + _DOCTOR + " and the service log has the "
                                "request ID", True),
    # The CLI's own codes.
    "api_unavailable": ErrorText("The LifeContext service is not reachable at 127.0.0.1:{port}.", "{start}", True),
    "api_timeout": ErrorText("The LifeContext service did not answer in time; it may still be working on the "
                             "request.", "wait, then check with `lifecontext status` and the matching list or "
                             "show command before re-running: running it again may repeat the work", True),
    "api_error": ErrorText("The service answered with an error this command does not know.", _SAME_VERSION),
    "invalid_api_response": ErrorText("The service's answer is not what this command expects.", _SAME_VERSION),
    "token_unavailable": ErrorText("No LifeContext API token for this user: the service writes it when it starts, "
                                   "under the same HOME.", "{start}", True),
    "input_unavailable": ErrorText("Unable to read the selected source input.",
                                   "give an existing regular file, or - for standard input"),
    "vault_missing": ErrorText("The vault's Journal folder does not exist.",
                               "create it with mode 0700, or set `vault` in ~/.lifeweave/config.yaml"),
    "destination_exists": ErrorText("The output file already exists; LifeContext never overwrites one.",
                                    "choose a new file name"),
    "output_unavailable": ErrorText("The output file cannot be written.",
                                    "check that its folder exists and that you can write to it"),
    "invalid_config": ErrorText("The config file ~/.lifeweave/config.yaml cannot be read.",
                                "`lifecontext doctor` says what is wrong"),
    "server_start_failed": ErrorText("The service could not start.",
                                     "`lifecontext preflight` and `lifecontext doctor` say why"),
    "nothing_to_change": ErrorText("Nothing to change.", "give --provider, --model, --base-url or --api-key-stdin; "
                                   "`lifecontext llm show` shows the current model"),
    "setup_failed": ErrorText("The setup files could not be written.",
                              "check that ~/.lifeweave belongs to you and is writable, then re-run"),
    "backup_failed": ErrorText("The backup could not be written.", "check the destination folder, then re-run"),
    "restore_failed": ErrorText("The restore could not read or write its files.",
                                "check the archive and the LifeContext home, then re-run"),
    "hindsight_failed": ErrorText("The memory engine could not be stopped.", _DOCTOR),
    # The MCP server's own codes.
    "busy": ErrorText("A previous {tool} call is still finishing.", "report it instead of retrying", True),
    "path_not_allowed": ErrorText("The file is outside the import directories the owner allowed, or in a "
                                  "protected place (the LifeContext home, the vault, a hidden directory in the "
                                  "home directory, or a system directory).", "ask the owner to import it"),
}


def describe(code: str, *, port: int, start: str | None = None, tool: str = "") -> tuple[str, str | None, bool]:
    """(message, next step, retryable) for a code; unknown codes get a plain sentence and no next step."""
    entry = ERRORS.get(code)
    if entry is None:
        return f"LifeContext request failed: {code.replace('_', ' ')}.", None, False
    start = start or f"start it with `lifecontext serve --port {port}`"
    fields = {"port": port, "tool": tool or "LifeContext", "start": start}
    next_step = entry.next_step.format(**fields) if entry.next_step else None
    return entry.message.format(**fields), next_step, entry.retryable


def start_hint(port: int, *, service_installed: bool) -> str:
    """Where to look when the service is not reachable: the installed unit, otherwise `serve`."""
    if service_installed:
        return ("check it with `lifecontext service status`: it may still be starting, or recovering "
                "after a restart")
    return f"start it with `lifecontext serve --port {port}`"


def error_json(code: str, *, port: int, start: str | None = None, retryable: bool | None = None,
               request_id: str | None = None, message_code: str | None = None) -> dict[str, object]:
    """``--json`` failure: ``error`` is the code as before; the other keys explain it."""
    message, next_step, default_retryable = describe(message_code or code, port=port, start=start)
    return {"error": code, "message": message, "next_step": next_step,
            "retryable": default_retryable if retryable is None else retryable, "request_id": request_id}


def error_text(code: str, *, port: int, start: str | None = None, message_code: str | None = None) -> str:
    """Plain failure: the sentence, then a "Next:" line when there is something to do."""
    message, next_step, _ = describe(message_code or code, port=port, start=start)
    return message + (f"\nNext: {next_step}." if next_step else "")


RENDERERS: dict[str, Callable[[Mapping[str, Any]], str | None]] = {
    "status": render_status,
    "brief": render_brief_result,
    "recall": render_recall,
    "reflect": render_reflect,
    "documents import": render_document_operation,
    "documents retry": render_document_operation,
    "documents list": render_document_list,
    "documents show": render_document_show,
    "wiki list": render_wiki_list,
    "wiki status": render_wiki_list,
    "wiki show": render_wiki_show,
    "categories list": render_categories,
}


def render(command: str, value: Mapping[str, Any]) -> str | None:
    """The plain text for one command's answer, or ``None`` for the generic fallback."""
    renderer = RENDERERS.get(command)
    return renderer(value) if renderer else None
