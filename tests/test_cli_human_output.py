"""Plain output, error messages and the unchanged ``--json`` success output of the CLI."""
from __future__ import annotations

import contextlib
import io
import json
import unittest
from unittest.mock import patch

from lifecontext import cli

DOC_ID = "doc_af7084b80123456789abcdef01234567"
OP_ID = "op_" + "1" * 32
SHA = "b" * 64


def stage(state: str, error_code: str | None = None, attempt: int = 1) -> dict[str, object]:
    return {"state": state, "revision": 1, "attempt": attempt, "error_code": error_code}


STATUS = {
    "ready": True,
    "max_file_bytes": 33554432,
    "dependencies": {
        "hindsight": {"ready": True, "capabilities": {
            "aretain": True, "arecall": True, "areflect": True, "attributed_reflect": True}},
        "vault": {"ready": True, "permissions": True, "error": None},
        "catalog": {"ready": True},
    },
    "reconciliation": {"fact_count": 0, "facts": {
        "missing_original": 0, "corrupt_original": 0, "stale_projection": 0}},
}
DOCUMENT = {
    "source_id": DOC_ID,
    "source_version": 1,
    "filename": "lease.pdf",
    "title": "Apartment lease",
    "mime_type": "application/pdf",
    "category_id": "inbox",
    "category_revision": 1,
    "accepted": True,
    "stages": {
        "custody": stage("completed", attempt=0),
        "extraction": stage("completed"),
        "indexing": stage("completed"),
        "wiki": stage("failed", "provider_unavailable", 2),
        "projection": stage("pending", attempt=0),
    },
    "download_url": f"/v1/documents/{DOC_ID}/original?version=1",
    "extraction_warnings": ["page 7: no text layer"],
    "coverage": {"unit": "page", "total": 12, "resolved": 11, "text": 10, "ocr": 1, "blank": 0, "vision": 0,
                 "unresolved": ["7"], "partial": [], "complete": False},
}
COMPLETE_DOCUMENT = {
    **DOCUMENT,
    "source_id": "doc_" + "c" * 32,
    "title": "Insurance letter",
    "category_id": "insurance",
    "stages": {name: stage("completed") for name in ("custody", "extraction", "indexing", "wiki", "projection")},
    "extraction_warnings": [],
    "coverage": {"unit": "page", "total": 2, "resolved": 2, "text": 2, "ocr": 0, "blank": 0, "vision": 0,
                 "unresolved": [], "partial": [], "complete": True},
}
WIKI_SHOW = {
    "page": {"page_id": "index", "title": "Index", "current_revision": 3, "state": "current"},
    "revision": {"page_id": "index", "revision": 3, "parent_revision": 2, "artifact_sha256": "a" * 64},
    "managed": "<!-- lifecontext:managed -->\n# Index\n\n- [[source_doc_af7084b8|Apartment lease]]\n",
}
WIKI_LIST = {"pages": [
    {"page_id": "index", "title": "Index", "current_revision": 3, "state": "current"},
    {"page_id": "source_" + DOC_ID, "title": "Apartment lease", "current_revision": 1, "state": "stale"},
]}
RECALL = {
    "results": [
        {"text": "The rent is 900 EUR per month.", "type": "world",
         "evidence": {"source_id": DOC_ID, "source_version": 1, "sha256": SHA}},
        {"text": "Notice period is three months.", "type": "world",
         "evidence": {"source_id": "journal_2026", "source_version": 2, "sha256": SHA}},
    ],
    "truncated": False,
    "omitted_unresolvable": 1,
}
REFLECT = {"text": "Your rent is 900 EUR a month.",
           "evidence": [{"source_id": DOC_ID, "source_version": 1, "sha256": SHA}]}
CATEGORIES = {"categories": [
    {"category_id": "inbox", "name": "Inbox", "description": "Unsorted", "aliases": [], "revision": 1},
    {"category_id": "housing", "name": "Housing", "description": "", "aliases": ["home", "rent"], "revision": 4},
]}
EMPTY_SECTION = {"items": [], "omitted": 0}
BRIEF = {
    "date": "2026-11-10",
    "since": "2026-11-09T23:00:00Z",
    "until": "2026-11-10T23:00:00Z",
    "timezone": "Europe/Vienna",
    "documents": {"items": [{"source_id": DOC_ID, "source_version": 1, "title": "Apartment lease",
                             "at": "2026-11-10 09:15", "stages": {"custody": "completed", "wiki": "failed"},
                             "unresolved": 1, "partial": 0}], "omitted": 0},
    "journal_entries": 2,
    "wiki_changes": EMPTY_SECTION,
    "contradictions": EMPTY_SECTION,
    "open_now": {"attention": EMPTY_SECTION, "stages": EMPTY_SECTION, "reviews_pending": 0,
                 "lint": {"counts": {"error": 0, "warning": 1, "info": 0}, "omitted": 0, "documents_omitted": 0,
                          "codes": {"page_stale": 1}}},
    "projection": None,
}
IMPORT_RESULT = {"state": "completed", "summary": {"sources": [{"source_id": DOC_ID, "source_version": 1,
                                                                 "sha256": SHA}], "item_count": None,
                                                    "error_code": None},
                 "document": DOCUMENT}


