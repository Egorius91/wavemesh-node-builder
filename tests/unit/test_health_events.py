import importlib.util
import json
import tempfile
from pathlib import Path

root = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("health_events", root / "scripts/lib/health_events.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

with tempfile.TemporaryDirectory() as directory:
    base = Path(directory)
    manual = base / "manual.json"
    auto = base / "auto.json"
    state = base / "state.json"
    events = base / "events.jsonl"

    manual.write_text(json.dumps({
        "node_status": "healthy",
        "routes": [
            {
                "route_id": "route-de-1",
                "display_name": "RU -> Germany",
                "status": "healthy",
                "latency_ms": 120,
                "outbound": "wm-exit-de-1",
            }
        ],
    }), encoding="utf-8")
    auto.write_text(json.dumps({
        "auto_routes": [
            {
                "id": "route-auto-europe",
                "display_name": "Auto Europe",
                "status": "healthy",
                "strategy": "leastPing",
                "healthy_exits": 2,
                "total_exits": 2,
                "override_exit_id": None,
            }
        ],
    }), encoding="utf-8")

    first = module.update(manual, auto, state, events)
    assert len(first["events"]) == 2
    assert {item["event"] for item in first["events"]} == {"baseline"}
    assert len(events.read_text(encoding="utf-8").splitlines()) == 2

    second = module.update(manual, auto, state, events)
    assert second["events"] == []
    assert len(events.read_text(encoding="utf-8").splitlines()) == 2

    changed = json.loads(manual.read_text(encoding="utf-8"))
    changed["node_status"] = "degraded"
    changed["routes"][0]["status"] = "unhealthy"
    manual.write_text(json.dumps(changed), encoding="utf-8")
    third = module.update(manual, auto, state, events)
    assert len(third["events"]) == 1
    assert third["events"][0]["event"] == "status_changed"
    assert third["events"][0]["previous_status"] == "healthy"
    assert third["events"][0]["status"] == "unhealthy"

    auto.write_text(json.dumps({"auto_routes": []}), encoding="utf-8")
    fourth = module.update(manual, auto, state, events)
    assert len(fourth["events"]) == 1
    assert fourth["events"][0]["event"] == "removed"
    assert fourth["events"][0]["kind"] == "auto"

print("health event tests: OK")
