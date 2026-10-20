"""bin/lifecontext-docker: argument mapping, staged inputs and copied outputs, with a fake docker."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

WRAPPER = Path(__file__).resolve().parents[1] / "bin" / "lifecontext-docker"

# Runs `exec [-i] [-t] CONTAINER COMMAND...` locally, so the wrapper's in-container
# script really runs; every call is logged first.
FAKE_DOCKER = """#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
assert args[0] == "exec", args
args = args[1:]
options = []
while args[0].startswith("-"):
    options.append(args.pop(0))
container, command = args[0], args[1:]
with open(os.environ["FAKE_DOCKER_LOG"], "a") as log:
    log.write(json.dumps({"options": options, "container": container, "command": command}) + "\\n")
os.execvp(command[0], command)
"""

# Reports its arguments, stdin and the files it was handed; writes outputs.
FAKE_LIFECONTEXT = """#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
paths = [arg.removeprefix("--input=") for arg in args]
files = {path: open(path).read() for path in paths if path.startswith("/tmp/lifecontext-docker.") and os.path.isfile(path)}
stdin = sys.stdin.read()
if args[:2] == ["documents", "download"]:
    open(args[3], "w").write("original bytes")
if args[:1] == ["export"]:
    open(args[1], "w").write("bundle bytes")
