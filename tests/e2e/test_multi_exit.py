import copy
import json
import subprocess
import sys
import tempfile
import urllib.parse
from pathlib import Path


root = Path(__file__).resolve().parents[2]
exit_peer = root / "scripts/lib/exit_peer.py"
cascade = root / "scripts/lib/cascade.py"
xray = root / "scripts/lib/xray_template.py"
subscriptions = root / "scripts/lib/subscription_renderer.py"
nginx = root / "scripts/lib/nginx_renderer.py"
checker = root / "scripts/lib/e2e_check.py"


def run(*args, capture=False):
    return subprocess.run(
        [sys.executable, *map(str, args)],
        check=True,
        capture_output=capture,
        text=capture,
    )


def exit_config(base, node_id, country, city, domain, public_ip):
    result = copy.deepcopy(base)
    result["node"].update({"id": node_id, "country": country, "city": city})
    result["server"].update({"domain": domain, "public_ip": public_ip})
    result["relay_peers"] = []
    return result


with tempfile.TemporaryDirectory() as name:
    temp = Path(name)
    entry = temp / "entry.json"
    entry_config = json.loads(
        (root / "tests/fixtures/config-entry-v2.json").read_text(encoding="utf-8")
    )
    entry_config.setdefault("network", {}).setdefault("subscription", {})[
        "path"
    ] = "/q8Kp2RmXn4/Dt7Vb3Ls9Qc6Hw2ZaP/"
    entry.write_text(json.dumps(entry_config), encoding="utf-8")
    exit_base = json.loads((root / "tests/fixtures/config-exit-v2.json").read_text(encoding="utf-8"))
    definitions = [
        {
            "id": "de-fra-1",
            "country": "DE",
            "city": "Frankfurt",
            "domain": "de-exit.example.com",
            "ip": "198.51.100.20",
            "relay_path": "/relay/de-example-secret/",
            "relay_uuid": "00000000-0000-0000-0000-000000000020",
            "relay_port": 22001,
            "route_path": "/api/de/example-route/",
            "route_port": 21001,
            "inbound_id": 12,
            "display_name": "RU -> Germany",
            "sort_order": 200,
        },
        {
            "id": "nl-ams-1",
            "country": "NL",
            "city": "Amsterdam",
            "domain": "nl-exit.example.com",
            "ip": "198.51.100.30",
            "relay_path": "/relay/nl-example-secret/",
            "relay_uuid": "00000000-0000-0000-0000-000000000030",
            "relay_port": 22002,
            "route_path": "/api/nl/example-route/",
            "route_port": 21002,
            "inbound_id": 13,
            "display_name": "RU -> Netherlands",
            "sort_order": 100,
        },
    ]
    template = temp / "xray-0.json"
    template.write_text((root / "tests/fixtures/xray-template.json").read_text(encoding="utf-8"), encoding="utf-8")

    for index, item in enumerate(definitions, start=1):
        exit_path = temp / f"exit-{index}.json"
        exit_path.write_text(
            json.dumps(exit_config(exit_base, item["id"], item["country"], item["city"], item["domain"], item["ip"])),
            encoding="utf-8",
        )
        exit_candidate = temp / f"exit-{index}-candidate.json"
        manifest = temp / f"manifest-{index}.json"
        run(
            exit_peer,
            "create",
            "--config", exit_path,
            "--candidate", exit_candidate,
            "--manifest", manifest,
            "--entry-id", "ru-msk-1",
            "--entry-ip", "203.0.113.10",
            "--display-name", "RU Entry",
            "--path", item["relay_path"],
            "--uuid", item["relay_uuid"],
            "--port", item["relay_port"],
            "--inbound-id", 20 + index,
        )
        run(cascade, "validate", "--config", entry, "--manifest", manifest, "--skip-network")
        candidate = temp / f"entry-{index}.json"
        clients = temp / f"clients-{index}.json"
        outbound = temp / f"outbound-{index}.json"
        run(
            cascade,
            "prepare",
            "--config", entry,
            "--manifest", manifest,
            "--candidate", candidate,
            "--clients", clients,
            "--outbound", outbound,
            "--path", item["route_path"],
            "--port", item["route_port"],
            "--display-name", item["display_name"],
            "--sort-order", item["sort_order"],
        )
        route_id = f"route-{item['id']}"
        run(cascade, "finalize", "--candidate", candidate, "--route-id", route_id, "--inbound-id", item["inbound_id"])
        merged = temp / f"xray-{index}.json"
        run(
            xray,
            "merge",
            "--template", template,
            "--outbound", outbound,
            "--inbound-tag", f"wm-route-{item['id']}",
            "--outbound-tag", f"wm-exit-{item['id']}",
            "--rule-tag", f"wm-rule-{item['id']}",
            "--output", merged,
        )
        entry = candidate
        template = merged

    final = temp / "entry-final.json"
    output = temp / "subscriptions"
    metadata = temp / "subscriptions.json"
    run(subscriptions, "--config", entry, "--output-config", final, "--output-dir", output, "--metadata", metadata)
    nginx_output = temp / "nginx.conf"
    run(nginx, "--config", final, "--output", nginx_output)

    config = json.loads(final.read_text(encoding="utf-8"))
    assert len(config["exits"]) == 2 and len(config["routes"]) == 2
    xray_state = json.loads(template.read_text(encoding="utf-8"))
    assert {item.get("tag") for item in xray_state["outbounds"]} >= {"wm-exit-de-fra-1", "wm-exit-nl-ams-1"}
    assert {item.get("ruleTag") for item in xray_state["routing"]["rules"]} >= {"wm-rule-de-fra-1", "wm-rule-nl-ams-1"}

    subscription_id = config["clients"][0]["subscription_id"]
    lines = (output / "users" / f"{subscription_id}.txt").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert [urllib.parse.unquote(urllib.parse.urlsplit(line).fragment) for line in lines] == ["RU -> Netherlands", "RU -> Germany"]
    assert all("@ru-entry.example.com:443" in line for line in lines)
    assert all(value not in "\n".join(lines) for item in definitions for value in (item["domain"], item["ip"], item["relay_uuid"], item["relay_path"]))
    metadata_item = json.loads(metadata.read_text(encoding="utf-8"))[0]
    expected_subscription_path = config["network"]["subscription"]["path"]
    assert metadata_item["profiles"] == 2
    assert metadata_item["path"] == expected_subscription_path
    assert not expected_subscription_path.startswith("/sub/")
    rendered_nginx = nginx_output.read_text(encoding="utf-8")
    assert f"location = {expected_subscription_path}" in rendered_nginx
    assert "/sub/" not in rendered_nginx
    assert rendered_nginx.count("wm-route-") == 0
    assert all(item["route_path"] in rendered_nginx for item in definitions)

    runtime = {
        "node_status": "healthy",
        "routes": {
            f"route-{item['id']}": {
                "status": "healthy",
                "route_test_outbound": f"wm-exit-{item['id']}",
            }
            for item in definitions
        },
    }
    runtime_path = temp / "runtime.json"
    runtime_path.write_text(json.dumps(runtime), encoding="utf-8")
    result = run(checker, "--config", final, "--runtime", runtime_path, "--subscriptions", output, "--json", capture=True)
    report = json.loads(result.stdout)
    assert report["node_status"] == "healthy"
    assert report["exit_count"] == 2 and report["route_count"] == 2
    assert report["client_count"] == 1 and report["profile_count"] == 2
    assert [item["display_name"] for item in report["routes"]] == ["RU -> Netherlands", "RU -> Germany"]
    assert all(secret not in result.stdout for item in definitions for secret in (item["relay_uuid"], item["relay_path"], item["domain"]))

    bad_runtime = copy.deepcopy(runtime)
    bad_runtime["routes"]["route-nl-ams-1"]["route_test_outbound"] = "direct"
    runtime_path.write_text(json.dumps(bad_runtime), encoding="utf-8")
    rejected = subprocess.run(
        [sys.executable, str(checker), "--config", str(final), "--runtime", str(runtime_path), "--subscriptions", str(output), "--json"],
        capture_output=True,
        text=True,
    )
    assert rejected.returncode == 1 and "routeTest did not select" in rejected.stderr
    assert all(secret not in rejected.stderr for item in definitions for secret in (item["relay_uuid"], item["relay_path"], item["domain"]))

print("multi-Exit E2E tests: OK")