UK_TITLE = "Договір оренди квартири"
UK_DOCUMENT = {**DOCUMENT, "title": UK_TITLE}
UK_DOCUMENT_JSON = (
    '{"accepted":true,"category_id":"inbox","category_revision":1,"coverage":{"blank":0,"complete":false,"ocr":1,'
    '"partial":[],"resolved":11,"text":10,"total":12,"unit":"page","unresolved":["7"],"vision":0},'
    '"download_url":"/v1/documents/doc_af7084b80123456789abcdef01234567/original?version=1",'
    '"extraction_warnings":["page 7: no text layer"],"filename":"lease.pdf","mime_type":"application/pdf",'
    '"source_id":"doc_af7084b80123456789abcdef01234567","source_version":1,"stages":{"custody":{"attempt":0,'
    '"error_code":null,"revision":1,"state":"completed"},"extraction":{"attempt":1,"error_code":null,"revision":1,'
    '"state":"completed"},"indexing":{"attempt":1,"error_code":null,"revision":1,"state":"completed"},'
    '"projection":{"attempt":0,"error_code":null,"revision":1,"state":"pending"},"wiki":{"attempt":2,'
    '"error_code":"provider_unavailable","revision":1,"state":"failed"}},"title":"Договір оренди квартири"}')
UK_IMPORT_RESULT = {**IMPORT_RESULT, "document": UK_DOCUMENT}
UK_RETRY_RESULT = {"operation_id": OP_ID, "state": "completed", "summary": {"sources": [], "item_count": None,
                                                                         "error_code": None},
                   "document": UK_DOCUMENT}
RETAIN_RESULT = {"source_id": "journal_2026", "source_version": 1, "sha256": SHA, "size_bytes": 8,
                 "submission_state": "submitted"}
CURATE_RESULT = {"state": "completed", "summary": {"sources": [{"source_id": "journal_2026", "source_version": 2,
                                                                 "sha256": SHA}], "item_count": None,
                                                    "error_code": None}}
UK_WIKI_LIST = {"pages": [{"page_id": "source_" + DOC_ID, "title": UK_TITLE, "current_revision": 1,
                           "state": "current"}]}
UK_REFLECT = {"text": "Оренда становить 900 євро на місяць.",
              "evidence": [{"source_id": DOC_ID, "source_version": 1, "sha256": SHA}]}


def run(argv: list[str], body: object = None, *, status: int = 200, raw: bytes | None = None,
        error: BaseException | None = None) -> tuple[int, str, str]:
    """Run ``cli.main`` against one canned API answer; return (exit, stdout, stderr)."""
    payload = raw if raw is not None else json.dumps(body).encode("utf-8")
    headers = {"x-lifeweave-operation-id": OP_ID, "content-type": "application/json"}
    exchange = (patch.object(cli, "_http_exchange", side_effect=error) if error is not None else
                patch.object(cli, "_http_exchange", return_value=(status, headers, payload)))
    stdout, stderr = io.StringIO(), io.StringIO()
    with exchange, patch.object(cli, "_read_input", return_value=b"%PDF-1.7"), \
            contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = cli.main(argv)
    return code, stdout.getvalue(), stderr.getvalue()


