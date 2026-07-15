import base64
import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path


root = Path(__file__).resolve().parents[2]
tool = root / "scripts" / "lib" / "native_subscription.py"
nginx = root / "scripts" / "lib" / "nginx_renderer.py"
spec = importlib.util.spec_from_file_location("native_subscription", tool)
native = importlib.util.module_from_spec(spec)
spec.loader.exec_module(native)

config = json.loads((root / "tests" / "fixtures" / "config-entry-v2.json").read_text())
config["network"]["subscription"].update(
    {"path": "/AbCdEf1234/XyZ9876543210abcde/", "mode": "xui-native", "backend": "xui-native", "local_port": 2096}
)
config["clients"][0]["uuid"] = "00000000-0000-0000-0000-000000000001"

settings = native.native_settings({"webPort": 2053}, config)
assert settings["webPort"] == 2053
assert settings["subEnable"] is True
assert settings["subListen"] == "127.0.0.1"
assert settings["subPath"] == config["network"]["subscription"]["path"]

openapi = {"paths": {path: {} for path in (
    "/panel/api/clients/list", "/panel/api/clients/subLinks/{subId}",
    "/panel/api/setting/all", "/panel/api/setting/update", "/panel/api/inbounds/list",
)}}
assert all(native.openapi_capabilities(openapi).values())

inbounds = {"obj": [
    {"remark": "Germany", "enable": True, "protocol": "vless", "streamSettings": {"network": "xhttp"}},
    {"remark": "--!wm-relay-entry", "enable": True, "protocol": "vless", "streamSettings": {"network": "xhttp"}},
    {"remark": "Disabled", "enable": False, "protocol": "vless", "streamSettings": {"network": "xhttp"}},
]}
view = native.visible_inbounds(inbounds)
assert view["public"] == ["Germany"] and view["hidden"] == ["--!wm-relay-entry"]

line = "vless://uuid@entry.example.com:443?security=tls&type=xhttp#Germany"
encoded = base64.b64encode(line.encode()).decode()
assert native.validate_content(encoded, "entry.example.com", ["127.0.0.1"], 1) == {"profiles": 1}

profile_config = json.loads(json.dumps(config))
profile_config["exits"] = [{"id": "exit-1", "enabled": True}]
profile_config["routes"] = [
    {"id": "route-public", "kind": "cascade", "exit_id": "exit-1", "enabled": True},
    {"id": "route-auto", "kind": "auto", "enabled": True, "presentation": {"published": False}},
]
profile_config["clients"][0]["credentials"] = [
    {"route_id": "route-public", "enabled": True},
    {"route_id": "route-auto", "enabled": True},
]
assert native.expected_client_profiles(profile_config, profile_config["clients"][0]["subscription_id"]) == 1
profile_config["routes"][1]["presentation"]["published"] = True
assert native.expected_client_profiles(profile_config, profile_config["clients"][0]["subscription_id"]) == 2
profile_config["routes"].append({"id": "route-manual", "kind": "direct", "enabled": True})
profile_config["clients"][0]["credentials"].append({"route_id": "route-manual", "enabled": True})
profile_config["node"]["role"] = "entry"
assert native.expected_client_profiles(profile_config, profile_config["clients"][0]["subscription_id"]) == 3

with tempfile.TemporaryDirectory() as name:
    source = Path(name) / "config.json"; output = Path(name) / "nginx.conf"
    source.write_text(json.dumps(config))
    subprocess.run([sys.executable, str(nginx), "--config", str(source), "--output", str(output)], check=True)
    rendered = output.read_text()
    assert f"location {config['network']['subscription']['path']}" in rendered
    assert "proxy_pass http://127.0.0.1:2096;" in rendered
    assert "root /var/www/wavemesh-sub/users;" not in rendered

    alias_output = Path(name) / "nginx-alias.conf"
    subprocess.run([
        sys.executable, str(nginx), "--config", str(source), "--output", str(alias_output),
        "--native-alias-from", "/old-native-path/", "--native-alias-to", config["network"]["subscription"]["path"],
    ], check=True)
    alias_rendered = alias_output.read_text()
    assert "location /old-native-path/" in alias_rendered
    assert f"proxy_pass http://127.0.0.1:2096{config['network']['subscription']['path']};" in alias_rendered

    generated_config = json.loads(json.dumps(config))
    generated_config["network"]["subscription"].update({"backend": "generated", "mode": "generated"})
    generated_config["clients"][0]["subscription_id"] = "generated-client-1234"
    generated_source = Path(name) / "generated.json"; migration_output = Path(name) / "nginx-migration.conf"
    generated_source.write_text(json.dumps(generated_config))
    subprocess.run([
        sys.executable, str(nginx), "--config", str(generated_source), "--output", str(migration_output),
        "--additional-native-path", "/new-native-path/",
    ], check=True)
    migration_rendered = migration_output.read_text()
    assert "root /var/www/wavemesh-sub/users;" in migration_rendered
    assert "location /new-native-path/" in migration_rendered

    clients = Path(name) / "clients.json"; reconciled = Path(name) / "reconciled.json"; actions = Path(name) / "actions.json"
    clients.write_text(json.dumps({"obj": [{"uuid": config["clients"][0]["uuid"], "email": "wm.client", "subId": "", "enable": True}]}))
    subprocess.run([sys.executable, str(tool), "client-plan", "--config", str(source), "--clients", str(clients), "--output-config", str(reconciled), "--actions", str(actions)], check=True)
    assert json.loads(actions.read_text()) == [{"email": "wm.client", "sub_id": config["clients"][0]["subscription_id"]}]

print("native subscription tests: OK")
