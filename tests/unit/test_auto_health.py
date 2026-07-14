import importlib.util
from pathlib import Path

root = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("auto_health", root / "scripts/lib/auto_health.py")
health = importlib.util.module_from_spec(spec)
spec.loader.exec_module(health)

config = {
    "exits": [
        {"id": "de-a", "enabled": True, "xray": {"outbound_tag": "wm-exit-de-a"}},
        {"id": "de-b", "enabled": True, "xray": {"outbound_tag": "wm-exit-de-b"}},
    ],
    "balancers": [{"id": "auto-europe", "enabled": True, "strategy": "leastPing", "selector": ["wm-exit-de-a", "wm-exit-de-b"], "balancer_tag": "wm-balancer-auto-europe"}],
    "routes": [
        {"id": "route-de-a", "kind": "cascade", "routing": {"outbound_tag": "wm-exit-de-a"}},
        {"id": "route-de-b", "kind": "cascade", "routing": {"outbound_tag": "wm-exit-de-b"}},
        {"id": "route-auto-auto-europe", "kind": "auto", "display_name": "Auto", "enabled": True, "balancer_id": "auto-europe", "entry": {"inbound_tag": "wm-route-auto-auto-europe"}, "routing": {"rule_tag": "wm-rule-auto-auto-europe", "balancer_tag": "wm-balancer-auto-europe"}, "presentation": {"published": False}},
    ],
}
inbounds = {"obj": [{"tag": "wm-route-auto-auto-europe", "enable": True}]}
template = {
    "outbounds": [{"tag": "wm-exit-de-a"}, {"tag": "wm-exit-de-b"}],
    "routing": {
        "balancers": [{"tag": "wm-balancer-auto-europe", "selector": ["wm-exit-de-a", "wm-exit-de-b"], "strategy": {"type": "leastPing"}}],
        "rules": [{"ruleTag": "wm-rule-auto-auto-europe", "inboundTag": ["wm-route-auto-auto-europe"], "balancerTag": "wm-balancer-auto-europe"}],
    },
    "observatory": {"subjectSelector": ["wm-exit-de-a", "wm-exit-de-b"]},
}

runtime = {"routes": {"route-de-a": {"status": "healthy"}, "route-de-b": {"status": "healthy"}}}
assert health.evaluate(config, runtime, inbounds, template)[0]["status"] == "healthy"
runtime["routes"]["route-de-b"]["status"] = "unhealthy"
assert health.evaluate(config, runtime, inbounds, template)[0]["status"] == "degraded"
runtime["routes"]["route-de-a"]["status"] = "unhealthy"
assert health.evaluate(config, runtime, inbounds, template)[0]["status"] == "unhealthy"

bad = {**template, "routing": {**template["routing"], "balancers": []}}
assert health.evaluate(config, runtime, inbounds, bad)[0]["status"] == "misconfigured"

disabled = {**config, "routes": [*config["routes"][:-1], {**config["routes"][-1], "enabled": False}]}
assert health.evaluate(disabled, runtime, {"obj": [{"tag": "wm-route-auto-auto-europe", "enable": False}]}, bad)[0]["status"] == "disabled"

override_template = {
    **template,
    "routing": {**template["routing"], "balancers": [{"tag": "wm-balancer-auto-europe", "selector": ["wm-exit-de-a"], "strategy": {"type": "leastPing"}}]},
    "observatory": {"subjectSelector": ["wm-exit-de-a"]},
}
runtime["routes"]["route-de-a"]["status"] = "healthy"
assert health.evaluate(config, runtime, inbounds, override_template, {"auto-europe": "de-a"})[0]["status"] == "healthy"

print("auto health tests: OK")