print(json.dumps({"args": args, "files": files, "stdin": stdin}))
sys.exit(3 if "--fail-on" in args else 0)
"""


class DockerWrapperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="lifecontext-docker-test.")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        tools = self.root / "tools"
        tools.mkdir()
        for name, text in (("docker", FAKE_DOCKER), ("lifecontext", FAKE_LIFECONTEXT)):
            (tools / name).write_text(text)
            (tools / name).chmod(0o755)
        self.log = self.root / "docker.log"
        self.env = {**os.environ, "PATH": f"{tools}:{os.environ['PATH']}", "FAKE_DOCKER_LOG": str(self.log)}
        self.env.pop("LIFECONTEXT_CONTAINER", None)
        self.env.pop("LIFECONTEXT_DOCKER", None)

    def run_wrapper(self, *args: str, stdin: str = "", env: dict[str, str] | None = None):
        return subprocess.run([str(WRAPPER), *args], input=stdin, text=True, capture_output=True,
                              env={**self.env, **(env or {})}, cwd=self.root, timeout=60, check=False)

    def calls(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def leftover_stage_dirs(self, before: set[str]) -> set[str]:
        return {name for name in os.listdir("/tmp") if name.startswith("lifecontext-docker.")} - before

    def stage_dirs(self) -> set[str]:
        return {name for name in os.listdir("/tmp") if name.startswith("lifecontext-docker.")}

    def test_plain_command_and_stdin_pass_through(self) -> None:
        result = self.run_wrapper("retain", "src_a", "1", "--input", "-", "--json", stdin="entry text\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(output["args"], ["retain", "src_a", "1", "--input", "-", "--json"])
        self.assertEqual(output["stdin"], "entry text\n")
        (call,) = self.calls()
        self.assertEqual(call["options"], ["-i"])
        self.assertEqual(call["container"], "lifecontext")
        self.assertEqual(call["command"][0], "lifecontext")

    def test_container_name_and_exit_status(self) -> None:
        result = self.run_wrapper("wiki", "lint", "--fail-on", "error", env={"LIFECONTEXT_CONTAINER": "lc-test"})
        self.assertEqual(result.returncode, 3)
        self.assertEqual(self.calls()[0]["container"], "lc-test")

    def test_retain_input_file_is_staged_under_its_name_and_removed(self) -> None:
        source = self.root / "my entry.txt"
        source.write_text("# Title\nEntry date: 2026-10-19 (UTC).\n")
        before = self.stage_dirs()
        result = self.run_wrapper("retain", "src_b", "1", "--input", str(source), "--idempotency-key", "k1", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        staged = output["args"][4]
        self.assertTrue(staged.startswith("/tmp/lifecontext-docker."), staged)
        self.assertTrue(staged.endswith("/in/my entry.txt"), staged)
        self.assertEqual(output["args"][:4], ["retain", "src_b", "1", "--input"])
        self.assertEqual(output["args"][5:], ["--idempotency-key", "k1", "--json"])
        self.assertEqual(output["files"][staged], source.read_text())
        self.assertEqual(self.leftover_stage_dirs(before), set())

    def test_equals_form_and_curate(self) -> None:
        source = self.root / "fixed.md"
        source.write_text("corrected")
        result = self.run_wrapper("curate", "correct", "src_c", "--expected-version", "1", f"--input={source}")
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertTrue(output["args"][-1].startswith("--input=/tmp/lifecontext-docker."))
        self.assertTrue(output["args"][-1].endswith("/in/fixed.md"))
        self.assertEqual(list(output["files"].values()), ["corrected"])

    def test_document_import_maps_the_file_after_valued_options(self) -> None:
        source = self.root / "lease.md"
        source.write_text("document")
        result = self.run_wrapper("documents", "import", "--mime", "text/markdown", "--source-id", "doc_x",
                                  "--source-version", "1", str(source), "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(output["args"][:8], ["documents", "import", "--mime", "text/markdown", "--source-id",
                                              "doc_x", "--source-version", "1"])
        self.assertTrue(output["args"][8].endswith("/in/lease.md"))
        self.assertEqual(output["files"][output["args"][8]], "document")

    def test_bundle_import_and_missing_or_linked_files_pass_unchanged(self) -> None:
        bundle = self.root / "bundle.lwb"
        bundle.write_text("bundle")
        output = json.loads(self.run_wrapper("import", str(bundle)).stdout)
        self.assertTrue(output["args"][1].endswith("/in/bundle.lwb"))
        missing = str(self.root / "missing.pdf")
        output = json.loads(self.run_wrapper("documents", "import", missing).stdout)
        self.assertEqual(output["args"], ["documents", "import", missing])
        link = self.root / "link.md"
        link.symlink_to(bundle)
        output = json.loads(self.run_wrapper("documents", "import", str(link)).stdout)
        self.assertEqual(output["args"], ["documents", "import", str(link)])

    def test_values_that_look_like_paths_are_not_mapped(self) -> None:
        source = self.root / "query.txt"
        source.write_text("x")
        output = json.loads(self.run_wrapper("recall", "--query", str(source), "--json").stdout)
        self.assertEqual(output["args"], ["recall", "--query", str(source), "--json"])
        output = json.loads(self.run_wrapper("evidence", "src_d", "--version", "1", "--json").stdout)
        self.assertEqual(output["args"], ["evidence", "src_d", "--version", "1", "--json"])

    def test_download_writes_a_new_host_file_and_refuses_an_existing_one(self) -> None:
        destination = self.root / "out" / "copy.pdf"
        destination.parent.mkdir()
        before = self.stage_dirs()
        result = self.run_wrapper("documents", "download", "doc_x", str(destination), "--version", "1", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(destination.read_text(), "original bytes")
        self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
        self.assertEqual(sorted(os.listdir(destination.parent)), ["copy.pdf"])
        self.assertEqual(self.leftover_stage_dirs(before), set())
        calls = len(self.calls())
        result = self.run_wrapper("documents", "download", "doc_x", str(destination), "--json")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout), {"error": "destination_exists"})
        self.assertEqual(len(self.calls()), calls)
        self.assertEqual(destination.read_text(), "original bytes")

    def test_export_writes_on_the_host(self) -> None:
        destination = self.root / "export.lwb"
        result = self.run_wrapper("export", str(destination), "--source-id", "src_a")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(destination.read_text(), "bundle bytes")


if __name__ == "__main__":
    unittest.main()
