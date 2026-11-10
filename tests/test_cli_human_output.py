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

    def test_other_read_commands_print_the_api_body_sorted_and_compact(self) -> None:
        for argv, body in ((["documents", "list"], {"documents": [DOCUMENT]}),
                           (["documents", "show", DOC_ID], DOCUMENT),
                           (["wiki", "list"], WIKI_LIST),
                           (["categories", "list"], CATEGORIES),
                           (["brief"], BRIEF),
                           (["reflect", "--query", "rent"], REFLECT)):
            with self.subTest(argv=argv):
                code, out, _ = run([*argv, "--json"], body)
                self.assertEqual(code, 0)
                self.assertEqual(out, json.dumps(body, ensure_ascii=False, sort_keys=True,
                                                 separators=(",", ":")) + "\n")

    def test_import_json_adds_only_the_operation_id(self) -> None:
        code, out, _ = run(["documents", "import", "lease.pdf", "--json"], IMPORT_RESULT)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {**IMPORT_RESULT, "operation_id": OP_ID})
        self.assertEqual(out, json.dumps({**IMPORT_RESULT, "operation_id": OP_ID}, ensure_ascii=False,
                                         sort_keys=True, separators=(",", ":")) + "\n")


if __name__ == "__main__":
    unittest.main()
