"""Native credentials are exported only as a complete bound public profile set."""
import base64
import copy
from pathlib import Path
import sys
import unittest
from urllib.parse import parse_qs, urlencode, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/lib"))
from native_subscription import placement_public_profiles

IDENTITY = "11111111-2222-4333-8444-555555555555"
DOMAIN = "entry.example.com"
PATH = "/api/public-route-one/"


def configuration():
    return {"node": {"role": "entry"}, "server": {"domain": DOMAIN},
        "network": {"public_port": 443, "subscription": {"backend": "xui-native"}},
        "exits": [{"id": "ch", "enabled": True, "xray": {"outbound_tag": "wm-exit-swiss-primary"}}],
        "routes": [{"id": "ch-manual", "kind": "cascade", "enabled": True, "exit_id": "ch",
            "display_name": "Швейцария", "entry": {"public_path": PATH, "xhttp_mode": "stream-one"}}]}


def link(**changes):
    params = {"encryption": "none", "security": "tls", "type": "xhttp", "host": DOMAIN,
              "sni": DOMAIN, "path": PATH, "mode": "stream-one", "fp": "randomized"}
    params.update(changes)
    return f"vless://{IDENTITY}@{DOMAIN}:443?{urlencode(params)}#native-private-email"


def response(*links):
    return {"success": True, "obj": list(links)}