class JsonGoldenTests(unittest.TestCase):
    """``--json`` success output is what agents parse: these bytes must not change."""

    def test_status_json_is_unchanged(self) -> None:
        code, out, _ = run(["status", "--json"], STATUS)
        self.assertEqual(code, 0)
        self.assertEqual(out, (
            '{"dependencies":{"catalog":{"ready":true},"hindsight":{"capabilities":{"arecall":true,'
            '"areflect":true,"aretain":true,"attributed_reflect":true},"ready":true},"vault":{"error":null,'
            '"permissions":true,"ready":true}},"max_file_bytes":33554432,"ready":true,"reconciliation":'
            '{"fact_count":0,"facts":{"corrupt_original":0,"missing_original":0,"stale_projection":0}}}\n'))

    def test_wiki_show_json_is_unchanged(self) -> None:
        code, out, _ = run(["wiki", "show", "index", "--json"], WIKI_SHOW)
        self.assertEqual(code, 0)
        self.assertEqual(out, (
            '{"managed":"<!-- lifecontext:managed -->\\n# Index\\n\\n- [[source_doc_af7084b8|Apartment lease]]\\n",'
            '"page":{"current_revision":3,"page_id":"index","state":"current","title":"Index"},'
            '"revision":{"artifact_sha256":"' + "a" * 64 + '","page_id":"index","parent_revision":2,'
            '"revision":3}}\n'))

    def test_recall_json_is_unchanged(self) -> None:
        code, out, _ = run(["recall", "--query", "rent", "--json"], RECALL)
        self.assertEqual(code, 0)
        self.assertEqual(out, (
            '{"omitted_unresolvable":1,"results":[{"evidence":{"sha256":"' + SHA + '","source_id":"' + DOC_ID
            + '","source_version":1},"text":"The rent is 900 EUR per month.","type":"world"},{"evidence":'
            '{"sha256":"' + SHA + '","source_id":"journal_2026","source_version":2},"text":'
            '"Notice period is three months.","type":"world"}],"truncated":false}\n'))

    def check(self, argv: list[str], body: object, expected: str) -> None:
        code, out, err = run([*argv, "--json"], body)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, expected + "\n")

    def test_document_commands_json_is_unchanged(self) -> None:
        self.check(["documents", "list"], {"documents": [UK_DOCUMENT]}, '{"documents":[' + UK_DOCUMENT_JSON + "]}")
        self.check(["documents", "show", DOC_ID], UK_DOCUMENT, UK_DOCUMENT_JSON)
        operation = ('"operation_id":"' + OP_ID + '","state":"completed","summary":{"error_code":null,'
                     '"item_count":null,"sources":')
        self.check(["documents", "import", "lease.pdf"], UK_IMPORT_RESULT,
                   '{"document":' + UK_DOCUMENT_JSON + "," + operation + '[{"sha256":"' + SHA + '","source_id":"'
                   + DOC_ID + '","source_version":1}]}}')
        self.check(["documents", "retry", OP_ID], UK_RETRY_RESULT,
                   '{"document":' + UK_DOCUMENT_JSON + "," + operation + "[]}}")

    def test_write_commands_json_is_unchanged(self) -> None:
        self.check(["retain", "journal_2026", "1", "--idempotency-key", "k1"], RETAIN_RESULT,
                   '{"operation_id":"' + OP_ID + '","sha256":"' + SHA + '","size_bytes":8,"source_id":'
                   '"journal_2026","source_version":1,"submission_state":"submitted"}')
        self.check(["curate", "correct", "journal_2026", "--expected-version", "1", "--idempotency-key", "k2"],
                   CURATE_RESULT,
                   '{"operation_id":"' + OP_ID + '","state":"completed","summary":{"error_code":null,'
                   '"item_count":null,"sources":[{"sha256":"' + SHA + '","source_id":"journal_2026",'
                   '"source_version":2}]}}')

    def test_other_read_commands_json_is_unchanged(self) -> None:
        self.check(["wiki", "list"], UK_WIKI_LIST,
                   '{"pages":[{"current_revision":1,"page_id":"source_' + DOC_ID + '","state":"current",'
                   '"title":"Договір оренди квартири"}]}')
        self.check(["categories", "list"], CATEGORIES,
                   '{"categories":[{"aliases":[],"category_id":"inbox","description":"Unsorted","name":"Inbox",'
                   '"revision":1},{"aliases":["home","rent"],"category_id":"housing","description":"",'
                   '"name":"Housing","revision":4}]}')
        self.check(["reflect", "--query", "rent"], UK_REFLECT,
                   '{"evidence":[{"sha256":"' + SHA + '","source_id":"' + DOC_ID + '","source_version":1}],'
                   '"text":"Оренда становить 900 євро на місяць."}')
        self.check(["brief"], BRIEF, (
            '{"contradictions":{"items":[],"omitted":0},"date":"2026-11-10","documents":{"items":[{"at":'
            '"2026-11-10 09:15","partial":0,"source_id":"' + DOC_ID + '","source_version":1,"stages":'
            '{"custody":"completed","wiki":"failed"},"title":"Apartment lease","unresolved":1}],"omitted":0},'
            '"journal_entries":2,"open_now":{"attention":{"items":[],"omitted":0},"lint":{"codes":'
            '{"page_stale":1},"counts":{"error":0,"info":0,"warning":1},"documents_omitted":0,"omitted":0},'
            '"reviews_pending":0,"stages":{"items":[],"omitted":0}},"projection":null,'
            '"since":"2026-11-09T23:00:00Z","timezone":"Europe/Vienna","until":"2026-11-10T23:00:00Z",'
            '"wiki_changes":{"items":[],"omitted":0}}'))


