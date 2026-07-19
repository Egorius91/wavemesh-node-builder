import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path

root = Path(__file__).resolve().parents[2]
renderer = root / "scripts" / "lib" / "subscription_renderer.py"
nginx_renderer = root / "scripts" / "lib" / "nginx_renderer.py"
runtime_tool = root / "scripts" / "lib" / "runtime_state.py"

config = {
    "schema_version": 2,
    "node": {"role": "entry"},
    "server": {"domain": "entry.example.com", "public_ip": "203.0.113.5"},
    "panel": {"path": "/panel-secret/", "listen_port": 50000},
    "network": {
        "xhttp": {"path": "/api/default-secret/", "port": 21000},
        "subscription": {
            "path": "/opaque-subscription-base/",
            "backend": "generated",
            "mode": "generated",
            "publication_mode": "auto-only",
        },
    },
    "exits": [
        {
            "id": "de-1",
            "enabled": True,
            "endpoint": {
                "domain": "exit.example.com",
                "relay_uuid": "00000000-0000-4000-8000-000000000099",
                "relay_path": "/relay/private/",
                "expected_public_ips": ["198.51.100.9"],
            },
        }
    ],
    "routes": [
        {
            "id": "route-auto-auto-europe",
            "kind": "auto",
            "display_name": "Auto → Europe",
            "enabled": True,
            "sort_order": 10,
            "presentation": {"published": True},
            "entry": {
                "local_port": 21100,
                "public_path": "/api/auto/public/",
                "inbound_tag": "wm-route-auto-auto-europe",
            },
            "routing": {"rule_tag": "wm-rule-auto", "balancer_tag": "wm-balancer-auto"},
        },
        {
            "id": "route-de-1",
            "kind": "cascade",
            "display_name": "Germany",
            "enabled": True,
            "sort_order": 20,
            "exit_id": "de-1",
            "entry": {
                "local_port": 21200,
                "public_path": "/api/manual/private/",
                "inbound_tag": "wm-route-de-1",
            },
            "routing": {"rule_tag": "wm-rule-de-1", "outbound_tag": "wm-exit-de-1"},
        },
    ],
    "clients": [
        {
            "id": "client-1",
            "enabled": True,
            "subscription_id": "opaque-client-id-123456",
            "credentials": [
                {
                    "route_id": "route-auto-auto-europe",
                    "uuid": "00000000-0000-4000-8000-000000000001",
                    "email": "wm.client-1.auto",
                    "enabled": True,
                },
                {
                    "route_id": "route-de-1",
                    "uuid": "00000000-0000-4000-8000-000000000002",
                    "email": "wm.client-1.manual",
                    "enabled": True,
                },
            ],
        }
    ],
}

with tempfile.TemporaryDirectory() as name:
    temp = Path(name)
    source = temp / "config.json"
    rendered_config = temp / "rendered.json"
    output = temp / "subscriptions"
    metadata = temp / "metadata.json"
    nginx = temp / "nginx.conf"
    source.write_text(json.dumps(config), encoding="utf-8")

    subprocess.run([
        sys.executable, str(renderer), "--config", str(source),
        "--output-config", str(rendered_config), "--output-dir", str(output),
        "--metadata", str(metadata),
    ], check=True)
    content = (output / "users" / "opaque-client-id-123456.txt").read_text(encoding="utf-8")
    assert content.count("vless://") == 1
    assert "Auto%20%E2%86%92%20Europe" in content
    assert "/api/manual/private/" not in content
    assert json.loads(metadata.read_text())[0]["profiles"] == 1

    subprocess.run([
        sys.executable, str(nginx_renderer), "--config", str(source), "--output", str(nginx)
    ], check=True)
    nginx_text = nginx.read_text(encoding="utf-8")
    assert "/api/auto/public/" in nginx_text
    assert "/api/manual/private/" not in nginx_text

    spec = importlib.util.spec_from_file_location("runtime_state_auto_only", runtime_tool)
    runtime = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runtime)
    manual_inbound = runtime.inbound_payload(config, config["routes"][1])
    assert manual_inbound["remark"] == "--!wm-route-de-1"
    assert manual_inbound["settings"]["clients"] == []
    auto_inbound = runtime.inbound_payload(config, config["routes"][0])
    assert auto_inbound["remark"] == "Auto → Europe"
    assert len(auto_inbound["settings"]["clients"]) == 1

print("auto-only publication tests: OK")
