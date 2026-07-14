#!/usr/bin/env python3
"""Persist WaveMesh health snapshots and append transition-only JSONL events."""

import argparse
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def load(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize(manual, auto):
    entities = {}
    for item in manual.get("routes", []):
        route_id = item.get("route_id") or item.get("id")
        if not route_id:
            continue
        entities[f"manual:{route_id}"] = {
            "kind": "manual",
            "id": route_id,
            "display_name": item.get("display_name") or route_id,
            "status": item.get("status") or "unknown",
            "latency_ms": item.get("latency_ms"),
            "outbound": item.get("outbound"),
        }
    for item in auto.get("auto_routes", []):
        route_id = item.get("route_id") or item.get("id")
        if not route_id:
            continue
        entities[f"auto:{route_id}"] = {
            "kind": "auto",
            "id": route_id,
            "display_name": item.get("display_name") or route_id,
            "status": item.get("status") or "unknown",
            "strategy": item.get("strategy"),
            "healthy_exits": item.get("healthy_exits"),
            "total_exits": item.get("total_exits"),
            "override_exit_id": item.get("override_exit_id"),
        }
    return entities


def atomic_write(path, data):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def update(manual_path, auto_path, state_path, events_path):
    now = utc_now()
    manual = load(manual_path, {})
    auto = load(auto_path, {})
    previous = load(state_path, {"entities": {}})
    current_entities = normalize(manual, auto)
    previous_entities = previous.get("entities", {})
    events = []

    for key, entity in current_entities.items():
        old = previous_entities.get(key)
        if old is None:
            event_type = "baseline"
            previous_status = None
        elif old.get("status") != entity.get("status"):
            event_type = "status_changed"
            previous_status = old.get("status")
        else:
            continue
        events.append({
            "observed_at": now,
            "event": event_type,
            "entity": key,
            "kind": entity["kind"],
            "id": entity["id"],
            "display_name": entity["display_name"],
            "previous_status": previous_status,
            "status": entity["status"],
        })

    for key, old in previous_entities.items():
        if key not in current_entities:
            events.append({
                "observed_at": now,
                "event": "removed",
                "entity": key,
                "kind": old.get("kind"),
                "id": old.get("id"),
                "display_name": old.get("display_name") or old.get("id"),
                "previous_status": old.get("status"),
                "status": "removed",
            })

    state = {
        "observed_at": now,
        "node_status": manual.get("node_status") or "unknown",
        "entities": current_entities,
    }
    atomic_write(state_path, state)
    event_file = Path(events_path)
    event_file.parent.mkdir(parents=True, exist_ok=True)
    if events:
        with event_file.open("a", encoding="utf-8") as stream:
            for event in events:
                stream.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
        os.chmod(event_file, 0o600)
    return {"state": state, "events": events}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manual", required=True)
    parser.add_argument("--auto", required=True)
    parser.add_argument("--state", required=True)
    parser.add_argument("--events", required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = update(args.manual, args.auto, args.state, args.events)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(f"Health snapshot stored; {len(result['events'])} event(s) appended")


if __name__ == "__main__":
    main()