class PlainRendererSnapshotTests(unittest.TestCase):
    """What a person sees without --json; widths come from the data, never the terminal."""

    maxDiff = None

    def plain(self, argv: list[str], body: object) -> str:
        code, out, err = run(argv, body)
        self.assertEqual((code, err), (0, ""))
        return out

    def test_wiki_show_prints_the_page_markdown_raw(self) -> None:
        self.assertEqual(self.plain(["wiki", "show", "index"], WIKI_SHOW),
                         "<!-- lifecontext:managed -->\n# Index\n\n- [[source_doc_af7084b8|Apartment lease]]\n")

    def test_wiki_list_is_a_table(self) -> None:
        self.assertEqual(self.plain(["wiki", "list"], WIKI_LIST), "\n".join([
            "PAGE ID" + " " * 38 + "TITLE" + " " * 12 + "STATE" + " " * 4 + "REVISION",
            "index" + " " * 40 + "Index" + " " * 12 + "current  3",
            "source_" + DOC_ID + "  Apartment lease  stale    1",
            "Next: `lifecontext wiki lint` says what to do about stale or conflicting pages.",
        ]) + "\n")

    def test_documents_list_is_a_table_with_one_state_per_document(self) -> None:
        self.assertEqual(self.plain(["documents", "list"], {"documents": [DOCUMENT, COMPLETE_DOCUMENT]}), "\n".join([
            "SOURCE ID" + " " * 29 + "V  TITLE" + " " * 13 + "CATEGORY" + " " * 3 + "PAGES  STATE",
            DOC_ID + "  1  Apartment lease   inbox      11/12  wiki failed",
            "doc_" + "c" * 32 + "  1  Insurance letter  insurance  2/2    complete",
            "2 documents.",
            "Next: `lifecontext documents show SOURCE_ID` says what failed and how to retry it.",
        ]) + "\n")
        self.assertEqual(self.plain(["documents", "list"], {"documents": []}),
                         "No documents yet. Import one with `lifecontext documents import FILE`.\n")

    def test_status_prints_ready_then_one_line_per_dependency(self) -> None:
        self.assertEqual(self.plain(["status"], STATUS), "\n".join([  # noqa: FLY002
            "Ready: yes",
            "- catalog: ready",
            "- hindsight: ready",
            "- vault: ready",
            "Reconciliation: nothing to repair",
            "Largest accepted file: 32 MiB",
        ]) + "\n")
        broken = {**STATUS, "ready": False,
                  "dependencies": {
                      "hindsight": {"ready": False, "capabilities": {
                          "aretain": True, "arecall": False, "areflect": False, "attributed_reflect": False}},
                      "vault": {"ready": False, "permissions": False, "error": "unsafe_permissions"},
                      "catalog": {"ready": True}},
                  "reconciliation": {"fact_count": 2, "facts": {
                      "missing_original": 1, "corrupt_original": 0, "stale_projection": 1}}}
        self.assertEqual(self.plain(["status"], broken), "\n".join([  # noqa: FLY002
            "Ready: no",
            "- catalog: ready",
            "- hindsight: not ready; without arecall, areflect, attributed_reflect",
            "- vault: not ready (unsafe_permissions); permissions too open",
            "Reconciliation: 2 to repair (1 missing original, 1 stale projection)",
            "Largest accepted file: 32 MiB",
            "Next: `lifecontext doctor` says what is wrong and how to fix it.",
        ]) + "\n")

    def test_brief_uses_the_vault_renderer(self) -> None:
        from lifecontext.brief import render_brief

        out = self.plain(["brief"], BRIEF)
        self.assertEqual(out, render_brief(BRIEF))
        self.assertTrue(out.startswith("# Brief 2026-11-10\n"))
        self.assertNotIn('{"items"', out)

    def test_documents_show_has_a_stage_checklist_coverage_and_next_step(self) -> None:
        self.assertEqual(self.plain(["documents", "show", DOC_ID], DOCUMENT), "\n".join([
            f"Apartment lease ({DOC_ID} v1)",
            "File: lease.pdf (application/pdf)",
            "Category: inbox",
            "Stages:",
            "  [x] custody     completed",
            "  [x] extraction  completed",
            "  [x] indexing    completed",
            "  [!] wiki        failed (provider_unavailable), attempt 2",
            "  [ ] projection  pending",
            "Coverage: 11 of 12 pages resolved (10 text layer, 1 OCR); unresolved: 7",
            "Extraction warnings: 1 (listed with --json)",
            ("Next: check the model with `lifecontext doctor --model-probe`, then run "
             "`lifecontext documents retry OPERATION_ID` with the operation ID the import printed."),
        ]) + "\n")
        out = self.plain(["documents", "show", COMPLETE_DOCUMENT["source_id"]], COMPLETE_DOCUMENT)
        self.assertIn("Coverage: 2 of 2 pages resolved (2 text layer)\n", out)
        self.assertNotIn("Next:", out)
        self.assertNotIn("download", out.lower())

    def test_documents_show_uses_newer_optional_fields_when_present(self) -> None:
        newer = {**DOCUMENT, "outcome": "failed", "last_operation_id": OP_ID, "wiki_page_id": "source_" + DOC_ID,
                 "vault_path": "Journal/wiki/sources/Apartment lease.md",
                 "problems": ["The model did not answer while compiling the wiki page.",
                              {"code": "page_partial", "message": "1 page was only partly read"}]}
        out = self.plain(["documents", "show", DOC_ID], newer)
        self.assertIn("Outcome: failed\n", out)
        self.assertIn(f"Wiki page: source_{DOC_ID} (Journal/wiki/sources/Apartment lease.md)\n", out)
        self.assertIn("Problems:\n- The model did not answer while compiling the wiki page.\n"
                      "- page_partial: 1 page was only partly read\n", out)
        self.assertTrue(out.endswith(f"then run `lifecontext documents retry {OP_ID}`.\n"))

    def test_import_result_names_the_operation_and_the_document(self) -> None:
        out = self.plain(["documents", "import", "lease.pdf"], IMPORT_RESULT)
        self.assertTrue(out.startswith(f"Durable operation ID: {OP_ID}\nState: completed\n"
                                       f"Apartment lease ({DOC_ID} v1)\n"), out)
        self.assertIn("  [!] wiki        failed (provider_unavailable), attempt 2\n", out)
        self.assertTrue(out.endswith(f"then run `lifecontext documents retry {OP_ID}`.\n"), out)

    def test_recall_lists_texts_with_short_source_ids(self) -> None:
        self.assertEqual(self.plain(["recall", "--query", "rent"], RECALL), "\n".join([  # noqa: FLY002
            "- The rent is 900 EUR per month. (doc_af7084b8 v1)",
            "- Notice period is three months. (journal_2026 v2)",
            "1 result without a source left out.",
        ]) + "\n")
        titled = {"results": [{"text": "Rent: 900 EUR.", "evidence": {
            "source_id": DOC_ID, "source_version": 1, "sha256": SHA, "title": "Apartment lease"}}],
            "truncated": True, "omitted_unresolvable": 0}
        self.assertEqual(self.plain(["recall", "--query", "rent"], titled),
                         "- Rent: 900 EUR. (Apartment lease, doc_af7084b8 v1)\n"
                         "More matched than fit; raise --max-results or --max-tokens to see more.\n")
        self.assertEqual(self.plain(["recall", "--query", "x"], {"results": [], "truncated": False}),
                         "Nothing found.\n")

    def test_reflect_prints_the_answer_then_its_sources(self) -> None:
        self.assertEqual(self.plain(["reflect", "--query", "rent"], REFLECT),
                         "Your rent is 900 EUR a month.\n\nSources:\n- doc_af7084b8 v1\n")

    def test_categories_list_is_a_table(self) -> None:
        self.assertEqual(self.plain(["categories", "list"], CATEGORIES), "\n".join([
            "CATEGORY ID  NAME     REVISION  ALIASES",
            "inbox" + " " * 8 + "Inbox" + " " * 4 + "1" + " " * 9 + "-",
            "housing" + " " * 6 + "Housing  4" + " " * 9 + "home, rent",
        ]) + "\n")

    def test_commands_without_a_renderer_keep_the_fallback(self) -> None:
        out = self.plain(["operations", "get", OP_ID], {"operation_id": OP_ID, "state": "completed",
                                                 "summary": {"sources": [], "item_count": 3, "error_code": None}})
        self.assertEqual(out, f"Durable operation ID: {OP_ID}\nState: completed\n"
                              'Summary: {"error_code": null, "item_count": 3, "sources": []}\n')
        # An answer of an unexpected shape falls back instead of failing.
        self.assertEqual(self.plain(["wiki", "list"], {"pages": "nope"}), 'Pages: "nope"\n')


