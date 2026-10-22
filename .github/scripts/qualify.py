"""Synthetic qualification scenario for install.sh and the Docker image.

Used by .github/workflows/qualify.yml with the runner's Python (standard library
only; `fixtures` also needs Pillow). Each subcommand runs one part of the
scenario through a `lifecontext` command line given with --cli (the installed
command, `env HOME=... lifecontext --port N`, or bin/lifecontext-docker) and
records checks in the evidence file named by $QUALIFY_EVIDENCE:

    qualify.py init --job native
    qualify.py fixtures --out DIR
    qualify.py capture --cli lifecontext --fixtures DIR --state FILE
    qualify.py verify --cli lifecontext --state FILE --label installed --wiki --ukrainian
    qualify.py mcp --cli lifecontext
    qualify.py llm --cli lifecontext
    qualify.py probe --cli lifecontext --label public
    qualify.py finalize

Evidence holds versions, timings, sizes and check outcomes only: never document
text, keys, endpoints or raw command output. A failed check makes the
subcommand exit 1.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import queue
import re
import shlex
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

FIXTURES = {
    "journal": {
        "source_id": "qualify_journal_01",
        "file": "seed-tray-check.md",
        "text": (
            "# Seed tray check\n"
            "Entry date: 2026-10-19 (UTC).\n"
            "\n"
            "Synthetic qualification entry with no personal data. The runner watered seed tray 3 "
            "and counted two new sprouts.\n"
        ),
    },
    "en": {
        "file": "corvalis-point-lighthouse.md",
        "mime": "text/markdown",
        "text": (
            "# Corvalis Point lighthouse maintenance note\n"
            "\n"
            "Synthetic qualification document about a fictional place. It contains no personal data.\n"
            "\n"
            "The spare Fresnel lens of the Corvalis Point lighthouse is kept in storage locker K-17 "
            "on the second landing.\n"
        ),
        "query": "Where is the spare Fresnel lens of the Corvalis Point lighthouse kept?",
        "answer": "K-17",
    },
    "uk": {
        "file": "biblioteka-zelenyi-yar.md",
        "mime": "text/markdown",
        "text": (
            "# Бібліотека в Зеленому Яру\n"
            "\n"
            "Синтетичний документ для перевірки встановлення. Він описує вигадане місце і не містить "
            "персональних даних.\n"
            "\n"
            "Ключ від архівної кімнати бібліотеки в Зеленому Яру зберігається у скриньці номер 42 "
            "біля входу.\n"
            "\n"
            "Бібліотека відкривається о 9:15 щосуботи.\n"
        ),
        "query": "Де зберігається ключ від архівної кімнати бібліотеки в Зеленому Яру?",
        "answer": "42",
    },
    "image": {
        "file": "synthetic-receipt-7731.png",
        "mime": "image/png",
        "lines": ("SYNTHETIC RECEIPT 7731", "TOTAL 418 EUR", "NO PERSONAL DATA"),
        "query": "What is the total on synthetic receipt 7731?",
        "answer": "418",
    },
}
DOCUMENTS = ("en", "uk", "image")
STAGES = ("custody", "extraction", "indexing", "wiki", "projection")
TERMINAL_FAILURES = {"failed", "blocked", "conflict"}
READ_TOOLS = {"status", "recall", "evidence", "wiki_list", "wiki_show", "wiki_lint", "brief"}
WRITE_TOOLS = {"retain", "documents_import", "wiki_reviews_resolve"}
_INSTALLED = ("doctor_installed", "journal_retain", "journal_readback", "document_import_en",
              "recall_cites_document_en_installed", "evidence_readback_en_installed",
              "journal_readback_installed")
_SCENARIO = ("document_import_uk", "document_import_image", "ocr_image")
# Providers whose CI route needs a base URL (the gateway); any other uses its own API.
GATEWAY_PROVIDERS = {"openai_compatible"}
_PUBLIC = ("install_sh_public", "release_signature_checked", "installed_version", "llm_settings",
           "service_running", "service_ready", "doctor_public", "model_probe_public", "image_pull", "image_signature")


def _verified(label: str, keys: tuple[str, ...], wiki: bool) -> tuple[str, ...]:
    names = [f"journal_readback_{label}"]
    for key in keys:
        names += [f"recall_cites_document_{key}_{label}", f"evidence_readback_{key}_{label}"]
    if wiki:
        names += [f"wiki_literal_quotes_{label}", f"wiki_lint_{label}", f"brief_{label}"]
    return tuple(names)


EXPECTED = {
    "native": (
        "systemd_user_session", "install_sh", "install_sh_next_steps", "llm_settings", "service_running",
        "service_ready", *_INSTALLED, *_SCENARIO,
        *_verified("installed", ("en", "uk", "image"), True), "skills_install_check", "mcp_tools", "backup",
        "service_ready_after_backup", "restore_fresh_home", "restored_service_ready",
        *_verified("restored", ("en",), False), "restore_keeps_catalog", "install_sh_rerun",
        "service_ready_after_rerun", "rerun_keeps_data", "install_sh_upgrade", "upgrade_keeps_data",
        "uninstall_keeps_data",
    ),
    "docker": (
        "docker_build", "container_healthy", "llm_settings", "no_published_ports", "non_root_user", "api_loopback_only",
        "doctor_container", "journal_retain", "journal_readback", "document_import_en", *_SCENARIO,
        *_verified("container", ("en", "uk", "image"), True), "mcp_tools", "wrapper_download_to_host",
        "container_healthy_recreated", "recreate_keeps_data", *_verified("recreated", ("en",), False),
        "clean_stop", "backup_in_container", "restore_in_container", "container_healthy_restored",
        *_verified("restored", ("en",), False), "restore_keeps_data",
    ),
    "macos": ("install_sh", "llm_settings", "service_running", "service_ready", *_INSTALLED),
    "release": ("release_assets", "install_sh_release", "release_signature_checked", "llm_settings",
                "doctor_release", "image_pull", "image_signature"),
    "wrong_key": ("systemd_user_session", "install_sh_wrong_key", "llm_settings"),
    "public_linux": _PUBLIC,
    "public_macos": _PUBLIC,
}
RECALL_TIMEOUT = 900
IMPORT_TIMEOUT = 1200


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def evidence_path() -> Path:
    value = os.environ.get("QUALIFY_EVIDENCE")
    if not value:
        sys.exit("QUALIFY_EVIDENCE is not set")
    return Path(value)


def load() -> dict:
    return json.loads(evidence_path().read_text(encoding="utf-8"))


def save(evidence: dict) -> None:
    path = evidence_path()
    staged = path.with_name(path.name + ".tmp")
    staged.write_text(json.dumps(evidence, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    staged.replace(path)


def record(name: str, passed: bool | None, detail: str = "", seconds: float | None = None) -> bool:
    """Store one check; ``None`` means skipped. Prints one line for the job log."""
    evidence = load()
    status = "skip" if passed is None else ("pass" if passed else "fail")
    entry: dict = {"status": status, "detail": detail[:500]}
    if seconds is not None:
        entry["seconds"] = round(seconds, 1)
    evidence["checks"][name] = entry
    save(evidence)
    print(f"[{status.upper()}] {name}" + (f" ({entry.get('seconds')} s)" if seconds is not None else "")
          + (f": {detail}" if detail else ""), flush=True)
    return passed is not False


def metric(kind: str, name: str, value) -> None:
    evidence = load()
    evidence.setdefault(kind, {})[name] = value
    save(evidence)


class CLI:
    def __init__(self, command: str):
        self.prefix = shlex.split(command)

    def run(self, *args: str, stdin: bytes | None = None, timeout: float = 600) -> subprocess.CompletedProcess:
        return subprocess.run([*self.prefix, *args], input=stdin, capture_output=True, timeout=timeout, check=False)

    def json(self, *args: str, stdin: bytes | None = None, timeout: float = 600) -> tuple[int, dict]:
        try:
            result = self.run(*args, "--json", stdin=stdin, timeout=timeout)
        except subprocess.TimeoutExpired:
            print(f"  {' '.join(args[:2])}: no answer within {timeout} s", flush=True)
            return 124, {"error": "timeout"}
        try:
            value = json.loads(result.stdout.decode("utf-8") or "{}")
        except (UnicodeError, ValueError):
            value = {}
        if result.returncode != 0:
            # Error codes only; stderr of the CLI is redacted by design.
            print(f"  {' '.join(args[:2])}: exit {result.returncode}, error {value.get('error')!r}, "
                  f"stderr {result.stderr.decode('utf-8', 'replace')[-300:]!r}", flush=True)
        return result.returncode, value if isinstance(value, dict) else {}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def normalized(text: str) -> str:
    return " ".join(text.split())


# --- subcommands -----------------------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    server, repository, run_id = (os.environ.get(name, "") for name in
                                  ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID"))
    evidence = {
        "format": "lifecontext-qualification-evidence/1",
        "job": args.job,
        "commit": os.environ.get("GITHUB_SHA"),
        "ref": os.environ.get("GITHUB_REF"),
        "run_url": f"{server}/{repository}/actions/runs/{run_id}" if run_id else None,
        "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
        "runner": {
            "os": os.environ.get("RUNNER_OS"), "arch": os.environ.get("RUNNER_ARCH"),
            "image": os.environ.get("ImageOS"), "image_version": os.environ.get("ImageVersion"),
            "machine": platform.machine(), "release": platform.release(), "cpus": os.cpu_count(),
        },
        "started_at": now(),
        "llm": {"provider": os.environ.get("LLM_PROVIDER"), "model": os.environ.get("LLM_MODEL")},
        "expected_checks": list(dict.fromkeys(EXPECTED[args.job])),
        "versions": {}, "timings_seconds": {}, "sizes_bytes": {}, "info": {}, "checks": {},
    }
    evidence_path().parent.mkdir(parents=True, exist_ok=True)
    save(evidence)
    return 0


def cmd_record(args: argparse.Namespace) -> int:
    passed = {"pass": True, "fail": False, "skip": None}[args.status]
    record(args.name, passed, args.detail or "", args.seconds)
    return 0 if passed is not False else 1


def cmd_metric(args: argparse.Namespace) -> int:
    value = args.value
    if args.kind in {"timings_seconds", "sizes_bytes"}:
        value = float(value) if "." in value else int(value)
    metric(args.kind, args.name, value)
    return 0


def cmd_fixtures(args: argparse.Namespace) -> int:
    from PIL import Image, ImageDraw, ImageFont

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for key in ("journal", "en", "uk"):
        (out / FIXTURES[key]["file"]).write_text(FIXTURES[key]["text"], encoding="utf-8")
    image = Image.new("L", (1400, 420), 255)
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=72)
    for number, line in enumerate(FIXTURES["image"]["lines"]):
        draw.text((60, 40 + number * 120), line, fill=0, font=font)
    image.save(out / FIXTURES["image"]["file"], format="PNG")
    for key in ("journal", *DOCUMENTS):
        print(f"{FIXTURES[key]['file']}: {sha256(out / FIXTURES[key]['file'])}")
    return 0


def load_state(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _document_state(cli: CLI, source_id: str) -> tuple[dict, bool]:
    """Poll until every stage is terminal; returns the last document and whether all completed."""
    deadline = time.monotonic() + IMPORT_TIMEOUT
    document: dict = {}
    while time.monotonic() < deadline:
        code, document = cli.json("documents", "show", source_id, "--version", "1")
        if code == 0:
            states = {stage: (document.get("stages") or {}).get(stage, {}).get("state") for stage in STAGES}
            # Custody ends in "accepted"; the later stages end in "completed".
            if all(state == ("accepted" if stage == "custody" else "completed") for stage, state in states.items()):
                return document, True
            if any(state in TERMINAL_FAILURES for state in states.values()):
                return document, False
        time.sleep(10)
    return document, False


def cmd_capture(args: argparse.Namespace) -> int:
    cli = CLI(args.cli)
    fixtures = Path(args.fixtures)
    ok = True
    journal = FIXTURES["journal"]
    text = (fixtures / journal["file"]).read_bytes()
    started = time.monotonic()
    code, value = cli.json("retain", journal["source_id"], "1", "--input", "-",
                           "--idempotency-key", "qualify-journal-01", stdin=text, timeout=RECALL_TIMEOUT)
    operation = value.get("operation_id")
    passed = code == 0 and value.get("source_id") == journal["source_id"] and bool(operation)
    if passed:
        code, state = cli.json("operations", "get", operation)
        passed = code == 0 and state.get("state") == "completed"
        detail = f"operation {state.get('state')}"
    else:
        detail = f"exit {code}, error {value.get('error')}"
    ok &= record("journal_retain", passed, detail, time.monotonic() - started)
    code, value = cli.json("evidence", journal["source_id"], "--version", "1", "--include-content")
    ok &= record("journal_readback", code == 0 and value.get("content") == text.decode("utf-8")
                 and value.get("sha256") == hashlib.sha256(text).hexdigest(), "exact content and SHA-256")

    state: dict = {"documents": {}}
    keys = ("en",) if args.english_only else DOCUMENTS
    for key in keys:
        fixture = FIXTURES[key]
        path = fixtures / fixture["file"]
        started = time.monotonic()
        code, value = cli.json("documents", "import", str(path), "--mime", fixture["mime"],
                               "--idempotency-key", f"qualify-{key}-{sha256(path)[:16]}", timeout=IMPORT_TIMEOUT)
        sources = (value.get("summary") or {}).get("sources") or [{}]
        source_id = (value.get("document") or {}).get("source_id") or sources[0].get("source_id")
        if code != 0 or not source_id:
            ok &= record(f"document_import_{key}", False, f"exit {code}, error {value.get('error')}")
            continue
        state["documents"][key] = {"source_id": source_id, "sha256": sha256(path)}
        Path(args.state).write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
        document, completed = _document_state(cli, source_id)
        stages = {stage: (document.get("stages") or {}).get(stage, {}) for stage in STAGES}
        detail = ", ".join(f"{stage} {item.get('state')}" + (f" ({item['error_code']})" if item.get("error_code") else "")
                           for stage, item in stages.items())
        ok &= record(f"document_import_{key}", completed, detail, time.monotonic() - started)
        if key == "image":
            coverage = document.get("coverage") or {}
            ok &= record("ocr_image", stages["extraction"].get("state") == "completed"
                         and (coverage.get("ocr") or 0) >= 1 and coverage.get("complete") is True,
                         f"coverage ocr {coverage.get('ocr')}, complete {coverage.get('complete')}, "
                         f"unresolved {coverage.get('unresolved')}")
    return 0 if ok else 1


def _recall(cli: CLI, key: str, label: str, state: dict) -> bool:
    fixture = FIXTURES[key]
    source_id = state["documents"].get(key, {}).get("source_id")
    if source_id is None:
        return record(f"recall_cites_document_{key}_{label}", False, "the document was not imported")
    started = time.monotonic()
    code, value = cli.json("recall", "--query", fixture["query"], "--max-results", "10", timeout=RECALL_TIMEOUT)
    seconds = time.monotonic() - started
    results = value.get("results") if isinstance(value.get("results"), list) else []
    ranks = [number for number, item in enumerate(results, 1)
             if (item.get("evidence") or {}).get("source_id") == source_id]
    answer = any(fixture["answer"] in str(item.get("text", "")) for item in results
                 if (item.get("evidence") or {}).get("source_id") == source_id)
    detail = (f"cited at rank {ranks[0]} of {len(results)}, answer text {'present' if answer else 'absent'}, "
              f"omitted {value.get('omitted_unresolvable')}" if ranks else
              f"exit {code}, error {value.get('error')}, {len(results)} results without the document")
    metric("timings_seconds", f"recall_{key}_{label}", round(seconds, 1))
    # The document must come first, and its cited passage must hold the fact.
    first = results[0] if results else {}
    first_answers = ((first.get("evidence") or {}).get("source_id") == source_id
                     and fixture["answer"] in str(first.get("text", "")))
    passed = record(f"recall_cites_document_{key}_{label}", bool(ranks) and ranks[0] == 1 and first_answers,
                    detail, seconds)
    if ranks:
        version = str(results[ranks[0] - 1]["evidence"].get("source_version"))
        expected = state["documents"][key]["sha256"]
        code, value = cli.json("evidence", source_id, "--version", version)
        passed &= record(f"evidence_readback_{key}_{label}", code == 0 and value.get("sha256") == expected,
                         f"cited version {version}, SHA-256 {'matches' if value.get('sha256') == expected else 'differs'}")
    return passed


def _wiki(cli: CLI, label: str, state: dict) -> bool:
    fixture = FIXTURES["en"]
    source_id = state["documents"].get("en", {}).get("source_id", "not-imported")
    document = normalized(fixture["text"])
    code, listing = cli.json("wiki", "list")
    pages = [page.get("page_id") for page in listing.get("pages") or []]
    preferred = f"source_{source_id}"
    ordered = ([preferred] if preferred in pages else []) + [page for page in pages if page != preferred]
    literal, cited_pages, fact = 0, [], False
    for page_id in ordered:
        code, value = cli.json("wiki", "show", page_id)
        managed = value.get("managed") or ""
        lines = [line for line in managed.splitlines() if f"{source_id} v" in line]
        if lines:
            cited_pages.append(page_id)
        for line in lines:
            text = re.sub(r"^\s*(?:>\s*)?(?:-\s+|\d+\.\s+)", "", line)
            claim = normalized(text.rsplit(" [", 1)[0])
            if len(claim) >= 12 and claim in document:
                literal += 1
                fact = fact or fixture["answer"] in claim
        if fact and preferred in cited_pages:
            break
    ok = record(f"wiki_literal_quotes_{label}", code == 0 and fact,
                f"{len(pages)} pages, document cited on {cited_pages[:3]}, {literal} claims are literal quotes, "
                f"the {fixture['answer']} fact {'among them' if fact else 'missing'}")
    started = time.monotonic()
    code, lint = cli.json("wiki", "lint", "--fail-on", "error")
    counts = lint.get("counts") or {}
    ok &= record(f"wiki_lint_{label}", code == 0 and counts.get("error") == 0,
                 f"exit {code}, errors {counts.get('error')}, warnings {counts.get('warning')}, "
                 f"info {counts.get('info')}", time.monotonic() - started)
    code, brief = cli.json("brief")
    listed = {item.get("source_id") for item in (brief.get("documents") or {}).get("items") or []}
    imported = {item["source_id"] for item in state["documents"].values()}
    ok &= record(f"brief_{label}", code == 0 and bool(imported) and imported <= listed,
                 f"lists {len(imported & listed)} of the {len(imported)} imported documents")
    return ok


def cmd_verify(args: argparse.Namespace) -> int:
    cli = CLI(args.cli)
    state = load_state(args.state)
    ok = _recall(cli, "en", args.label, state)
    if args.ukrainian:
        ok &= _recall(cli, "uk", args.label, state)
    if args.ocr:
        ok &= _recall(cli, "image", args.label, state)
    journal = FIXTURES["journal"]
    code, value = cli.json("evidence", journal["source_id"], "--version", "1", "--include-content")
    ok &= record(f"journal_readback_{args.label}", code == 0 and value.get("content") == journal["text"],
                 "exact content")
    if args.wiki:
        ok &= _wiki(cli, args.label, state)
    return 0 if ok else 1


def cmd_doctor(args: argparse.Namespace) -> int:
    cli = CLI(args.cli)
    code, report = cli.json("doctor", timeout=180)
    service = report.get("service") or {}
    tesseract = report.get("tesseract") or {}
    models = report.get("models") or {}
    if args.versions:
        metric("versions", "lifecontext", report.get("version"))
        metric("versions", "python", (report.get("python") or {}).get("version"))
        metric("versions", "hindsight", report.get("hindsight_packages"))
        metric("versions", "tesseract", tesseract.get("version"))
        metric("info", "tesseract_languages", tesseract.get("languages"))
    metric("info", f"doctor_{args.label}", {
        "ok": report.get("ok"), "problems": report.get("problems"), "warnings": report.get("warnings"),
        "service_ready": service.get("ready"), "models_cached": models.get("complete"),
        "last_start_offline": (models.get("last_daemon_start") or {}).get("hf_hub_offline"),
    })
    warnings = report.get("warnings") or []
    unexpected = [warning for warning in warnings if warning not in (args.allow_warning or [])]
    passed = code == 0 and report.get("ok") is True and service.get("ready") is True and not unexpected and not (
        args.require_ocr and tesseract.get("missing_languages"))
    return 0 if record(f"doctor_{args.label}", passed,
                       f"exit {code}, problems {report.get('problems')}, warnings {warnings}") else 1


def cmd_llm(args: argparse.Namespace) -> int:
    """The model settings the install wrote, from `llm show`: provider, model and, never the URL itself,
    whether a base URL is set. Only a gateway provider may have one."""
    cli = CLI(args.cli)
    code, value = cli.json("llm", "show")
    provider, model = os.environ.get("LLM_PROVIDER"), os.environ.get("LLM_MODEL")
    base_url = "set" if value.get("base_url") else "none"
    expected = "set" if provider in GATEWAY_PROVIDERS else "none"
    metric("info", "llm_settings", {"provider": value.get("provider"), "model": value.get("model"),
                                    "base_url": base_url, "api_key": value.get("api_key")})
    passed = (code == 0 and value.get("provider") == provider and value.get("model") == model
              and base_url == expected and value.get("api_key") == "set")
    return 0 if record("llm_settings", passed, f"exit {code}, provider {value.get('provider')}, model "
                       f"{value.get('model')}, base_url {base_url} (expected {expected}), key {value.get('api_key')}") else 1


def cmd_probe(args: argparse.Namespace) -> int:
    """`preflight --model-probe`: one model call through retain's own path; exit 0 on a pass, 3 on a failure."""
    cli = CLI(args.cli)
    started = time.monotonic()
    code, value = cli.json("preflight", "--model-probe", timeout=180)
    probe = value.get("model_probe") if isinstance(value.get("model_probe"), dict) else {}
    return 0 if record(f"model_probe_{args.label}", code == 0 and bool(probe),
                       f"exit {code}, status {probe.get('status')}, cause {probe.get('cause')}",
                       time.monotonic() - started) else 1


