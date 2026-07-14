import importlib.util
import json
import tempfile
from pathlib import Path

root = Path(__file__).resolve().parents[2]
tool = root / "scripts/lib/auto_route.py"
spec = importlib.util.spec_from_file_location("auto_route", tool)
auto = importlib.util.module_from_spec(spec)
spec.loader.exec_module(auto)

config = json.loads((root / "tests/fixtures/config-entry-v2.json").read_text(encoding="utf-8"))
config["node"]["role"] = "entry"
config["clients"] = [{
    "id": "client-1",
    "enabled": True,
    "subscription_id": "sub-auto-test",
    "credentials": [],
}]
config["exits"] = [
    {"id": "de-fra-1", "enabled": True, "xray": {"outbound_tag": "wm-exit-de-fra-1"}},
    {"id": "de-frankfurt-2", "enabled": True, "xray": {"outbound_tag": "wm-exit-de-frankfurt-2"}},
    {"id": "disabled-exit", "enabled": False, "xray": {"outbound_tag": "wm-exit-disabled-exit"}},
]
config["routes"] = []
config["balancers"] = []

candidate, clients = auto.prepare(
    config,
    "auto-europe",
    "⚡ RU -> Auto Europe",
    [],
    21090,
    "/api/auto/abcdefghijklmnopqr/",
    50,
)
assert len(candidate["balancers"]) == 1
balancer = candidate["balancers"][0]
assert balancer["strategy"] == "leastPing"
assert balancer["selector"] == ["wm-exit-de-fra-1", "wm-exit-de-frankfurt-2"]
route = candidate["routes"][0]
assert route["kind"] == "auto"
assert route["presentation"]["published"] is False
assert route["routing"]["balancer_tag"] == "wm-balancer-auto-europe"
assert route["entry"]["inbound_id"] is None
assert len(clients) == 1 and clients[0]["subId"] == "sub-auto-test"
credential = candidate["clients"][0]["credentials"][0]
assert credential["route_id"] == "route-auto-auto-europe"

finished = auto.finalize(candidate, "route-auto-auto-europe", 77)
assert finished["routes"][0]["entry"]["inbound_id"] == 77

try:
    auto.prepare(candidate, "auto-europe", "duplicate", [], 21091, "/api/auto/duplicateabcdefgh/", 50)
    raise AssertionError("duplicate Auto Route accepted")
except ValueError as error:
    assert "already exists" in str(error)

try:
    auto.prepare(config, "only-disabled", "bad", ["disabled-exit"], 21092, "/api/auto/disabledabcdefgh/", 50)
    raise AssertionError("disabled Exit accepted")
except ValueError as error:
    assert "disabled" in str(error)

try:
    auto.prepare(config, "missing", "bad", ["missing-exit"], 21093, "/api/auto/missingabcdefghij/", 50)
    raise AssertionError("missing Exit accepted")
except ValueError as error:
    assert "unknown" in str(error)

with tempfile.TemporaryDirectory() as name:
    path = Path(name) / "state.json"
    auto.atomic(path, candidate)
    assert json.loads(path.read_text(encoding="utf-8"))["routes"][0]["kind"] == "auto"

print("auto route tests: OK")