class PlacementPublicProfiles(unittest.TestCase):
    def reject(self, cfg=None, body=None, identifier=IDENTITY):
        with self.assertRaisesRegex(ValueError, "^placement public profiles are not verified$"):
            placement_public_profiles(cfg or configuration(), identifier, body or response(link()))

    def test_list_raw_and_base64_share_canonical_output_without_native_remark(self):
        raw = link()
        expected = placement_public_profiles(configuration(), IDENTITY, response(raw))
        for value in (raw, base64.b64encode(raw.encode()).decode()):
            self.assertEqual(placement_public_profiles(configuration(), IDENTITY, {"success": True, "obj": value}), expected)
        self.assertNotIn("private-email", expected[0])
        self.assertTrue(expected[0].endswith("#%D0%A8%D0%B2%D0%B5%D0%B9%D1%86%D0%B0%D1%80%D0%B8%D1%8F"))

    def test_supported_native_tls_values_are_preserved(self):
        profiles = placement_public_profiles(configuration(), IDENTITY,
            response(link(fp="chrome", alpn="h2,http/1.1", extra='{"mode":"stream-one"}')))
        actual = parse_qs(urlsplit(profiles[0]).query)
        self.assertEqual(actual["fp"], ["chrome"])
        self.assertEqual(actual["alpn"], ["h2,http/1.1"])
        self.assertEqual(actual["extra"], ['{"mode":"stream-one"}'])

    def test_every_uuid_host_port_and_authority_is_bound(self):
        for bad in (link().replace(IDENTITY, "22222222-2222-4222-8222-222222222222"),
                    link().replace(f"@{DOMAIN}:443", "@foreign.example.com:443"),
                    link().replace(":443?", ":8443?"), link().replace(IDENTITY + "@", IDENTITY + ":secret@"),
                    link().replace(":443?", ":443/hidden?"), link().replace("vless://", "trojan://")):
            with self.subTest(case=bad.split("?")[0]): self.reject(body=response(bad))

    def test_expected_uuid_must_be_canonical(self):
        for identifier in (None, "synthetic", IDENTITY.replace("-", ""), IDENTITY + "/route"):
            self.reject(identifier=identifier)

    def test_transport_changes_or_unknown_parameters_cannot_be_silently_dropped(self):
        for changes in ({"security": "none"}, {"security": "reality"}, {"type": "tcp"},
                {"mode": "packet-up"}, {"sni": "exit.example.com"}, {"host": "localhost"},
                {"encryption": "custom"}, {"path": "/api/foreign-route/"}, {"allowInsecure": "1"},
                {"extra": '{"mode":"stream-one","downloadSettings":{"address":"private"}}'},
                {"extra": '{"mode":"packet-up","mode":"stream-one"}'}, {"fp": "private"}, {"alpn": "h3"}):
            with self.subTest(parameters=changes): self.reject(body=response(link(**changes)))

    def test_duplicate_keys_bad_escapes_controls_and_unframed_content_are_refused(self):
        for bad in (link().replace("#native", "&security=tls#native"), link().replace("#native", "&unknown=%GG#native"),
                    link(sni=DOMAIN + "\t"), link() + "\x7f", "#comment\n" + link(), "vmess://private\n" + link()):
            self.reject(body={"success": True, "obj": bad})

    def test_missing_hidden_duplicate_or_additional_native_profile_refuses_whole_set(self):
        for body in ({"success": True, "obj": []}, response(link(), link()), response(link(), "vmess://private"),
                     response(link().replace("#native", "#--!native")), {"success": False, "obj": [link()]}):
            self.reject(body=body)

    def test_two_profiles_are_complete_and_deterministically_ordered(self):
        cfg = configuration(); second = copy.deepcopy(cfg["routes"][0])
        second.update(id="ch-second", display_name="Second", sort_order=10)
        second["entry"]["public_path"] = "/api/public-route-two/"; cfg["routes"].append(second)
        a, b = link(), link(path=second["entry"]["public_path"])
        profiles = placement_public_profiles(cfg, IDENTITY, response(b, a))
        self.assertEqual(profiles, placement_public_profiles(cfg, IDENTITY, response(a, b)))
        self.assertEqual(len(profiles), 2)
        self.reject(cfg=cfg, body=response(a))

    def test_hidden_or_disabled_routes_are_not_in_the_expected_set(self):
        cfg = configuration(); hidden = copy.deepcopy(cfg["routes"][0])
        hidden.update(id="hidden", enabled=False); hidden["entry"]["public_path"] = "/api/hidden-route-two/"
        cfg["routes"].append(hidden)
        self.assertEqual(len(placement_public_profiles(cfg, IDENTITY, response(link()))), 1)
        self.reject(cfg=cfg, body=response(link(), link(path=hidden["entry"]["public_path"])))

    def test_direct_ru_disabled_exit_and_prepared_channel_are_not_accepted(self):
        for change in ("direct", "disabled-exit", "prepared"):
            cfg = configuration()
            if change == "direct": cfg["routes"][0]["kind"] = "direct"
            elif change == "disabled-exit": cfg["exits"][0]["enabled"] = False
            else: cfg["routes"][0]["prepared_plan"] = {"not": "qualified"}
            self.reject(cfg=cfg)

    def test_published_auto_uses_only_known_enabled_exit_selectors(self):
        cfg = configuration(); route = cfg["routes"][0]
        route.update(kind="auto", balancer_id="auto", routing={"balancer_tag": "wm-balancer-auto"},
                     presentation={"published": True})
        cfg["balancers"] = [{"id": "auto", "enabled": True, "balancer_tag": "wm-balancer-auto", "selector": ["wm-exit-swiss-primary"]}]
        self.assertEqual(len(placement_public_profiles(cfg, IDENTITY, response(link()))), 1)
        for selectors in ([], ["direct"], ["wm-exit-ch"], ["wm-exit-swiss-primary", "wm-exit-unknown"]):
            cfg["balancers"][0]["selector"] = selectors; self.reject(cfg=cfg)

    def test_node_role_backend_public_port_and_path_must_be_supported(self):
        for change in ("exit", "generated", "port", "path", "duplicate-path", "domain"):
            cfg = configuration()
            if change == "exit": cfg["node"]["role"] = "exit"
            elif change == "generated": cfg["network"]["subscription"]["backend"] = "generated"
            elif change == "port": cfg["network"]["public_port"] = 8443
            elif change == "path": cfg["routes"][0]["entry"]["public_path"] = "/api/%252f-private/"
            elif change == "domain": cfg["server"]["domain"] = "127.0.0.1"
            else: cfg["routes"].append(copy.deepcopy(cfg["routes"][0]))
            self.reject(cfg=cfg)


if __name__ == "__main__":
    unittest.main()
