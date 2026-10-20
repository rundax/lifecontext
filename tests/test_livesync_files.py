"""docker/livesync: the CouchDB hub's compose file and settings, and init-couchdb.sh against a fake CouchDB."""

from __future__ import annotations

import base64
import configparser
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
LIVESYNC = ROOT / "docker" / "livesync"
DIGEST = "couchdb@sha256:ccea1e8035bf7afd68336e24056a1ca9c1c46d7abe42da686322c0ac7218e5c2"
ADMIN_PASSWORD = "admin-password-0123456789"


class FakeCouch(BaseHTTPRequestHandler):
    """The few CouchDB calls the init script makes, with CouchDB's account rules for them."""

    admin = ("admin", ADMIN_PASSWORD)
    users: dict[str, dict]
    databases: dict[str, dict]

    def log_message(self, *args) -> None:
        pass

    def _who(self) -> tuple[str, str] | None:
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return None
        name, _, password = base64.b64decode(header[6:]).decode().partition(":")
        return name, password

    def _send(self, status: int, body: dict | None = None) -> None:
        raw = json.dumps(body or {}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _role(self) -> str | None:
        who = self._who()
        if who == self.admin:
            return "admin"
        for document in self.users.values():
            if who == (document["name"], document["password"]):
                return "user"
        return None

    def _body(self) -> dict:
        return json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")

    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        role = self._role()
        if path == "/_up":
            return self._send(200 if role else 401)
        if role is None:
            return self._send(401, {"error": "unauthorized"})
        if path == "/_all_dbs":
            return self._send(200 if role == "admin" else 403)
        if path.startswith("/_users/"):
            document = self.users.get(path.rsplit("/", 1)[1])
            return self._send(200, document) if role == "admin" and document else self._send(404 if role == "admin" else 403)
        database = self.databases.get(path[1:])
        if database is None:
            return self._send(404)
        members = database["security"].get("members", {}).get("names", [])
        who = self._who()
        return self._send(200 if role == "admin" or who[0] in members else 403)

    def do_PUT(self) -> None:
        path, _, query = self.path.partition("?")
        role = self._role()
        if role != "admin":
            return self._send(401 if role is None else 403)
        body = self._body()
        if path.startswith("/_users/"):
            key = path.rsplit("/", 1)[1]
            existing = self.users.get(key)
            revision = query.removeprefix("rev=")
            if existing and revision != existing["_rev"]:
                return self._send(409)
            number = int(existing["_rev"].split("-")[0]) + 1 if existing else 1
            self.users[key] = {**body, "_id": key, "_rev": f"{number}-x"}
            return self._send(201, {"ok": True})
        if path.endswith("/_security"):
            self.databases[path.split("/")[1]]["security"] = body
            return self._send(200, {"ok": True})
        if path[1:] in self.databases:
            return self._send(412)
        self.databases[path[1:]] = {"security": {}}
        return self._send(201, {"ok": True})


class StaticFilesTests(unittest.TestCase):
    def test_compose_pins_the_image_and_publishes_only_on_loopback(self) -> None:
        compose = yaml.safe_load((LIVESYNC / "compose.yaml").read_text())
        service = compose["services"]["couchdb"]
        self.assertEqual(service["image"], DIGEST)
        self.assertEqual(service["ports"], ["127.0.0.1:5984:5984"])
        self.assertIn("couchdb-data:/opt/couchdb/data", service["volumes"])
        self.assertIn("couchdb-data", compose["volumes"])
        self.assertEqual(service["env_file"], ["${COUCHDB_ENV_FILE:-./couchdb.env}"])
        self.assertNotIn("environment", service)
        self.assertEqual(service["restart"], "unless-stopped")
        self.assertIn("healthcheck", service)
        self.assertIn("./livesync.ini:/opt/couchdb/etc/local.d/livesync.ini:ro", service["volumes"])

    def test_couchdb_settings_require_accounts_and_allow_what_livesync_needs(self) -> None:
        settings = configparser.ConfigParser(interpolation=None)
        settings.read(LIVESYNC / "livesync.ini")
        self.assertEqual(settings["chttpd"]["require_valid_user"], "true")
        self.assertEqual(settings["chttpd_auth"]["require_valid_user"], "true")
        self.assertEqual(settings["couchdb"]["single_node"], "true")
        self.assertEqual(int(settings["couchdb"]["max_document_size"]), 50_000_000)
        self.assertEqual(int(settings["chttpd"]["max_http_request_size"]), 4 * 1024**3)
        self.assertEqual(settings["httpd"]["enable_cors"], "true")
        self.assertEqual(settings["cors"]["credentials"], "true")
        self.assertEqual(set(settings["cors"]["origins"].split(",")),
                         {"app://obsidian.md", "capacitor://localhost", "http://localhost"})
        for method in ("GET", "PUT", "POST", "HEAD", "DELETE"):
            self.assertIn(method, settings["cors"]["methods"])

    def test_secret_files_are_ignored_by_git(self) -> None:
        ignored = (LIVESYNC / ".gitignore").read_text().split()
        self.assertIn("couchdb.env", ignored)
        self.assertIn("*.password", ignored)


@unittest.skipUnless(shutil.which("curl"), "needs curl")
class InitScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="lifecontext-livesync-test.")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        FakeCouch.users, FakeCouch.databases = {}, {}
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeCouch)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.env_file = self.root / "couchdb.env"
        self.env_file.write_text(f"COUCHDB_USER=admin\nCOUCHDB_PASSWORD={ADMIN_PASSWORD}\n")
        self.env_file.chmod(0o600)
        self.password_file = self.root / "sync-user.password"
        # A curl that records its arguments, to show that no password is ever one of them.
        self.stubs = self.root / "stubs"
        self.stubs.mkdir()
        self.argv = self.root / "argv.log"
        curl = self.stubs / "curl"
        curl.write_text(f'#!/bin/sh\necho "$*" >> "{self.argv}"\nexec "{shutil.which("curl")}" "$@"\n')
        curl.chmod(0o755)

    def run_script(self, *arguments: str, url: str | None = None):
        env = {"PATH": f"{self.stubs}:/usr/bin:/bin", "HOME": str(self.root), "TMPDIR": str(self.root),
               "COUCHDB_URL": url or self.url}
        return subprocess.run(
            ["sh", str(LIVESYNC / "init-couchdb.sh"), "--env-file", str(self.env_file), "--password-file",
             str(self.password_file), *arguments],
            text=True, capture_output=True, env=env, stdin=subprocess.DEVNULL, timeout=120, check=False)

    def test_creates_the_database_and_a_member_only_sync_user_without_leaking_passwords(self) -> None:
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        password = self.password_file.read_text().strip()
        self.assertRegex(password, r"^[A-Za-z0-9]{40}$")
        self.assertEqual(stat.S_IMODE(self.password_file.stat().st_mode), 0o600)
        self.assertEqual(FakeCouch.databases["obsidian-livesync"]["security"], {
            "admins": {"names": [], "roles": []}, "members": {"names": ["livesync"], "roles": []}})
        user = FakeCouch.users["org.couchdb.user:livesync"]
        self.assertEqual((user["name"], user["password"], user["roles"], user["type"]),
                         ("livesync", password, [], "user"))
        self.assertIn("Created the database obsidian-livesync", result.stdout)
        for text in (result.stdout, result.stderr, self.argv.read_text()):
            self.assertNotIn(password, text)
            self.assertNotIn(ADMIN_PASSWORD, text)
        self.assertEqual(os.listdir(self.root / "stubs"), ["curl"])
        self.assertEqual([name for name in os.listdir(self.root) if name.startswith("livesync-init.")], [])

    def test_running_it_again_keeps_the_database_and_updates_the_user(self) -> None:
        self.assertEqual(self.run_script().returncode, 0)
        password = self.password_file.read_text()
        self.password_file.write_text("a-new-sync-password-from-the-file\n")
        again = self.run_script("--database", "obsidian-livesync")
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn("already exists", again.stdout)
        self.assertNotIn("Created", again.stdout)
        user = FakeCouch.users["org.couchdb.user:livesync"]
        self.assertEqual(user["password"], "a-new-sync-password-from-the-file")
        self.assertNotEqual(user["password"], password.strip())
        self.assertEqual(user["_rev"], "2-x")

    def test_other_names_and_a_wrong_admin_password(self) -> None:
        self.assertEqual(self.run_script("--database", "vault-two", "--user", "phone").returncode, 0)
        self.assertIn("vault-two", FakeCouch.databases)
        self.assertIn("org.couchdb.user:phone", FakeCouch.users)
        self.env_file.write_text("COUCHDB_USER=admin\nCOUCHDB_PASSWORD=wrong\n")
        refused = self.run_script()
        self.assertEqual(refused.returncode, 1)
        self.assertIn("refused the admin account", refused.stderr)

    def test_unsafe_input_is_refused_before_anything_is_sent(self) -> None:
        for arguments, url in (
            (("--database", "Bad Name"), None),
            (("--user", "bad:name"), None),
            ((), "http://example.invalid:5984"),
        ):
            with self.subTest(arguments=arguments, url=url):
                result = self.run_script(*arguments, url=url)
                self.assertEqual(result.returncode, 1)
        self.assertFalse(self.argv.exists())

    def test_the_url_must_really_name_this_machine_before_the_admin_password_is_sent(self) -> None:
        port = self.server.server_address[1]
        for url in (
            f"http://127.0.0.1:{port}@evil.example",  # the real host is after the "@"
            f"http://127.0.0.1:{port}@evil.example/",
            f"http://localhost:{port}@127.0.0.1:{port}",
            f"http://user:secret@127.0.0.1:{port}",
            f"http://127.0.0.1:{port}.evil.example",
            f"http://127.0.0.1.evil.example:{port}",
            f"http://localhost.evil.example:{port}",
            f"http://127.0.0.1:{port}#@evil.example",
            f"http://127.0.0.1:{port}?@evil.example",
            f"http://127.0.0.1:{port}\\@evil.example",
            f"http://127.0.0.1:{port}:evil",
            "http://127.0.0.1",
            "http://127.0.0.1:/",
            "http://[::1]:abc",
            "http://example.invalid:5984",
            "ftp://127.0.0.1:5984",
            "https://secure@evil.example/",
            "https://evil.example:12x",
            "https://:5984",
            "127.0.0.1:5984",
        ):
            with self.subTest(url=url):
                result = self.run_script(url=url)
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn("COUCHDB_URL must be", result.stderr)
        self.assertFalse(self.argv.exists())

    def test_loopback_and_https_urls_are_accepted_with_or_without_a_trailing_slash(self) -> None:
        for url in (self.url, self.url + "/"):
            with self.subTest(url=url):
                self.assertEqual(self.run_script(url=url).returncode, 0)

    def test_the_url_check_accepts_the_forms_it_documents(self) -> None:
        text = (LIVESYNC / "init-couchdb.sh").read_text()
        function = re.search(r"^valid_url\(\) \{\n.*?^\}\n", text, re.MULTILINE | re.DOTALL).group(0)
        harness = self.root / "valid_url.sh"
        harness.write_text(function + '\nvalid_url "$1"\n')
        for url, valid in (
            ("http://127.0.0.1:5984", True), ("http://127.0.0.1:5984/", True), ("http://localhost:5984", True),
            ("http://[::1]:5984", True), ("http://127.0.0.1:5984/couch", True),
            ("https://hub.example.org", True), ("https://hub.example.org:8443/", True),
            ("http://[::1]:5984@evil.example", False), ("https://hub.example.org@evil.example", False),
            ("http://hub.example.org:5984", False), ("http://[::2]:5984", False),
        ):
            with self.subTest(url=url):
                result = subprocess.run(["sh", str(harness), url], capture_output=True, timeout=30, check=False)
                self.assertEqual(result.returncode == 0, valid)

    def test_a_bad_env_file_is_refused_before_anything_is_sent(self) -> None:
        self.env_file.write_text('COUCHDB_USER=admin\nCOUCHDB_PASSWORD=has"quote\n')
        self.assertEqual(self.run_script().returncode, 1)
        self.assertFalse(self.argv.exists())
        self.env_file.write_text("COUCHDB_USER=admin\n")
        self.assertIn("must set COUCHDB_USER and COUCHDB_PASSWORD", self.run_script().stderr)

    def test_a_readable_env_file_is_flagged(self) -> None:
        self.env_file.chmod(0o644)
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("accessible to other users", result.stderr)


if __name__ == "__main__":
    unittest.main()
