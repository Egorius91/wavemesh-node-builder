import importlib.util
import json
import tempfile
from pathlib import Path

root = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("e2e_check", root / "scripts/lib/e2e_check.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def profile(uuid, domain, path, name):
    from urllib.parse import quote, urlencode

    query = urlencode({
        "encryption": "none",
        "security": "tls",
        "type": "xhttp",
        "host": domain,
        "sni": domain,
        "fp": "randomized",
        "path": path,
        "mode": "stream-one",
    })
    return f"vless://{uuid}@{domain}:443?{query}#{quote(name, safe='')}"


with tempfile.TemporaryDirectory() as directory:
    base = Path(directory)
    subscriptions = base / "subscriptions"
    users = subscriptions / "users"
    users.mkdir(parents=True)

    config = {
        "node": {"role": "entry"},
        "server": {"domain": "entry.example.com"},
        "exits": [
            {
                "id": "de-1",
                "enabled": True,
                "endpoint": {"domain": "exit1.example.com", "expected_public_ips": ["203.0.113.10"]},
                "xray": {"outbound_tag": "wm-exit-de-1"},
            },
            {
                "id": "de-2",
                "enabled": True,
                "endpoint": {"domain": "exit2.example.com", "expected_public_ips": ["203.0.113.20"]},
                "xray": {"outbound_tag": "wm-exit-de-2"},
            },
        ],
        "balancers": [
            {
                "id": "auto-europe",
                "enabled": True,
                "strategy": "leastPing",
                "selector": ["wm-exit-de-1", "wm-exit-de-2"],
                "balancer_tag": "wm-balancer-auto-europe",
            }
        ],
        "routes": [
            {
                "id": "route-auto-auto-europe",
                "kind": "auto",
                "display_name": "Auto Europe",
                "enabled": True,
                "sort_order": 10,
                "balancer_id": "auto-europe",
                "entry": {"public_path": "/api/auto/example/"},
                "routing": {"balancer_tag": "wm-balancer-auto-europe"},
                "presentation": {"published": True},
            },
            {
                "id": "route-de-1",
                "kind": "cascade",
                "display_name": "Germany 1",
                "enabled": True,
                "sort_order": 20,
                "exit_id": "de-1",
                "entry": {"public_path": "/api/de/one/"},
                "routing": {"outbound_tag": "wm-exit-de-1"},
            },
            {
                "id": "route-de-2",
                "kind": "cascade",
                "display_name": "Germany 2",
                "enabled": True,
                "sort_order": 30,
                "exit_id": "de-2",
                "entry": {"public_path": "/api/de/two/"},
                "routing": {"outbound_tag": "wm-exit-de-2"},
            },
        ],
        "clients": [
            {
                "id": "client-1",
                "enabled": True,
                "subscription_id": "subscription-client-1",
                "credentials": [
                    {"route_id": "route-auto-auto-europe", "uuid": "00000000-0000-4000-8000-000000000001", "enabled": True},
                    {"route_id": "route-de-1", "uuid": "00000000-0000-4000-8000-000000000002", "enabled": True},
                    {"route_id": "route-de-2", "uuid": "00000000-0000-4000-8000-000000000003", "enabled": True},
                ],
            }
        ],
    }
    runtime = {
        "node_status": "healthy",
        "routes": {
            "route-de-1": {"status": "healthy", "route_test_outbound": "wm-exit-de-1"},
            "route-de-2": {"status": "healthy", "route_test_outbound": "wm-exit-de-2"},
        },
    }

    config_path = base / "config.json"
    runtime_path = base / "runtime.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    runtime_path.write_text(json.dumps(runtime), encoding="utf-8")

    lines = [
        profile("00000000-0000-4000-8000-000000000001", "entry.example.com", "/api/auto/example/", "Auto Europe"),
        profile("00000000-0000-4000-8000-000000000002", "entry.example.com", "/api/de/one/", "Germany 1"),
        profile("00000000-0000-4000-8000-000000000003", "entry.example.com", "/api/de/two/", "Germany 2"),
    ]
    (users / "subscription-client-1.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = module.verify(config_path, runtime_path, subscriptions)
    assert result["manual_route_count"] == 2
    assert result["auto_route_count"] == 1
    assert result["published_route_count"] == 3
    assert result["profile_count"] == 3
    assert result["auto_routes"][0]["selectors"] == ["wm-exit-de-1", "wm-exit-de-2"]

    broken = json.loads(json.dumps(config))
    broken["balancers"][0]["selector"] = ["wm-exit-missing"]
    broken_path = base / "broken.json"
    broken_path.write_text(json.dumps(broken), encoding="utf-8")
    try:
        module.verify(broken_path, runtime_path, subscriptions)
    except ValueError as error:
        assert "unknown managed outbounds" in str(error)
    else:
        raise AssertionError("unknown Auto selector should fail verification")

print("auto E2E tests: OK")
