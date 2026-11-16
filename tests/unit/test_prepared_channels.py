"""Compiler/selection proof only: no network or installed engine claims."""
import copy
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/lib"))
import auto_route
import auto_health
import prepared_channels as channels
import runtime_state
import transaction_state
import xray_template as xray


def fixture():
    config = json.loads((ROOT / "tests/fixtures/config-entry-v2.json").read_text())
    config["routes"], config["exits"], config["balancers"] = [], [], []
    channel = channels.channel_descriptor({"name": "xray", "version": "26.1.1", "sha256": "1"*64})
    approvals, paths = [], []
    for index, provider in enumerate(("upcloud", "infomaniak"), 1):
        exit_id = f"{provider}-existing"
        addresses = [f"192.0.0.{8+index}", f"2001:4860:{index}::1"]
        config["exits"].append({"id": exit_id, "enabled": True, "country": "CH", "manifest_checksum": str(index)*64, "xray": {"outbound_tag": f"wm-exit-{exit_id}"}, "endpoint": {"domain": f"{provider}.example.com", "expected_public_ips": addresses, "port": 443, "relay_uuid": f"00000000-0000-0000-0000-{index:012d}", "relay_path": f"/relay/{provider}-private/", "sni": f"{provider}.example.com", "host": f"{provider}.example.com", "fingerprint": "chrome", "transport": "xhttp", "security": "tls", "xhttp_mode": "stream-one"}})
        approvals.append({"id": exit_id, "provider": provider, "failure_domain": f"{provider}-site", "manifest_checksum": str(index)*64, "channel": copy.deepcopy(channel)})
        for family, address in zip((4, 6), addresses):
            paths.append({"id": f"{provider}-v{family}", "exit_id": exit_id, "address_family": family, "address": address})
    config, _ = auto_route.prepare(config, "europe", "Auto", [], 21090, "/api/auto/abcdefghijklmnop/", 50)
    config = auto_route.finalize(config, "route-auto-europe", 77)
    plan = {"schema_version": 1, "node_id": config["node"]["id"], "route_id": "route-auto-europe", "channel": channel, "exits": approvals, "paths": paths, "health": {"failure_samples": 2, "recovery_samples": 2, "sample_interval_seconds": 10, "max_age_seconds": 40, "cooldown_seconds": 20}}
    template = json.loads((ROOT / "tests/fixtures/xray-template.json").read_text())
    return config, plan, template


