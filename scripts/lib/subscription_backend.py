#!/usr/bin/env python3
"""Subscription backend configuration and 3X-UI settings helpers."""

import argparse
import json
import os
import secrets
import sqlite3
import tempfile
from pathlib import Path

BACKENDS = {"xui-native", "wavemesh-renderer"}
SETTING_KEYS = (
    "subEnable", "subJsonEnable", "subClashEnable", "subListen", "subPort",
    "subPath", "subDomain", "subURI", "subShowInfo", "remarkModel",
)


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def atomic(path, data):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def configured_backend(config):
    value = config.get("network", {}).get("subscription", {}).get("backend", "wavemesh-renderer")
    if value not in BACKENDS:
        raise ValueError(f"unsupported subscription backend: {value}")
    return value


def candidate(config, backend):
    if backend not in BACKENDS:
        raise ValueError(f"unsupported subscription backend: {backend}")
    result = json.loads(json.dumps(config))
    subscription = result.setdefault("network", {}).setdefault("subscription", {})
    subscription["backend"] = backend
    subscription["mode"] = "native" if backend == "xui-native" else "generated"
    if backend == "xui-native":
        subscription["local_port"] = 2096
        if str(subscription.get("path", "")).startswith("/sub/"):
            alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
            segment = lambda size: "".join(secrets.choice(alphabet) for _ in range(size))
            subscription["path"] = f"/{segment(10)}/{segment(18)}/"
    return result


def desired_xui_settings(config):
    subscription = config["network"]["subscription"]
    backend = configured_backend(config)
    domain = config["server"]["domain"]
    path = subscription["path"]
    if not path.startswith("/") or not path.endswith("/"):
        raise ValueError("subscription path must start and end with a slash")
    port = int(subscription.get("local_port", 2096))
    if backend == "xui-native" and port != 2096:
        raise ValueError("xui-native subscription port must be 2096")
    return {
        "subEnable": "true" if backend == "xui-native" else "false",
        "subJsonEnable": "false",
        "subClashEnable": "false",
        "subListen": "127.0.0.1",
        "subPort": str(port),
        "subPath": path,
        "subDomain": domain,
        "subURI": f"https://{domain}{path}",
        "subShowInfo": "false",
        "remarkModel": "-o",
    }


def read_settings(database):
    conn = sqlite3.connect(database)
    try:
        rows = conn.execute(
            f"SELECT key,value FROM settings WHERE key IN ({','.join('?' for _ in SETTING_KEYS)})",
            SETTING_KEYS,
        ).fetchall()
    finally:
        conn.close()
    return {key: str(value) for key, value in rows}


def apply_settings(database, config):
    values = desired_xui_settings(config)
    conn = sqlite3.connect(database)
    try:
        conn.execute("BEGIN IMMEDIATE")
        for key, value in values.items():
            if conn.execute("SELECT 1 FROM settings WHERE key=?", (key,)).fetchone():
                conn.execute("UPDATE settings SET value=? WHERE key=?", (value, key))
            else:
                conn.execute("INSERT INTO settings (key,value) VALUES (?,?)", (key, value))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return values


def public_routes(config):
    routes = []
    for route in sorted(config.get("routes", []), key=lambda item: (item.get("sort_order", 0), item.get("id", ""))):
        kind = route.get("kind")
        if kind not in ("cascade", "auto"):
            continue
        published = bool(route.get("enabled", True))
        if kind == "auto":
            published = published and bool(route.get("presentation", {}).get("published", False))
        if not published:
            continue
        routes.append({
            "route_id": route.get("id"),
            "kind": kind,
            "display_name": route.get("display_name", ""),
            "inbound_id": route.get("entry", {}).get("inbound_id"),
            "published": published,
        })
    subscription = config.get("network", {}).get("subscription", {})
    return {
        "node_id": config.get("node", {}).get("id"),
        "domain": config.get("server", {}).get("domain"),
        "subscription_backend": configured_backend(config),
        "subscription_base_url": f"https://{config['server']['domain']}{subscription.get('path', '')}",
        "routes": routes,
    }


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    get = commands.add_parser("get"); get.add_argument("--config", required=True)
    set_backend = commands.add_parser("set"); set_backend.add_argument("--config", required=True); set_backend.add_argument("--output", required=True); set_backend.add_argument("--backend", required=True, choices=sorted(BACKENDS))
    settings = commands.add_parser("settings-read"); settings.add_argument("--database", required=True)
    apply = commands.add_parser("settings-apply"); apply.add_argument("--database", required=True); apply.add_argument("--config", required=True)
    routes = commands.add_parser("public-routes"); routes.add_argument("--config", required=True)
    args = parser.parse_args()
    if args.command == "get":
        print(configured_backend(load(args.config)))
    elif args.command == "set":
        atomic(args.output, candidate(load(args.config), args.backend))
    elif args.command == "settings-read":
        print(json.dumps(read_settings(args.database), sort_keys=True))
    elif args.command == "settings-apply":
        print(json.dumps(apply_settings(args.database, load(args.config)), sort_keys=True))
    else:
        print(json.dumps(public_routes(load(args.config)), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
