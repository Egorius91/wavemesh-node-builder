#!/usr/bin/env python3
"""Build persistent WaveMesh Auto Route desired state without public exposure."""

import argparse
import copy
import json
import os
import tempfile
import uuid
from pathlib import Path


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def atomic(path, data):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def validate_id(value):
    if not value or len(value) > 32 or not value.replace("-", "").isalnum() or value.lower() != value:
        raise ValueError("Auto Route id must contain lowercase letters, digits, and hyphens")


def enabled_exit_tags(config, requested):
    exits = {item["id"]: item for item in config.get("exits", []) if item.get("enabled", True)}
    if requested:
        missing = [value for value in requested if value not in exits]
        if missing:
            raise ValueError(f"unknown or disabled Exit ids: {', '.join(missing)}")
        chosen = requested
    else:
        chosen = [item["id"] for item in config.get("exits", []) if item.get("enabled", True)]
    selectors = []
    for exit_id in chosen:
        tag = exits[exit_id].get("xray", {}).get("outbound_tag")
        if not tag or not tag.startswith("wm-exit-"):
            raise ValueError(f"Exit has no managed outbound tag: {exit_id}")
        selectors.append(tag)
    selectors = list(dict.fromkeys(selectors))
    if not selectors:
        raise ValueError("Auto Route requires at least one enabled Exit")
    return selectors


def prepare(config, auto_id, display_name, requested_exits, local_port, public_path, sort_order):
    if config.get("node", {}).get("role") != "entry":
        raise ValueError("Auto Route requires node.role=entry")
    validate_id(auto_id)
    if any(route.get("id") == f"route-auto-{auto_id}" for route in config.get("routes", [])):
        raise ValueError(f"Auto Route already exists: {auto_id}")
    if any(item.get("id") == auto_id for item in config.get("balancers", [])):
        raise ValueError(f"Auto balancer already exists: {auto_id}")
    if not (21000 <= int(local_port) <= 21999):
        raise ValueError("Auto Route local port must be in 21000..21999")
    if not public_path.startswith("/api/") or not public_path.endswith("/") or len(public_path) < 16:
        raise ValueError("invalid Auto Route public path")

    result = copy.deepcopy(config)
    selectors = enabled_exit_tags(result, requested_exits)
    route_id = f"route-auto-{auto_id}"
    inbound_tag = f"wm-route-auto-{auto_id}"
    balancer_tag = f"wm-balancer-{auto_id}"
    rule_tag = f"wm-rule-auto-{auto_id}"
    observatory_tag = f"wm-observatory-{auto_id}"

    used = {
        credential.get("uuid")
        for client in result.get("clients", [])
        for credential in client.get("credentials", [])
    }
    inbound_clients = []
    for client in result.get("clients", []):
        value = str(uuid.uuid4())
        while value in used:
            value = str(uuid.uuid4())
        used.add(value)
        email = f"wm.{client['id']}.{route_id}"
        credential = {
            "route_id": route_id,
            "email": email,
            "uuid": value,
            "enabled": True,
        }
        client.setdefault("credentials", []).append(credential)
        inbound_clients.append({
            "id": value,
            "email": email,
            "enable": bool(client.get("enabled", True)),
            "flow": "",
            "limitIp": 0,
            "totalGB": 0,
            "expiryTime": 0,
            "tgId": 0,
            "subId": client.get("subscription_id", ""),
        })
    if not inbound_clients:
        raise ValueError("Entry has no clients")

    result.setdefault("balancers", []).append({
        "id": auto_id,
        "display_name": display_name,
        "enabled": True,
        "strategy": "leastPing",
        "selector": selectors,
        "balancer_tag": balancer_tag,
        "observatory_tag": observatory_tag,
    })
    result.setdefault("routes", []).append({
        "id": route_id,
        "kind": "auto",
        "display_name": display_name,
        "enabled": True,
        "sort_order": int(sort_order),
        "balancer_id": auto_id,
        "entry": {
            "listen": "127.0.0.1",
            "local_port": int(local_port),
            "public_path": public_path,
            "inbound_id": None,
            "inbound_tag": inbound_tag,
            "xhttp_mode": "stream-one",
        },
        "routing": {
            "rule_tag": rule_tag,
            "balancer_tag": balancer_tag,
        },
        "presentation": {"published": False},
    })
    return result, inbound_clients


def finalize(config, route_id, inbound_id):
    result = copy.deepcopy(config)
    route = next((item for item in result.get("routes", []) if item.get("id") == route_id and item.get("kind") == "auto"), None)
    if not route:
        raise ValueError(f"Auto Route not found: {route_id}")
    route["entry"]["inbound_id"] = int(inbound_id)
    return result


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("prepare")
    build.add_argument("--config", required=True)
    build.add_argument("--candidate", required=True)
    build.add_argument("--clients", required=True)
    build.add_argument("--id", required=True)
    build.add_argument("--display-name", required=True)
    build.add_argument("--exit-ids", default="")
    build.add_argument("--port", type=int, required=True)
    build.add_argument("--path", required=True)
    build.add_argument("--sort-order", type=int, default=50)
    finish = sub.add_parser("finalize")
    finish.add_argument("--candidate", required=True)
    finish.add_argument("--route-id", required=True)
    finish.add_argument("--inbound-id", type=int, required=True)
    args = parser.parse_args()
    if args.command == "prepare":
        requested = [value.strip() for value in args.exit_ids.split(",") if value.strip()]
        candidate, clients = prepare(load(args.config), args.id, args.display_name, requested, args.port, args.path, args.sort_order)
        atomic(args.candidate, candidate)
        atomic(args.clients, clients)
    else:
        atomic(args.candidate, finalize(load(args.candidate), args.route_id, args.inbound_id))


if __name__ == "__main__":
    main()
