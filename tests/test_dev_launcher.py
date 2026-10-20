"""Exercise the executable launcher with disposable paths, never a live API."""
import json
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import tempfile
import unittest


V1 = Path(__file__).resolve().parents[1]


class DevLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "checkout with spaces"
        (self.root / "bin").mkdir(parents=True)
        self.launcher = self.root / "bin" / "lifecontext-dev"
        shutil.copy2(V1 / "bin" / "lifecontext-dev", self.launcher)
        for relative in (".dev", ".dev/home", ".dev/home/.lifeweave", ".dev/tmp",
                         ".dev/vault", ".dev/vault/Journal", ".dev/venv/bin"):
            (self.root / relative).mkdir(mode=0o700, parents=True, exist_ok=True)
        python = self.root / ".dev/venv/bin/python"
        python.write_text("#!/bin/sh\nexec " + shlex.quote(str(V1 / ".dev/venv/bin/python")) + ' "$@"\n')
        python.chmod(0o755)
        self.config = self.root / ".dev/home/.lifeweave/config.yaml"
        self.write_config(self.root / ".dev/vault")
        cli = self.root / ".dev/venv/bin/lifecontext"
        cli.write_text('#!/usr/bin/python3\nimport os, sys, json\n'
                       'print(json.dumps({"args": sys.argv[1:], "home": os.environ["HOME"], '
                       '"cwd": os.getcwd(), "tmp": os.environ["TMPDIR"], "stdin": sys.stdin.read()}))\n')
        cli.chmod(0o755)

    def write_config(self, vault):
        self.config.write_text(f'vault:\n  root: {vault}\n  journal_path: {vault}/Journal\n')
        self.config.chmod(0o600)

    def run_launcher(self, *args):
        return subprocess.run([str(self.launcher), *args], cwd=self.temp.name,
                              env={**os.environ, "HOME": "/production", "PYTHONPATH": "/bad"},
                              text=True, capture_output=True, input="piped source bytes")

    def test_arguments_and_environment_from_other_directory(self):
        result = self.run_launcher("recall", "--query", "a query with spaces", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(output["args"], ["--port", "18765", "recall", "--query", "a query with spaces", "--json"])
        self.assertEqual(output["home"], str(self.root / ".dev/home"))
        self.assertEqual(output["tmp"], str(self.root / ".dev/tmp"))
        self.assertEqual(output["cwd"], self.temp.name)
        self.assertEqual(output["stdin"], "piped source bytes")

    def test_missing_or_unsafe_paths_fail_without_running_cli(self):
        for relative in (".dev/home", ".dev/home/.lifeweave", ".dev/vault/Journal"):
            path = self.root / relative
            with self.subTest(relative=relative):
                path.chmod(0o755)
                result = self.run_launcher("--version")
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertEqual(path.stat().st_mode & 0o777, 0o755)
                path.chmod(0o700)
        self.config.unlink()
        self.assertNotEqual(self.run_launcher("--version").returncode, 0)

    def test_config_drift_and_symlink_fail(self):
        self.write_config(Path("/production"))
        self.assertNotEqual(self.run_launcher("status").returncode, 0)
        self.write_config(self.root / ".dev/vault")
        self.config.chmod(0o644)
        self.assertNotEqual(self.run_launcher("status").returncode, 0)
        self.config.chmod(0o600)
        journal = self.root / ".dev/vault/Journal"
        journal.rmdir()
        journal.symlink_to(self.root / ".dev/home", target_is_directory=True)
        self.assertNotEqual(self.run_launcher("status").returncode, 0)

    def test_legacy_launcher_name_links_to_the_launcher(self):
        legacy = V1 / "bin" / "life-context-dev"
        self.assertEqual(os.readlink(legacy), "lifecontext-dev")
        self.assertEqual(legacy.resolve(strict=True), (V1 / "bin" / "lifecontext-dev").resolve(strict=True))

    def test_port_overrides_including_argparse_abbreviations_fail(self):
        for option in ("--port", "--p", "--po", "--por", "--port=8765"):
            with self.subTest(option=option):
                result = self.run_launcher("status", option, "8765")
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
