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
    """``wiki: wiki_provider_failed`` for one of the API's ``{stage, code, reason, next_step}`` problems."""
    if isinstance(item, Mapping):
        code, reason = item.get("code"), item.get("reason")
        return _cell(f"{item.get('stage')}: {code}" + (f" ({reason})" if reason and reason != code else ""), 200)
    return _cell(item, 200)


def _next_line(document: Mapping[str, Any]) -> str | None:
    """What to run: the service's step for each problem, else a retry or a look by source ID."""
    problems = document.get("problems")
    steps = [item["next_step"].strip().rstrip(".") for item in problems if isinstance(item, Mapping)
             and isinstance(item.get("next_step"), str) and item["next_step"].strip()] if isinstance(problems, list) else []
    if steps:
        return "Next: " + ". ".join(dict.fromkeys(steps)) + "."
    source_id = document.get("source_id")
    retry = f"`lifecontext documents retry {source_id}`"
    failed = _failed_stage(document)
    if failed is not None:
        if failed[1].get("error_code") == "provider_unavailable":
            return f"Next: check the model with `lifecontext doctor --model-probe`, then run {retry}."
        return f"Next: run {retry}."
    state = document_state(document)
    if state in {"complete", "completed"}:
        return None
    if state in {"running", "pending"} or not isinstance(document.get("outcome"), str):
        return f"Next: check again with `lifecontext documents show {source_id}`."
    return f"Next: run {retry}."


def document_lines(document: Mapping[str, Any]) -> list[str]:
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
    following = _next_line(document)
    if following:
        lines.append(following)
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
    lines.extend(document_lines(document))
    if value.get("state") == "retryable_failed" and not lines[-1].startswith("Next:"):
        lines.append(f"Next: run `lifecontext documents retry {document.get('source_id')}`.")
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


def render_operation_list(value: Mapping[str, Any]) -> str | None:
    operations = value.get("operations")
    if not isinstance(operations, list) or not all(isinstance(item, Mapping) for item in operations):
        return None
    if not operations:
        return "No operations match."
    rows = []
    for item in operations:
        summary = item.get("summary") if isinstance(item.get("summary"), Mapping) else {}
        sources = summary.get("sources") if isinstance(summary.get("sources"), list) else []
        source = sources[0].get("source_id") if sources and isinstance(sources[0], Mapping) else None
        rows.append((item.get("operation_id"), item.get("operation_kind"), item.get("state"),
                     item.get("outcome") or "-", source or "-", item.get("updated_at")))
    lines = table(("OPERATION ID", "KIND", "STATE", "OUTCOME", "SOURCE", "UPDATED"), rows)
    lines.append(f"{len(operations)} operation{'s' if len(operations) != 1 else ''}.")
    if any(str(item.get("operation_kind")).startswith("document_")
           and (item.get("outcome") or item.get("state")) not in {"completed", "running", "pending"}
           for item in operations):
        lines.append("Next: `lifecontext documents retry SOURCE_ID` runs a document's unfinished stages again.")
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


def wiki_show_note(value: Mapping[str, Any], *, revision_asked: bool) -> str | None:
    """One stderr line when the page printed is not simply the current one: stale, conflicting or an old revision."""
    page, revision = value.get("page"), value.get("revision")
    if not isinstance(page, Mapping) or not isinstance(revision, Mapping):
        return None
    state = page.get("state")
    if state == "current" and not revision_asked:
        return None
    line = f"Page {page.get('page_id')}: revision {revision.get('revision')}, state {state}"
    if page.get("current_revision") != revision.get("revision"):
        line += f" (current revision is {page.get('current_revision')})"
    return line + "."


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
        name = evidence.get("title") or evidence.get("filename") if isinstance(evidence, Mapping) else None
        # The title names a hit; without a title or file name (an older service) the short ID does.
        source = (f" ({_cell(name, 120)})" if isinstance(name, str) and name.strip()
                  else f" ({_citation(evidence)})" if isinstance(evidence, Mapping) else "")
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
_MODEL = "check the model with `lifecontext doctor --model-probe`, then re-run the command"
_UPDATE = "update the CLI and restart the service so both run the same version"
_FIX_AND_RUN = "`lifecontext preflight` and `lifecontext doctor` say what to fix"
# What an agent is told when the fix is the owner's: it reports and never repairs anything itself.
AGENT_START = ("the owner starts the LifeContext service or checks it with `lifecontext service status`; "
               "do not start it yourself")