REQUEST_ID = "0123456789abcdef0123"


def api_error(code: str, retryable: bool) -> bytes:
    return json.dumps({"request_id": REQUEST_ID, "error_code": code, "retryable": retryable}).encode()


ERROR_CLASSES = frozenset({"CLIError", "_failure", "InitError", "ServiceError", "SkillsError", "CaptureError",
                           "VaultError", "BackupError", "HindsightStartupError", "PostgresError"})


def literal_codes() -> set[str]:
    """The code in every ``CLIError("code")``, ``cli.CLIError("code")``, ``InitError("code")`` ... of the package."""
    import ast
    from pathlib import Path

    codes = set()
    for path in Path(cli.__file__).parent.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant):
                function = node.func
                name = function.id if isinstance(function, ast.Name) else getattr(function, "attr", None)
                if name in ERROR_CLASSES and isinstance(node.args[0].value, str):
                    codes.add(node.args[0].value)
    return codes


class ErrorTableTests(unittest.TestCase):
    maxDiff = None

    def test_every_code_the_cli_can_print_has_a_message_and_a_next_step(self) -> None:
        import typing

        from lifecontext import cli_output
        from lifecontext.api import models

        codes = set(typing.get_args(models._ERROR_CODES)) | set(cli._API_ERROR_CODES) | set(cli._TRANSIENT_API_ERRORS)
        codes |= literal_codes()
        # Built from the command or the program name: {command}_failed in cli and service.
        codes |= {f"{command}_failed" for command in ("backup", "restore", "hindsight", "systemctl", "launchctl")}
        codes |= {"systemctl_unavailable", "launchctl_unavailable", "unsafe_credentials"}
        self.assertTrue({"provider_unavailable", "api_unavailable", "busy", "invalid_api_response", "lock_timeout",
                         "postgres_instance_missing", "invalid_timezone", "service_running"} <= codes)
        missing = sorted(code for code in codes if code not in cli_output.ERRORS)
        self.assertEqual(missing, [], "add these codes to cli_output.ERRORS")
        for code in cli_output.ERRORS:
            with self.subTest(code=code):
                for agent in (False, True):
                    message, next_step, retryable = cli_output.describe(code, port=8765, agent=agent)
                    self.assertTrue(message.endswith(".") and message[0].isupper(), message)
                    self.assertTrue(next_step and not next_step.endswith("."), next_step)
                    self.assertIsInstance(retryable, bool)
                    self.assertNotIn("{", message + next_step)

    def test_the_failure_codes_section_of_the_operations_guide_is_generated_from_the_table(self) -> None:
        from pathlib import Path

        from lifecontext import cli_output

        text = (Path(__file__).resolve().parents[1] / "docs" / "operations.md").read_text(encoding="utf-8")
        begin, end = "<!-- failure-codes:begin -->\n\n", "\n<!-- failure-codes:end -->"
        self.assertEqual(text.partition(begin)[2].partition(end)[0] + "\n", cli_output.failure_codes_section() + "\n",
                         "regenerate: python -m lifecontext.cli_output --write docs/operations.md")
        for code in cli_output.ERRORS:
            self.assertEqual(f"- `{code}`:" in text, code not in cli_output.EXPLAINERS, code)

    def test_json_errors_keep_the_code_and_add_message_next_step_retryable_and_request_id(self) -> None:
        code, out, err = run(["recall", "--query", "rent", "--json"], status=503,
                             raw=api_error("provider_unavailable", True))
        self.assertEqual((code, err), (1, ""))
        self.assertEqual(json.loads(out), {
            "error": "provider_unavailable",
            "message": "The model did not answer.",
            "next_step": "check the model with `lifecontext doctor --model-probe`, then re-run the command",
            "retryable": True,
            "request_id": REQUEST_ID,
        })
        # The API's own retryable flag wins over the table's.
        code, out, _ = run(["documents", "show", DOC_ID, "--json"], status=404, raw=api_error("source_not_found", False))
        failure = json.loads(out)
        self.assertEqual((code, failure["error"], failure["retryable"], failure["request_id"]),
                         (1, "source_not_found", False, REQUEST_ID))
        # A body that is not the API's typed error stays api_error, with no request ID.
        code, out, _ = run(["status", "--json"], status=500, raw=b"<html>oops</html>")
        failure = json.loads(out)
        self.assertEqual((code, failure["error"], failure["request_id"], failure["retryable"]),
                         (1, "api_error", None, False))
        self.assertEqual(sorted(failure), ["error", "message", "next_step", "request_id", "retryable"])

    def test_plain_errors_say_what_happened_and_what_to_do(self) -> None:
        code, out, err = run(["recall", "--query", "rent"], status=503, raw=api_error("provider_unavailable", True))
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err, "The model did not answer.\nNext: check the model with "
                              "`lifecontext doctor --model-probe`, then re-run the command.\n")
        code, _, err = run(["documents", "show", DOC_ID], status=404, raw=api_error("source_not_found", False))
        self.assertTrue(err.startswith("No source or page has that ID.\nNext: `lifecontext documents list`"), err)
        self.assertNotIn("a" * 64, err)


