#!/usr/bin/env python3
"""Atomic config migration helpers for WaveMesh."""

import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,47}$")


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def atomic_write(path, data):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def stable_node_id(config):
    source = config.get("server", {}).get("domain") or config.get("server", {}).get("hostname") or "standalone-node"
    value = re.sub(r"[^a-z0-9]+", "-", source.lower()).strip("-")[:48]
    return value if ID_RE.fullmatch(value) else "standalone-node"


def migrate_v1(config, backup_path):
    clients = []
    for index, client in enumerate(config.get("clients", []), start=1):
        client_id = client.get("id") or f"client-{index}"
        uuid = client.get("uuid", "")
        migrated = dict(client)
        migrated.update({
            "id": client_id,
            "subscription_id": client.get("subscription_id") or f"sub-{client_id}",
            "credentials": [{"route_id": "route-standalone-default", "email": f"wm.{client_id}.route-standalone-default", "uuid": uuid, "enabled": client.get("enabled", True)}],
        })
        clients.append(migrated)

    panel = config.setdefault("panel", {})
    old_token = panel.pop("token", "")
    panel.setdefault("api_auth", {"mode": "legacy" if old_token else "pending", "token_name": "wavemesh-node-builder", "token": old_token})
    config.pop("version", None)
    config["schema_version"] = 2
    config["node"] = {"id": stable_node_id(config), "role": "standalone", "country": "", "city": ""}
    config["clients"] = clients
    config.setdefault("exits", [])
    config.setdefault("relay_peers", [])
    config.setdefault("routes", [{"id": "route-standalone-default", "kind": "direct", "display_name": "Direct", "enabled": True, "sort_order": 0}])
    config.setdefault("migrations", []).append({"from": 1, "to": 2, "applied_at": utc_now(), "backup": str(backup_path)})
    config.setdefault("builder", {})["version"] = "0.2.0"
    config.setdefault("network", {}).setdefault("subscription", {})["backend"] = "wavemesh-renderer"
    return config


def migrate_v2_defaults(config, backup_path):
    """Add compatible defaults without changing a running node's subscription backend."""
    subscription = config.setdefault("network", {}).setdefault("subscription", {})
    if "backend" in subscription:
        return config, False
    has_metadata = any(client.get("subscription_id") for client in config.get("clients", []))
    mode = str(subscription.get("mode", ""))
    nginx_path = Path(os.environ.get("WM_NGINX_MANAGED_CONF", "/etc/nginx/wavemesh-managed-locations.conf"))
    has_locations = False
    try:
        nginx_text = nginx_path.read_text(encoding="utf-8")
        has_locations = "/var/www/wavemesh-sub/users" in nginx_text or "try_files /sub.txt" in nginx_text
    except OSError:
        pass
    files_present = Path(os.environ.get("WM_SUB_DIR", "/var/www/wavemesh-sub")).joinpath("sub.txt").is_file()
    renderer_state = mode in {"generated", "fallback-generated"} or has_locations or files_present
    detected = "wavemesh-renderer" if renderer_state or (has_metadata and mode != "native") else "xui-native"
    subscription["backend"] = detected
    config.setdefault("migrations", []).append({
        "name": "subscription-backend",
        "applied_at": utc_now(),
        "backup": str(backup_path),
        "selected_backend": detected,
    })
    return config, True


def migrate(path, backup_dir):
    path = Path(path)
    config = json.loads(path.read_text(encoding="utf-8"))
    version = config.get("schema_version", config.get("version", 1))
    if version == 2:
        backup_dir = Path(backup_dir)
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_path = backup_dir / f"config.pre-subscription-backend.{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
        migrated, changed = migrate_v2_defaults(config, backup_path)
        if not changed:
            return
        shutil.copy2(path, backup_path)
        os.chmod(backup_path, 0o600)
        atomic_write(path, migrated)
        return
    if version != 1:
        raise SystemExit(f"unsupported config schema version: {version}")
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"config.v1.{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    shutil.copy2(path, backup_path)
    os.chmod(backup_path, 0o600)
    atomic_write(path, migrate_v1(config, backup_path))


def main():
    if len(sys.argv) != 4 or sys.argv[1] != "migrate":
        raise SystemExit("usage: config_json.py migrate CONFIG BACKUP_DIR")
    migrate(sys.argv[2], sys.argv[3])


if __name__ == "__main__":
    main()
