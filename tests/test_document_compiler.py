"""Supplied-evidence generation shares validated provider config and guards secrets."""
import asyncio
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import AsyncMock, patch

from lifecontext.hindsight import HindsightStartupError, document_compiler


class DocumentCompilerTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_provider_key_and_reasoning_without_retrieval(self):
        settings = {"llm": {"provider": "openai_compatible", "model": "chosen-model",
                            "base_url": "https://example.test/v1", "reasoning_effort": "high"}}
        with patch("lifecontext.hindsight.load_credentials", return_value={"LIFEWEAVE_LLM_API_KEY": "private-test-key"}), patch("hindsight_api.engine.llm_wrapper.LLMProvider") as factory:
            model = factory.return_value
            model.call = AsyncMock(return_value='```json\n{"claims":[]}\n```')
            model.cleanup = AsyncMock()
            compile_document = document_compiler(settings)
            result = await compile_document("supplied evidence", max_tokens=2048)
            self.assertEqual(result, {"text": '{"claims":[]}'})
            self.assertEqual(factory.call_args.kwargs["provider"], "openai")
            self.assertEqual(factory.call_args.kwargs["api_key"], "private-test-key")
            self.assertEqual(factory.call_args.kwargs["reasoning_effort"], "high")
            self.assertEqual(model.call.call_args.kwargs["max_completion_tokens"], 2048)
            self.assertEqual(model.call.call_args.kwargs["max_retries"], 0)
            self.assertNotIn("tools", model.call.call_args.kwargs)
            model.cleanup.assert_awaited_once()

    async def test_failure_is_redacted_and_provider_closed(self):
        with patch("lifecontext.hindsight.load_credentials", return_value={"LIFEWEAVE_LLM_API_KEY": "private-test-key"}), patch("hindsight_api.engine.llm_wrapper.LLMProvider") as factory:
            model = factory.return_value
            model.call = AsyncMock(side_effect=RuntimeError("private-test-key"))
            model.cleanup = AsyncMock()
            compile_document = document_compiler({"llm": {"provider": "openai", "model": "chosen"}})
            with self.assertRaises(HindsightStartupError) as error:
                await compile_document("evidence")
            self.assertNotIn("private-test-key", str(error.exception))
            model.cleanup.assert_awaited_once()
            self.assertIsNone(factory.call_args.kwargs["reasoning_effort"])

    async def test_bounds_reject_before_model_call(self):
        with patch("lifecontext.hindsight.load_credentials", return_value={"LIFEWEAVE_LLM_API_KEY": "private-test-key"}), patch("hindsight_api.engine.llm_wrapper.LLMProvider") as factory:
            compile_document = document_compiler({"llm": {"provider": "openai", "model": "chosen"}})
            for query, max_tokens in (("x" * 65537, 4096), ("evidence", True), ("evidence", 8193)):
                with self.assertRaises(HindsightStartupError):
                    await compile_document(query, max_tokens=max_tokens)
            factory.assert_not_called()


    async def test_native_provider_sends_json_mode_and_returns_unparsed_text(self):
        observed = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                observed.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                payload = {"id": "test", "object": "chat.completion", "created": 1, "model": "chosen",
                           "choices": [{"index": 0, "message": {"role": "assistant", "content": '{"value":1,"value":2}'}, "finish_reason": "stop"}],
                           "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch("lifecontext.hindsight.load_credentials", return_value={"LIFEWEAVE_LLM_API_KEY": "private-test-key"}):
                compile_document = document_compiler({"llm": {"provider": "openai_compatible", "model": "chosen",
                                                              "base_url": f"http://127.0.0.1:{server.server_port}/v1"}})
                response = await compile_document("Return JSON", max_tokens=256)
            self.assertEqual(observed[0]["response_format"], {"type": "json_object"})
            self.assertEqual(response["text"], '{"value":1,"value":2}')
        finally:
            await asyncio.to_thread(server.shutdown)
            server.server_close()