class RepairWordingTests(unittest.TestCase):
    def test_a_repair_that_is_the_owners_is_worded_that_way_for_agents_only(self) -> None:
        _, out, _ = run(["documents", "show", DOC_ID, "--json"], status=503,
                        raw=api_error("evidence_unavailable", True))
        self.assertEqual(json.loads(out)["next_step"], "the owner runs `lifecontext reconcile`; if it still "
                                                       "fails, `lifecontext doctor` says what is wrong")
        _, _, err = run(["documents", "show", DOC_ID], status=503, raw=api_error("evidence_unavailable", True))
        self.assertIn("Next: run `lifecontext reconcile`;", err)
        for code_name in ("unsafe_permissions", "unsafe_path", "vault_unavailable", "internal_error", "unauthorized",
                          "api_error", "invalid_api_response", "vault_missing"):
            with self.subTest(code=code_name):
                _, next_step, _ = cli_output_module().describe(code_name, port=8765, agent=True)
                self.assertTrue(next_step.startswith(("the owner", "report it")), next_step)

    def test_the_two_sentences_that_promised_a_version_check_do_not_any_more(self) -> None:
        for code_name in ("api_error", "invalid_api_response"):
            _, next_step, _ = cli_output_module().describe(code_name, port=8765)
            self.assertEqual(next_step, "update the CLI and restart the service so both run the same version")
        self.assertEqual(cli_output_module().describe("unauthorized", port=1)[0],
                         "The API token was refused or cannot be read.")


def cli_output_module():
    from lifecontext import cli_output

    return cli_output


class LocalCommandErrorTests(unittest.TestCase):
    def run_failing(self, argv: list[str], error: cli.CLIError) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.dict(cli._LOCAL_COMMANDS, {"skills": unittest.mock.Mock(side_effect=error)}), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_json_keeps_the_code_and_adds_the_same_keys_as_the_api_commands(self) -> None:
        code, out, err = self.run_failing(["skills", "install", "--json"], cli.CLIError("skill_missing"))
        self.assertEqual((code, err), (1, ""))
        self.assertEqual(json.loads(out), {
            "error": "skill_missing", "message": "The agent skill files are missing from this installation.",
            "next_step": "reinstall LifeContext, then re-run", "retryable": False, "request_id": None})

    def test_plain_output_names_the_command_gives_a_sentence_and_a_next_line(self) -> None:
        code, out, err = self.run_failing(["skills", "install"], cli.CLIError("capture_requires_inbox"))
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err, "lifecontext skills failed: The capture folder must be named Inbox.\n"
                              "Next: leave out --inbox to use Journal/Inbox of the vault, or give a folder named Inbox.\n")

    def test_a_message_the_command_already_has_wins_over_the_table(self) -> None:
        error = cli.CLIError("model_unsupported", message="That model refuses forced tool choice.")
        _, out, _ = self.run_failing(["skills", "install", "--json"], error)
        failure = json.loads(out)
        self.assertEqual((failure["error"], failure["message"]), ("model_unsupported",
                                                                  "That model refuses forced tool choice."))
        self.assertTrue(failure["next_step"])
        _, _, err = self.run_failing(["skills", "install"], error)
        self.assertTrue(err.startswith("lifecontext skills failed: That model refuses forced tool choice.\nNext: "))

    def test_a_code_outside_the_table_still_gets_a_sentence(self) -> None:
        _, _, err = self.run_failing(["skills", "install"], cli.CLIError("brand_new_code"))
        self.assertEqual(err, "lifecontext skills failed: LifeContext request failed: brand new code.\n")


