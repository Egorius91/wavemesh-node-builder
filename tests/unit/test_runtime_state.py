import copy
import importlib.util
import json
import tempfile
from pathlib import Path


root = Path(__file__).resolve().parents[2]
tool = root / "scripts/lib/runtime_state.py"
spec = importlib.util.spec_from_file_location("runtime_state", tool)
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


assert runtime.transition({}, True) == ("unknown", 1, 0)
assert runtime.transition({"status": "unknown", "consecutive_successes": 2}, True) == ("healthy", 3, 0)
assert runtime.transition({"status": "healthy", "consecutive_failures": 0}, False) == ("healthy", 0, 1)
assert runtime.transition({"status": "healthy", "consecutive_failures": 2}, False) == ("unhealthy", 0, 3)
assert runtime.transition({}, False, enabled=False) == ("disabled", 0, 0)
assert runtime.transition({}, False, misconfigured=True)[0] == "misconfigured"


config = json.loads((root / "tests/fixtures/config-entry-v2.json").read_text(encoding="utf-8"))
config["clients"][0]["subscription_id"] = "sub-runtime-example-1234"
config["clients"][0]["credentials"] = [{"route_id": "route-de-fra-1", "email": "wm.client-1.route-de-fra-1", "uuid": "00000000-0000-0000-0000-000000000010", "enabled": True}]
config["exits"] = [{"id": "de-fra-1", "display_name": "Germany", "enabled": True, "endpoint": {"domain": "de-exit.example.com", "port": 443, "relay_uuid": "00000000-0000-0000-0000-000000000020", "relay_path": "/relay/runtime-example/", "sni": "de-exit.example.com", "host": "de-exit.example.com", "fingerprint": "chrome"}, "xray": {"outbound_tag": "wm-exit-de-fra-1"}}]
config["routes"] = [{"id": "route-de-fra-1", "kind": "cascade", "display_name": "RU -> Germany", "exit_id": "de-fra-1", "enabled": True, "sort_order": 100, "entry": {"listen": "127.0.0.1", "local_port": 21001, "public_path": "/api/de/runtime-route/", "inbound_id": 12, "inbound_tag": "wm-route-de-fra-1", "xhttp_mode": "stream-one"}, "routing": {"rule_tag": "wm-rule-de-fra-1", "outbound_tag": "wm-exit-de-fra-1"}}]


disabled, affected = runtime.mutate(config, "disable", "route-de-fra-1")
assert affected == ["route-de-fra-1"] and disabled["routes"][0]["enabled"] is False
assert disabled["clients"][0]["credentials"][0]["enabled"] is False
enabled, _ = runtime.mutate(disabled, "enable", "route-de-fra-1")
assert enabled["routes"][0]["enabled"] is True and enabled["clients"][0]["credentials"][0]["enabled"] is True
removed, _ = runtime.mutate(config, "remove-route", "route-de-fra-1")
assert removed["routes"] == [] and removed["clients"][0]["credentials"] == []
try:
    runtime.mutate(config, "remove-exit", "de-fra-1")
    raise AssertionError("remove-exit without force accepted an attached route")
except ValueError as error:
    assert "--force" in str(error)
removed_exit, removed_routes = runtime.mutate(config, "remove-exit", "de-fra-1", True)
assert removed_routes == ["route-de-fra-1"] and removed_exit["routes"] == [] and removed_exit["exits"] == []


with tempfile.TemporaryDirectory() as name:
    temp = Path(name)
    inbound = temp / "inbound.json"
    outbound = temp / "outbound.json"
    subs = temp / "subscriptions/users"
    subs.mkdir(parents=True)
    runtime.write_artifacts(config, "route-de-fra-1", inbound, outbound)
    desired_inbound = json.loads(inbound.read_text(encoding="utf-8"))
    desired_outbound = json.loads(outbound.read_text(encoding="utf-8"))
    assert desired_inbound["listen"] == "127.0.0.1" and desired_inbound["enable"] is True
    assert desired_outbound["tag"] == "wm-exit-de-fra-1"
    assert desired_outbound["streamSettings"]["tlsSettings"]["allowInsecure"] is False
    (subs / "sub-runtime-example-1234.txt").write_text("vless://00000000-0000-0000-0000-000000000010@ru-entry.example.com:443\n", encoding="utf-8")
    actual_inbounds = {"obj": [{**desired_inbound, "id": 12}]}
    template = {"outbounds": [desired_outbound], "routing": {"rules": [{"ruleTag": "wm-rule-de-fra-1", "inboundTag": ["wm-route-de-fra-1"], "outboundTag": "wm-exit-de-fra-1"}]}}
    probes = {"control": {"service": "active", "api": "reachable", "xray": "running", "panel_bind": "loopback", "bearer": "valid", "nginx": "active", "tls": "valid"}, "routes": [{"route_id": "route-de-fra-1", "route_test": True, "test_outbound": True, "latency_ms": 82, "route_test_outbound": "wm-exit-de-fra-1"}]}
    state = {}
    for expected in ("unknown", "unknown", "healthy"):
        state = runtime.evaluate(config, state, actual_inbounds, template, "location /api/de/runtime-route/ { proxy_pass http://127.0.0.1:21001; }", temp / "subscriptions", probes)
        assert state["exits"]["de-fra-1"]["status"] == expected
    assert state["node_status"] == "healthy" and runtime.drift_plan(config, state) == []
    drifted = runtime.evaluate(config, state, {"obj": []}, template, "", temp / "subscriptions", probes)
    assert drifted["routes"]["route-de-fra-1"]["status"] == "misconfigured"
    plan = runtime.drift_plan(config, drifted)
    assert {item["component"] for item in plan} >= {"inbound", "nginx"}
    altered_template = copy.deepcopy(template)
    altered_template["outbounds"][0]["streamSettings"]["tlsSettings"]["allowInsecure"] = True
    altered = runtime.evaluate(config, state, actual_inbounds, altered_template, "location /api/de/runtime-route/ { proxy_pass http://127.0.0.1:21001; }", temp / "subscriptions", probes)
    assert altered["routes"]["route-de-fra-1"]["outbound"] == "drifted"
    assert "xray" in {item["component"] for item in runtime.drift_plan(config, altered)}
    catch_all_first = copy.deepcopy(template)
    catch_all_first["routing"]["rules"].insert(0, {"type": "field", "outboundTag": "direct"})
    misplaced = runtime.evaluate(config, state, actual_inbounds, catch_all_first, "location /api/de/runtime-route/ { proxy_pass http://127.0.0.1:21001; }", temp / "subscriptions", probes)
    assert misplaced["routes"]["route-de-fra-1"]["routing_rule"] == "missing"
    synced = runtime.sync_runtime(disabled, state)
    assert synced["routes"]["route-de-fra-1"]["status"] == "disabled"

print("runtime state tests: OK")
