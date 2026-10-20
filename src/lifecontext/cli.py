"""Entry point for the independent LifeContext command."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import re
import secrets
import stat
import sys
import tempfile
from collections.abc import Sequence
from datetime import date, datetime
from pathlib import Path
from urllib.parse import quote, urlencode

from lifecontext import __version__
from lifecontext.api.security import local_token, protected_path
from lifecontext.config import LIMIT_CEILINGS
from lifecontext.hindsight import HINDSIGHT_CLIENT_TIMEOUT
from lifecontext.paths import LifeContextPaths

DEFAULT_PORT = 8765
# Leave five seconds after Hindsight's bounded provider request for API cleanup.
HTTP_TIMEOUT = HINDSIGHT_CLIENT_TIMEOUT + 5.0
MAX_TEXT_BYTES = LIMIT_CEILINGS["max_text_bytes"]
MAX_QUERY_BYTES = LIMIT_CEILINGS["max_query_bytes"]
MAX_FILE_BYTES = LIMIT_CEILINGS["max_file_bytes"]
MAX_REQUEST_BYTES = min(
    MAX_FILE_BYTES, MAX_TEXT_BYTES * 6 + 65_536
)
MAX_RESPONSE_BYTES = LIMIT_CEILINGS["max_response_bytes"]
EXPORT_MEDIA_TYPE = "application/vnd.lifeweave.bundle"
MAX_DOCUMENT_BYTES = 32 * 1024 * 1024
# `wiki lint --fail-on` found something; 1 stays "the command itself failed".
LINT_FAILED_EXIT = 3
_SOURCE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_OPERATION_ID = re.compile(r"op_[0-9a-f]{32}\Z")
_EDGE_ID = re.compile(r"edge_[0-9a-f]{64}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_IDEMPOTENCY_KEY = re.compile(r"[\x21-\x7e]{1,255}\Z")
_API_ERROR_CODES = frozenset({
    "invalid_request", "body_too_large", "response_too_large", "host_not_allowed",
    "origin_not_allowed", "source_not_found", "source_conflict", "evidence_unavailable",
    "invalid_source_text", "invalid_provider_response", "provider_unavailable",
    "retain_failed", "recall_failed", "reflect_failed", "unattributed_evidence",
    "result_limit_exceeded", "operation_status_unavailable", "curation_unavailable",
    "transfer_unavailable", "conflict", "relationship_unavailable", "unsafe_path",
    "unsafe_permissions", "vault_unavailable", "internal_error", "unauthorized", "catalog_busy",
})


class CLIError(RuntimeError):
    """Stable command failure that never includes paths or remote response text."""

    def __init__(self, code: str, status: int | None = None):
        super().__init__(code)
        self.code = code
        # The HTTP status when the API itself answered with an error.
        self.status = status


def _positive_int(value: str) -> int:
    try:
        result = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a positive integer") from None
    if not 1 <= result <= 2_147_483_647:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return result


def _port(value: str) -> int:
    try:
        result = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a TCP port from 1 to 65535") from None
    if not 1 <= result <= 65535:
        raise argparse.ArgumentTypeError("must be a TCP port from 1 to 65535")
    return result


def _source_id(value: str) -> str:
    if not _SOURCE_ID.fullmatch(value):
        raise argparse.ArgumentTypeError("invalid source ID")
    return value


def _edge_id(value: str) -> str:
    if not _EDGE_ID.fullmatch(value):
        raise argparse.ArgumentTypeError("invalid relationship ID")
    return value


def _operation_id(value: str) -> str:
    if not _OPERATION_ID.fullmatch(value):
        raise argparse.ArgumentTypeError("invalid operation ID")
    return value


def _sha256(value: str) -> str:
    if not _SHA256.fullmatch(value):
        raise argparse.ArgumentTypeError("expected a SHA-256 digest")
    return value


def _day(value: str) -> str:
    try:
        if date.fromisoformat(value).isoformat() != value:
            raise ValueError
    except ValueError:
        raise argparse.ArgumentTypeError("expected a date as YYYY-MM-DD") from None
    return value


def _moment(value: str) -> str:
    try:
        datetime.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError("expected an ISO 8601 date and time") from None
    return value


def _idempotency_key(value: str) -> str:
    if not _IDEMPOTENCY_KEY.fullmatch(value):
        raise argparse.ArgumentTypeError("idempotency key must be printable ASCII")
    return value


def _shared_options(parser: argparse.ArgumentParser, *, root: bool = False) -> None:
    default = DEFAULT_PORT if root else argparse.SUPPRESS
    json_default = False if root else argparse.SUPPRESS
    parser.add_argument("--port", type=_port, default=default, help="loopback API port (default: 8765)")
    parser.add_argument("--json", dest="as_json", action="store_true", default=json_default,
                        help="print stable JSON output")


def _mutation_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--idempotency-key", type=_idempotency_key,
                        help="reuse this key to explicitly retry the same mutation")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lifecontext",
        description="Set up LifeContext and use its explicitly running local API.",
    )
    parser.add_argument("--version", action="version", version=f"lifecontext {__version__}")
    _shared_options(parser, root=True)
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser(
        "init", help="set up ~/.lifeweave, the vault and the model key (never starts anything)"
    )
    _shared_options(init)
    init.add_argument("--provider", help="model provider ID (default: $LIFECONTEXT_PROVIDER, else anthropic)")
    init.add_argument("--model", help="exact model ID (default: $LIFECONTEXT_MODEL)")
    init.add_argument("--base-url",
                      help="provider API root, required for openai_compatible (default: $LIFECONTEXT_BASE_URL)")
    init.add_argument("--vault", metavar="DIR",
                      help="vault folder (default: $LIFECONTEXT_VAULT, else ~/LifeContext/vault)")
    init.add_argument("--timezone", help="IANA time zone (default: the system's)")
    init.add_argument("--api-key-stdin", action="store_true",
                      help="read the provider key from stdin, one line (else from $LIFECONTEXT_API_KEY_FILE)")
    init.add_argument("--non-interactive", action="store_true", help="never prompt; use flags and defaults")
    init.add_argument("--force", action="store_true",
                      help="apply these flags to an existing config.yaml, keeping every other setting")
    init.add_argument("--replace-key", action="store_true",
                      help="replace the stored provider key with the one given; changes only .env")

    doctor = commands.add_parser(
        "doctor", help="report safe diagnostics; exit 1 only for broken prerequisites"
    )
    _shared_options(doctor)

    skills = commands.add_parser(
        "skills", help="link the agent skill into Claude Code, Codex, Gemini CLI and Hermes"
    )
    _shared_options(skills)
    skill_commands = skills.add_subparsers(dest="skills_command", required=True)
    for name, text in (
        ("install", "link each agent to the packaged skill; a different entry is backed up first"),
        ("check", "report each link; exit 1 when one is missing or differs"),
        ("remove", "delete only links that point at a LifeContext skill"),
    ):
        command = skill_commands.add_parser(name, help=text)
        _shared_options(command)
        command.add_argument("--agent", choices=("claude", "codex", "gemini", "hermes", "all"),
                             help="default: the agents found in this HOME (remove: all)")
        if name != "check":
            command.add_argument("--dry-run", action="store_true", help="show what would change")

    llm = commands.add_parser("llm", help="show or change the model, endpoint and key")
    _shared_options(llm)
    llm_commands = llm.add_subparsers(dest="llm_command", required=True)
    _shared_options(llm_commands.add_parser(
        "show", help="print provider, model and base URL, and whether a key is stored (never the key)"))
    llm_set = llm_commands.add_parser(
        "set", help="change only the given fields, then restart the service if it runs")
    _shared_options(llm_set)
    llm_set.add_argument("--provider", help="model provider ID")
    llm_set.add_argument("--model", help="exact model ID")
    llm_set.add_argument("--base-url", help="provider API root, required for openai_compatible")
    llm_set.add_argument("--api-key-stdin", action="store_true",
                         help="replace the stored key with one line from stdin (else from $LIFECONTEXT_API_KEY_FILE)")

    capture = commands.add_parser(
        "capture", help="turn settled Markdown notes in the vault's Journal/Inbox into journal entries")
    _shared_options(capture)
    capture.add_argument("--once", action="store_true", required=True, help="scan the inbox once, then exit")
    capture.add_argument("--inbox", metavar="DIR",
                         help="a folder named Inbox (default: Journal/Inbox of the configured vault)")

    service = commands.add_parser(
        "service", help="run the API (and daily maintenance, note capture) as a systemd --user or launchd service"
    )
    _shared_options(service)
    service_commands = service.add_subparsers(dest="service_command", required=True)
    for name, text in (
        ("install", "write the unit files, then enable and start the service"),
        ("uninstall", "stop and remove only LifeContext's unit files"),
        ("status", "show the units and the last maintenance run"),
    ):
        command = service_commands.add_parser(name, help=text)
        _shared_options(command)
        if name != "status":
            command.add_argument("--maintenance", action="store_true",
                                 help="install: add the daily 07:00 maintenance timer; "
                                      "uninstall: remove only that timer")
            command.add_argument("--capture", action="store_true",
                                 help="install: add the 30-second Journal/Inbox capture timer; "
                                      "uninstall: remove only that timer")
            command.add_argument("--dry-run", action="store_true", help="print the files and commands only")
        if name == "install":
            command.add_argument("--no-start", action="store_true",
                                 help="write and enable the units for the next login; start nothing now")

    backup = commands.add_parser(
        "backup", help="archive this HOME's LifeContext data with a checksummed manifest (service stopped)"
    )
    _shared_options(backup)
    backup.add_argument("destination", nargs="?", metavar="DEST",
                        help="file or directory (default: ~/LifeContext/backups/)")
    backup.add_argument("--stop-service", action="store_true",
                        help="stop the user service and the lifeweave Hindsight profile first, "
                             "then start the service again")
    hindsight = commands.add_parser("hindsight", help="manage this HOME's lifeweave Hindsight profile")
    _shared_options(hindsight)
    hindsight_commands = hindsight.add_subparsers(dest="hindsight_command", required=True)
    _shared_options(hindsight_commands.add_parser(
        "stop", help="stop only the lifeweave profile's daemon and its Postgres (the API must be stopped)"))
    restore = commands.add_parser("restore", help="restore a backup into a HOME without LifeContext data")
    _shared_options(restore)
    restore.add_argument("archive", metavar="ARCHIVE")
    restore.add_argument("--vault", metavar="DIR", help="put the vault here instead of its old place")

    maintain = commands.add_parser(
        "maintain", help="reconcile, lint and write yesterday's and today's brief (no model calls)"
    )
    _shared_options(maintain)
    maintain.add_argument("--date", type=_day, help="local day to treat as today (default: today)")
    maintain.add_argument("--retry-wiki", type=_positive_int, metavar="N",
                          help="also rebuild at most N documents whose wiki stage failed (model calls)")
    maintain.add_argument("--rebuild-stale", type=_positive_int, metavar="N",
                          help="also rebuild at most N documents behind stale wiki pages (model calls)")

    preflight = commands.add_parser(
        "preflight", help="check the isolated LifeContext Hindsight profile"
    )
    _shared_options(preflight)
    preflight.add_argument(
        "--retain-smoke", action="store_true",
        help="also retain one fixed disposable startup test source",
    )
    preflight.add_argument(
        "--vision-probe", action="store_true",
        help="also send one generated test image to the configured vision model",
    )

    serve = commands.add_parser("serve", help="start the API on loopback")
    _shared_options(serve)

    status = commands.add_parser("status", help="show API and dependency readiness")
    _shared_options(status)

    reconcile = commands.add_parser("reconcile", help="verify and repair managed projections from custody")
    _shared_options(reconcile)
    reconcile.add_argument("--limit", type=_positive_int, default=200)

    retain = commands.add_parser("retain", help="retain source bytes from a file or stdin")
    _shared_options(retain)
    retain.add_argument("source_id", type=_source_id)
    retain.add_argument("source_version", type=_positive_int)
    retain.add_argument("--input", default="-", metavar="FILE", help="source file, or - for stdin")
    _mutation_options(retain)

    for name in ("recall", "reflect"):
        command = commands.add_parser(name, help=f"{name} over the Journal")
        _shared_options(command)
        command.add_argument("--query", required=True)
        command.add_argument("--max-results", type=_positive_int)
        command.add_argument("--max-tokens", type=_positive_int, default=4096)
        command.add_argument("--budget", choices=("low", "mid", "high"),
                             default="mid" if name == "recall" else "low")

    evidence = commands.add_parser("evidence", help="retrieve exact evidence metadata")
    _shared_options(evidence)
    evidence.add_argument("source_id", type=_source_id)
    evidence.add_argument("--version", type=_positive_int, required=True)
    evidence.add_argument("--include-content", action="store_true",
                          help="include source text in the response")

    curate = commands.add_parser(
        "curate", help="correct, re-index, forget or delete one source version",
        description=(
            "Correct, re-index, forget or delete one source version. Forget withdraws a source from "
            "recall and the wiki but keeps it: the original stays in custody, vault files already "
            "written stay (for a document, its source note under documents/), a document stays in "
            "documents list, and it can no longer be re-indexed or replaced. Delete removes its "
            "original, its vault files and its recall index and withdraws it from the wiki; the catalog "
            "keeps a record of the deletion. Use delete to remove a source. Forget and delete each need "
            "their prepare_ step first, whose operation ID goes to --prepared-operation-id."))
    _shared_options(curate)
    curate.add_argument("action", choices=(
        "correct", "reindex", "prepare_forget", "forget", "prepare_delete", "delete"
    ), help="forget withdraws a source from recall and the wiki and keeps it on this machine; "
            "delete removes it")
    curate.add_argument("source_id", type=_source_id)
    curate.add_argument("--expected-version", type=_positive_int)
    curate.add_argument("--prepared-operation-id", type=_operation_id)
    curate.add_argument("--input", default="-", metavar="FILE",
                        help="replacement source file for correct, or - for stdin")
    _mutation_options(curate)

    relationships = commands.add_parser("relationships", help="manage local relationship proposals")
    _shared_options(relationships)
    relationship_commands = relationships.add_subparsers(dest="relationship_command", required=True)
    list_relationships = relationship_commands.add_parser("list", help="list relationship records")
    _shared_options(list_relationships)
    list_relationships.add_argument("--edge-id", type=_edge_id)
    list_relationships.add_argument("--source-id", type=_source_id)
    list_relationships.add_argument("--status", choices=("proposed", "approved", "rejected"))
    list_relationships.add_argument("--max-results", type=_positive_int)
    scan = relationship_commands.add_parser("scan", help="propose links from the configured vault")
    _shared_options(scan)
    scan.add_argument("--source-id", type=_source_id)
    scan.add_argument("--max-results", type=_positive_int)
    _mutation_options(scan)
    decide = relationship_commands.add_parser("decide", help="approve or reject one proposal")
    _shared_options(decide)
    decide.add_argument("edge_id", type=_edge_id)
    decide.add_argument("decision", choices=("approve", "reject"))
    decide.add_argument("--expected-revision", type=_positive_int, required=True)
    _mutation_options(decide)

    export = commands.add_parser("export", help="save a bundle exported by the API")
    _shared_options(export)
    export.add_argument("destination", metavar="DEST")
    export.add_argument("--source-id", action="append", dest="source_ids", type=_source_id)
    _mutation_options(export)
    import_command = commands.add_parser("import", help="import a bundle through the API")
    _shared_options(import_command)
    import_command.add_argument("input", metavar="PATH", help="bundle file, or - for stdin")
    _mutation_options(import_command)

    operations = commands.add_parser("operations", help="look up a completed operation")
    _shared_options(operations)
    operation_commands = operations.add_subparsers(dest="operation_command", required=True)
    operation_get = operation_commands.add_parser("get", help="get one operation result")
    _shared_options(operation_get)
    operation_get.add_argument("operation_id", type=_operation_id)

    documents = commands.add_parser("documents", help="manage original documents")
    _shared_options(documents)
    document_commands = documents.add_subparsers(dest="document_command", required=True)
    upload = document_commands.add_parser("import", help="preserve and index an original")
    _shared_options(upload)
    upload.add_argument("input", metavar="FILE", help="document file, or - for stdin")
    upload.add_argument("--filename", help="original basename (required for stdin)")
    upload.add_argument("--mime", default="application/octet-stream")
    upload.add_argument("--provenance", default="{}", help="bounded provenance JSON")
    upload.add_argument("--source-id", type=_source_id)
    upload.add_argument("--source-version", type=_positive_int)
    upload.add_argument("--no-vision", action="store_true",
                        help="never send this document's pages to the configured vision model")
    _mutation_options(upload)
    document_list = document_commands.add_parser("list")
    _shared_options(document_list)
    document_list.add_argument("--limit", type=_positive_int, default=100)
    for name in ("show", "download"):
        command = document_commands.add_parser(name)
        _shared_options(command)
        command.add_argument("source_id", type=_source_id)
        command.add_argument("--version", type=_positive_int)
        if name == "download":
            command.add_argument("destination", metavar="DEST")
    retry = document_commands.add_parser("retry")
    _shared_options(retry)
    retry.add_argument("operation_id", type=_operation_id)
    retry.add_argument("--rerun-extraction", action="store_true")
    retry_vision = retry.add_mutually_exclusive_group()
    retry_vision.add_argument("--no-vision", dest="vision", action="store_false", default=None,
                              help="with --rerun-extraction: keep this document away from vision")
    retry_vision.add_argument("--vision", dest="vision", action="store_true",
                              help="with --rerun-extraction: lift an earlier --no-vision")
    _mutation_options(retry)

    categories = commands.add_parser("categories", help="manage approved categories")
    _shared_options(categories)
    category_commands = categories.add_subparsers(dest="category_command", required=True)
    for name in ("list", "proposals"):
        command = category_commands.add_parser(name)
        _shared_options(command)
        if name == "proposals":
            command.add_argument("--state", choices=("pending", "approved", "rejected", "redirected"), default="pending")
    propose = category_commands.add_parser("propose")
    _shared_options(propose)
    propose.add_argument("--input", required=True, metavar="FILE", help="classification and document references JSON, or - for stdin")
    _mutation_options(propose)
    for name in ("approve", "reject", "redirect", "rename", "alias", "merge", "reassign"):
        command = category_commands.add_parser(name)
        _shared_options(command)
        _mutation_options(command)
        if name in {"approve", "reject", "redirect"}:
            command.add_argument("proposal_id", type=_source_id)
        elif name == "reassign":
            command.add_argument("source_id", type=_source_id)
        else:
            command.add_argument("category_id", type=_source_id)
        if name != "merge":
            command.add_argument("--expected-revision", type=_positive_int, required=True)
        if name in {"approve", "redirect", "reassign"}:
            command.add_argument("--category-id", type=_source_id, required=True)
        if name == "approve":
            command.add_argument("--budget-override", action="store_true")
        elif name == "rename":
            command.add_argument("--name", required=True)
            command.add_argument("--description")
        elif name == "alias":
            command.add_argument("--alias", required=True)
        elif name == "merge":
            command.add_argument("--target-category-id", type=_source_id, required=True)
            command.add_argument("--expected-source-revision", type=_positive_int, required=True)
            command.add_argument("--expected-target-revision", type=_positive_int, required=True)

    wiki = commands.add_parser("wiki", help="inspect and rebuild maintained wiki pages")
    _shared_options(wiki)
    wiki_commands = wiki.add_subparsers(dest="wiki_command", required=True)
    for name in ("list", "show", "history", "status", "rebuild"):
        command = wiki_commands.add_parser(name)
        _shared_options(command)
        if name in {"show", "history"}:
            command.add_argument("page_id", type=_source_id)
        if name == "show":
            command.add_argument("--revision", type=_positive_int)
        if name == "rebuild":
            command.add_argument("--source-id", type=_source_id)
            command.add_argument("--version", type=_positive_int)
            _mutation_options(command)
    lint = wiki_commands.add_parser("lint", help="report wiki and document health without a model call")
    _shared_options(lint)
    lint.add_argument("--fail-on", choices=("error", "warning"),
                      help=f"exit {LINT_FAILED_EXIT} when a finding of this severity or worse exists")
    reviews = wiki_commands.add_parser("reviews", help="list contradiction and manual-edit reviews")
    _shared_options(reviews)
    reviews.add_argument("--page", type=_source_id)
    reviews.add_argument("--state", choices=("pending", "resolved"), default="pending")
    review_commands = reviews.add_subparsers(dest="review_command")
    resolve = review_commands.add_parser("resolve", help="record the owner's decision on one review")
    _shared_options(resolve)
    resolve.add_argument("review_id", type=_source_id)
    resolve.add_argument("--note", required=True, help="the owner's decision, one line")
    _mutation_options(resolve)

    brief = commands.add_parser("brief", help="summarize what changed, without a model call")
    _shared_options(brief)
    window = brief.add_mutually_exclusive_group()
    window.add_argument("--date", type=_day, help="local day in the configured timezone (default: today)")
    window.add_argument("--since", type=_moment, help="ISO 8601 moment; a time without offset is local")
    brief.add_argument("--project", action="store_true",
                       help="also write wiki/Briefs/DATE - Brief.md and wiki/Log.md (not with --since)")

    mcp = commands.add_parser("mcp", help="serve the API to MCP clients over stdio (read-only by default)")
    mcp.add_argument("--port", type=_port, default=argparse.SUPPRESS, help="loopback API port (default: 8765)")
    mcp.add_argument("--allow-writes", action="store_true",
                     help="also offer retain, wiki_reviews_resolve and, with --import-dir, documents_import")
    mcp.add_argument("--import-dir", action="append", dest="import_dirs", default=[], metavar="DIR",
                     help="with --allow-writes: documents_import may read files under DIR (repeatable)")
    return parser


def _read_bounded(stream, maximum: int) -> bytes:
    value = stream.read(maximum + 1)
    if len(value) > maximum:
        raise CLIError("body_too_large")
    return value


def _read_input(source: str, *, maximum: int = MAX_TEXT_BYTES) -> bytes:
    if source == "-":
        try:
            return _read_bounded(sys.stdin.buffer, maximum)
        except OSError:
            raise CLIError("input_unavailable") from None

    descriptor = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        descriptor = os.open(source, flags)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise CLIError("input_unavailable")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            return _read_bounded(stream, maximum)
    except CLIError:
        raise
    except OSError:
        raise CLIError("input_unavailable") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _json_bytes(value: object) -> bytes:
    try:
        body = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise CLIError("invalid_request") from None
    if len(body) > MAX_REQUEST_BYTES:
        raise CLIError("body_too_large")
    return body


def _typed_api_error(raw: bytes) -> str:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError):
        return "api_error"
    if not isinstance(value, dict) or set(value) != {"request_id", "error_code", "retryable"}:
        return "api_error"
    request_id = value.get("request_id")
    code = value.get("error_code")
    retryable = value.get("retryable")
    if (
        not isinstance(request_id, str)
        or not re.fullmatch(r"[0-9a-f]{16,64}", request_id)
        or not isinstance(code, str)
        or code not in _API_ERROR_CODES
        or not isinstance(retryable, bool)
    ):
        return "api_error"
    return code


def _http_exchange(
    args: argparse.Namespace,
    method: str,
    target: str,
    *,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    max_request_bytes: int = MAX_REQUEST_BYTES,
    max_response_bytes: int = MAX_RESPONSE_BYTES,
) -> tuple[int, dict[str, str], bytes]:
    if body is not None and len(body) > max_request_bytes:
        raise CLIError("body_too_large")
    request_headers = dict(headers or {"Accept": "application/json"})
    path = target.split("?", 1)[0]
    if protected_path(path):
        try:
            token = local_token(LifeContextPaths.current(), create=False)
        except FileNotFoundError:
            # The service writes the token on start under the same HOME.
            raise CLIError("token_unavailable") from None
        except OSError:
            raise CLIError("unauthorized") from None
        request_headers["Authorization"] = "Bearer " + token
    timeout = HTTP_TIMEOUT
    if method == "POST" and (path == "/v1/documents" or
                             (path.startswith("/v1/documents/operations/") and path.endswith("/retry"))):
        from lifecontext.extraction import ExtractionLimits
        from lifecontext.vision import VISION_DEADLINE_SECONDS, VISION_PROBE_SECONDS
        timeout += ExtractionLimits().max_seconds + VISION_DEADLINE_SECONDS + VISION_PROBE_SECONDS
    connection = http.client.HTTPConnection("127.0.0.1", args.port, timeout=timeout)
    try:
        connection.request(method, target, body=body, headers=request_headers)
        response = connection.getresponse()
        raw = response.read(max_response_bytes + 1)
        if len(raw) > max_response_bytes:
            raise CLIError("response_too_large")
        response_headers = {
            name.casefold(): value for name, value in response.getheaders()
        }
        return response.status, response_headers, raw
    except CLIError:
        raise
    except (http.client.HTTPException, OSError, TimeoutError):
        raise CLIError("api_unavailable") from None
    finally:
        connection.close()


def _http_request(
    args: argparse.Namespace,
    method: str,
    target: str,
    *,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    max_request_bytes: int = MAX_REQUEST_BYTES,
    max_response_bytes: int = MAX_RESPONSE_BYTES,
) -> tuple[dict[str, object], str | None]:
    status, response_headers, raw = _http_exchange(
        args,
        method,
        target,
        body=body,
        headers=headers,
        max_request_bytes=max_request_bytes,
        max_response_bytes=max_response_bytes,
    )
    if not 200 <= status < 300:
        raise CLIError(_typed_api_error(raw), status)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError):
        raise CLIError("invalid_api_response") from None
    if not isinstance(value, dict):
        raise CLIError("invalid_api_response")
    return value, response_headers.get("x-lifeweave-operation-id")


def _http_binary_response(
    args: argparse.Namespace,
    target: str,
    *,
    body: bytes,
    headers: dict[str, str],
    maximum: int,
) -> bytes:
    status, response_headers, raw = _http_exchange(
        args,
        "POST",
        target,
        body=body,
        headers=headers,
        max_request_bytes=MAX_REQUEST_BYTES,
        max_response_bytes=maximum,
    )
    if not 200 <= status < 300:
        raise CLIError(_typed_api_error(raw))
    content_type = response_headers.get("content-type", "").split(";", 1)[0].strip().casefold()
    if content_type != EXPORT_MEDIA_TYPE or not raw:
        raise CLIError("invalid_api_response")
    return raw


def _idempotency_headers(args: argparse.Namespace) -> dict[str, str]:
    key = getattr(args, "idempotency_key", None) or secrets.token_hex(16)
    return {"Accept": "application/json", "Idempotency-Key": key}


def _with_operation_id(
    body: dict[str, object], operation_id: str | None
) -> dict[str, object]:
    if operation_id is None or not _OPERATION_ID.fullmatch(operation_id):
        raise CLIError("invalid_api_response")
    body_id = body.get("operation_id")
    if body_id is not None and body_id != operation_id:
        raise CLIError("invalid_api_response")
    return {**body, "operation_id": operation_id}


def _get(args: argparse.Namespace, target: str) -> dict[str, object]:
    value, _ = _http_request(args, "GET", target)
    return value


def _api_file_limit(args: argparse.Namespace) -> int:
    status = _get(args, "/v1/status")
    maximum = status.get("max_file_bytes")
    if (
        isinstance(maximum, bool)
        or not isinstance(maximum, int)
        or not 1 <= maximum <= MAX_FILE_BYTES
    ):
        raise CLIError("invalid_api_response")
    return maximum


def _ensure_new_destination(destination: str) -> Path:
    target = Path(destination)
    try:
        if os.path.lexists(target):
            raise CLIError("destination_exists")
    except CLIError:
        raise
    except OSError:
        raise CLIError("output_unavailable") from None
    return target


def _write_export(destination: str, content: bytes) -> None:
    target = _ensure_new_destination(destination)
    descriptor: int | None = None
    staged_path: str | None = None
    try:
        descriptor, staged_path = tempfile.mkstemp(
            prefix=".lifeweave-export-", dir=os.fspath(target.parent or Path("."))
        )
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            if os.name == "posix":
                os.fchmod(stream.fileno(), 0o600)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(staged_path, target)
        except FileExistsError:
            raise CLIError("destination_exists") from None
    except CLIError:
        raise
    except OSError:
        raise CLIError("output_unavailable") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if staged_path is not None:
            try:
                os.unlink(staged_path)
            except FileNotFoundError:
                pass
            except OSError:
                pass


def _post_json(
    args: argparse.Namespace, target: str, payload: object
) -> dict[str, object]:
    value, operation_id = _http_request(
        args,
        "POST",
        target,
        body=_json_bytes(payload),
        headers={
            **_idempotency_headers(args),
            "Content-Type": "application/json",
        },
    )
    return _with_operation_id(value, operation_id)


def _dispatch(args: argparse.Namespace) -> dict[str, object]:
    if args.command == "documents":
        if args.document_command == "import":
            from pydantic import ValidationError

            from lifecontext.api.models import ProvenanceInput, _basename

            if (args.source_id is None) != (args.source_version is None):
                raise CLIError("invalid_request")
            filename = args.filename or (Path(args.input).name if args.input != "-" else None)
            try:
                if filename is None:
                    raise ValueError
                _basename(filename)
                provenance = ProvenanceInput.model_validate_json(args.provenance).model_dump(exclude_none=True)
                provenance_header = json.dumps(provenance, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
                if len(provenance_header) > 4096 or len(args.mime) > 255 or not re.fullmatch(r"[a-z0-9][a-z0-9.+_-]*/[a-z0-9][a-z0-9.+_-]*", args.mime):
                    raise ValueError
            except (ValueError, UnicodeError, ValidationError):
                raise CLIError("invalid_request") from None
            content = _read_input(args.input, maximum=MAX_DOCUMENT_BYTES)
            if not content:
                raise CLIError("invalid_request")
            headers = {
                **_idempotency_headers(args),
                "Content-Type": "application/octet-stream",
                "X-Document-Filename": quote(filename, safe=""),
                "X-Document-Mime": args.mime,
                "X-Document-Provenance": provenance_header,
            }
            if args.source_id is not None:
                headers["X-Document-Source-ID"] = args.source_id
                headers["X-Document-Source-Version"] = str(args.source_version)
            if args.no_vision:
                headers["X-Document-Vision"] = "off"
            value, operation_id = _http_request(args, "POST", "/v1/documents", body=content, headers=headers, max_request_bytes=MAX_DOCUMENT_BYTES)
            return _with_operation_id(value, operation_id)
        if args.document_command == "list":
            if args.limit > 4096:
                raise CLIError("invalid_request")
            return _get(args, f"/v1/documents?limit={args.limit}")
        if args.document_command == "retry":
            if args.vision is not None and not args.rerun_extraction:
                raise CLIError("invalid_request")
            body = {"rerun_extraction": args.rerun_extraction}
            if args.vision is not None:
                body["vision"] = args.vision
            return _post_json(args, f"/v1/documents/operations/{args.operation_id}/retry", body)
        target = f"/v1/documents/{quote(args.source_id, safe='')}"
        query = "?" + urlencode({"version": args.version}) if args.version is not None else ""
        if args.document_command == "show":
            return _get(args, target + query)
        _ensure_new_destination(args.destination)
        status, headers, raw = _http_exchange(args, "GET", target + "/original" + query, headers={"Accept": "application/octet-stream"}, max_response_bytes=MAX_DOCUMENT_BYTES)
        if not 200 <= status < 300:
            raise CLIError(_typed_api_error(raw))
        checksum = hashlib.sha256(raw).hexdigest()
        if (
            headers.get("content-type", "").split(";", 1)[0].strip().casefold() != "application/octet-stream"
            or headers.get("etag") != f'"{checksum}"'
            or headers.get("content-length") != str(len(raw))
            or not raw
        ):
            raise CLIError("invalid_api_response")
        _write_export(args.destination, raw)
        return {"source_id": args.source_id, "size_bytes": len(raw), "sha256": checksum}
    if args.command == "categories":
        name = args.category_command
        if name == "list":
            return _get(args, "/v1/categories")
        if name == "proposals":
            return _get(args, "/v1/categories/proposals?" + urlencode({"state": args.state}))
        if name == "propose":
            from pydantic import ValidationError

            from lifecontext.api.models import CategoryProposeRequest

            try:
                payload = CategoryProposeRequest.model_validate_json(_read_input(args.input, maximum=MAX_REQUEST_BYTES)).model_dump()
            except (ValueError, ValidationError):
                raise CLIError("invalid_request") from None
            return _post_json(args, "/v1/categories/proposals", payload)
        payload = {}
        if name == "merge":
            payload = {"target_category_id": args.target_category_id, "expected_source_revision": args.expected_source_revision, "expected_target_revision": args.expected_target_revision}
        else:
            payload["expected_revision"] = args.expected_revision
        if name in {"approve", "redirect", "reassign"}:
            payload["category_id"] = args.category_id
        if name == "approve":
            payload["budget_override"] = args.budget_override
        elif name == "rename":
            payload["name"] = args.name
            if args.description is not None:
                payload["description"] = args.description
        elif name == "alias":
            payload["alias"] = args.alias
        elif name == "reassign":
            payload["source_id"] = args.source_id
        if name in {"approve", "reject", "redirect"}:
            target = f"/v1/categories/proposals/{args.proposal_id}/{name}"
        elif name == "reassign":
            target = "/v1/categories/reassign"
        else:
            target = f"/v1/categories/{args.category_id}/{name}"
        return _post_json(args, target, payload)
    if args.command == "wiki":
        name = args.wiki_command
        if name in {"list", "status", "lint"}:
            return _get(args, "/v1/wiki" + ("" if name == "list" else "/" + name))
        if name == "reviews":
            if args.review_command == "resolve":
                return _post_json(args, f"/v1/wiki/reviews/{args.review_id}/resolve", {"note": args.note})
            query = {"page": args.page, "state": args.state}
            return _get(args, "/v1/wiki/reviews?" + urlencode({key: value for key, value in query.items() if value is not None}))
        if name == "rebuild":
            if args.version is not None and args.source_id is None:
                raise CLIError("invalid_request")
            return _post_json(args, "/v1/wiki/rebuild", {"source_id": args.source_id, "version": args.version})
        target = f"/v1/wiki/{args.page_id}"
        if name == "history":
            return _get(args, target + "/history")
        if args.revision is not None:
            target += "?" + urlencode({"revision": args.revision})
        return _get(args, target)
    if args.command == "brief":
        query = urlencode({key: value for key, value in (("date", args.date), ("since", args.since)) if value is not None})
        if not args.project:
            return _get(args, "/v1/brief" + ("?" + query if query else ""))
        if args.since is not None:
            raise CLIError("invalid_request")
        value, _ = _http_request(args, "POST", "/v1/brief/project" + ("?" + query if query else ""))
        return value
    if args.command == "status":
        return _get(args, "/v1/status")
    if args.command == "reconcile":
        if args.limit > 4096:
            raise CLIError("invalid_request")
        value, _ = _http_request(args, "POST", f"/v1/projections/reconcile?limit={args.limit}")
        return value
    if args.command == "retain":
        content = _read_input(args.input)
        return _retain(args, content, None if args.input == "-" else Path(args.input).name)
    if args.command in {"recall", "reflect"}:
        if len(args.query.encode("utf-8")) > MAX_QUERY_BYTES:
            raise CLIError("body_too_large")
        payload: dict[str, object] = {"query": args.query, "max_tokens": args.max_tokens, "budget": args.budget}
        if args.max_results is not None:
            payload["max_results"] = args.max_results
        return _get_json_post(args, f"/v1/{args.command}", payload)
    if args.command == "evidence":
        target = f"/v1/evidence/{quote(args.source_id, safe='')}?" + urlencode({
            "version": args.version, "include_content": str(args.include_content).lower(),
        })
        return _get(args, target)
    if args.command == "curate":
        payload: dict[str, object] = {"action": args.action, "source_id": args.source_id}
        if args.action in {"correct", "prepare_forget", "forget", "prepare_delete", "delete"}:
            if args.expected_version is None:
                raise CLIError("invalid_request")
            payload["expected_version"] = args.expected_version
        elif args.expected_version is not None:
            payload["expected_version"] = args.expected_version
        if args.action == "correct":
            raw = _read_input(args.input)
            try:
                payload["content"] = raw.decode("utf-8")
            except UnicodeError:
                raise CLIError("invalid_source_text") from None
        if args.action in {"forget", "delete"}:
            if args.prepared_operation_id is None:
                raise CLIError("invalid_request")
            payload["prepared_operation_id"] = args.prepared_operation_id
        return _post_json(args, "/v1/curation", payload)
    if args.command == "relationships":
        if args.relationship_command == "list":
            query = {
                "edge_id": args.edge_id,
                "source_id": args.source_id,
                "status": args.status,
                "max_results": args.max_results,
            }
            target = "/v1/relationships"
            encoded = urlencode({key: value for key, value in query.items() if value is not None})
            return _get(args, target + ("?" + encoded if encoded else ""))
        if args.relationship_command == "scan":
            payload: dict[str, object] = {}
            if args.source_id is not None:
                payload["source_id"] = args.source_id
            if args.max_results is not None:
                payload["max_results"] = args.max_results
            return _post_json(args, "/v1/relationships/scan", payload)
        target = f"/v1/relationships/{quote(args.edge_id, safe='')}/decision"
        return _post_json(args, target, {
            "decision": args.decision,
            "expected_revision": args.expected_revision,
        })
    if args.command == "export":
        target = _ensure_new_destination(args.destination)
        maximum = _api_file_limit(args)
        body = _json_bytes({"source_ids": args.source_ids or None})
        raw = _http_binary_response(
            args,
            "/v1/exports",
            body=body,
            headers={
                **_idempotency_headers(args),
                "Content-Type": "application/json",
            },
            maximum=maximum,
        )
        _write_export(os.fspath(target), raw)
        return {
            "size_bytes": len(raw),
            "bundle_sha256": hashlib.sha256(raw).hexdigest(),
        }
    if args.command == "import":
        maximum = _api_file_limit(args)
        raw = _read_input(args.input, maximum=maximum)
        if not raw:
            raise CLIError("invalid_request")
        value, operation_id = _http_request(
            args,
            "POST",
            "/v1/imports",
            body=raw,
            headers={
                **_idempotency_headers(args),
                "Content-Type": "application/octet-stream",
            },
            max_request_bytes=maximum,
        )
        return _with_operation_id(value, operation_id)
    if args.command == "operations":
        operation_id = quote(args.operation_id, safe="")
        return _get(args, f"/v1/operations/{operation_id}")
    raise CLIError("invalid_request")


def _retain(
    args: argparse.Namespace, content: bytes, basename: str | None
) -> dict[str, object]:
    """POST one source version's bytes; shared by `retain` and the MCP server."""
    if not content:
        raise CLIError("invalid_request")
    if len(content) > MAX_TEXT_BYTES:
        raise CLIError("body_too_large")
    target = "/v1/retain?" + urlencode({
        "source_id": args.source_id, "source_version": args.source_version,
    })
    headers = {
        **_idempotency_headers(args),
        "Accept": "application/json",
        "Content-Type": "application/octet-stream",
    }
    if basename is not None and basename not in {"", ".", ".."} and len(basename.encode("utf-8")) <= 255:
        headers["X-Source-Basename"] = basename
    value, operation_id = _http_request(args, "POST", target, body=content, headers=headers)
    return _with_operation_id(value, operation_id)