_OWNER_DOCTOR = "the owner runs `lifecontext doctor`, which says what is wrong"


class ErrorText:
    """What one failure code means, what to do next, and whether the same call may work later.

    ``agent_next_step`` replaces ``next_step`` in ``--json`` and over MCP when the step is a repair the
    owner makes and an agent must only report.
    """

    __slots__ = ("agent_next_step", "message", "next_step", "retryable")

    def __init__(self, message: str, next_step: str | None, retryable: bool = False,
                 agent_next_step: str | None = None):
        self.message = message
        self.next_step = next_step
        self.retryable = retryable
        self.agent_next_step = agent_next_step


# The API's codes (api.models._ERROR_CODES).
_API: dict[str, ErrorText] = {
    "invalid_request": ErrorText("The request was refused: an argument or the input is not valid.",
                                 "check the arguments with `lifecontext COMMAND --help`"),
    "unauthorized": ErrorText("The API token was refused or cannot be read.",
                              "run the command as the same user, with the same HOME, as the service",
                              agent_next_step="the owner runs this tool as the same user, with the same HOME, "
                                              "as the service"),
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
                                      "its checksum.", "run `lifecontext reconcile`; if it still fails, "
                                      "`lifecontext doctor` says what is wrong", True,
                                      "the owner runs `lifecontext reconcile`; if it still fails, "
                                      "`lifecontext doctor` says what is wrong"),
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
                             "`lifecontext doctor` names the path to fix",
                             agent_next_step="the owner runs `lifecontext doctor`, which names the path to fix"),
    "unsafe_permissions": ErrorText("Files or folders of LifeContext are open to other users.",
                                    "`lifecontext doctor` names them; make folders 0700 and files 0600",
                                    agent_next_step="the owner makes the LifeContext folders 0700 and files "
                                                    "0600; `lifecontext doctor` names them"),
    "vault_unavailable": ErrorText("The vault cannot be read or written.",
                                   "check that the vault folder exists and belongs to you; " + _DOCTOR, True,
                                   "the owner checks that the vault folder exists and belongs to them; "
                                   + _DOCTOR),
    "catalog_busy": ErrorText("The catalog is busy with another change.", "wait a few seconds and re-run", True),
    "internal_error": ErrorText("The service failed unexpectedly.",
                                "re-run once; if it fails again, run `lifecontext doctor` and look for the "
                                "request ID in the service log", True,
                                "report it with the request ID; the owner runs `lifecontext doctor` and looks "
                                "for the ID in the service log"),
}
# The CLI's own codes. api_timeout and api_timeout_read only explain an api_unavailable that timed out.
_CLI: dict[str, ErrorText] = {
    "api_unavailable": ErrorText("The LifeContext service is not reachable at 127.0.0.1:{port}.", "{start}", True),
    "api_timeout": ErrorText("The LifeContext service did not answer in time; it may still be working on the "
                             "request.", "wait, then check with the matching list or show command before "
                             "re-running: running it again may repeat the work",
                             agent_next_step="report it with the command you ran; do not resend it"),
    "api_timeout_read": ErrorText("The LifeContext service did not answer in 30 s; it may be busy with an "
                                  "import.", "try again in a few minutes", True),
    "api_error": ErrorText("The service answered with an error this command does not know.", _UPDATE,
                           agent_next_step="the owner updates the CLI and restarts the service"),
    "invalid_api_response": ErrorText("The service's answer is not what this command expects.", _UPDATE,
                                      agent_next_step="the owner updates the CLI and restarts the service"),
    "token_unavailable": ErrorText("No LifeContext API token for this user: the service writes it when it starts, "
                                   "under the same HOME.", "{start}", True),
    "input_unavailable": ErrorText("Unable to read the selected source input.",
                                   "give an existing regular file, or - for standard input"),
    "unsupported_format": ErrorText("LifeContext does not import files of that type; the message names the ones "
                                    "it does.", "save the file in one of those formats and import that copy"),
    "vault_missing": ErrorText("The vault's Journal folder does not exist.",
                               "create it with mode 0700, or set `vault` in ~/.lifeweave/config.yaml",
                               agent_next_step="the owner creates it with mode 0700, or sets `vault` in "
                                               "~/.lifeweave/config.yaml"),
    "destination_exists": ErrorText("The output file already exists; LifeContext never overwrites one.",
                                    "choose a new file name"),
    "output_unavailable": ErrorText("The output file cannot be written.",
                                    "check that its folder exists and that you can write to it"),
    "invalid_config": ErrorText("The config file ~/.lifeweave/config.yaml cannot be read.",
                                "`lifecontext doctor` says what is wrong", agent_next_step=_OWNER_DOCTOR),
    "server_start_failed": ErrorText("The service could not start.",
                                     "`lifecontext preflight` and `lifecontext doctor` say why"),
    "nothing_to_change": ErrorText("Nothing to change.", "give --provider, --model, --base-url or --api-key-stdin; "
                                   "`lifecontext llm show` shows the current model"),
}
_MCP: dict[str, ErrorText] = {
    "busy": ErrorText("A previous {tool} call is still finishing.", "report it instead of retrying", True),
    "path_not_allowed": ErrorText("The file is outside the import directories the owner allowed, or in a "
                                  "protected place (the LifeContext home, the vault, a hidden directory in the "
                                  "home directory, or a system directory).", "ask the owner to import it"),
}
# Local commands: init, llm, service, skills, capture, backup, restore, hindsight.
_LOCAL: dict[str, ErrorText] = {
    "setup_failed": ErrorText("The setup files could not be written.",
                              "check that ~/.lifeweave belongs to you and is writable, then re-run"),
    "backup_failed": ErrorText("The backup could not be written.", "check the destination folder, then re-run"),
    "restore_failed": ErrorText("The restore could not read or write its files.",
                                "check the archive and the LifeContext home, then re-run"),
    "hindsight_failed": ErrorText("The memory engine could not be stopped.", _DOCTOR),
    "provider_required": ErrorText("No model provider was chosen.", "give --provider, or run `lifecontext init` in "
                                                                    "a terminal to choose one"),
    "model_required": ErrorText("No model was chosen.", "give --model with the exact ID your provider lists"),
    "model_unsupported": ErrorText("That model does not work with LifeContext yet.",
                                   "choose another model with --model"),
    "invalid_llm_settings": ErrorText("The model settings are not valid.",
                                      "check --provider, --model and --base-url, or `lifecontext llm show`"),
    "invalid_api_key": ErrorText("The API key is not valid: one line of printable ASCII, without quotes.",
                                 "give it again at the hidden prompt or on stdin with --api-key-stdin"),
    "api_key_required": ErrorText("A model API key is required and none was given.",
                                  "give it at the hidden prompt, on stdin with --api-key-stdin, or in the file "
                                  "named by LIFECONTEXT_API_KEY_FILE"),
    "api_key_file_unreadable": ErrorText("The file named by LIFECONTEXT_API_KEY_FILE cannot be read.",
                                         "check that it exists, holds one line and is readable by you"),
    "invalid_timezone": ErrorText("That time zone is not known.", "give an IANA name such as Europe/Kyiv"),
    "invalid_vault": ErrorText("That vault location is not allowed.",
                               "choose a folder that is not `/`, not inside ~/.lifeweave and not a parent of it"),
    "vault_path_has_symlink": ErrorText("The vault path in config.yaml goes through a symbolic link.",
                                        "run `lifecontext init --force` to store the real path"),
    "unsafe_vault": ErrorText("The vault folder is not a real folder of yours.",
                              "choose a folder you own, or fix its owner and type"),
    "unsafe_home": ErrorText("The LifeContext home is not private to you, or HOME is not an absolute path.",
                             "check that HOME is an absolute path and ~/.lifeweave belongs to you with mode "
                             "0700, then re-run"),
    "unsafe_credentials": ErrorText("The credentials file is open to other users or not a regular file.",
                                    "make it a regular file with mode 0600, or delete it and run "
                                    "`lifecontext init` again"),
    "invalid_credentials": ErrorText("The credentials file is malformed.",
                                     "delete it and run `lifecontext init` again to enter the key"),
    "config_incomplete": ErrorText("The config file has no model section.",
                                   "run `lifecontext init --force` with --provider and --model"),
    "config_missing": ErrorText("There is no config file with model settings yet.", "run `lifecontext init` first"),
    "executable_not_found": ErrorText("The `lifecontext` program to run as a service was not found.",
                                      "install LifeContext so `lifecontext` is on PATH, then re-run"),
    "unsupported_platform": ErrorText("Services are supported on Linux (systemd) and macOS (launchd) only.",
                                      "run the API in a terminal with `lifecontext serve`"),
    "unit_write_failed": ErrorText("The service files could not be written.",
                                   "check that the user service folder is writable, then re-run"),
    "unit_remove_failed": ErrorText("The service files could not be removed.",
                                    "remove them by hand from the user service folder"),
    "systemctl_unavailable": ErrorText("The `systemctl` program was not found.",
                                       "use a systemd session, or run the API with `lifecontext serve`"),
    "systemctl_failed": ErrorText("The `systemctl` program refused the command.",
                                  "run `systemctl --user status lifecontext` to see why"),
    "launchctl_unavailable": ErrorText("The `launchctl` program was not found.",
                                       "run the API in a terminal with `lifecontext serve`"),
    "launchctl_failed": ErrorText("The `launchctl` program refused the command.",
                                  "run `launchctl list` to see the state of the LifeContext jobs"),
    "skill_missing": ErrorText("The agent skill files are missing from this installation.",
                               "reinstall LifeContext, then re-run"),
    "link_failed": ErrorText("The skill could not be linked into the agent's folder.",
                             "check that the folder exists and is writable, then re-run"),
    "unlink_failed": ErrorText("The skill link could not be removed.", "remove the link by hand"),
    "capture_requires_inbox": ErrorText("The capture folder must be named Inbox.",
                                        "leave out --inbox to use Journal/Inbox of the vault, or give a folder "
                                        "named Inbox"),
    "capture_unavailable": ErrorText("The inbox folder cannot be read.",
                                     "check that it exists and that you can read it", True),
    "unsafe_state": ErrorText("The capture state in ~/.lifeweave/capture is not private to you or is damaged.",
                              "check that the folder is yours with mode 0700, or remove its state.json, then "
                              "re-run"),
    "file_exists": ErrorText("A file is already at that place in the vault.", "choose a new name"),
    "file_too_large": ErrorText("The file is larger than the vault accepts.", "use a smaller file"),
    "invalid_file": ErrorText("The vault file is not valid.", _DOCTOR),
    "not_found": ErrorText("No such file in the vault.", "check the name and run it again"),
    "read_only_path": ErrorText("That vault path is read-only for LifeContext.", "choose another path"),
    "unmanaged_file": ErrorText("The vault file is not one LifeContext manages.",
                                "leave it as it is, or move it out of the way"),
    "sync_conflict": ErrorText("The vault holds a sync conflict copy of a LifeContext file.",
                               "settle the conflict in your sync tool, then run `lifecontext reconcile`"),
    "export_durability": ErrorText("The vault copy could not be made durable on disk.",
                                   "check the free space and that the disk is writable, then re-run", True),
    "locking_unavailable": ErrorText("File locking is not available on this system.",
                                     "run LifeContext on Linux or macOS"),
    "lock_timeout": ErrorText("Another LifeContext process holds the vault lock.",
                              "wait a moment and re-run", True),
    "nothing_to_back_up": ErrorText("There is no LifeContext data to back up.", "run `lifecontext init` first"),
    "service_running": ErrorText("The service is running.", "stop it, or add --stop-service where the command "
                                                            "allows it, then re-run"),
    "hindsight_running": ErrorText("The memory engine is still running.",
                                   "run `lifecontext hindsight stop`, then re-run"),
    "backup_write_failed": ErrorText("The archive could not be written.",
                                     "check the free space and the destination, then re-run"),
    "catalog_unreadable": ErrorText("The catalog database is damaged or unreadable.", _DOCTOR),
    "destination_inside_backup": ErrorText("The archive would be written inside the data it backs up.",
                                           "choose a destination outside the LifeContext home and the vault"),
    "source_changed": ErrorText("A file changed while it was being backed up.", "re-run the backup", True),
    "archive_invalid": ErrorText("The archive is not a LifeContext backup.", "check that you gave the right file"),
    "archive_unreadable": ErrorText("The archive cannot be read.", "check the path and that you can read it"),
    "archive_incomplete": ErrorText("The archive lacks files its manifest lists.", "make a new backup"),
    "archive_changed": ErrorText("The archive changed while it was being read.", "copy it to a stable place, "
                                                                                 "then re-run"),
    "archive_checksum_mismatch": ErrorText("The archive does not match its checksum.",
                                           "copy it again or use another backup"),
    "archive_config_invalid": ErrorText("The config file in the archive is not valid.",
                                        "use another backup"),
    "archive_instance_invalid": ErrorText("The memory engine's instance file in the archive is not valid.",
                                          "use another backup"),
    "home_not_empty": ErrorText("This HOME already holds LifeContext data; a restore never overwrites it.",
                                "restore into a fresh HOME"),
    "vault_not_empty": ErrorText("The vault folder to restore into is not empty.",
                                 "restore into an empty or new folder"),
    "vault_outside_home": ErrorText("The vault to restore is outside this HOME.",
                                    "give `--vault DIR` with a folder inside your home directory"),
    "vault_location_refused": ErrorText("The vault location is not one a restore writes to.",
                                        "give `--vault DIR` with a folder inside ~/LifeContext"),
    "schema_unsupported": ErrorText("The archive was made by a newer LifeContext.",
                                    "update LifeContext, then re-run"),
}
# The memory engine (Hindsight) and its Postgres.
_ENGINE: dict[str, ErrorText] = {
    "missing_package": ErrorText("A package of the memory engine is not installed.", "reinstall LifeContext"),
    "unsupported_version": ErrorText("The memory engine is not the version LifeContext pins.",
                                     "reinstall LifeContext so the packages match"),
    "missing_capability": ErrorText("The memory engine lacks a capability LifeContext needs.",
                                    "reinstall LifeContext so the packages match"),
    "missing_credentials": ErrorText("The model API key is missing.", "run `lifecontext init` and give the key"),
    "document_generation_failed": ErrorText("The model could not write the document.",
                                            _MODEL, True),
    "daemon_unavailable": ErrorText("The memory engine is not answering or not healthy.",
                                    _FIX_AND_RUN, True),
    "daemon_port_foreign": ErrorText("Another program holds the port this HOME's memory engine needs.",
                                     "stop that program, then run `lifecontext doctor`"),
    "daemon_port_unavailable": ErrorText("No free port was found near this HOME's engine port.",
                                         "free a port near it, then run `lifecontext doctor`"),
    "daemon_stop_failed": ErrorText("An old memory engine daemon of this HOME did not stop.",
                                    "stop that process by hand, then run `lifecontext doctor`"),
    "engine_auth_disabled": ErrorText("The memory engine runs without its API token.", _FIX_AND_RUN),
    "engine_auth_rejected": ErrorText("The memory engine refused LifeContext's API token.", _FIX_AND_RUN),
    "engine_token_missing": ErrorText("The memory engine's token file is missing.",
                                      "start the service once so it writes the token", True),
    "unsafe_engine_token": ErrorText("The engine token file is not a private regular file of 64 hex digits.",
                                     "delete it and start again"),
    "unsafe_profile_paths": ErrorText("The memory engine's profile folders are not private to you.",
                                      _FIX_AND_RUN),
    "unsafe_profile_file": ErrorText("The memory engine's profile file is not a private regular file of yours.",
                                     _FIX_AND_RUN),
    "unsafe_profile_lock": ErrorText("The profile lock file is not a private regular file of yours.",
                                     _FIX_AND_RUN),
    "profile_lock_unavailable": ErrorText("The profile lock cannot be taken.", _FIX_AND_RUN),
    "profile_lock_timeout": ErrorText("Another LifeContext process holds the profile lock.",
                                      "wait a moment and re-run", True),
    "profile_setup_failed": ErrorText("The memory engine's profile could not be set up.", _FIX_AND_RUN),
    "postgres_setup_failed": ErrorText("The Postgres of the memory engine could not be set up.", _FIX_AND_RUN),
    "postgres_binaries_missing": ErrorText("The Postgres programs of the memory engine are missing.",
                                           "reinstall LifeContext"),
    "postgres_start_failed": ErrorText("The Postgres of the memory engine did not start.", _FIX_AND_RUN),
    "postgres_stop_failed": ErrorText("The Postgres of the memory engine did not stop.", _FIX_AND_RUN),
    "postgres_control_failed": ErrorText("The Postgres control program failed.", _FIX_AND_RUN),
    "postgres_state_unknown": ErrorText("The state of the memory engine's Postgres cannot be told.",
                                        _FIX_AND_RUN),
    "postgres_instance_invalid": ErrorText("The Postgres instance file of the memory engine is not valid.",
                                           "restore it from a backup"),
    "postgres_instance_unsafe": ErrorText("The Postgres instance files are not private to you.", _FIX_AND_RUN),
    "postgres_socket_dir_unsafe": ErrorText("The Postgres socket folder is not private to you.", _FIX_AND_RUN),
    "postgres_socket_path_too_long": ErrorText("The Postgres socket path is too long for this HOME.",
                                               "use a shorter HOME path"),
    "postgres_socket_path_unsupported": ErrorText("The HOME path has a quote, comma or backslash, which the "
                                                  "Postgres socket path cannot hold.", "use a HOME path without them"),
    "postgres_instance_missing": ErrorText("The Postgres data folder has no `instance.json`; pg0 deletes it when "
                                           "one of its own starts fails.", "restore the file from a backup"),
}
GROUPS: tuple[tuple[str, dict[str, ErrorText]], ...] = (
    ("The API's codes", _API),
    ("The CLI's own codes", _CLI),
    ("The MCP server's own codes", _MCP),
    ("Local commands (`init`, `llm`, `service`, `skills`, `capture`, `backup`, `restore`, `hindsight stop`)", _LOCAL),
    ("The memory engine", _ENGINE),
)
# Every code the CLI prints as {"error": ...}, except the agent gate's own transport codes. {port}, {tool}
# and {start} are filled in.
ERRORS: dict[str, ErrorText] = {code: entry for _, group in GROUPS for code, entry in group.items()}
# Codes that only choose the sentence for another code (a timeout reports api_unavailable).
EXPLAINERS = frozenset({"api_timeout", "api_timeout_read"})