class SmallOutputTests(unittest.TestCase):
    def test_a_long_unresolved_page_list_stops_at_twenty(self) -> None:
        pages = [str(number) for number in range(1, 26)]
        document = {**DOCUMENT, "coverage": {**DOCUMENT["coverage"], "unresolved": pages, "partial": pages}}
        _, out, _ = run(["documents", "show", DOC_ID], document)
        line = next(item for item in out.splitlines() if item.startswith("Coverage:"))
        listed = ", ".join(pages[:20]) + ", and 5 more"
        self.assertTrue(line.endswith(f"unresolved: {listed}; 25 with unread parts: {listed}"), line)
        short = {**DOCUMENT, "coverage": {**DOCUMENT["coverage"], "unresolved": ["7", "9"]}}
        _, out, _ = run(["documents", "show", DOC_ID], short)
        self.assertIn("unresolved: 7, 9\n", out)

    def test_wiki_show_says_on_stderr_when_the_page_is_not_the_current_one(self) -> None:
        stale = {**WIKI_SHOW, "page": {**WIKI_SHOW["page"], "state": "stale"}}
        code, out, err = run(["wiki", "show", "index"], stale)
        self.assertEqual((code, err), (0, "Page index: revision 3, state stale.\n"))
        self.assertEqual(out, WIKI_SHOW["managed"])
        old = {**WIKI_SHOW, "revision": {**WIKI_SHOW["revision"], "revision": 2}}
        _, _, err = run(["wiki", "show", "index", "--revision", "2"], old)
        self.assertEqual(err, "Page index: revision 2, state current (current revision is 3).\n")
        _, _, err = run(["wiki", "show", "index", "--revision", "3"], WIKI_SHOW)
        self.assertEqual(err, "Page index: revision 3, state current.\n")
        _, _, err = run(["wiki", "show", "index"], WIKI_SHOW)
        self.assertEqual(err, "")
        _, out, err = run(["wiki", "show", "index", "--json"], stale)
        self.assertEqual(err, "")


