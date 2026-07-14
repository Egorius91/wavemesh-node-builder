import json
import subprocess
import sys
import tempfile
from pathlib import Path

root = Path(__file__).resolve().parents[2]
tool = root / "scripts/lib/xray_template.py"
template = root / "tests/fixtures/xray-template.json"
outbound = root / "tests/fixtures/xray-outbound-de.json"

with tempfile.TemporaryDirectory() as name:
    temp = Path(name)
    first = temp / "first.json"
    second = temp / "second.json"
    removed = temp / "removed.json"

    def merge(source, output):
        subprocess.run([
            sys.executable, str(tool), "merge",
            "--template", str(source),
            "--outbound", str(outbound),
            "--inbound-tag", "wm-route-de-fra-1",
            "--outbound-tag", "wm-exit-de-fra-1",
            "--rule-tag", "wm-rule-de-fra-1",
            "--output", str(output),
        ], check=True)

    merge(template, first)
    merge(first, second)
    data = json.loads(second.read_text())
    assert data["api"]["tag"] == "api" and "RoutingService" in data["api"]["services"]
    assert data["inbounds"][0]["listen"] == "127.0.0.1" and data["inbounds"][0]["port"] == 62789
    assert [x["tag"] for x in data["outbounds"]].count("wm-exit-de-fra-1") == 1
    assert data["outbounds"][0]["tag"] == "user-proxy"
    tags = [x.get("ruleTag") for x in data["routing"]["rules"]]
    assert tags == ["wm-api-rule", "user-rule", "wm-rule-de-fra-1", "catch-all"]

    subprocess.run([
        sys.executable, str(tool), "remove",
        "--template", str(second),
        "--inbound-tag", "wm-route-de-fra-1",
        "--outbound-tag", "wm-exit-de-fra-1",
        "--rule-tag", "wm-rule-de-fra-1",
        "--output", str(removed),
    ], check=True)
    clean = json.loads(removed.read_text())
    assert all(x.get("tag") != "wm-exit-de-fra-1" for x in clean["outbounds"])
    assert [x.get("ruleTag") for x in clean["routing"]["rules"]] == ["wm-api-rule", "user-rule", "catch-all"]

    # Add a second managed Exit so Auto Route can select between two exact outbounds.
    auto_source = json.loads(second.read_text())
    second_exit = json.loads(outbound.read_text())
    second_exit["tag"] = "wm-exit-de-frankfurt-2"
    auto_source["outbounds"].append(second_exit)
    auto_input = temp / "auto-input.json"
    auto_input.write_text(json.dumps(auto_source), encoding="utf-8")
    auto_first = temp / "auto-first.json"
    auto_second = temp / "auto-second.json"

    def merge_balancer(source, output, selectors="wm-exit-de-fra-1,wm-exit-de-frankfurt-2"):
        return subprocess.run([
            sys.executable, str(tool), "merge-balancer",
            "--template", str(source),
            "--selectors", selectors,
            "--inbound-tag", "wm-route-auto-europe",
            "--balancer-tag", "wm-balancer-auto-europe",
            "--rule-tag", "wm-rule-auto-europe",
            "--strategy", "leastPing",
            "--output", str(output),
        ], check=False, capture_output=True, text=True)

    assert merge_balancer(auto_input, auto_first).returncode == 0
    assert merge_balancer(auto_first, auto_second).returncode == 0
    auto = json.loads(auto_second.read_text())
    balancers = auto["routing"]["balancers"]
    assert len([item for item in balancers if item.get("tag") == "wm-balancer-auto-europe"]) == 1
    balancer = next(item for item in balancers if item.get("tag") == "wm-balancer-auto-europe")
    assert balancer["selector"] == ["wm-exit-de-fra-1", "wm-exit-de-frankfurt-2"]
    assert balancer["strategy"] == {"type": "leastPing"}
    auto_rules = auto["routing"]["rules"]
    auto_index = next(index for index, item in enumerate(auto_rules) if item.get("ruleTag") == "wm-rule-auto-europe")
    catch_index = next(index for index, item in enumerate(auto_rules) if item.get("ruleTag") == "catch-all")
    assert auto_index < catch_index
    assert auto_rules[auto_index]["balancerTag"] == "wm-balancer-auto-europe"
    assert "outboundTag" not in auto_rules[auto_index]
    assert auto["observatory"]["subjectSelector"] == ["wm-exit-de-fra-1", "wm-exit-de-frankfurt-2"]
    assert auto["observatory"]["probeInterval"] == "30s"

    missing = merge_balancer(auto_input, temp / "missing.json", "wm-exit-de-fra-1,wm-exit-missing")
    assert missing.returncode != 0 and "missing outbounds" in missing.stderr
    empty = merge_balancer(auto_input, temp / "empty.json", "")
    assert empty.returncode != 0 and "at least one" in empty.stderr
    unsupported = subprocess.run([
        sys.executable, str(tool), "merge-balancer",
        "--template", str(auto_input),
        "--selectors", "wm-exit-de-fra-1",
        "--inbound-tag", "wm-route-auto-europe",
        "--balancer-tag", "wm-balancer-auto-europe",
        "--rule-tag", "wm-rule-auto-europe",
        "--strategy", "random",
        "--output", str(temp / "unsupported.json"),
    ], check=False, capture_output=True, text=True)
    assert unsupported.returncode != 0 and "leastPing" in unsupported.stderr

    auto_removed = temp / "auto-removed.json"
    subprocess.run([
        sys.executable, str(tool), "remove-balancer",
        "--template", str(auto_second),
        "--inbound-tag", "wm-route-auto-europe",
        "--balancer-tag", "wm-balancer-auto-europe",
        "--rule-tag", "wm-rule-auto-europe",
        "--output", str(auto_removed),
    ], check=True)
    removed_auto = json.loads(auto_removed.read_text())
    assert all(item.get("tag") != "wm-balancer-auto-europe" for item in removed_auto["routing"].get("balancers", []))
    assert all(item.get("ruleTag") != "wm-rule-auto-europe" for item in removed_auto["routing"]["rules"])
    assert "observatory" not in removed_auto

print("xray template tests: OK")