def cmd_fingerprint(args: argparse.Namespace) -> int:
    """Catalog-level state, kept in a private file outside the evidence."""
    cli = CLI(args.cli)
    _, documents = cli.json("documents", "list")
    _, wiki = cli.json("wiki", "list")
    state = {
        "documents": sorted((item.get("source_id"), item.get("source_version"),
                             tuple((item.get("stages") or {}).get(stage, {}).get("state") for stage in STAGES))
                            for item in documents.get("documents") or []),
        "wiki": sorted((page.get("page_id"), page.get("current_revision")) for page in wiki.get("pages") or []),
        "files": {},
    }
    for path in args.file or []:
        candidate = Path(path)
        state["files"][path] = sha256(candidate) if candidate.is_file() else None
    Path(args.out).write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    before = json.loads(Path(args.before).read_text(encoding="utf-8"))
    after = json.loads(Path(args.after).read_text(encoding="utf-8"))
    differences = [key for key in ("documents", "wiki", "files") if before.get(key) != after.get(key)]
    unchanged_files = [Path(path).name for path, value in after.get("files", {}).items()
                       if value is not None and before.get("files", {}).get(path) == value]
    passed = not differences and bool(before.get("documents"))
    return 0 if record(args.name, passed, f"{len(after.get('documents', []))} documents, "
                       f"{len(after.get('wiki', []))} wiki pages, unchanged files {unchanged_files}"
                       + (f"; changed: {differences}" if differences else "")) else 1


