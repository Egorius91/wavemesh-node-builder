"""Regression coverage for the native panel log denial locations."""
import copy
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT / "scripts/lib"))
from nginx_renderer import render


class NginxLogIngressTests(unittest.TestCase):
    def fixture(self, backend="xui-native"):
        return {
            "node": {"role": "entry"},
            "panel": {"path": "/fixture-panel/"},
            "network": {"subscription": {
                "backend": backend,
                "path": "/fixture-subscription/",
                "local_port": 2096,
            }},
            "clients": [{"id": "fixture-client", "enabled": True,
                          "subscription_id": "fixture_sub_12345678"}],
            "relay_peers": [{"id": "fixture-relay",
                             "inbound": {"public_path": "/fixture-relay/",
                                         "local_port": 12001},
                             "allowed_entry_ips": ["192.0.2.1"]}],
            "routes": [{"id": "fixture-route", "kind": "cascade",
                        "entry": {"public_path": "/fixture-vpn-route/",
                                  "local_port": 12002}}],
        }

    def assert_denials_and_existing_routes(self, config, **kwargs):
        rendered = render(config, **kwargs)
        for family in ("logs", "xraylogs"):
            denial = (
                "location ^~ /fixture-panel/panel/api/server/"
                f"{family}/ {{\n    return 403;\n}}"
            )
            self.assertEqual(rendered.count(denial), 1)
        self.assertEqual(rendered.count("return 403;"), 2)
        for protected in (
            "/fixture-subscription/",
            "/fixture-relay/",
            "/fixture-vpn-route/",
        ):
            self.assertIn(protected, rendered)
        self.assertEqual(render(copy.deepcopy(config), **kwargs), rendered)
        return rendered

    def test_native_subscription_routes_alias_and_extra_listener_are_preserved(self):
        rendered = self.assert_denials_and_existing_routes(
            self.fixture(),
            native_alias=("/fixture-old-sub/", "/fixture-subscription/"),
            additional_native_path="/fixture-next-sub/",
            additional_native_port=2097,
        )
        self.assertIn("/fixture-old-sub/", rendered)
        self.assertIn("/fixture-next-sub/", rendered)

    def test_generated_subscription_and_legacy_nonentry_output_are_preserved(self):
        rendered = self.assert_denials_and_existing_routes(self.fixture("generated"))
        self.assertIn("try_files /fixture_sub_12345678.txt =404;", rendered)
        legacy = {"network": {"subscription": {
            "path": "/fixture-subscription/", "backend": "xui-native"
        }}}
        legacy_rendered = render(legacy)
        self.assertNotIn("return 403;", legacy_rendered)
        self.assertIn("location /fixture-subscription/ {", legacy_rendered)
        self.assertIn("proxy_pass http://127.0.0.1:2096;", legacy_rendered)

    def test_missing_or_unsafe_entry_base_is_rejected(self):
        for base in (None, "/fixture;bad/", "/fixture//nested/", "/fixture/./nested/",
                     "/fixture%2Fpanel/", "/fixture/has space/"):
            with self.subTest(base=base):
                config = self.fixture()
                config["panel"]["path"] = base
                with self.assertRaises(ValueError) as error:
                    render(config)
                if base:
                    self.assertNotIn(base, str(error.exception))

    def test_nested_literal_panel_base_keeps_denials_for_entry_and_exit(self):
        for role in ("entry", "exit"):
            with self.subTest(role=role):
                config = self.fixture()
                config["node"]["role"] = role
                config["panel"]["path"] = "/fixture-panel/nested-base/"
                rendered = render(config)
                self.assertEqual(rendered.count("return 403;"), 2)
                for family in ("logs", "xraylogs"):
                    self.assertIn(
                        "location ^~ /fixture-panel/nested-base/panel/api/server/"
                        f"{family}/ {{\n    return 403;\n}}",
                        rendered,
                    )

    def test_longer_managed_route_cannot_bypass_reserved_denial_prefix(self):
        config = self.fixture()
        config["routes"][0]["entry"]["public_path"] = (
            "/fixture-panel/panel/api/server/logs/override/"
        )
        with self.assertRaisesRegex(ValueError, "reserved native log reader"):
            render(config)


if __name__ == "__main__":
    unittest.main(verbosity=2)
