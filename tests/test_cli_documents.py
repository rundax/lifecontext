"""Document, category, and wiki client contract checks."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from local_paths import scratch_root

from lifecontext import cli


class DocumentCLIContractTests(unittest.TestCase):
    def args(self, *arguments):
        return cli._build_parser().parse_args(arguments)

    def test_document_mutations_allow_extraction_time_without_slowing_reads(self):
        from lifecontext.extraction import ExtractionLimits
        from lifecontext.vision import VISION_DEADLINE_SECONDS, VISION_PROBE_SECONDS
        connection = MagicMock()
        response = connection.getresponse.return_value
        response.read.return_value = b"{}"
        response.getheaders.return_value = []
        response.status = 200
        with patch.object(cli, "local_token", return_value="a" * 64), patch.object(cli.http.client, "HTTPConnection", return_value=connection) as connect:
            for target in ("/v1/documents", "/v1/documents/operations/op_test/retry"):
                cli._http_exchange(self.args("status"), "POST", target)
                self.assertEqual(
                    connect.call_args.kwargs["timeout"],
                    cli.HTTP_TIMEOUT + ExtractionLimits().max_seconds + VISION_DEADLINE_SECONDS + VISION_PROBE_SECONDS,
                )
            cli._http_exchange(self.args("status"), "GET", "/v1/documents")
            self.assertEqual(connect.call_args.kwargs["timeout"], cli.HTTP_TIMEOUT)

    def test_available_token_applies_to_all_versioned_routes(self):
        connection = MagicMock()
        response = connection.getresponse.return_value
        response.read.return_value = b"{}"
        response.getheaders.return_value = []
        response.status = 200
        args = self.args("status")
        with patch.object(cli, "local_token", return_value="a" * 64) as token, patch.object(cli.http.client, "HTTPConnection", return_value=connection):
            for target in ("/v1/documents?limit=1", "/v1/categories", "/v1/wiki/page"):
                cli._http_exchange(args, "GET", target)
                self.assertEqual(connection.request.call_args.kwargs["headers"]["Authorization"], "Bearer " + "a" * 64)
            self.assertEqual(token.call_count, 3)
            for target in ("/v1/status", "/v1/evidence/source", "/v1/documents-other"):
                cli._http_exchange(args, "GET", target)
                self.assertEqual(connection.request.call_args.kwargs["headers"]["Authorization"], "Bearer " + "a" * 64)
            self.assertEqual(token.call_count, 6)
            self.assertTrue(all(call.kwargs == {"create": False} for call in token.call_args_list))

    def test_missing_token_fails_every_versioned_route_without_connecting(self):
        with patch.object(cli, "local_token", side_effect=FileNotFoundError), patch.object(cli.http.client, "HTTPConnection") as connect:
            for target in ("/v1/status", "/v1/evidence/source", "/v1/recall", "/v1/documents"):
                with self.subTest(target=target), self.assertRaises(cli.CLIError) as error:
                    cli._http_exchange(self.args("status"), "GET", target)
                self.assertEqual(error.exception.code, "token_unavailable")
            connect.assert_not_called()
        stderr = io.StringIO()
        with patch.object(cli, "local_token", side_effect=FileNotFoundError), contextlib.redirect_stderr(stderr):
            self.assertEqual(cli.main(["status"]), 1)
        self.assertIn("No LifeContext API token for this user", stderr.getvalue())

    def test_unavailable_token_fails_redacted_without_connecting(self):
        with patch.object(cli, "local_token", side_effect=OSError("private path")), patch.object(cli.http.client, "HTTPConnection") as connect:
            with self.assertRaises(cli.CLIError) as error:
                cli._http_exchange(self.args("documents", "list"), "GET", "/v1/documents")
            self.assertEqual(error.exception.code, "unauthorized")
            connect.assert_not_called()

    def test_category_mutation_payloads_match_routes(self):
        cases = [
            (("approve", "proposal", "--category-id", "research", "--expected-revision", "2"), "/v1/categories/proposals/proposal/approve", {"expected_revision": 2, "category_id": "research", "budget_override": False}),
            (("reject", "proposal", "--expected-revision", "2"), "/v1/categories/proposals/proposal/reject", {"expected_revision": 2}),
            (("redirect", "proposal", "--category-id", "research", "--expected-revision", "2"), "/v1/categories/proposals/proposal/redirect", {"expected_revision": 2, "category_id": "research"}),
            (("rename", "research", "--name", "Studies", "--expected-revision", "2"), "/v1/categories/research/rename", {"expected_revision": 2, "name": "Studies"}),
            (("alias", "research", "--alias", "Studies", "--expected-revision", "2"), "/v1/categories/research/alias", {"expected_revision": 2, "alias": "Studies"}),
            (("merge", "research", "--target-category-id", "studies", "--expected-source-revision", "2", "--expected-target-revision", "3"), "/v1/categories/research/merge", {"target_category_id": "studies", "expected_source_revision": 2, "expected_target_revision": 3}),
            (("reassign", "doc_fixture", "--category-id", "research", "--expected-revision", "2"), "/v1/categories/reassign", {"expected_revision": 2, "source_id": "doc_fixture", "category_id": "research"}),
        ]
        for arguments, target, payload in cases:
            with self.subTest(arguments=arguments), patch.object(cli, "_post_json", return_value={}) as post:
                args = self.args("categories", *arguments)
                cli._dispatch(args)
                post.assert_called_once_with(args, target, payload)

    def test_document_import_sends_the_vision_opt_out_header(self):
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".pdf") as source:
            source.write(b"%PDF-1.4 fixture")
            source.flush()
            for flags, expected in (((), None), (("--no-vision",), "off")):
                with self.subTest(flags=flags), patch.object(cli, "_http_request", return_value=({}, "op_" + "b" * 32)) as request:
                    cli._dispatch(self.args("documents", "import", source.name, *flags))
                    self.assertEqual(request.call_args.kwargs["headers"].get("X-Document-Vision"), expected)

    def test_document_retry_and_wiki_routes(self):
        operation = "op_" + "a" * 32
        with patch.object(cli, "_post_json", return_value={}) as post:
            args = self.args("documents", "retry", operation, "--rerun-extraction")
            cli._dispatch(args)
            post.assert_called_once_with(args, f"/v1/documents/operations/{operation}/retry", {"rerun_extraction": True})
        for flag, value in (("--no-vision", False), ("--vision", True)):
            with self.subTest(flag=flag), patch.object(cli, "_post_json", return_value={}) as post:
                args = self.args("documents", "retry", operation, "--rerun-extraction", flag)
                cli._dispatch(args)
                post.assert_called_once_with(
                    args, f"/v1/documents/operations/{operation}/retry",
                    {"rerun_extraction": True, "vision": value},
                )
        with patch.object(cli, "_post_json", return_value={}) as post, self.assertRaises(cli.CLIError):
            cli._dispatch(self.args("documents", "retry", operation, "--no-vision"))
        post.assert_not_called()
        with self.assertRaises(SystemExit), patch("sys.stderr"):
            self.args("documents", "retry", operation, "--rerun-extraction", "--vision", "--no-vision")
        for arguments, target in [
            (("list",), "/v1/wiki"),
            (("status",), "/v1/wiki/status"),
            (("show", "page", "--revision", "2"), "/v1/wiki/page?revision=2"),
            (("history", "page"), "/v1/wiki/page/history"),
        ]:
            with self.subTest(arguments=arguments), patch.object(cli, "_get", return_value={}) as get:
                args = self.args("wiki", *arguments)
                cli._dispatch(args)
                get.assert_called_once_with(args, target)
        with patch.object(cli, "_post_json", return_value={}) as post:
            args = self.args("wiki", "rebuild", "--source-id", "doc_fixture", "--version", "2")
            cli._dispatch(args)
            post.assert_called_once_with(args, "/v1/wiki/rebuild", {"source_id": "doc_fixture", "version": 2})
        with self.assertRaises(cli.CLIError):
            cli._dispatch(self.args("wiki", "rebuild", "--version", "2"))

    def test_wiki_lint_and_review_routes(self):
        for arguments, target in [
            (("lint",), "/v1/wiki/lint"),
            (("lint", "--fail-on", "warning"), "/v1/wiki/lint"),
            (("reviews",), "/v1/wiki/reviews?state=pending"),
            (("reviews", "--page", "topic_x", "--state", "resolved"), "/v1/wiki/reviews?page=topic_x&state=resolved"),
        ]:
            with self.subTest(arguments=arguments), patch.object(cli, "_get", return_value={}) as get:
                args = self.args("wiki", *arguments)
                cli._dispatch(args)
                get.assert_called_once_with(args, target)
        with patch.object(cli, "_post_json", return_value={}) as post:
            args = self.args("wiki", "reviews", "resolve", "review_1", "--note", "Both stand.", "--idempotency-key", "k1")
            cli._dispatch(args)
            post.assert_called_once_with(args, "/v1/wiki/reviews/review_1/resolve", {"note": "Both stand."})
            self.assertEqual(args.idempotency_key, "k1")
        for invalid in (("reviews", "resolve", "review_1"), ("reviews", "resolve", "../x", "--note", "n"),
                        ("lint", "--fail-on", "info"), ("reviews", "--state", "open")):
            with self.subTest(invalid=invalid), self.assertRaises(SystemExit), patch("sys.stderr"):
                self.args("wiki", *invalid)

    def test_brief_routes(self):
        for arguments, target in [
            ((), "/v1/brief"),
            (("--date", "2026-10-16"), "/v1/brief?date=2026-10-16"),
            (("--since", "2026-10-16T08:00:00+02:00"), "/v1/brief?since=2026-10-16T08%3A00%3A00%2B02%3A00"),
        ]:
            with self.subTest(arguments=arguments), patch.object(cli, "_get", return_value={}) as get:
                args = self.args("brief", *arguments)
                cli._dispatch(args)
                get.assert_called_once_with(args, target)
        for arguments, target in [(("--project",), "/v1/brief/project"),
                                  (("--project", "--date", "2026-10-16"), "/v1/brief/project?date=2026-10-16")]:
            with self.subTest(arguments=arguments), patch.object(cli, "_http_request", return_value=({}, None)) as request:
                args = self.args("brief", *arguments)
                cli._dispatch(args)
                request.assert_called_once_with(args, "POST", target)
        with patch.object(cli, "_http_request") as request, self.assertRaises(cli.CLIError):
            cli._dispatch(self.args("brief", "--project", "--since", "2026-10-16T08:00:00"))
        request.assert_not_called()
        for invalid in (("--date", "2026-10-32"), ("--date", "16.10.2026"), ("--since", "yesterday"),
                        ("--date", "2026-10-16", "--since", "2026-10-16T08:00:00")):
            with self.subTest(invalid=invalid), self.assertRaises(SystemExit), patch("sys.stderr"):
                self.args("brief", *invalid)

    def test_wiki_lint_fail_on_exit_codes(self):
        import contextlib
        import io

        def report(error, warning, info):
            finding = {"code": "page_stale", "severity": "warning", "page_id": "source_doc", "source_id": "doc",
                       "detail": None, "hint": "lifecontext wiki rebuild --source-id doc --json"}
            return {"findings": [finding] if warning else [], "counts": {"error": error, "warning": warning, "info": info}, "omitted": 0}

        cases = [
            ((), report(1, 1, 1), 0),
            (("--fail-on", "error"), report(0, 2, 3), 0),
            (("--fail-on", "error"), report(1, 0, 0), cli.LINT_FAILED_EXIT),
            (("--fail-on", "warning"), report(0, 0, 5), 0),
            (("--fail-on", "warning"), report(0, 1, 0), cli.LINT_FAILED_EXIT),
            (("--fail-on", "warning"), {"findings": [], "counts": {"error": "0"}, "omitted": 0}, 1),
        ]
        for flags, payload, expected in cases:
            for output in ((), ("--json",)):
                with self.subTest(flags=flags, output=output), patch.object(cli, "_get", return_value=payload):
                    stdout = io.StringIO()
                    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
                        self.assertEqual(cli.main(["wiki", "lint", *flags, *output]), expected)
                    if expected != 1 and output:
                        self.assertEqual(json.loads(stdout.getvalue()), payload)
                    elif expected != 1 and payload["findings"]:
                        self.assertIn("- [warning] page_stale source_doc: lifecontext wiki rebuild", stdout.getvalue())
        with (patch.object(cli, "_get", side_effect=cli.CLIError("api_unavailable")),
              contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO())):
            self.assertEqual(cli.main(["wiki", "lint", "--fail-on", "error", "--json"]), 1)

    def test_download_verifies_checksum_before_publication(self):
        with tempfile.TemporaryDirectory(dir=scratch_root()) as temporary:
            destination = Path(temporary) / "original.bin"
            args = self.args("documents", "download", "doc_fixture", str(destination))
            content = b"binary\x00fixture"
            headers = {"content-type": "application/octet-stream", "content-length": str(len(content)), "etag": '"' + "0" * 64 + '"'}
            with patch.object(cli, "_http_exchange", return_value=(200, headers, content)):
                with self.assertRaises(cli.CLIError) as error:
                    cli._dispatch(args)
                self.assertEqual(error.exception.code, "invalid_api_response")
                self.assertFalse(destination.exists())
            headers["etag"] = '"' + hashlib.sha256(content).hexdigest() + '"'
            with patch.object(cli, "_http_exchange", return_value=(200, headers, content)):
                cli._dispatch(args)
                self.assertEqual(destination.read_bytes(), content)
                with self.assertRaises(cli.CLIError) as error:
                    cli._dispatch(args)
                self.assertEqual(error.exception.code, "destination_exists")

    def test_upload_requires_paired_identity_and_stdin_filename(self):
        for arguments in [
            ("-", "--source-id", "doc_fixture"),
            ("-", "--source-version", "2"),
            ("-",),
            ("-", "--filename", "../fixture.txt"),
        ]:
            with self.subTest(arguments=arguments), patch.object(cli, "_read_input") as read:
                with self.assertRaises(cli.CLIError):
                    cli._dispatch(self.args("documents", "import", *arguments))
                read.assert_not_called()


if __name__ == "__main__":
    unittest.main()