def _get_json_post(
    args: argparse.Namespace, target: str, payload: object
) -> dict[str, object]:
    value, _ = _http_request(
        args,
        "POST",
        target,
        body=_json_bytes(payload),
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )
    return value


def _coverage_line(coverage: dict[str, object]) -> str:
    unit, total = coverage.get("unit"), coverage.get("total")
    if isinstance(unit, str) and isinstance(total, int):
        line = f"Coverage: {coverage.get('resolved')} of {total} {unit}s resolved"
        detail = ", ".join(
            f"{coverage[key]} {label}"
            for key, label in (("text", "text layer"), ("ocr", "OCR"), ("vision", "model-read"), ("blank", "blank"))
            if coverage.get(key)
        )
        if detail:
            line += f" ({detail})"
    else:
        line = "Coverage: " + ("complete" if coverage.get("complete") else "incomplete")
    unresolved, partial = coverage.get("unresolved"), coverage.get("partial")
    if isinstance(unresolved, list) and unresolved:
        line += "; unresolved: " + ", ".join(map(str, unresolved))
    if isinstance(partial, list) and partial:
        shown = ", ".join(map(str, partial[:20]))
        line += f"; {len(partial)} with unread parts: {shown}" + (", ..." if len(partial) > 20 else "")
    return line