def describe(code: str, *, port: int, start: str | None = None, tool: str = "",
             agent: bool = False) -> tuple[str, str | None, bool]:
    """(message, next step, retryable) for a code; unknown codes get a plain sentence and no next step.

    ``agent`` words the next step for a caller that reports to the owner instead of repairing: the
    ``--json`` output and the MCP server.
    """
    entry = ERRORS.get(code)
    if entry is None:
        return f"LifeContext request failed: {code.replace('_', ' ')}.", None, False
    start = start or (AGENT_START if agent else f"start it with `lifecontext serve --port {port}`")
    fields = {"port": port, "tool": tool or "LifeContext", "start": start}
    step = entry.next_step if not agent or entry.agent_next_step is None else entry.agent_next_step
    return entry.message.format(**fields), step.format(**fields) if step else None, entry.retryable


def start_hint(port: int | str, *, service_installed: bool) -> str:
    """Where to look when the service is not reachable; `serve` stays the answer without an installed unit."""
    serve = f"start it with `lifecontext serve --port {port}`"
    if service_installed:
        return ("if LifeContext runs as the installed service, check it with `lifecontext service status`: it "
                f"may still be starting, or recovering after a restart; otherwise {serve}")
    return serve


def error_json(code: str, *, port: int, retryable: bool | None = None, request_id: str | None = None,
               message_code: str | None = None, message: str | None = None) -> dict[str, object]:
    """``--json`` failure: ``error`` is the code as before; the other keys explain it."""
    text, next_step, default_retryable = describe(message_code or code, port=port, agent=True)
    return {"error": code, "message": message or text, "next_step": next_step,
            "retryable": default_retryable if retryable is None else retryable, "request_id": request_id}