def _rpc(process: subprocess.Popen, lines: queue.Queue, message: dict, timeout: float) -> dict | None:
    process.stdin.write((json.dumps(message) + "\n").encode())
    process.stdin.flush()
    if "id" not in message:
        return None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            line = lines.get(timeout=max(0.1, deadline - time.monotonic()))
        except queue.Empty:
            break
        if line is None:
            break
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if value.get("id") == message["id"]:
            return value
    raise TimeoutError(message["method"])


def cmd_mcp(args: argparse.Namespace) -> int:
    cli = CLI(args.cli)
    started = time.monotonic()
    process = subprocess.Popen([*cli.prefix, "mcp"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL)
    lines: queue.Queue = queue.Queue()

    def reader() -> None:
        for raw in process.stdout:
            lines.put(raw.decode("utf-8", "replace"))
        lines.put(None)

    threading.Thread(target=reader, daemon=True).start()
    try:
        initialized = _rpc(process, lines, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "lifecontext-qualify", "version": "1"}}}, 120)
        _rpc(process, lines, {"jsonrpc": "2.0", "method": "notifications/initialized"}, 0)
        listed = _rpc(process, lines, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, 60)
        called = _rpc(process, lines, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                       "params": {"name": "status", "arguments": {}}}, 120)
    except (TimeoutError, BrokenPipeError, OSError) as exc:
        return 0 if record("mcp_tools", False, f"no answer to {exc}", time.monotonic() - started) else 1
    finally:
        try:
            process.stdin.close()
            process.wait(timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
    tools = {tool.get("name") for tool in ((listed or {}).get("result") or {}).get("tools") or []}
    server = ((initialized or {}).get("result") or {}).get("serverInfo") or {}
    result = (called or {}).get("result")
    status_ok = isinstance(result, dict) and result.get("isError") is not True
    passed = READ_TOOLS <= tools and not (tools & WRITE_TOOLS) and status_ok
    metric("info", "mcp_tools", sorted(tools))
    return 0 if record("mcp_tools", passed, f"server {server.get('name')} {server.get('version')}, "
                       f"{len(tools)} tools, read-only {not (tools & WRITE_TOOLS)}, status call "
                       f"{'ok' if status_ok else 'failed'}", time.monotonic() - started) else 1


def cmd_wait_http(args: argparse.Namespace) -> int:
    started = time.monotonic()
    while time.monotonic() - started < args.timeout:
        try:
            with urllib.request.urlopen(args.url, timeout=5) as response:
                if response.status == 200:
                    metric("timings_seconds", args.name, round(time.monotonic() - started, 1))
                    return 0 if record(args.name, True, "healthz answered", time.monotonic() - started) else 1
        except OSError:
            pass
        time.sleep(5)
    record(args.name, False, f"no answer within {args.timeout} s", time.monotonic() - started)
    return 1


def cmd_wait_docker(args: argparse.Namespace) -> int:
    started = time.monotonic()
    state = ""
    while time.monotonic() - started < args.timeout:
        result = subprocess.run(["docker", "inspect", "-f", "{{.State.Health.Status}} {{.RestartCount}}",
                                 args.container], capture_output=True, text=True, check=False)
        state, _, restarts = result.stdout.strip().partition(" ")
        if restarts.isdigit() and int(restarts) >= 3:
            record(args.name, False, f"restarted {restarts} times while {state}", time.monotonic() - started)
            return 1
        if state == "healthy":
            metric("timings_seconds", args.name, round(time.monotonic() - started, 1))
            return 0 if record(args.name, True, "container healthy", time.monotonic() - started) else 1
        time.sleep(5)
    record(args.name, False, f"health {state or 'unknown'} after {args.timeout} s", time.monotonic() - started)
    return 1


def cmd_finalize(args: argparse.Namespace) -> int:
    evidence = load()
    checks = evidence["checks"]
    for name in evidence.get("expected_checks", []):
        if name not in checks:
            checks[name] = {"status": "fail", "detail": "not run: an earlier step ended before this check"}
    failed = sorted(name for name, check in checks.items() if check["status"] == "fail")
    evidence["finished_at"] = now()
    evidence["result"] = "fail" if failed or not checks else "pass"
    save(evidence)
    lines = [f"## Qualification: {evidence['job']} ({evidence['result']})", "",
             "| Check | Result | Seconds | Detail |", "|---|---|---|---|"]
    for name, check in checks.items():
        detail = check.get("detail", "").replace("|", "/")
        lines.append(f"| `{name}` | {check['status']} | {check.get('seconds', '')} | {detail} |")
    for kind in ("timings_seconds", "sizes_bytes", "versions"):
        if evidence.get(kind):
            lines += ["", f"**{kind}**: `{json.dumps(evidence[kind], sort_keys=True)}`"]
    # Next to the evidence, so the leak scan covers it before it reaches the run summary.
    evidence_path().with_name(f"{evidence['job']}-summary.md").write_text("\n".join(lines) + "\n\n", encoding="utf-8")
    print("\n".join(lines))
    return 1 if evidence["result"] == "fail" else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("init")
    command.add_argument("--job", required=True, choices=sorted(EXPECTED))
    command = commands.add_parser("record")
    command.add_argument("name")
    command.add_argument("status", choices=("pass", "fail", "skip"))
    command.add_argument("--detail")
    command.add_argument("--seconds", type=float)
    command = commands.add_parser("metric")
    command.add_argument("kind", choices=("versions", "timings_seconds", "sizes_bytes", "info"))
    command.add_argument("name")
    command.add_argument("value")
    command = commands.add_parser("fixtures")
    command.add_argument("--out", required=True)
    for name in ("capture", "verify", "doctor", "mcp", "llm", "probe", "fingerprint"):
        command = commands.add_parser(name)
        command.add_argument("--cli", required=True, help="the lifecontext command line, shell-quoted")
        if name in {"capture", "verify"}:
            command.add_argument("--state", required=True, help="document IDs: written by capture, read by verify")
        if name == "capture":
            command.add_argument("--fixtures", required=True)
            command.add_argument("--english-only", action="store_true")
        if name == "verify":
            command.add_argument("--label", required=True)
            command.add_argument("--ukrainian", action="store_true")
            command.add_argument("--ocr", action="store_true", help="also recall a fact read from the image")
            command.add_argument("--wiki", action="store_true")
        if name in {"doctor", "probe"}:
            command.add_argument("--label", required=True)
        if name == "doctor":
            command.add_argument("--versions", action="store_true")
            command.add_argument("--require-ocr", action="store_true")
            command.add_argument("--allow-warning", action="append", help="a doctor warning that may appear")
        if name == "fingerprint":
            command.add_argument("--out", required=True)
            command.add_argument("--file", action="append")
    command = commands.add_parser("compare")
    command.add_argument("name")
    command.add_argument("before")
    command.add_argument("after")
    command = commands.add_parser("wait-http")
    command.add_argument("name")
    command.add_argument("url")
    command.add_argument("--timeout", type=int, default=900)
    command = commands.add_parser("wait-docker")
    command.add_argument("name")
    command.add_argument("container")
    command.add_argument("--timeout", type=int, default=900)
    commands.add_parser("finalize")
    args = parser.parse_args(argv)
    handler = globals()["cmd_" + args.command.replace("-", "_")]
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
