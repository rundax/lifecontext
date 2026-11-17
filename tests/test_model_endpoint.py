"""Which model server URLs are accepted: https anywhere, plain http only on this machine, a private network
(or host.docker.internal), or on any host with --allow-insecure-http. Names are never resolved."""

from __future__ import annotations

import unittest

from lifecontext.hindsight import (
    HindsightStartupError,
    _validated_profile_settings,
    is_loopback_host,
    llm_key_required,
    plain_http_allowed,
)

ACCEPTED = (
    "http://127.0.0.1:11434/v1", "http://localhost:11434/v1", "http://[::1]:11434/v1",
    "http://[::ffff:127.0.0.1]/v1",
    "http://10.0.0.1/v1", "http://10.255.255.254:8000/v1", "http://[::ffff:10.0.0.1]/v1",
    "http://172.16.0.1/v1", "http://172.31.255.254/v1",
    "http://192.168.0.5:8080/v1",
    "http://100.64.0.1/v1", "http://100.127.255.254/v1",
    "http://[fc00::1]/v1", "http://[fd12:3456::1]:8000/v1",
    "http://host.docker.internal:11434/v1", "http://host.docker.internal./v1", "http://HOST.DOCKER.INTERNAL/v1",
    "https://api.example.com/v1", "https://10.0.0.1/v1",
)
REFUSED = (
    # Not on the private ranges.
    "http://8.8.8.8/v1", "http://172.15.255.254/v1", "http://172.32.0.1/v1", "http://100.63.255.254/v1",
    "http://100.128.0.1/v1", "http://192.169.0.1/v1", "http://11.0.0.1/v1",
    # Unspecified, link-local (cloud metadata), and mapped forms of them.
    "http://0.0.0.0:11434/v1", "http://[::]/v1", "http://169.254.169.254/v1", "http://[fe80::1]/v1",
    "http://[::ffff:0.0.0.0]/v1", "http://[::ffff:169.254.169.254]/v1", "http://[::ffff:8.8.8.8]/v1",
    "http://fe80::1%25eth0/v1", "http://[fe80::1%25eth0]/v1",
    # Other spellings of an address are names, and names are not resolved.
    "http://2130706433/v1", "http://0x7f000001/v1", "http://0177.0.0.1/v1", "http://10.1/v1",
    "http://0xa.1/v1", "http://010.0.0.1/v1",
    # Look-alikes and userinfo.
    "http://evil.com@10.0.0.1/v1", "http://10.0.0.1@evil.com/v1", "http://10.0.0.1.evil.com/v1",
    "http://host.docker.internal.evil.com/v1", "http://evil.host.docker.internal/v1",
    "http://host.docker.internal@evil.com/v1",
    # IDNA forms: a Cyrillic letter, full-width letters, a punycode name.
    "http://host.docker.internаl/v1", "http://ｈost.docker.internal/v1", "http://xn--hst-docker-internal-9ob.test/v1",
    # A public name, a hostname on a LAN, a public IPv6 address.
    "http://example.com/v1", "http://models.lan/v1", "http://[2001:db8::1]/v1",
)


def _settings(base_url: str, **llm: object) -> dict:
    return {"llm": {"provider": "openai_compatible", "model": "model", "base_url": base_url, **llm}}


class ModelEndpointPolicyTests(unittest.TestCase):
    def test_accepted_forms_need_no_flag(self) -> None:
        for url in ACCEPTED:
            with self.subTest(url=url):
                self.assertEqual(_validated_profile_settings(_settings(url))["base_url"], url)

    def test_everything_else_is_refused_and_a_policy_refusal_says_how_to_accept_it(self) -> None:
        for url in REFUSED:
            with self.subTest(url=url), self.assertRaises(HindsightStartupError) as caught:
                _validated_profile_settings(_settings(url))
            self.assertEqual(caught.exception.code, "invalid_config")
            if "@" not in url and "%" not in url:  # those two are malformed, not a policy refusal
                self.assertIn("--allow-insecure-http", caught.exception.message)
        with self.assertRaises(HindsightStartupError) as caught:
            _validated_profile_settings(_settings("http://example.com/v1"))
        self.assertIn("--allow-insecure-http", caught.exception.message)
        self.assertNotIn("example.com", caught.exception.message)

    def test_the_flag_accepts_a_plain_http_host_but_only_a_real_boolean(self) -> None:
        validated = _validated_profile_settings(_settings("http://models.lan:8000/v1", allow_insecure_http=True))
        self.assertEqual((validated["base_url"], validated["allow_insecure_http"]), ("http://models.lan:8000/v1", True))
        self.assertNotIn("allow_insecure_http",
                         _validated_profile_settings(_settings("http://10.0.0.1/v1", allow_insecure_http=False)))
        for value in ("true", "yes", 1, None):
            with self.subTest(value=value), self.assertRaises(HindsightStartupError):
                _validated_profile_settings(_settings("http://models.lan/v1", allow_insecure_http=value))

    def test_the_flag_does_not_excuse_the_other_url_rules(self) -> None:
        for url in ("http://evil.com@10.0.0.1/v1", "http://models.lan/v1?x=1", "http://models.lan/v1/chat/completions",
                    "http://models.lan:99999/v1", "http://fe80::1%25eth0/v1", "ftp://models.lan/v1"):
            with self.subTest(url=url), self.assertRaises(HindsightStartupError):
                _validated_profile_settings(_settings(url, allow_insecure_http=True))

    def test_plain_http_allowed_directly(self) -> None:
        self.assertTrue(plain_http_allowed("192.168.1.2"))
        self.assertTrue(plain_http_allowed("::ffff:192.168.1.2"))
        self.assertFalse(plain_http_allowed("example.com"))
        self.assertTrue(plain_http_allowed("example.com", True))
        self.assertTrue(is_loopback_host("::ffff:127.0.0.1"))
        self.assertFalse(is_loopback_host("::ffff:10.0.0.1"))

    def test_a_key_is_optional_for_any_openai_compatible_endpoint_only(self) -> None:
        for url in ("http://127.0.0.1:1/v1", "http://10.0.0.1/v1", "https://api.example.com/v1"):
            self.assertFalse(llm_key_required(_validated_profile_settings(_settings(url))))
        self.assertTrue(llm_key_required({"provider": "openai", "model": "m"}))
        self.assertTrue(llm_key_required({"provider": "openai", "model": "m", "base_url": "http://127.0.0.1/v1"}))


if __name__ == "__main__":
    unittest.main()
