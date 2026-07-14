#!/usr/bin/env python3
"""Desired/observed state helpers for WaveMesh route lifecycle commands."""

import argparse
import copy
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load(path, default=None):
    target = Path(path)
    if not target.exists() or target.stat().st_size == 0:
        return copy.deepcopy(default)
    return json.loads(target.read_text(encoding="utf-8"))


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


def route(config, route_id):
    found = next((item for item in config.get("routes", []) if item.get("id") == route_id), None)
    if not found or found.get("kind") != "cascade":
        raise ValueError(f"cascade route not found: {route_id}")
    return found


def exit_for_route(config, item):
    found = next((value for value in config.get("exits", []) if value.get("id") == item.get("exit_id")), None)
    if not found:
        raise ValueError(f"Exit not found for route: {item['id']}")
    return found


def set_credentials_enabled(config, route_id, enabled):
    for client in config.get("clients", []):
        for credential in client.get("credentials", []):
            if credential.get("route_id") == route_id:
                credential["enabled"] = enabled


def mutate(config, operation, target, force=False):
    result = copy.deepcopy(config)
    if operation in ("enable", "disable"):
        item = route(result, target)
        enabled = operation == "enable"
        item["enabled"] = enabled
        set_credentials_enabled(result, target, enabled)
        return result, [target]
    if operation == "remove-route":
        route(result, target)
        result["routes"] = [item for item in result.get("routes", []) if item.get("id") != target]
        for client in result.get("clients", []):
            client["credentials"] = [item for item in client.get("credentials", []) if item.get("route_id") != target]
        return result, [target]
    if operation == "remove-exit":
        if not any(item.get("id") == target for item in result.get("exits", [])):
            raise ValueError(f"Exit not found: {target}")
        attached = [item["id"] for item in result.get("routes", []) if item.get("exit_id") == target]
        if attached and not force:
            raise ValueError("Exit still has routes; use --force to remove them")
        result["routes"] = [item for item in result.get("routes", []) if item.get("exit_id") != target]
        for client in result.get("clients", []):
            client["credentials"] = [item for item in client.get("credentials", []) if item.get("route_id") not in attached]
        result["exits"] = [item for item in result.get("exits", []) if item.get("id") != target]
        return result, attached
    raise ValueError(f"unsupported mutation: {operation}")


def inbound_payload(config, item):
    published = item.get("kind") != "auto" or item.get("presentation", {}).get("published", False)
    clients = []
    for client in config.get("clients", []) if published else []:
        credential = next((value for value in client.get("credentials", []) if value.get("route_id") == item["id"]), None)
        if not credential:
            continue
        clients.append({
            "id": credential["uuid"],
            "email": credential["email"],
            "enable": bool(client.get("enabled", True) and credential.get("enabled", True)),
            "flow": "", "limitIp": 0, "totalGB": 0, "expiryTime": 0, "tgId": 0,
            "subId": client.get("subscription_id", ""),
        })
    entry = item["entry"]
    domain = config["server"]["domain"]
    tag = entry["inbound_tag"]
    public_name = item.get("display_name") or tag
    inbound_remark = public_name if published else f"--!{tag}"
    return {
        "up": 0, "down": 0, "total": 0, "remark": inbound_remark, "tag": tag,
        "enable": bool(item.get("enabled", True)), "expiryTime": 0,
        "listen": "127.0.0.1", "port": entry["local_port"], "protocol": "vless",
        "settings": {"clients": clients, "decryption": "none", "fallbacks": []},
        "streamSettings": {
            "network": "xhttp", "security": "none",
            "xhttpSettings": {"path": entry["public_path"], "host": domain, "mode": "stream-one"},
            "externalProxy": [{"dest": domain, "port": 443, "remark": public_name, "forceTls": "tls", "sni": domain, "fingerprint": "randomized"}],
        },
        "sniffing": {"enabled": True, "destOverride": ["http", "tls", "quic", "fakedns"], "metadataOnly": False, "routeOnly": False},
        "allocate": {"strategy": "always"},
    }