class UnreachableServiceTests(unittest.TestCase):
    maxDiff = None

    def fail_with(self, argv: list[str], failure: BaseException, *, installed: bool) -> tuple[int, str, str]:
        from unittest.mock import MagicMock

        connection = MagicMock()
        connection.request.side_effect = failure
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(cli, "local_token", return_value="a" * 64), \
                patch.object(cli.http.client, "HTTPConnection", return_value=connection), \
                patch.object(cli, "_service_installed", return_value=installed), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_a_refused_connection_points_to_the_installed_service_or_to_serve(self) -> None:
        code, out, err = self.fail_with(["status", "--port", "18799"], ConnectionRefusedError(), installed=True)
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err, "The LifeContext service is not reachable at 127.0.0.1:18799.\n"
                              "Next: if LifeContext runs as the installed service, check it with "
                              "`lifecontext service status`: it may still be starting, or recovering after a "
                              "restart; otherwise start it with `lifecontext serve --port 18799`.\n")
        _, _, err = self.fail_with(["status", "--port", "18799"], ConnectionRefusedError(), installed=False)
        self.assertTrue(err.endswith("Next: start it with `lifecontext serve --port 18799`.\n"), err)

    def test_json_next_step_is_for_the_owner_and_never_says_serve(self) -> None:
        for installed in (True, False):
            code, out, _ = self.fail_with(["status", "--json"], ConnectionRefusedError(), installed=installed)
            failure = json.loads(out)
            self.assertEqual((code, failure["error"], failure["retryable"], failure["request_id"]),
                             (1, "api_unavailable", True, None))
            self.assertEqual(failure["next_step"], "the owner starts the LifeContext service or checks it with "
                                                   "`lifecontext service status`; do not start it yourself")
            self.assertNotIn("serve", failure["next_step"])

    def test_a_timed_out_read_says_the_service_may_be_busy_with_an_import(self) -> None:
        code, out, err = self.fail_with(["documents", "list"], TimeoutError(), installed=True)
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err, f"The LifeContext service did not answer in {int(cli.GET_TIMEOUT)} s; it may be busy "
                              "with an import.\nNext: try again in a few minutes.\n")
        code, out, _ = self.fail_with(["wiki", "list", "--json"], TimeoutError(), installed=False)
        failure = json.loads(out)
        # Scripts keep seeing the same code; only the explanation differs.
        self.assertEqual((code, failure["error"], failure["retryable"]), (1, "api_unavailable", True))
        self.assertEqual(failure["next_step"], "try again in a few minutes")

    def test_a_timed_out_write_or_recall_is_not_retryable(self) -> None:
        code, out, err = self.fail_with(["recall", "--query", "rent"], TimeoutError(), installed=True)
        self.assertEqual((code, out), (1, ""))
        self.assertIn("did not answer in time; it may still be working on the request.", err)
        self.assertIn("running it again may repeat the work", err)
        self.assertNotIn("serve", err)
        self.assertNotIn("lifecontext status", err)
        for argv in (["recall", "--query", "rent", "--json"], ["documents", "import", "lease.pdf", "--json"]):
            with self.subTest(argv=argv):
                with patch.object(cli, "_read_input", return_value=b"%PDF-1.7"):
                    code, out, _ = self.fail_with(argv, TimeoutError(), installed=False)
                failure = json.loads(out)
                self.assertEqual((code, failure["error"], failure["retryable"]), (1, "api_unavailable", False))
                self.assertIn("may still be working", failure["message"])

    def test_the_installed_unit_is_found_where_the_service_manager_reads_it(self) -> None:
        import os
        import tempfile
        from pathlib import Path

        from local_paths import scratch_root

        with tempfile.TemporaryDirectory(dir=scratch_root()) as directory, \
                patch("platform.system", return_value="Linux"), \
                patch.dict(os.environ, {"XDG_CONFIG_HOME": directory}):
            self.assertFalse(cli._service_installed())
            units = Path(directory) / "systemd" / "user"
            units.mkdir(parents=True)
            (units / "lifecontext.service").write_text("[Service]\n", encoding="utf-8")
            self.assertTrue(cli._service_installed())
        with patch("platform.system", return_value="Plan9"):
            self.assertFalse(cli._service_installed())

    def test_the_installed_launchd_agent_is_found_in_the_users_library(self) -> None:
        import tempfile
        from pathlib import Path

        from local_paths import scratch_root

        from lifecontext import service

        with tempfile.TemporaryDirectory(dir=scratch_root()) as directory, \
                patch("platform.system", return_value="Darwin"), \
                patch.object(service, "account_home", return_value=Path(directory)):
            self.assertFalse(cli._service_installed())
            agents = Path(directory) / "Library" / "LaunchAgents"
            agents.mkdir(parents=True)
            (agents / f"{service.LAUNCHD_LABEL}.plist").write_text("<plist/>", encoding="utf-8")
            self.assertTrue(cli._service_installed())


class InterruptAndTimeoutTests(unittest.TestCase):
    def interrupt(self, argv: list[str], target: str) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        patcher = (patch.dict(cli._LOCAL_COMMANDS, {"doctor": unittest.mock.Mock(side_effect=KeyboardInterrupt)})
                   if target == "local" else patch.object(cli, "_dispatch", side_effect=KeyboardInterrupt))
        with patcher, contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_ctrl_c_prints_one_line_and_exits_130(self) -> None:
        for argv, target in ((["documents", "import", "lease.pdf"], "api"), (["status", "--json"], "api")):
            with self.subTest(argv=argv):
                code, out, err = self.interrupt(argv, target)
                self.assertEqual((code, out), (130, ""))
                self.assertEqual(err.count("\n"), 1, err)
                self.assertTrue(err.startswith("Interrupted. The service may still be working on what was "
                                               "already sent"), err)
                self.assertIn("`lifecontext documents list`", err)
                self.assertNotIn("lifecontext status", err)
                self.assertNotIn("Traceback", err)

    def test_ctrl_c_in_a_local_command_does_not_talk_about_the_service(self) -> None:
        code, out, err = self.interrupt(["doctor"], "local")
        self.assertEqual((code, out), (130, ""))
        self.assertEqual(err, "Interrupted. The command stopped before it finished: check what it wrote before "
                              "running it again.\n")

    def test_reads_use_a_short_timeout_and_writes_keep_theirs(self) -> None:
        from unittest.mock import MagicMock

        connection = MagicMock()
        response = connection.getresponse.return_value
        response.read.return_value = b"{}"
        response.getheaders.return_value = []
        response.status = 200
        args = cli._build_parser().parse_args(["status"])
        with patch.object(cli, "local_token", return_value="a" * 64), \
                patch.object(cli.http.client, "HTTPConnection", return_value=connection) as connect:
            for method, target, expected in (("GET", "/v1/status", 30.0), ("GET", "/v1/wiki/index", 30.0),
                                             ("POST", "/v1/recall", cli.HTTP_TIMEOUT),
                                             ("POST", "/v1/reflect", cli.HTTP_TIMEOUT),
                                             ("POST", "/v1/retain?source_id=a&source_version=1", cli.HTTP_TIMEOUT)):
                with self.subTest(method=method, target=target):
                    cli._http_exchange(args, method, target)
                    self.assertEqual(connect.call_args.kwargs["timeout"], expected)
        self.assertGreater(cli.HTTP_TIMEOUT, cli.GET_TIMEOUT)


if __name__ == "__main__":
    unittest.main()
