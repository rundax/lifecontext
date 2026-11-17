"""Which model server URLs are accepted: https anywhere, plain http only on this machine, a private network
(or host.docker.internal), or on any host with --allow-insecure-http. Names are never resolved."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from lifecontext.hindsight import (
    HindsightStartupError,
    _validated_profile_settings,
    is_loopback_host,
    llm_key_required,
    needs_plain_http_grant,
    plain_http_allowed,
    validate_model_endpoint,
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
    def setUp(self) -> None:
        # host.docker.internal is accepted only inside the Docker image, which sets this variable.
        patcher = patch.dict(os.environ, {"LIFECONTEXT_IN_CONTAINER": "1"})
        patcher.start()
        self.addCleanup(patcher.stop)

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

    def test_the_grant_is_the_url_it_was_given_for_and_nothing_else(self) -> None:
        url = "http://models.lan:8000/v1"
        validated = _validated_profile_settings(_settings(url, allow_insecure_http=url))
        self.assertEqual((validated["base_url"], validated["allow_insecure_http"]), (url, url))
        # A URL that needs no grant keeps none, also when the grant names it.
        for needless in ("http://10.0.0.1/v1", "https://api.example.com/v1"):
            self.assertNotIn("allow_insecure_http",
                             _validated_profile_settings(_settings(needless, allow_insecure_http=needless)))
        # base_url edited by hand to any other http host: the old grant no longer applies.
        for edited in ("http://other.lan:8000/v1", "http://models.lan:8000/v1/", "http://models.lan:8001/v1",
                       "http://8.8.8.8/v1"):
            with self.subTest(edited=edited), self.assertRaises(HindsightStartupError) as caught:
                _validated_profile_settings(_settings(edited, allow_insecure_http=url))
            self.assertIn("--allow-insecure-http", caught.exception.message)
        # An old boolean true names no URL and grants nothing; false and a missing value are the same.
        for old in (True, False):
            with self.subTest(old=old):
                if old:
                    with self.assertRaises(HindsightStartupError):
                        _validated_profile_settings(_settings(url, allow_insecure_http=old))
                else:
                    _validated_profile_settings(_settings("https://api.example.com/v1", allow_insecure_http=old))
        for wrong in (1, ["http://models.lan/v1"], {"url": url}):
            with self.subTest(wrong=wrong), self.assertRaises(HindsightStartupError):
                _validated_profile_settings(_settings(url, allow_insecure_http=wrong))
        self.assertTrue(needs_plain_http_grant(url))
        self.assertFalse(needs_plain_http_grant("http://192.168.0.2/v1"))
        self.assertFalse(needs_plain_http_grant("https://models.lan/v1"))

    def test_vision_stays_loopback_only_for_plain_http(self) -> None:
        # Only the llm block gets the private-range rule: validate_model_endpoint's default is loopback.
        for url in ("http://127.0.0.1:1/v1", "http://[::ffff:127.0.0.1]/v1", "https://api.example.com/v1"):
            validate_model_endpoint("openai_compatible", "m", url, requires_base_url=True)
        for url in ("http://10.0.0.1/v1", "http://192.168.1.2/v1", "http://100.64.0.1/v1",
                    "http://host.docker.internal/v1", "http://models.lan/v1"):
            with self.subTest(url=url), self.assertRaises(HindsightStartupError):
                validate_model_endpoint("openai_compatible", "m", url, requires_base_url=True)
        from lifecontext.vision import load_vision_config, validated_vision_settings

        block = {"provider": "openai_compatible", "model": "v", "base_url": "http://192.168.1.2/v1"}
        with self.assertRaises(HindsightStartupError):
            validated_vision_settings({"vision": block})
        # So the llm key is never reused for a private-address vision endpoint.
        with self.assertRaises(HindsightStartupError):
            load_vision_config({"llm": {"provider": "openai_compatible", "model": "m",
                                        "base_url": "http://192.168.1.2/v1"}, "vision": block},
                               {"LIFEWEAVE_LLM_API_KEY": "key"})

    def test_host_docker_internal_is_accepted_only_inside_the_image(self) -> None:
        url = "http://host.docker.internal:11434/v1"
        self.assertEqual(_validated_profile_settings(_settings(url))["base_url"], url)
        for value in (None, "", "0", "yes"):
            with self.subTest(value=value), patch.dict(os.environ, {"LIFECONTEXT_IN_CONTAINER": value or ""}):
                if value is None:
                    os.environ.pop("LIFECONTEXT_IN_CONTAINER")
                with self.assertRaises(HindsightStartupError) as caught:
                    _validated_profile_settings(_settings(url))
                self.assertIn("inside the Docker image", caught.exception.message)
                # Outside the image it needs the grant, like any other name.
                self.assertEqual(
                    _validated_profile_settings(_settings(url, allow_insecure_http=url))["base_url"], url)

    def test_the_flag_does_not_excuse_the_other_url_rules(self) -> None:
        for url in ("http://evil.com@10.0.0.1/v1", "http://models.lan/v1?x=1", "http://models.lan/v1/chat/completions",
                    "http://models.lan:99999/v1", "http://fe80::1%25eth0/v1", "ftp://models.lan/v1"):
            with self.subTest(url=url), self.assertRaises(HindsightStartupError):
                _validated_profile_settings(_settings(url, allow_insecure_http=url))

    def test_plain_http_allowed_directly(self) -> None:
        self.assertTrue(plain_http_allowed("192.168.1.2", private_http=True))
        self.assertTrue(plain_http_allowed("::ffff:192.168.1.2", private_http=True))
        self.assertFalse(plain_http_allowed("192.168.1.2"))
        self.assertFalse(plain_http_allowed("example.com", private_http=True))
        self.assertTrue(plain_http_allowed("example.com", True))
        self.assertTrue(plain_http_allowed("::ffff:127.0.0.1"))
        self.assertTrue(is_loopback_host("::ffff:127.0.0.1"))
        self.assertFalse(is_loopback_host("::ffff:10.0.0.1"))

    def test_a_key_is_optional_for_any_openai_compatible_endpoint_only(self) -> None:
        for url in ("http://127.0.0.1:1/v1", "http://10.0.0.1/v1", "https://api.example.com/v1"):
            self.assertFalse(llm_key_required(_validated_profile_settings(_settings(url))))
        self.assertTrue(llm_key_required({"provider": "openai", "model": "m"}))
        self.assertTrue(llm_key_required({"provider": "openai", "model": "m", "base_url": "http://127.0.0.1/v1"}))


if __name__ == "__main__":
    unittest.main()