def outbound_payload(target, item):
    endpoint = target["endpoint"]
    return {
        "tag": item["routing"]["outbound_tag"], "protocol": "vless",
        "settings": {"vnext": [{"address": endpoint["domain"], "port": endpoint["port"], "users": [{"id": endpoint["relay_uuid"], "encryption": "none"}]}]},
        "streamSettings": {
            "network": "xhttp", "security": "tls",
            "tlsSettings": {"serverName": endpoint["sni"], "allowInsecure": False, "fingerprint": endpoint["fingerprint"]},
            "xhttpSettings": {"path": endpoint["relay_path"], "host": endpoint["host"], "mode": "stream-one"},
        },
    }


def write_artifacts(config, route_id, inbound_path, outbound_path):
    item = route(config, route_id)
    target = exit_for_route(config, item)
    atomic(inbound_path, inbound_payload(config, item))
    atomic(outbound_path, outbound_payload(target, item))


def set_inbound_id(config, route_id, inbound_id):
    result = copy.deepcopy(config)
    route(result, route_id)["entry"]["inbound_id"] = int(inbound_id)
    return result


def parse_inbounds(response):
    values = response.get("obj", []) if isinstance(response, dict) else []
    return values if isinstance(values, list) else []


def as_object(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return {}
    return value if isinstance(value, dict) else {}


def normalize_inbound(value):
    stream = as_object(value.get("streamSettings"))
    settings = as_object(value.get("settings"))
    xhttp = as_object(stream.get("xhttpSettings"))
    clients = settings.get("clients") if isinstance(settings.get("clients"), list) else []
    proxies = stream.get("externalProxy") if isinstance(stream.get("externalProxy"), list) else []
    proxy_remark = next((item.get("remark") for item in proxies if isinstance(item, dict)), None)
    return {
        "tag": value.get("tag") or value.get("remark"),
        "remark": value.get("remark"),
        "external_proxy_remark": proxy_remark,
        "listen": value.get("listen"),
        "port": int(value.get("port", -1)),
        "protocol": value.get("protocol"),
        "path": xhttp.get("path"),
        "host": xhttp.get("host"),
        "mode": xhttp.get("mode"),
        "clients": sorted((item.get("id"), item.get("email"), item.get("enable", True)) for item in clients if isinstance(item, dict)),
    }


def inbound_structure_matches(actual, expected):
    """Compare managed inbound fields while allowing unrelated extra clients."""
    actual_normalized = normalize_inbound(actual)
    expected_normalized = normalize_inbound(expected)
    actual_clients = set(actual_normalized.pop("clients"))
    expected_clients = set(expected_normalized.pop("clients"))
    return actual_normalized == expected_normalized and expected_clients.issubset(actual_clients)


MATCH_KEYS = {"domain", "ip", "port", "sourcePort", "localPort", "network", "source", "sourceIP", "user", "inboundTag", "protocol", "attrs"}


def is_catch_all(rule):
    return not any(key in rule and rule[key] not in (None, "", []) for key in MATCH_KEYS)


def transition(previous, success, enabled=True, misconfigured=False):
    previous = previous or {}
    if not enabled:
        return "disabled", 0, 0
    if misconfigured:
        return "misconfigured", 0, previous.get("consecutive_failures", 0) + 1
    if success:
        successes = previous.get("consecutive_successes", 0) + 1
        status = "healthy" if successes >= 3 else ("healthy" if previous.get("status") == "healthy" else "unknown")
        return status, successes, 0
    failures = previous.get("consecutive_failures", 0) + 1
    status = "unhealthy" if failures >= 3 else ("healthy" if previous.get("status") == "healthy" else "unknown")
    return status, 0, failures


def evaluate(config, previous, inbounds_response, template, nginx_text, subscription_dir, probes):
    observed_at = utc_now()
    actual_inbounds = parse_inbounds(inbounds_response)
    actual_by_tag = {item.get("tag") or item.get("remark"): item for item in actual_inbounds}
    actual_outbounds = {item.get("tag"): item for item in template.get("outbounds", [])}
    rules = template.get("routing", {}).get("rules", [])
    route_probes = {item.get("route_id"): item for item in probes.get("routes", [])}
    old_exits = (previous or {}).get("exits", {})
    result = {"observed_at": observed_at, "node_status": "healthy", "xui": probes.get("control", {}), "exits": {}, "routes": {}}
    if not all(value in (True, "active", "reachable", "running", "valid", "loopback") for value in result["xui"].values()):
        result["node_status"] = "unhealthy"
    subscriptions = Path(subscription_dir)
    subscription_config = config.get("network", {}).get("subscription", {})
    subscription_backend = subscription_config.get("backend") or subscription_config.get("mode") or "generated"
    for item in config.get("routes", []):
        if item.get("kind") != "cascade":
            continue
        route_id = item["id"]
        enabled = item.get("enabled", True)
        entry = item["entry"]
        routing = item["routing"]
        inbound = actual_by_tag.get(entry["inbound_tag"])
        inbound_present = inbound is not None
        inbound_enabled = bool(inbound.get("enable", True)) if inbound else False
        inbound_matches = inbound_present and inbound_structure_matches(inbound, inbound_payload(config, item))
        rule_index = next((index for index, rule in enumerate(rules) if rule.get("ruleTag") == routing["rule_tag"] and rule.get("outboundTag") == routing["outbound_tag"] and entry["inbound_tag"] in ([rule.get("inboundTag")] if isinstance(rule.get("inboundTag"), str) else rule.get("inboundTag", []))), None)
        catch_index = next((index for index, rule in enumerate(rules) if is_catch_all(rule)), len(rules))
        rule_present = rule_index is not None and rule_index < catch_index
        actual_outbound = actual_outbounds.get(routing["outbound_tag"])
        outbound_present = actual_outbound is not None
        outbound_matches = outbound_present and actual_outbound == outbound_payload(exit_for_route(config, item), item)
        nginx_present = entry["public_path"] in nginx_text and f"proxy_pass http://127.0.0.1:{entry['local_port']};" in nginx_text
        profile_matches = True
        native_clients = as_object(inbound.get("settings") if inbound else {}).get("clients", []) if subscription_backend == "xui-native" else []
        for client in config.get("clients", []):
            credential = next((value for value in client.get("credentials", []) if value.get("route_id") == route_id), None)
            if not credential:
                continue
            if subscription_backend == "xui-native":
                found = any(
                    value.get("id") == credential["uuid"] and value.get("subId") == client.get("subscription_id", "")
                    for value in native_clients if isinstance(value, dict)
                )
            else:
                target = subscriptions / "users" / f"{client.get('subscription_id', '')}.txt"
                found = target.exists() and credential["uuid"] in target.read_text(encoding="utf-8")
            expected = enabled and client.get("enabled", True) and credential.get("enabled", True)
            if found != expected:
                profile_matches = False
        probe = route_probes.get(route_id, {})
        structural = inbound_matches and inbound_enabled == enabled and rule_present and outbound_matches and nginx_present == enabled and profile_matches
        success = structural and probe.get("route_test") is True and probe.get("test_outbound") is True
        misconfigured = enabled and not structural
        old = old_exits.get(item.get("exit_id"), {})
        status, successes, failures = transition(old, success, enabled, misconfigured)
        error = "managed state differs from desired configuration" if misconfigured else probe.get("error")
        exit_state = {"status": status, "consecutive_successes": successes, "consecutive_failures": failures, "last_latency_ms": probe.get("latency_ms"), "last_error": str(error)[:240] if error else None, "last_success_at": observed_at if success else old.get("last_success_at")}
        result["exits"][item["exit_id"]] = exit_state
        result["routes"][route_id] = {
            "status": status, "enabled": enabled, "exit_id": item["exit_id"],
            "inbound": "present" if inbound_matches else ("drifted" if inbound_present else "missing"),
            "inbound_enabled": inbound_enabled,
            "outbound": "present" if outbound_matches else ("drifted" if outbound_present else "missing"),
            "routing_rule": "present" if rule_present else "missing",
            "route_test_outbound": probe.get("route_test_outbound"),
            "nginx_location": "present" if nginx_present else "missing",
            "subscription_profile": ("present" if enabled else "absent") if profile_matches else "drifted",
        }
        if status in ("unhealthy", "misconfigured"):
            result["node_status"] = "unhealthy"
        elif status == "unknown" and result["node_status"] == "healthy":
            result["node_status"] = "unknown"
    return result


def drift_plan(config, runtime):
    actions = []
    for item in config.get("routes", []):
        if item.get("kind") != "cascade":
            continue
        observed = runtime.get("routes", {}).get(item["id"], {})
        enabled = item.get("enabled", True)
        if observed.get("inbound") != "present" or observed.get("inbound_enabled") != enabled:
            actions.append({"route_id": item["id"], "component": "inbound", "action": "reconcile"})
        if enabled and (observed.get("outbound") != "present" or observed.get("routing_rule") != "present"):
            actions.append({"route_id": item["id"], "component": "xray", "action": "reconcile-managed-route"})
        if observed.get("nginx_location") != ("present" if enabled else "missing"):
            actions.append({"route_id": item["id"], "component": "nginx", "action": "render-desired"})
        if observed.get("subscription_profile") != ("present" if enabled else "absent"):
            actions.append({"route_id": item["id"], "component": "subscription", "action": "rebuild"})
    return actions


def sync_runtime(config, runtime):
    result = copy.deepcopy(runtime or {})
    route_ids = {item["id"] for item in config.get("routes", []) if item.get("kind") == "cascade"}
    exit_ids = {item["id"] for item in config.get("exits", [])}
    result["routes"] = {key: value for key, value in result.get("routes", {}).items() if key in route_ids}
    result["exits"] = {key: value for key, value in result.get("exits", {}).items() if key in exit_ids}
    for item in config.get("routes", []):
        if item.get("kind") != "cascade":
            continue
        state = result.setdefault("routes", {}).setdefault(item["id"], {})
        previous_enabled = state.get("enabled")
        state["enabled"] = item.get("enabled", True)
        state["exit_id"] = item.get("exit_id")
        if not state["enabled"]:
            state["status"] = "disabled"
            result.setdefault("exits", {}).setdefault(item["exit_id"], {}).update({"status": "disabled", "consecutive_successes": 0, "consecutive_failures": 0, "last_error": None})
        elif previous_enabled is False or state.get("status") == "disabled":
            state["status"] = "unknown"
            result.setdefault("exits", {}).setdefault(item["exit_id"], {}).update({"status": "unknown", "consecutive_successes": 0, "consecutive_failures": 0})
    return result


def render_status(config, runtime, as_json=False):
    rows = []
    exits = {item["id"]: item for item in config.get("exits", [])}
    for item in sorted((value for value in config.get("routes", []) if value.get("kind") == "cascade"), key=lambda value: (value.get("sort_order", 0), value["id"])):
        state = runtime.get("routes", {}).get(item["id"], {})
        exit_state = runtime.get("exits", {}).get(item.get("exit_id"), {})
        rows.append({"route_id": item["id"], "display_name": item.get("display_name") or exits.get(item.get("exit_id"), {}).get("display_name", item["id"]), "exit_id": item.get("exit_id"), "enabled": item.get("enabled", True), "status": state.get("status", "unknown" if item.get("enabled", True) else "disabled"), "latency_ms": exit_state.get("last_latency_ms"), "outbound": item.get("routing", {}).get("outbound_tag"), "last_check": runtime.get("observed_at")})
    if as_json:
        return json.dumps({"node_status": runtime.get("node_status", "unknown"), "observed_at": runtime.get("observed_at"), "routes": rows}, indent=2, ensure_ascii=False)
    if not rows:
        return "No cascade routes"
    lines = ["ROUTE\tSTATUS\tLATENCY\tOUTBOUND\tLAST CHECK"]
    for row in rows:
        latency = f"{row['latency_ms']} ms" if row["latency_ms"] is not None else "-"
        lines.append(f"{row['display_name']}\t{row['status']}\t{latency}\t{row['outbound']}\t{row['last_check'] or '-'}")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    mutation = sub.add_parser("mutate")
    mutation.add_argument("--config", required=True); mutation.add_argument("--output", required=True)
    mutation.add_argument("--operation", choices=("enable", "disable", "remove-route", "remove-exit"), required=True)
    mutation.add_argument("--target", required=True); mutation.add_argument("--force", action="store_true"); mutation.add_argument("--affected")
    artifacts = sub.add_parser("artifacts")
    artifacts.add_argument("--config", required=True); artifacts.add_argument("--route-id", required=True); artifacts.add_argument("--inbound", required=True); artifacts.add_argument("--outbound", required=True)
    inbound_artifact = sub.add_parser("inbound-artifact")
    inbound_artifact.add_argument("--config", required=True); inbound_artifact.add_argument("--route-id", required=True); inbound_artifact.add_argument("--output", required=True)
    inbound_id = sub.add_parser("set-inbound-id")
    inbound_id.add_argument("--config", required=True); inbound_id.add_argument("--output", required=True); inbound_id.add_argument("--route-id", required=True); inbound_id.add_argument("--inbound-id", type=int, required=True)
    assess = sub.add_parser("evaluate")
    for name in ("config", "runtime", "inbounds", "template", "nginx", "subscriptions", "probes", "output"):
        assess.add_argument("--" + name, required=True)
    plan = sub.add_parser("plan")
    plan.add_argument("--config", required=True); plan.add_argument("--runtime", required=True); plan.add_argument("--json", action="store_true")
    status = sub.add_parser("status")
    status.add_argument("--config", required=True); status.add_argument("--runtime", required=True); status.add_argument("--json", action="store_true")
    sync = sub.add_parser("sync")
    sync.add_argument("--config", required=True); sync.add_argument("--runtime", required=True); sync.add_argument("--output", required=True)
    args = parser.parse_args()
    if args.command == "mutate":
        changed, affected = mutate(load(args.config), args.operation, args.target, args.force)
        atomic(args.output, changed)
        if args.affected:
            atomic(args.affected, affected)
    elif args.command == "artifacts":
        write_artifacts(load(args.config), args.route_id, args.inbound, args.outbound)
    elif args.command == "inbound-artifact":
        config = load(args.config)
        atomic(args.output, inbound_payload(config, route(config, args.route_id)))
    elif args.command == "set-inbound-id":
        atomic(args.output, set_inbound_id(load(args.config), args.route_id, args.inbound_id))
    elif args.command == "evaluate":
        runtime = evaluate(load(args.config), load(args.runtime, {}), load(args.inbounds, {}), load(args.template, {}), Path(args.nginx).read_text(encoding="utf-8") if Path(args.nginx).exists() else "", args.subscriptions, load(args.probes, {}))
        atomic(args.output, runtime)
    elif args.command == "plan":
        actions = drift_plan(load(args.config), load(args.runtime, {}))
        print(json.dumps(actions, indent=2) if args.json else ("No managed drift detected" if not actions else "\n".join(f"{item['route_id']}\t{item['component']}\t{item['action']}" for item in actions)))
    elif args.command == "status":
        print(render_status(load(args.config), load(args.runtime, {}), args.json))
    else:
        atomic(args.output, sync_runtime(load(args.config), load(args.runtime, {})))


if __name__ == "__main__":
    main()
