#!/usr/bin/env python3
"""Expose the existing LAN TLS LLM route to the loopback-only dev runtime."""
from __future__ import annotations

import argparse
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ssl
from urllib.parse import urlsplit


MAX_BYTES = 4 * 1024 * 1024


def handler_for(upstream: str, ca_file: str):
    target = urlsplit(upstream)
    if (target.scheme != "https" or not target.hostname or target.username
            or target.password or target.path not in {"", "/"} or target.query or target.fragment):
        raise ValueError("upstream must be an HTTPS origin")
    context = ssl.create_default_context(cafile=ca_file)

    class Forwarder(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # Requests and provider responses may contain private source text.

        def do_GET(self):
            self.forward()

        def do_POST(self):
            self.forward()

        def forward(self):
            self.connection.settimeout(185)
            hosts = self.headers.get_all("Host", [])
            port = self.server.server_address[1]
            if (len(hosts) != 1 or hosts[0] not in {f"127.0.0.1:{port}", f"localhost:{port}"}
                    or self.headers.get("Origin")):
                self.fail(403)
                return
            if (self.command, self.path) not in {
                ("GET", "/v1/models"), ("POST", "/v1/chat/completions"),
            }:
                self.fail(404)
                return
            if self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length", [])) > 1:
                self.fail(400)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self.fail(400)
                return
            if not 0 <= length <= MAX_BYTES:
                self.fail(413)
                return
            body = self.rfile.read(length)
            if len(body) != length:
                self.fail(400)
                return
            connection = http.client.HTTPSConnection(
                target.hostname, target.port or 443, timeout=180, context=context,
            )
            try:
                headers = {name: self.headers[name] for name in ("Authorization", "Content-Type")
                           if name in self.headers}
                connection.request(self.command, self.path, body=body, headers=headers)
                response = connection.getresponse()
                payload = response.read(MAX_BYTES + 1)
                if len(payload) > MAX_BYTES:
                    self.fail(502)
                    return
                if response.status >= 400:
                    self.fail(response.status)
                    return
                self.send_response(response.status)
                self.send_header("Content-Type", response.getheader("Content-Type", "application/json"))
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            except (OSError, http.client.HTTPException):
                self.fail(502)
            finally:
                connection.close()

        def fail(self, status):
            payload = b'{"error":{"message":"dev LLM route unavailable"}}'
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return Forwarder


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream", required=True, help="existing HTTPS origin")
    parser.add_argument("--ca-file", required=True, help="trusted upstream CA certificate")
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", 18317), handler_for(args.upstream, args.ca_file))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
