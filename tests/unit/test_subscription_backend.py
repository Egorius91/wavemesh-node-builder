import importlib.util
import json
import sqlite3
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load_module(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


backend = load_module("subscription_backend", "scripts/lib/subscription_backend.py")
config_json = load_module("config_json", "scripts/lib/config_json.py")
nginx = load_module("nginx_renderer_native", "scripts/lib/nginx_renderer.py")
inbound = load_module("inbound_adapter_native", "scripts/lib/inbound_adapter.py")
nginx_site = load_module("nginx_site_native", "scripts/lib/nginx_site.py")


def sample_config(selected="wavemesh-renderer"):
    return {
        "schema_version": 2,
        "node": {"id": "entry-1", "role": "entry"},
        "server": {"domain": "entry.example.com", "public_ip": "198.51.100.10"},
        "network": {
            "subscription": {"backend": selected, "path": "/opaquePart/secondOpaqueSegment/", "local_port": 2096},
            "xhttp": {"path": "/api/base/example/", "port": 12001},
        },
        "panel": {"path": "/panel/example/", "listen_port": 52001},
        "clients": [{"id": "client-1", "enabled": True, "subscription_id": "ABCDEFGHIJKLMNOP", "credentials": []}],
        "exits": [],
        "relay_peers": [],
        "routes": [
            {"id": "route-de", "kind": "cascade", "display_name": "Germany", "enabled": True, "entry": {"inbound_id": 4, "public_path": "/api/de/example-route/", "local_port": 21001}},
            {"id": "route-auto-main", "kind": "auto", "display_name": "Auto", "enabled": True, "presentation": {"published": False}, "entry": {"inbound_id": 5, "public_path": "/api/auto/example-route/", "local_port": 21002}},
        ],
    }


assert '"backend": "xui-native"' in (ROOT / "scripts/00_common.sh").read_text(encoding="utf-8")

legacy = sample_config()
legacy["network"]["subscription"].pop("backend")
legacy["network"]["subscription"]["mode"] = "generated"
with tempfile.TemporaryDirectory() as directory:
    config_path = Path(directory, "config.json")
    backup_dir = Path(directory, "backups")
    config_path.write_text(json.dumps(legacy), encoding="utf-8")
    config_json.migrate(config_path, backup_dir)
    migrated = json.loads(config_path.read_text(encoding="utf-8"))
    assert migrated["network"]["subscription"]["backend"] == "wavemesh-renderer"
    assert list(backup_dir.glob("config.pre-subscription-backend.*.json"))

native_legacy = sample_config()
native_legacy["network"]["subscription"].pop("backend")
native_legacy["network"]["subscription"]["mode"] = "native"
native_legacy["clients"] = []
detected, changed = config_json.migrate_v2_defaults(native_legacy, "backup.json")
assert changed and detected["network"]["subscription"]["backend"] == "xui-native"

native = backend.candidate(sample_config(), "xui-native")
assert native["network"]["subscription"]["local_port"] == 2096
settings = backend.desired_xui_settings(native)
assert settings["subEnable"] == "true"
assert settings["subListen"] == "127.0.0.1"
assert settings["subPath"] == native["network"]["subscription"]["path"]

with tempfile.TemporaryDirectory() as directory:
    database = Path(directory, "x-ui.db")
    conn = sqlite3.connect(database)
    conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
    conn.commit(); conn.close()
    backend.apply_settings(database, native)
    stored = backend.read_settings(database)
    assert stored["subEnable"] == "true" and stored["subPort"] == "2096"
    renderer = backend.candidate(native, "wavemesh-renderer")
    backend.apply_settings(database, renderer)
    assert backend.read_settings(database)["subEnable"] == "false"

rendered_native = nginx.render(native)
assert "# wavemesh-subscription-backend: xui-native" in rendered_native
assert "proxy_pass http://127.0.0.1:2096;" in rendered_native
assert "/var/www/wavemesh-sub/users" not in rendered_native
assert "try_files /sub.txt" not in rendered_native
assert "location = /opaquePart/secondOpaqueSegment" in rendered_native

legacy_site = """server {
    location = /sub/legacy-token { return 301 https://$host/sub/legacy-token/; }
    location = /sub/legacy-token/ {
        root /var/www/wavemesh-sub;
        try_files /sub.txt =404;
    }
    location /sub/legacy-token/ {
        proxy_pass http://127.0.0.1:2096;
    }
    location /panel/ { proxy_pass http://127.0.0.1:50000; }
}
"""
sanitized = nginx_site.sanitize(legacy_site, native["network"]["subscription"]["path"])
assert "sub/legacy-token" not in sanitized
assert "location /panel/" in sanitized

rendered_fallback = nginx.render(sample_config())
assert "# wavemesh-subscription-backend: wavemesh-renderer" in rendered_fallback
assert "/var/www/wavemesh-sub/users" in rendered_fallback

routes = backend.public_routes(native)
assert [item["route_id"] for item in routes["routes"]] == ["route-de"]
assert "clients" not in routes and "exits" not in routes

desired = {"tag": "wm-route-de", "remark": "Germany", "settings": {"clients": [{"id": "managed", "email": "wm.test", "subId": "shared-sub"}]}, "streamSettings": {}}
external = {
    "id": "external-id", "email": "customer@example.com", "subId": "shared-sub",
    "expiryTime": 123, "totalGB": 456, "limitIp": 2, "enable": False,
    "tgId": 789, "flow": "", "comment": "keep", "reset": 30,
}
actual = {"obj": [{"id": 4, "tag": "wm-route-de", "remark": "Germany", "settings": {"clients": [external]}, "streamSettings": {}}]}
merged = inbound.merge_external_clients(desired, actual)
preserved = next(item for item in merged["settings"]["clients"] if item["email"] == "customer@example.com")
assert preserved == external

hidden = inbound.update_remark(actual, 4, "--!Germany")
assert hidden["remark"] == "--!Germany"

print("subscription backend tests: OK")