class PreparedChannels(unittest.TestCase):
    def setUp(self):
        self.config, self.plan, self.template = fixture()
        self.config, self.runtime, self.initial, self.binding = channels.stage(self.config, {}, "route-auto-europe", self.plan, self.template, now=100)

    def sample(self, at, live=None, previous=None):
        values = {p["id"]: True for p in self.plan["paths"]} if live is None else live
        observation = {"schema_version": 1, "binding": self.binding, "engine": self.plan["channel"]["engine"], "at": at, "paths": values}
        return channels.select_paths(self.plan, self.binding, previous or {}, observation, at)

    def rule(self, template):
        return next(r for r in template["routing"]["rules"] if r.get("ruleTag") == "wm-rule-auto-europe")

    def test_initial_ipv4_ipv6_dns_block_and_sentinel_preserved(self):
        self.assertEqual(self.rule(self.initial), {"type": "field", "inboundTag": ["wm-route-auto-europe"], "outboundTag": xray.BLOCK_TAG, "ruleTag": "wm-rule-auto-europe"})
        self.assertEqual(self.initial["routing"]["rules"][0], self.rule(self.initial))
        self.assertEqual(self.initial["routing"]["rules"][3:], self.template["routing"]["rules"])
        self.assertEqual(self.initial["outbounds"][:2], self.template["outbounds"])
        self.assertTrue(xray.validate_eu_only(self.initial))

    def test_prepared_two_exits_four_paths_preserve_tls_identity(self):
        outbounds = [o for o in self.initial["outbounds"] if o["tag"].startswith("wm-exit-path-")]
        self.assertEqual(len(outbounds), 4)
        for path, outbound in zip(self.plan["paths"], outbounds):
            self.assertEqual(outbound["settings"]["vnext"][0]["address"], path["address"])
            tls = outbound["streamSettings"]["tlsSettings"]
            self.assertFalse(tls["allowInsecure"])
            self.assertEqual(tls["serverName"], f"{path['id'].split('-')[0]}.example.com")
        self.assertEqual(len({p["exit_id"] for p in self.plan["paths"]}), 2)

    def test_recovery_hysteresis_and_prepared_switch(self):
        first = self.sample(100)
        self.assertIsNone(first["selected"])
        second = self.sample(110, previous=first)
        self.assertEqual(second["selected"], "upcloud-v4")
        live = {p["id"]: p["id"] != "upcloud-v4" for p in self.plan["paths"]}
        third = self.sample(120, live, second)
        self.assertEqual(third["selected"], "upcloud-v4")
        fourth = self.sample(130, live, third)
        self.assertEqual(fourth["selected"], "upcloud-v6")
        compiled, _ = channels.compile_template(self.config, "route-auto-europe", self.plan, self.initial, fourth, now=130)
        self.assertEqual(self.rule(compiled)["outboundTag"], "wm-exit-path-upcloud-v6")

    def test_exit_failure_selects_other_provider(self):
        state = self.sample(110, previous=self.sample(100))
        live = {p["id"]: p["exit_id"] == "infomaniak-existing" for p in self.plan["paths"]}
        state = self.sample(120, live, state)
        state = self.sample(130, live, state)
        self.assertEqual(state["selected"], "infomaniak-v4")

    def test_all_paths_down_blocks_before_failure_threshold_or_cooldown(self):
        state = self.sample(110, previous=self.sample(100))
        state = self.sample(120, {p["id"]: False for p in self.plan["paths"]}, state)
        self.assertIsNone(state["selected"])
        compiled, _ = channels.compile_template(self.config, "route-auto-europe", self.plan, self.initial, state, now=120)
        self.assertEqual(self.rule(compiled)["outboundTag"], xray.BLOCK_TAG)

    def test_expired_or_changed_config_state_cannot_forward(self):
        state = self.sample(110, previous=self.sample(100))
        for now in (109, 151):
            result, _ = channels.compile_template(self.config, "route-auto-europe", self.plan, self.initial, state, now=now)
            self.assertEqual(self.rule(result)["outboundTag"], xray.BLOCK_TAG)
        altered = copy.deepcopy(self.config)
        altered["exits"][0]["endpoint"]["relay_uuid"] = "00000000-0000-0000-0000-000000000099"
        with self.assertRaises(ValueError):
            channels.compile_template(altered, "route-auto-europe", self.plan, self.initial, state, now=110)
        result, _ = channels.compile_template(altered, "route-auto-europe", self.plan, self.template, state, now=110)
        self.assertEqual(self.rule(result)["outboundTag"], xray.BLOCK_TAG)

    def test_distinct_samples_and_continuity(self):
        first = self.sample(100)
        with self.assertRaises(ValueError): self.sample(100, previous=first)
        gap = self.sample(150, previous=first)
        self.assertIsNone(gap["selected"])

    def test_unknown_combinations_refused(self):
        for field, value in (("protocol", "wireguard"), ("security", "reality"), ("schema_version", 2), ("transport", {"name": "grpc", "mode": "stream-one"})):
            invalid = copy.deepcopy(self.plan["channel"]); invalid[field] = value
            with self.assertRaises(ValueError): channels.validate_descriptor(invalid)
        invalid = copy.deepcopy(self.plan["channel"]); invalid["engine"]["name"] = "sing-box"
        with self.assertRaises(ValueError): channels.validate_descriptor(invalid)

    def test_plan_and_approval_binding_negatives(self):
        mutations = [lambda p: p.update(node_id="other-entry"), lambda p: p["exits"][0].update(provider="other"), lambda p: p["exits"][1].update(failure_domain=p["exits"][0]["failure_domain"]), lambda p: p["exits"][0].update(manifest_checksum="f"*64), lambda p: p["paths"][0].update(address_family=6), lambda p: p["paths"][0].update(address="127.0.0.1"), lambda p: p["paths"][0].update(address="8.8.8.8"), lambda p: p.update(paths=[x for x in p["paths"] if x["address_family"] == 6])]
        for mutate in mutations:
            plan = copy.deepcopy(self.plan); mutate(plan)
            with self.assertRaises(ValueError): channels.bound_plan(self.config, "route-auto-europe", plan)

    def test_public_descriptor_never_contains_private_exit_material(self):
        text = json.dumps(channels.public_descriptor(self.plan))
        for exit_obj in self.config["exits"]:
            for value in (exit_obj["id"], exit_obj["endpoint"]["domain"], exit_obj["endpoint"]["relay_uuid"], exit_obj["endpoint"]["relay_path"], *exit_obj["endpoint"]["expected_public_ips"]):
                self.assertNotIn(value, text)

    def test_prefix_collision_and_ru_direct_are_refused(self):
        invalid = copy.deepcopy(self.initial)
        invalid["outbounds"].append({"tag": "wm-exit-path-upcloud-v4-hidden", "protocol": "freedom"})
        with self.assertRaises(ValueError): xray.validate_eu_only(invalid)
        invalid = copy.deepcopy(self.initial); self.rule(invalid)["outboundTag"] = "direct"
        with self.assertRaises(ValueError): xray.validate_eu_only(invalid)
        invalid = copy.deepcopy(self.initial); invalid["routing"]["rules"].insert(0, {"type": "field", "network": "udp", "outboundTag": "direct"})
        with self.assertRaises(ValueError): xray.validate_eu_only(invalid)

    def test_prepared_ownership_guards(self):
        with self.assertRaises(ValueError): runtime_state.mutate(self.config, "remove-exit", "upcloud-existing", force=True)
        with self.assertRaises(ValueError): auto_route.set_enabled(self.config, "europe", True)
        with self.assertRaises(ValueError): auto_route.set_published(self.config, "europe", True)

    def test_compilation_is_idempotent(self):
        candidate, binding = channels.compile_template(self.config, "route-auto-europe", self.plan, self.initial, now=100)
        self.assertEqual(candidate, self.initial)
        self.assertEqual(binding, self.binding)

    def test_observation_freshness_and_complete_binding(self):
        observation = {"schema_version": 1, "binding": self.binding, "engine": self.plan["channel"]["engine"], "at": 100, "paths": {p["id"]: True for p in self.plan["paths"]}}
        for at in (99, 141):
            with self.assertRaises(ValueError): channels.select_paths(self.plan, self.binding, {}, observation, at)
        for mutate in (lambda o: o.update(binding="f"*64), lambda o: o["paths"].pop("upcloud-v4"), lambda o: o["paths"].update({"upcloud-v4": 1}), lambda o: o.update(schema_version=True), lambda o: o.update(engine={"name": "unknown"})):
            invalid = copy.deepcopy(observation); mutate(invalid)
            with self.assertRaises(ValueError): channels.select_paths(self.plan, self.binding, {}, invalid, 100)

    def test_cooldown_cannot_be_bypassed_by_blocked_tick(self):
        plan = copy.deepcopy(self.plan); plan["health"]["cooldown_seconds"] = 60
        state = self.sample(110, previous=self.sample(100))
        live = {p["id"]: p["id"] != "upcloud-v4" for p in self.plan["paths"]}
        def tick(at, previous):
            observation = {"schema_version": 1, "binding": self.binding, "engine": self.plan["channel"]["engine"], "at": at, "paths": live}
            return channels.select_paths(plan, self.binding, previous, observation, at)
        state = tick(120, state)
        state = tick(130, state)
        self.assertIsNone(state["selected"])
        state = tick(140, state)
        self.assertIsNone(state["selected"])
        state = tick(170, state)
        self.assertEqual(state["selected"], "upcloud-v6")

    def test_auto_health_uses_prepared_state_without_claiming_service(self):
        inbound = {"obj": [{"tag": "wm-route-auto-europe", "enable": True}]}
        row = auto_health.evaluate(self.config, self.runtime, inbound, self.initial, now=100)[0]
        self.assertEqual(row["status"], "blocked")
        state = self.sample(110, previous=self.sample(100))
        template, _ = channels.compile_template(self.config, "route-auto-europe", self.plan, self.initial, state, now=110)
        runtime = {"prepared_routes": {"route-auto-europe": state}}
        row = auto_health.evaluate(self.config, runtime, inbound, template, now=110)[0]
        self.assertEqual((row["status"], row["healthy_exits"], row["evidence_scope"]), ("healthy", 2, "local-entry-path-only"))
        self.assertNotEqual(auto_health.evaluate(self.config, runtime, inbound, template, now=151)[0]["status"], "healthy")

    def test_unsafe_rollback_snapshot_cannot_restore_direct(self):
        with self.assertRaises(ValueError): xray.validate_eu_only(self.template, self.config)
        invalid = copy.deepcopy(self.initial)
        invalid["routing"]["balancers"][0].pop("fallbackTag")
        with self.assertRaises(ValueError): xray.validate_eu_only(invalid, self.config)
        self.assertTrue(xray.validate_eu_only(self.initial, self.config))

    def test_removing_route_keeps_block_before_direct_catchall(self):
        route = self.config["routes"][0]
        removed = xray.remove_balancer(self.initial, route["entry"]["inbound_tag"], route["routing"]["balancer_tag"], route["routing"]["rule_tag"])
        rule = next(r for r in removed["routing"]["rules"] if r.get("inboundTag") == [route["entry"]["inbound_tag"]])
        self.assertEqual(rule["outboundTag"], xray.BLOCK_TAG)
        self.assertTrue(xray.validate_eu_only(removed, self.config))

    def test_descriptor_strict_version_and_public_allowlist(self):
        for mutate in (lambda d: d.update(schema_version=True), lambda d: d.update(internal_exit="private"), lambda d: d["transport"].update(extra="unsupported"), lambda d: d["engine"].update(sha256="unpinned")):
            invalid = copy.deepcopy(self.plan["channel"]); mutate(invalid)
            with self.assertRaises(ValueError): channels.validate_descriptor(invalid)

    def test_unmatched_installed_engine_stops_before_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            fake = Path(directory)/"unmatched-engine"
            fake.write_bytes(b"not an executable")
            with self.assertRaisesRegex(ValueError, "hash differs"):
                channels.check_engine(self.plan, fake)

    def test_published_legacy_route_cannot_silently_enter_prepared_mode(self):
        config = copy.deepcopy(self.config); config["routes"][0]["presentation"]["published"] = True
        with self.assertRaises(ValueError): channels.stage(config, {}, "route-auto-europe", self.plan, self.initial, now=100)

    def test_unrelated_observatory_and_manual_route_sentinel_preserved(self):
        template = copy.deepcopy(self.template)
        template["observatory"] = {"subjectSelector": ["user-proxy"]}
        compiled, _ = channels.compile_template(self.config, "route-auto-europe", self.plan, template, now=100)
        self.assertIn("user-proxy", compiled["observatory"]["subjectSelector"])
        for rule in self.template["routing"]["rules"]:
            self.assertIn(rule, compiled["routing"]["rules"])


if __name__ == "__main__":
    unittest.main()
