"""`lifecontext init --api-key-stdin` at a terminal asks for the key hidden instead of reading echoed input."""

from __future__ import annotations

import contextlib
import io
import json
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from test_llm import NEW_KEY, _home

from lifecontext import cli
from lifecontext.paths import LifeContextPaths


class InitKeyPromptTests(unittest.TestCase):
    def _init(self, *arguments: str, stdin: str = "", tty: bool = False) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        fake_stdin = SimpleNamespace(buffer=io.BytesIO(stdin.encode()), isatty=lambda: tty)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), patch.object(sys, "stdin", fake_stdin):
            code = cli.main(["init", "--provider", "openai", "--model", "gpt-test-model", "--timezone", "UTC",
                             *arguments])
        return code, out.getvalue(), err.getvalue()

    def test_api_key_stdin_at_a_terminal_asks_hidden_instead_of_reading_echoed_input(self) -> None:
        with _home() as home:
            with patch.object(cli, "_ask_secret", return_value=NEW_KEY) as asked:
                code, out, err = self._init("--api-key-stdin", stdin="echoed-key-must-not-be-read\n", tty=True)
            self.assertEqual((code, asked.call_count), (0, 1), err)
            self.assertIn("hidden", asked.call_args.args[0])
            stored = LifeContextPaths(home).credentials_path.read_text()
            self.assertIn(f"LIFEWEAVE_LLM_API_KEY={NEW_KEY}", stored)
            self.assertNotIn(NEW_KEY, out + err)
        with _home() as home:
            # Not at a terminal the pipe is read and nothing is asked.
            with patch.object(cli, "_ask_secret", side_effect=AssertionError("asked")):
                code, _, err = self._init("--api-key-stdin", stdin="sk-from-a-pipe-1234\n")
            self.assertEqual(code, 0, err)
            self.assertIn("sk-from-a-pipe-1234", LifeContextPaths(home).credentials_path.read_text())
        with _home():
            # An empty answer or Ctrl-D is an invalid key, and --json never asks.
            with patch.object(cli, "_ask_secret", side_effect=EOFError):
                code, _, err = self._init("--api-key-stdin", tty=True)
            self.assertEqual(code, 1)
            self.assertNotIn("Traceback", err)
            with patch.object(cli, "_ask_secret", side_effect=AssertionError("asked")):
                code, out, _ = self._init("--api-key-stdin", "--json", tty=True)
            self.assertEqual((code, json.loads(out)["error"]), (1, "api_key_required"))


if __name__ == "__main__":
    unittest.main()