def _human_result(value: dict[str, object]) -> str:
    lines: list[str] = []
    operation_id = value.get("operation_id")
    if isinstance(operation_id, str):
        lines.append(f"Durable operation ID: {operation_id}")
    if "ready" in value:
        lines.append(f"Ready: {'yes' if value['ready'] else 'no'}")
    if "source_id" in value and "source_version" in value:
        lines.append(f"Source: {value['source_id']} v{value['source_version']}")
    if "state" in value:
        lines.append(f"State: {value['state']}")
    if isinstance(value.get("text"), str):
        lines.append(str(value["text"]))
    if isinstance(value.get("coverage"), dict):
        lines.append(_coverage_line(value["coverage"]))
    results = value.get("results")
    if isinstance(results, list):
        lines.append(f"Results: {len(results)}")
        for item in results:
            if isinstance(item, dict):
                evidence = item.get("evidence")
                source = ""
                if isinstance(evidence, dict):
                    source = f" ({evidence.get('source_id')} v{evidence.get('source_version')})"
                lines.append(f"- {item.get('text', '')}{source}")
    relationships = value.get("relationships", value.get("edges"))
    if isinstance(relationships, list):
        lines.append(f"Relationships: {len(relationships)}")
        for item in relationships:
            if isinstance(item, dict):
                lines.append(f"- {item.get('from', '?')} -> {item.get('to', '?')} [{item.get('status', '?')}]" )
    findings, counts = value.get("findings"), value.get("counts")
    if isinstance(findings, list) and isinstance(counts, dict):
        lines.append(f"Findings: {counts.get('error')} errors, {counts.get('warning')} warnings, {counts.get('info')} info")
        for item in findings:
            if isinstance(item, dict):
                target = item.get("page_id") or item.get("source_id") or "-"
                detail = f" ({item['detail']})" if item.get("detail") else ""
                lines.append(f"- [{item.get('severity')}] {item.get('code')} {target}{detail}: {item.get('hint')}")
    if not lines:
        for key, child in value.items():
            rendered = json.dumps(child, ensure_ascii=False, sort_keys=True)
            lines.append(f"{key.replace('_', ' ').capitalize()}: {rendered}")
    else:
        hidden = {"operation_id", "ready", "source_id", "source_version", "state", "text", "results", "relationships", "edges", "coverage", "findings", "counts"}
        for key, child in value.items():
            if key not in hidden:
                rendered = json.dumps(child, ensure_ascii=False, sort_keys=True)
                lines.append(f"{key.replace('_', ' ').capitalize()}: {rendered}")
    return "\n".join(lines)


