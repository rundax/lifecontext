"""Dev TLS forwarding boundaries, without credentials or external requests."""
import http.client
from http.server import ThreadingHTTPServer
import importlib.util
from pathlib import Path
import threading
import unittest
from unittest.mock import MagicMock, patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/dev_llm_forwarder.py"
spec = importlib.util.spec_from_file_location("dev_llm_forwarder", SCRIPT)
forwarder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(forwarder)


class DevLLMForwarderTests(unittest.TestCase):
    def test_only_bounded_loopback_requests_cross_verified_tls(self):
        with patch.object(forwarder.ssl, "create_default_context") as tls, patch.object(
            forwarder.http.client, "HTTPSConnection"
        ) as upstream:
            context = tls.return_value
            response = upstream.return_value.getresponse.return_value
            response.status = 200
            response.read.return_value = b'{"data":[]}'
            response.getheader.return_value = "application/json"
            server = ThreadingHTTPServer(("127.0.0.1", 0), forwarder.handler_for("https://llm.test:8317", "/trusted/ca.crt"))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                def request(path, body=None, headers=None, method="POST"):
                    # Use the real connection class, which the module patch also shadows.
                    conn = original_http_connection("127.0.0.1", server.server_port, timeout=2)
                    conn.request(method, path, body=body, headers=headers or {})
                    reply = conn.getresponse()
                    status, payload = reply.status, reply.read()
                    conn.close()
                    return status, payload

                status, _ = request("/v1/chat/completions", b'{"messages":[]}', {"Authorization": "Bearer synthetic"})
                self.assertEqual(status, 200)
                tls.assert_called_once_with(cafile="/trusted/ca.crt")
                upstream.assert_called_with("llm.test", 8317, timeout=180, context=context)
                sent = upstream.return_value.request.call_args
                self.assertEqual(sent.kwargs["headers"]["Authorization"], "Bearer synthetic")
                calls = upstream.call_count
                for path, headers in (("/other", {}), ("/v1/models", {"Origin": "https://outside.test"}),
                                      ("/v1/models", {"Host": "outside.test"}),
                                      ("/v1/models", {"Content-Length": str(forwarder.MAX_BYTES + 1)})):
                    self.assertIn(request(path, headers=headers, method="GET")[0], (403, 404, 413))
                    self.assertEqual(upstream.call_count, calls)
                response.status = 401
                response.read.return_value = b"secret provider detail"
                status, payload = request("/v1/models", method="GET")
                self.assertEqual(status, 401)
                self.assertNotIn(b"secret", payload)
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

    def test_refuses_unencrypted_or_credential_bearing_upstream(self):
        for value in ("http://llm.test", "https://user:secret@llm.test", "https://llm.test/v1"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                forwarder.handler_for(value, "/unused")


original_http_connection = http.client.HTTPConnection

if __name__ == "__main__":
    unittest.main()
