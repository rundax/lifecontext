"""``hindsight.model_probe`` against a fake provider on loopback: one cause per failure, no provider text out."""

from __future__ import annotations

import contextlib
import io
import json
import logging
import socket
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from lifecontext.hindsight import PROBE_CAUSES, model_probe, probe_cause

KEY = "sk-probe-private-key-0123456789"
# What a provider puts in an error: the raw message, often with a masked key.
LEAK = f"SECRET-PROVIDER-TEXT key {KEY[:3]}...{KEY[-4:]} {KEY}"
CREDENTIALS = {"LIFEWEAVE_LLM_API_KEY": KEY}


def _error(status: int, message: str = LEAK) -> tuple[int, dict]:
    return status, {"error": {"message": message, "type": "invalid_request_error", "code": None}}


class _FakeProvider(ThreadingHTTPServer):
    """Answers OpenAI chat completions and Anthropic messages from a script; ``None`` answers correctly."""

    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.script: list[tuple[int, dict] | float | None] = []
        self.requests: list[dict] = []

    @property
    def port(self) -> int:
        return self.server_address[1]


class _Handler(BaseHTTPRequestHandler):
    server: _FakeProvider

    def log_message(self, *args) -> None:  # keep the test output clean
        pass

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append({"path": self.path, "body": body})
        step = self.server.script.pop(0) if self.server.script else None
        if isinstance(step, float):
            time.sleep(step)
            step = None
        status, answer = step if step is not None else (200, self._answer(body))
        data = json.dumps(answer).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _answer(self, body: dict) -> dict:
        usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
        if self.path.endswith("/messages"):  # Anthropic: a tool_use block for the (first) tool
            name = body.get("tool_choice", {}).get("name") or body["tools"][0]["name"]
            return {"id": "msg_1", "type": "message", "role": "assistant", "model": body["model"],
                    "content": [{"type": "tool_use", "id": "toolu_1", "name": name, "input": {"answer": "ok"}}],
                    "stop_reason": "tool_use", "stop_sequence": None,
                    "usage": {"input_tokens": 1, "output_tokens": 1}}
        if "tools" in body:
            message = {"role": "assistant", "content": None, "tool_calls": [{
                "id": "call_1", "type": "function",
                "function": {"name": body["tools"][0]["function"]["name"], "arguments": '{"answer": "ok"}'}}]}
            finish = "tool_calls"
        else:
            message, finish = {"role": "assistant", "content": '{"answer": "ok"}'}, "stop"
        return {"id": "c1", "object": "chat.completion", "created": 0, "model": body["model"],
                "choices": [{"index": 0, "message": message, "finish_reason": finish}], "usage": usage}


class ModelProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = _FakeProvider()
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def settings(self, provider: str = "openai_compatible", port: int | None = None) -> dict:
        root = f"http://127.0.0.1:{port or self.server.port}"
        return {"llm": {"provider": provider, "model": "probe-model",
                        "base_url": root if provider == "anthropic" else root + "/v1"}}

    def probe(self, *script, provider: str = "openai_compatible", port: int | None = None,
              timeout: float = 20.0, credentials=CREDENTIALS) -> dict:
        """Run the probe with every log record and all output captured, and check none of it leaks."""
        self.server.script = list(script)
        records: list[logging.LogRecord] = []
        handler = logging.Handler(logging.DEBUG)
        handler.emit = records.append
        root = logging.getLogger()
        root.addHandler(handler)
        previous = root.level
        root.setLevel(logging.DEBUG)
        out, err = io.StringIO(), io.StringIO()
        try:
            with patch("lifecontext.hindsight.load_credentials", return_value=credentials), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                result = model_probe(self.settings(provider, port), timeout=timeout)
        finally:
            root.removeHandler(handler)
            root.setLevel(previous)
        printed = json.dumps(result) + out.getvalue() + err.getvalue() + "".join(
            record.getMessage() for record in records)
        for secret in ("SECRET-PROVIDER-TEXT", KEY, KEY[-4:]):
            self.assertNotIn(secret, printed)
        self.assertEqual(logging.getLogger("hindsight_api").level, logging.NOTSET)
        self.assertEqual(logging.root.manager.disable, logging.NOTSET)
        return result

    def test_a_working_model_passes_through_retains_strict_path_and_a_named_tool(self) -> None:
        result = self.probe()
        self.assertEqual(result, {"status": "pass", "cause": None, "reflect": {"status": "pass"}})
        retain, reflect = (request["body"] for request in self.server.requests)
        self.assertEqual(retain["response_format"]["type"], "json_schema")
        self.assertIs(retain["response_format"]["json_schema"]["strict"], True)
        # Hindsight sends a named choice as "required" with only that tool, as reflect does.
        self.assertEqual(reflect["tool_choice"], "required")
        self.assertEqual([tool["function"]["name"] for tool in reflect["tools"]], ["record_answer"])

    def test_each_failure_has_its_own_cause_and_no_provider_text(self) -> None:
        for script, cause in (
            ([_error(401)], "key_rejected"),
            ([_error(403)], "key_rejected"),
            ([_error(404)], "model_not_found"),
            ([_error(400, f"response_format json_schema is not supported {LEAK}")], "structured_output_unsupported"),
            ([_error(400, f"tool_choice is not supported {LEAK}")], "structured_output_unsupported"),
            ([_error(400)], "failed"),
            ([_error(500)], "failed"),
            ([(200, {"choices": [{"index": 0, "finish_reason": "stop",
                                  "message": {"role": "assistant", "content": "not json at all"}}]})],
             "structured_output_unsupported"),
        ):
            with self.subTest(cause=cause, status=script[0][0]):
                self.server.requests.clear()
                result = self.probe(*script)
                message, fix = PROBE_CAUSES[cause]
                self.assertEqual(result, {"status": "fail", "cause": cause, "message": message, "fix": fix,
                                          "reflect": {"status": "skipped"}})
                self.assertEqual(len(self.server.requests), 1)  # no retries, no reflect check
                self.assertIn("lifecontext", fix)

    def test_the_key_message_and_fix_stay_as_ci_expects(self) -> None:
        result = self.probe(_error(401))
        self.assertEqual(result["message"], "the provider rejected the key")
        self.assertTrue(result["fix"].startswith("lifecontext llm set --api-key-stdin"))

    def test_a_rate_limit_is_retried_once(self) -> None:
        self.assertEqual(self.probe(_error(429), None)["status"], "pass")
        self.assertEqual(len(self.server.requests), 3)  # 429, the retry, the reflect check
        self.server.requests.clear()
        self.assertEqual(self.probe(_error(429), _error(429))["cause"], "rate_limited")
        self.assertEqual(len(self.server.requests), 2)

    def test_an_endpoint_that_does_not_answer(self) -> None:
        with socket.socket() as unused:
            unused.bind(("127.0.0.1", 0))
            closed = unused.getsockname()[1]
        self.assertEqual(self.probe(port=closed)["cause"], "unreachable")
        started = time.monotonic()
        self.assertEqual(self.probe(3.0, timeout=1.0)["cause"], "unreachable")
        self.assertLess(time.monotonic() - started, 2.9)

    def test_a_refused_named_tool_only_warns(self) -> None:
        result = self.probe(None, _error(400, f"tool_choice with a named function is not supported {LEAK}"))
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["reflect"]["status"], "warn")
        self.assertEqual(result["reflect"]["cause"], "structured_output_unsupported")
        self.assertIn("reflect", result["reflect"]["message"])

    def test_anthropic_takes_the_forced_tool_path(self) -> None:
        self.assertEqual(self.probe(provider="anthropic")["status"], "pass")
        retain, reflect = (request for request in self.server.requests)
        self.assertEqual(retain["path"], "/v1/messages")
        self.assertEqual(retain["body"]["tool_choice"], {"type": "tool", "name": "structured_response"})
        # Hindsight 0.9.2's Anthropic adapter sends tools without a tool_choice; the probe sends what reflect does.
        self.assertEqual([tool["name"] for tool in reflect["body"]["tools"]], ["record_answer"])
        self.server.requests.clear()
        result = self.probe((401, {"type": "error", "error": {"type": "authentication_error", "message": LEAK}}),
                            provider="anthropic")
        self.assertEqual(result["cause"], "key_rejected")

    def test_no_key_and_bad_settings_fail_without_a_request(self) -> None:
        settings = {"llm": {"provider": "openai", "model": "gpt-test"}}
        with patch("lifecontext.hindsight.load_credentials", return_value={}):
            self.assertEqual(model_probe(settings)["cause"], "key_missing")
        self.assertEqual(model_probe({"llm": {"provider": "nonsense", "model": "m"}})["cause"], "invalid_config")
        self.assertEqual(self.server.requests, [])


class ProbeCauseTests(unittest.TestCase):
    """Errors from SDKs the fake endpoint cannot stand in for, such as google-genai's ``code``."""

    def test_status_from_code_and_through_the_cause_chain(self) -> None:
        class GenaiError(Exception):
            def __init__(self, code: int, text: str):
                super().__init__(text)
                self.code = code

        self.assertEqual(probe_cause(GenaiError(400, "API key not valid. Please pass a valid API key.")),
                         "key_rejected")
        self.assertEqual(probe_cause(GenaiError(404, "models/x is not found")), "model_not_found")
        self.assertEqual(probe_cause(GenaiError(429, "quota")), "rate_limited")
        try:
            try:
                raise GenaiError(429, "quota")
            except GenaiError as inner:
                raise RuntimeError("quota exhausted; retry at a later time") from inner
        except RuntimeError as outer:
            self.assertEqual(probe_cause(outer), "rate_limited")
        self.assertEqual(probe_cause(TimeoutError()), "unreachable")
        self.assertEqual(probe_cause(ConnectionRefusedError()), "unreachable")
        self.assertEqual(probe_cause(ValueError("something else")), "failed")


if __name__ == "__main__":
    unittest.main()