def error_text(code: str, *, port: int, start: str | None = None, message_code: str | None = None,
               message: str | None = None) -> str:
    """Plain failure: the sentence, then a "Next:" line when there is something to do."""
    text, next_step, _ = describe(message_code or code, port=port, start=start)
    return (message or text) + (f"\nNext: {next_step}." if next_step else "")


def failure_codes_section() -> str:
    """The Markdown list for docs/operations.md, one bullet per code of the table."""
    lines = []
    for title, group in GROUPS:
        lines += [f"### {title}", ""]
        for code, entry in group.items():
            if code in EXPLAINERS:
                continue
            fields = {"port": "PORT", "tool": "MCP tool", "start": start_hint("PORT", service_installed=True)}
            step = entry.next_step.format(**fields) if entry.next_step else ""
            lines.append(f"- `{code}`: {entry.message.format(**fields)}" + (f" Next: {step}." if step else ""))
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


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
    "operations list": render_operation_list,
}


def render(command: str, value: Mapping[str, Any]) -> str | None:
    """The plain text for one command's answer, or ``None`` for the generic fallback."""
    renderer = RENDERERS.get(command)
    return renderer(value) if renderer else None


if __name__ == "__main__":
    import sys
    from pathlib import Path

    BEGIN, END = "<!-- failure-codes:begin -->\n", "<!-- failure-codes:end -->"
    if sys.argv[1:2] != ["--write"] or len(sys.argv) != 3:
        raise SystemExit("usage: python -m lifecontext.cli_output --write docs/operations.md")
    document = Path(sys.argv[2])
    text = document.read_text(encoding="utf-8")
    head, _, rest = text.partition(BEGIN)
    document.write_text(head + BEGIN + "\n" + failure_codes_section() + "\n" + END + rest.partition(END)[2],
                        encoding="utf-8")