def _print_success(args: argparse.Namespace, value: dict[str, object]) -> None:
    if args.as_json:
        print(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    else:
        print(_human_result(value))


def _print_error(args: argparse.Namespace, code: str) -> None:
    if args.as_json:
        print(json.dumps({"error": code}, sort_keys=True, separators=(",", ":")))
        return
    if code == "api_unavailable":
        message = (
            f"LifeContext API is unavailable at 127.0.0.1:{args.port}; "
            f"start it with `lifecontext serve --port {args.port}`."
        )
    elif code in {"input_unavailable", "body_too_large", "invalid_source_text", "token_unavailable", "vault_missing"}:
        message = {
            "input_unavailable": "Unable to read the selected source input.",
            "body_too_large": "Request or response exceeded the allowed size.",
            "invalid_source_text": "Source input must be valid UTF-8 text.",
            "token_unavailable": ("No LifeContext API token for this user: the service creates it when it starts "
                                  "(`lifecontext serve`) under the same HOME."),
            "vault_missing": ("The vault's Journal folder does not exist: create it with mode 0700, "
                              "or set `vault` in ~/.lifeweave/config.yaml."),
        }[code]
    else:
        message = f"LifeContext request failed: {code.replace('_', ' ')}."
    print(message, file=sys.stderr)


def _preflight(args: argparse.Namespace) -> int:
    from lifecontext.config import ConfigError, load_settings
    from lifecontext.hindsight import HindsightStartupError, preflight, retain_smoke

    try:
        settings = load_settings()
    except ConfigError:
        report = {"ready": False, "error": "invalid_config"}
    else:
        try:
            report = retain_smoke(settings) if args.retain_smoke else preflight(settings)
        except HindsightStartupError as exc:
            report = {"ready": False, "error": exc.code}
        except Exception:
            report = {"ready": False, "error": "daemon_unavailable"}
        report["vision"] = _vision_report(settings, probe=args.vision_probe)
    print(json.dumps(report, sort_keys=True))
    return 0 if report.get("ready") else 1


def _vision_report(settings, *, probe: bool) -> dict[str, object]:
    """Vision status for preflight; only --vision-probe makes a network call."""
    import asyncio

    from lifecontext.hindsight import HindsightStartupError
    from lifecontext.vision import VisionUnavailable, vision_reader

    try:
        reader = vision_reader(settings)
    except HindsightStartupError as exc:
        return {"configured": True, "status": "blocked", "error": exc.code}
    if reader is None:
        return {"configured": False, "status": "off"}
    report: dict[str, object] = {"configured": True, **reader.config.public()}
    if not probe:
        return {**report, "status": "not_probed"}
    try:
        result = asyncio.run(reader.probe())
    except VisionUnavailable:
        return {**report, "status": "unavailable"}
    return {**report, "status": "ready" if result == "ok" else "blocked",
            **({} if result == "ok" else {"error": result})}


def _build_application():
    from pathlib import Path
    from urllib.parse import urlsplit

    from lifecontext.application import JournalApplication
    from lifecontext.config import load_settings
    from lifecontext.hindsight import (
        PROFILE,
        _load_runtime,
        document_compiler,
        engine_token,
        preflight,
    )
    from lifecontext.hindsight_adapter import LifeContextHindsightAdapter
    from lifecontext.paths import LifeContextPaths
    from lifecontext.vault import ManagedVault, VaultError, default_journal_paths
    from lifecontext.vision import vision_reader

    settings = load_settings()
    paths = LifeContextPaths.current()
    vault_settings = settings.get("vault")
    if vault_settings:
        root = Path(vault_settings["root"])
        journal = Path(vault_settings["journal_path"])
    else:
        root, journal = default_journal_paths(paths.home)
    # Say what is missing before the Hindsight profile is started.
    if not journal.is_dir():
        raise VaultError("vault_missing")
    startup = preflight(settings)
    vision = vision_reader(settings)
    runtime = _load_runtime()
    client = None

    def connect():
        return runtime.Hindsight(
            base_url=startup["base_url"], api_key=engine_token(paths), timeout=HINDSIGHT_CLIENT_TIMEOUT
        )

    try:
        client = connect()
        application = JournalApplication(
            paths=paths,
            hindsight=LifeContextHindsightAdapter(
                client, bank_id=PROFILE, port=urlsplit(startup["base_url"]).port, reconnect=connect),
            vault=ManagedVault(root, journal),
            compiler=document_compiler(settings),
            vision=vision,
        )
        return application
    except Exception:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
        raise


def _serve(args: argparse.Namespace) -> int:
    from lifecontext.api.app import run as api_run
    from lifecontext.config import ConfigError
    from lifecontext.hindsight import HindsightStartupError, process_environment
    from lifecontext.vault import VaultError

    application = None
    try:
        # The service imports Hindsight's LLM layer too (litellm, tiktoken).
        os.environ.update(process_environment(LifeContextPaths.current()))
        application = _build_application()
        api_run(application, host="127.0.0.1", port=args.port)
        return 0
    except ConfigError:
        raise CLIError("invalid_config") from None
    except HindsightStartupError as exc:
        raise CLIError(exc.code) from None
    except VaultError as exc:
        raise CLIError(exc.code) from None
    except (OSError, ValueError):
        raise CLIError("server_start_failed") from None
    except KeyboardInterrupt:
        return 0
    except Exception:
        raise CLIError("server_start_failed") from None
    finally:
        if application is not None:
            close = getattr(application, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
            client = getattr(getattr(application, "hindsight", None), "_client", None)
            close_client = getattr(client, "close", None)
            if callable(close_client):
                try:
                    close_client()
                except Exception:
                    pass


def _ask(question: str, default: str) -> str:
    sys.stderr.write(f"{question} [{default}]: " if default else f"{question}: ")
    sys.stderr.flush()
    return sys.stdin.readline().rstrip("\r\n")


def _ask_secret(question: str) -> str:
    import getpass

    return getpass.getpass(question + ": ", stream=sys.stderr)


def _init(args: argparse.Namespace) -> tuple[dict[str, object], str, int]:
    from lifecontext import initialize

    options = initialize.InitOptions(
        provider=args.provider, model=args.model, base_url=args.base_url, vault=args.vault,
        timezone=args.timezone, api_key_stdin=args.api_key_stdin, force=args.force, replace_key=args.replace_key,
        interactive=not (args.non_interactive or args.api_key_stdin) and sys.stdin.isatty(),
    )
    try:
        report = initialize.run_init(
            options, ask=_ask, ask_secret=_ask_secret,
            key_stream=sys.stdin.buffer if args.api_key_stdin else None,
        )
    except initialize.InitError as exc:
        raise CLIError(exc.code) from None
    except OSError:
        raise CLIError("setup_failed") from None
    return report, initialize.render(report), 0


def _doctor(args: argparse.Namespace) -> tuple[dict[str, object], str, int]:
    from lifecontext import doctor

    report, code = doctor.run_doctor(port=args.port)
    return report, doctor.render(report), code


def _skills(args: argparse.Namespace) -> tuple[dict[str, object], str, int]:
    from lifecontext import skills

    home = LifeContextPaths.current().home
    agents = skills.select_agents(home, args.agent or ("all" if args.skills_command == "remove" else None))
    try:
        if args.skills_command == "install":
            report = skills.install(home, agents, dry_run=args.dry_run)
        elif args.skills_command == "check":
            report = skills.check(home, agents)
        else:
            report = skills.remove(home, agents, dry_run=args.dry_run)
    except skills.SkillsError as exc:
        raise CLIError(exc.code) from None
    return report, skills.render(report), 1 if report.get("ok") is False else 0


def _service(args: argparse.Namespace) -> tuple[dict[str, object], str, int]:
    import platform

    from lifecontext import service

    # Units live where the service manager reads them, under the account's home; a
    # different HOME for the data is passed to the service instead.
    home = service.account_home()
    data_home = LifeContextPaths.current().home
    system = platform.system()
    try:
        if args.service_command == "install":
            report = service.install(
                system, home, executable=service.lifecontext_executable(), port=args.port,
                maintenance=args.maintenance, capture=args.capture, dry_run=args.dry_run, start=not args.no_start,
                data_home=None if os.path.realpath(data_home) == os.path.realpath(home) else data_home)
        elif args.service_command == "uninstall":
            report = service.uninstall(system, home, maintenance_only=args.maintenance, capture_only=args.capture,
                                       dry_run=args.dry_run)
        else:
            report = service.status(system, home, data_home=data_home)
    except service.ServiceError as exc:
        raise CLIError(exc.code) from None
    return report, service.render(report), 0


# Failures that say nothing about the note: the API is unreachable, or the model provider is.
_TRANSIENT_API_ERRORS = frozenset({"api_unavailable", "token_unavailable", "unauthorized", "provider_unavailable"})


class _CaptureApi:
    """Capture's API calls.

    ``False`` or ``None`` mean "not done yet, try again": the API is unreachable or answered 5xx.
    Any other refusal of the note raises ``capture.Rejected`` with the API's error code.
    """

    def __init__(self, args: argparse.Namespace):
        self.args = args

    @staticmethod
    def _transient(exc: CLIError) -> bool:
        from lifecontext.capture import Rejected

        if exc.code in _TRANSIENT_API_ERRORS or (exc.status or 0) >= 500:
            return False
        raise Rejected(exc.code) from None

    def _completed(self, args: argparse.Namespace, receipt: dict[str, object]) -> bool:
        from lifecontext.capture import Rejected

        operation = _get(args, "/v1/operations/" + quote(str(receipt["operation_id"]), safe=""))
        state = operation.get("state")
        if state in {"permanent_failed", "cancelled"}:
            raise Rejected(f"operation_{state}")
        return state == "completed"

    def retain(self, source_id: str, raw: bytes, key: str) -> bool:
        args = argparse.Namespace(**{**vars(self.args), "source_id": source_id, "source_version": 1,
                                     "idempotency_key": key})
        try:
            return self._completed(args, _retain(args, raw, None))
        except CLIError as exc:
            return self._transient(exc)

    def correct(self, source_id: str, expected_version: int, raw: bytes, key: str) -> bool:
        args = argparse.Namespace(**{**vars(self.args), "idempotency_key": key})
        try:
            receipt = _post_json(args, "/v1/curation", {
                "action": "correct", "source_id": source_id, "expected_version": expected_version,
                "content": raw.decode("utf-8")})
            return self._completed(args, receipt)
        except CLIError as exc:
            return self._transient(exc)

    def evidence(self, source_id: str, version: int) -> dict[str, object] | None:
        try:
            return _get(self.args, f"/v1/evidence/{quote(source_id, safe='')}?" + urlencode({
                "version": version, "include_content": "false"}))
        except CLIError as exc:
            self._transient(exc)
            return None


def _capture(args: argparse.Namespace) -> tuple[dict[str, object], str, int]:
    from lifecontext import capture
    from lifecontext.config import ConfigError, load_settings

    paths = LifeContextPaths.current()
    try:
        inbox = Path(args.inbox).expanduser() if args.inbox else capture.default_inbox(load_settings(), paths)
        report = capture.run_once(inbox, paths, _CaptureApi(args))
    except ConfigError:
        raise CLIError("invalid_config") from None
    except capture.CaptureError as exc:
        raise CLIError(exc.code) from None
    return report, capture.render(report), 0


def _llm(args: argparse.Namespace) -> tuple[dict[str, object], str, int]:
    import platform

    from lifecontext import initialize, service

    try:
        if args.llm_command == "show":
            report = initialize.show_llm()
            return report, initialize.render_llm(report), 0
        if (args.provider is None and args.model is None and args.base_url is None and not args.api_key_stdin
                and not os.environ.get(initialize.KEY_FILE_VARIABLE)):
            raise CLIError("nothing_to_change")
        report = initialize.set_llm(
            initialize.InitOptions(provider=args.provider, model=args.model, base_url=args.base_url,
                                   api_key_stdin=args.api_key_stdin),
            key_stream=sys.stdin.buffer if args.api_key_stdin else None)
    except initialize.InitError as exc:
        raise CLIError(exc.code) from None
    except OSError:
        raise CLIError("setup_failed") from None
    restart = service.restart_if_running(platform.system())
    text = "\n".join([initialize.render_llm(report), service.render_restart(restart)])
    return {**report, "service": restart}, text, 0


def _configured_today() -> date:
    from zoneinfo import ZoneInfo

    from lifecontext.config import ConfigError, load_settings

    try:
        zone = load_settings().get("timezone")
    except ConfigError:
        zone = None
    return datetime.now(ZoneInfo(zone)).date() if zone else datetime.now().astimezone().date()


def _maintain(args: argparse.Namespace) -> tuple[dict[str, object], str, int]:
    """Model-free upkeep through the running API; rebuilds only when asked for."""
    from datetime import UTC, timedelta

    from lifecontext.paths import atomic_write

    def now() -> str:
        return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    started = now()
    today = date.fromisoformat(args.date) if args.date else _configured_today()
    steps: dict[str, object] = {}
    failed: list[str] = []
    lint: dict[str, object] = {}
    try:
        steps["reconcile"], _ = _http_request(args, "POST", "/v1/projections/reconcile?limit=4096")
    except CLIError as exc:
        steps["reconcile"] = {"error": exc.code}
        failed.append("reconcile")
    try:
        lint = _get(args, "/v1/wiki/lint")
        steps["lint"] = {**dict(lint.get("counts") or {}), "omitted": lint.get("omitted")}
    except CLIError as exc:
        steps["lint"] = {"error": exc.code}
        failed.append("lint")
    findings = [item for item in lint.get("findings") or [] if isinstance(item, dict)
                and isinstance(item.get("source_id"), str)]
    for step, limit, selected in (
        ("retry_wiki", args.retry_wiki, lambda item: item.get("code") == "stage_failed"
         and str(item.get("detail") or "").startswith("wiki:")),
        ("rebuild_stale", args.rebuild_stale, lambda item: item.get("code") == "page_stale"),
    ):
        if not limit:
            continue
        sources = list(dict.fromkeys(item["source_id"] for item in findings if selected(item)))[:limit]
        result = {"attempted": len(sources), "completed": 0, "failed": 0}
        for source_id in sources:
            # One key per source and day: a second run that day replays instead of paying again.
            key = f"maintain-{today.isoformat()}-{step.replace('_', '-')}-{source_id}"
            try:
                value = _post_json(argparse.Namespace(**{**vars(args), "idempotency_key": key}),
                                   "/v1/wiki/rebuild", {"source_id": source_id, "version": None})
                done = value.get("state") == "completed"
            except CLIError:
                done = False
            result["completed" if done else "failed"] += 1
        steps[step] = result
        if result["failed"]:
            failed.append(step)
    briefs: dict[str, object] = {}
    for day in (today - timedelta(days=1), today):
        try:
            value, _ = _http_request(args, "POST", "/v1/brief/project?" + urlencode({"date": day.isoformat()}))
            briefs[day.isoformat()] = value.get("projection")
        except CLIError as exc:
            briefs[day.isoformat()] = {"error": exc.code}
            failed.append("briefs")
    steps["briefs"] = briefs
    ok = not failed
    report = {"date": today.isoformat(), "ok": ok, "failed_steps": sorted(set(failed)), "steps": steps}
    try:
        atomic_write(LifeContextPaths.current().maintenance_record_path, json.dumps(
            {**report, "started_at": started, "finished_at": now()}, sort_keys=True).encode())
    except OSError:
        report["record"] = "not_written"
    lines = [f"Maintenance for {report['date']}: {'ok' if ok else 'failed'}"]
    lines += [f"- {name.replace('_', ' ')}: {json.dumps(value, sort_keys=True)}" for name, value in steps.items()]
    return report, "\n".join(lines), 0 if ok else 1


def _backup(args: argparse.Namespace) -> tuple[dict[str, object], str, int]:
    from lifecontext import backup

    try:
        if args.command == "backup":
            report = backup.backup(destination=args.destination, port=args.port, stop_service=args.stop_service)
        elif args.command == "hindsight":
            report = backup.stop_hindsight(port=args.port)
        else:
            report = backup.restore(args.archive, port=args.port, vault=args.vault)
    except backup.BackupError as exc:
        raise CLIError(exc.code) from None
    except OSError:
        raise CLIError(f"{args.command}_failed") from None
    # The archive is written, but a service that did not come back is worth a failing exit.
    return report, backup.render(report), 1 if report.get("warnings") else 0


_LOCAL_COMMANDS = {"init": _init, "doctor": _doctor, "skills": _skills, "service": _service,
                   "llm": _llm, "capture": _capture,
                   "maintain": _maintain, "backup": _backup, "restore": _backup, "hindsight": _backup}


def _run_local(args: argparse.Namespace) -> int:
    """Commands that work on this HOME directly rather than through the API."""
    try:
        report, text, code = _LOCAL_COMMANDS[args.command](args)
    except CLIError as exc:
        if args.as_json:
            print(json.dumps({"error": exc.code}, sort_keys=True, separators=(",", ":")))
        else:
            print(f"lifecontext {args.command} failed: {exc.code.replace('_', ' ')}.", file=sys.stderr)
        return 1
    if args.as_json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    else:
        print(text)
    return code


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command in _LOCAL_COMMANDS:
        return _run_local(args)
    if args.command == "preflight":
        return _preflight(args)
    if args.command == "serve":
        try:
            return _serve(args)
        except CLIError as exc:
            _print_error(args, exc.code)
            return 1
    if args.command == "mcp":
        if args.import_dirs and not args.allow_writes:
            parser.error("--import-dir requires --allow-writes")
        from lifecontext.mcp_server import run as run_mcp

        return run_mcp(port=args.port, allow_writes=args.allow_writes, import_dirs=args.import_dirs)

    try:
        value = _dispatch(args)
        failing = _lint_failures(args, value)
        _print_success(args, value)
    except CLIError as exc:
        _print_error(args, exc.code)
        return 1
    return LINT_FAILED_EXIT if failing else 0


def _lint_failures(args: argparse.Namespace, value: dict[str, object]) -> int:
    """Findings at or above ``--fail-on``; zero without it."""
    threshold = getattr(args, "fail_on", None)
    if threshold is None:
        return 0
    counts = value.get("counts")
    severities = ("error",) if threshold == "error" else ("error", "warning")
    if not isinstance(counts, dict) or any(type(counts.get(name)) is not int for name in severities):
        raise CLIError("invalid_api_response")
    return sum(counts[name] for name in severities)


if __name__ == "__main__":
    raise SystemExit(main())
