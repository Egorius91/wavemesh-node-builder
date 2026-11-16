#!/usr/bin/env python3
"""Prepared Exit paths. No rights, credentials lifecycle or provider operations.

The existing Auto Route transaction owns installation and runtime.json owns the
local health history. This module only compiles current, explicitly bound inputs.
"""
import argparse
import copy
import hashlib
import ipaddress
import json
import re
import sys
import subprocess
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import runtime_state
import xray_template

ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,47}$")
HASH = re.compile(r"^[a-f0-9]{64}$")
VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
CAPABILITY = "prepared_eu_paths_v1"


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def shape(value, keys, label):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ValueError(f"unsupported {label} shape")


def channel_descriptor(engine):
    shape(engine, ("name", "version", "sha256"), "engine")
    if engine["name"] != "xray" or not isinstance(engine["version"], str) or not VERSION.fullmatch(engine["version"]):
        raise ValueError("unsupported channel engine or unpinned version")
    if not isinstance(engine["sha256"], str) or not HASH.fullmatch(engine["sha256"]):
        raise ValueError("channel engine hash is required")
    return {"schema_version": 1, "protocol": "vless", "transport": {"name": "xhttp", "mode": "stream-one"}, "security": "tls", "engine": copy.deepcopy(engine)}


def validate_descriptor(value):
    shape(value, ("schema_version", "protocol", "transport", "security", "engine"), "channel descriptor")
    if type(value["schema_version"]) is not int or value != channel_descriptor(value["engine"]):
        raise ValueError("unsupported protocol/transport/security/engine combination")
    return copy.deepcopy(value)


def bound_plan(config, route_id, plan):
    shape(plan, ("schema_version", "node_id", "route_id", "channel", "exits", "paths", "health"), "prepared plan")
    if type(plan["schema_version"]) is not int or plan["schema_version"] != 1:
        raise ValueError("unsupported prepared plan version")
    if config.get("node", {}).get("role") != "entry" or plan["node_id"] != config["node"]["id"] or plan["route_id"] != route_id:
        raise ValueError("prepared plan does not bind this Entry/route")
    channel = validate_descriptor(plan["channel"])
    route = next((r for r in config.get("routes", []) if r.get("id") == route_id and r.get("kind") == "auto"), None)
    if not route or route.get("enabled") is not True or not route.get("entry", {}).get("inbound_id"):
        raise ValueError("prepared paths require an enabled materialized Auto Route")
    if not isinstance(plan["exits"], list) or len(plan["exits"]) != 2:
        raise ValueError("prepared plan requires the two approved existing Exits")
    exits = {}
    providers = set()
    domains = set()
    for approval in plan["exits"]:
        shape(approval, ("id", "provider", "failure_domain", "manifest_checksum", "channel"), "Exit approval")
        if approval["provider"] not in ("upcloud", "infomaniak") or approval["provider"] in providers:
            raise ValueError("only existing UpCloud and Infomaniak Exits are approved")
        if not isinstance(approval["failure_domain"], str) or not ID.fullmatch(approval["failure_domain"]) or approval["failure_domain"] in domains:
            raise ValueError("Exit failure domains must be explicit and distinct")
        if validate_descriptor(approval["channel"]) != channel:
            raise ValueError("Exit capability does not match the pinned channel")
        current = [e for e in config.get("exits", []) if e.get("id") == approval["id"] and e.get("enabled") is True]
        if len(current) != 1 or not HASH.fullmatch(str(approval["manifest_checksum"])) or current[0].get("manifest_checksum") != approval["manifest_checksum"]:
            raise ValueError("Exit approval is not bound to the current imported manifest")
        if approval["id"] in exits:
            raise ValueError("duplicate Exit identity")
        # Geography/capability statements here bind an operator's input; they
        # are not live qualification. In particular CH is a permitted country.
        if current[0].get("country") not in {"AT", "BE", "CH", "CZ", "DE", "DK", "EE", "ES", "FI", "FR", "GB", "IE", "IS", "IT", "LT", "LU", "LV", "NL", "NO", "PL", "PT", "SE"}:
            raise ValueError("Exit geography is outside the permitted European set")
        if approval["provider"] == "infomaniak" and current[0].get("country") != "CH":
            raise ValueError("Infomaniak approval requires the existing CH Exit")
        providers.add(approval["provider"])
        domains.add(approval["failure_domain"])
        exits[approval["id"]] = current[0]
    if not isinstance(plan["paths"], list) or not 2 <= len(plan["paths"]) <= 4:
        raise ValueError("prepared plan requires two to four underlay paths")
    ids, families = set(), set()
    outbounds = []
    for path in plan["paths"]:
        shape(path, ("id", "exit_id", "address_family", "address"), "underlay path")
        if not isinstance(path["id"], str) or not ID.fullmatch(path["id"]) or path["id"] in ids or path["exit_id"] not in exits:
            raise ValueError("invalid or duplicate prepared path identity")
        if type(path["address_family"]) is not int or path["address_family"] not in (4, 6):
            raise ValueError("unsupported underlay address family")
        address = ipaddress.ip_address(path["address"])
        if address.version != path["address_family"] or not address.is_global:
            raise ValueError("underlay must bind a global literal of the declared family")
        key = (path["exit_id"], address.version)
        if key in families or str(address) not in exits[path["exit_id"]]["endpoint"].get("expected_public_ips", []):
            raise ValueError("underlay address is duplicated or absent from the imported Exit identity")
        target = exits[path["exit_id"]]
        outbound = runtime_state.outbound_payload(target, {"routing": {"outbound_tag": f"wm-exit-path-{path['id']}"}})
        outbound["settings"]["vnext"][0]["address"] = str(address)
        outbounds.append(outbound)
        ids.add(path["id"])
        families.add(key)
    if any((exit_id, 4) not in families for exit_id in exits):
        raise ValueError("IPv6 supplements, and cannot replace, each Exit's IPv4 path")
    shape(plan["health"], ("failure_samples", "recovery_samples", "sample_interval_seconds", "max_age_seconds", "cooldown_seconds"), "health policy")
    for key, value in plan["health"].items():
        minimum = 0 if key == "cooldown_seconds" else 1
        if type(value) is not int or not minimum <= value <= 86400:
            raise ValueError("health policy requires bounded explicit integer values")
    if plan["health"]["max_age_seconds"] < plan["health"]["sample_interval_seconds"]:
        raise ValueError("health freshness is shorter than the sampling interval")
    binding = digest({"plan": plan, "route": route, "exits": exits})
    return route, outbounds, binding


def select_paths(plan, binding, previous, observations, now):
    """One distinct fresh complete-path sample per interval; unknown means block.

    Hysteresis delays recovery and ordinary failover. An all-paths-down sample
    blocks immediately; cooldown can never keep a forwarding path alive there.
    """
    shape(observations, ("schema_version", "binding", "engine", "at", "paths"), "path observations")
    if type(observations["schema_version"]) is not int or observations["schema_version"] != 1 or observations["binding"] != binding or observations["engine"] != plan["channel"]["engine"]:
        raise ValueError("stale path observation binding")
    at = observations["at"]
    if type(at) is not int or type(now) is not int or not 0 <= now-at <= plan["health"]["max_age_seconds"]:
        raise ValueError("path observations expired or from the future")
    expected = {p["id"] for p in plan["paths"]}
    if not isinstance(observations["paths"], dict) or set(observations["paths"]) != expected or any(type(v) is not bool for v in observations["paths"].values()):
        raise ValueError("complete boolean VPN-path observations are required")
    previous = previous if previous.get("binding") == binding else {}
    last = previous.get("sample_at")
    if last is not None and at-last < plan["health"]["sample_interval_seconds"]:
        raise ValueError("replayed or too frequent health sample")
    consecutive = last is not None and at-last <= plan["health"]["max_age_seconds"]
    counters = {}
    for path_id, alive in observations["paths"].items():
        old = previous.get("paths", {}).get(path_id, {}) if consecutive else {}
        status, successes, failures = runtime_state.transition(old, alive, failure_threshold=plan["health"]["failure_samples"], recovery_threshold=plan["health"]["recovery_samples"])
        counters[path_id] = {"status": status, "consecutive_successes": successes, "consecutive_failures": failures}
    current = previous.get("selected") if consecutive else None
    available = [p["id"] for p in plan["paths"] if observations["paths"][p["id"]] and counters[p["id"]]["status"] == "healthy"]
    selected = current
    if not any(observations["paths"].values()):
        selected = None
    elif current not in expected or (not observations["paths"][current] and counters[current]["status"] == "unhealthy"):
        selected = available[0] if available else None
    elif observations["paths"][current] and current not in available:
        # Retain an established good path; recovery hysteresis concerns entry
        # into service, not each successful sample after a transient failure.
        selected = current
    switched_at = previous.get("switched_at")
    if selected != current:
        if selected is not None and type(switched_at) is int and at-switched_at < plan["health"]["cooldown_seconds"]:
            selected = None
        if selected != current and selected is not None:
            switched_at = at
    return {"binding": binding, "sample_at": at, "switched_at": switched_at, "selected": selected, "paths": counters}


def compile_template(config, route_id, plan, template, state=None, now=None):
    route, outbounds, binding = bound_plan(config, route_id, plan)
    result = copy.deepcopy(template)
    xray_template.ensure_block_outbound(result)
    owned = {item["tag"] for item in outbounds}
    # Prefix collisions and conflicting existing path bytes are refused rather
    # than silently taking ownership of an unrelated outbound.
    for existing in result.get("outbounds", []):
        if existing.get("tag") in owned and existing not in outbounds:
            raise ValueError("prepared outbound conflicts with existing state")
    result["outbounds"] = [o for o in result.get("outbounds", []) if o.get("tag") not in owned] + outbounds
    selectors = [o["tag"] for o in outbounds]
    args = (route["entry"]["inbound_tag"], route["routing"]["balancer_tag"], route["routing"]["rule_tag"])
    result = xray_template.merge_balancer(result, selectors, *args)
    # A direct rule selects only an admitted prepared path, and observes ALL
    # backups. Block is the initial state and is independent of probe timing.
    now = int(time.time()) if now is None else now
    selected = state.get("selected") if state and state.get("binding") == binding and type(state.get("sample_at")) is int and 0 <= now-state["sample_at"] <= plan["health"]["max_age_seconds"] else None
    if selected not in {p["id"] for p in plan["paths"]}:
        selected = None
    rule = next(r for r in result["routing"]["rules"] if r.get("ruleTag") == args[2])
    rule.pop("balancerTag")
    rule["outboundTag"] = f"wm-exit-path-{selected}" if selected else xray_template.BLOCK_TAG
    return result, binding


def public_descriptor(plan):
    # Deliberate allow-list: no Exit address, ID, path, panel or relay material.
    return {"capability": CAPABILITY, "channel": validate_descriptor(plan["channel"]), "address_families": sorted({p["address_family"] for p in plan["paths"]}), "all_paths_down": "block"}


def check_engine(plan, binary):
    """Installed bytes, reported version, then native parser are separate gates."""
    expected = validate_descriptor(plan["channel"])["engine"]
    path = Path(binary)
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected["sha256"]:
        raise ValueError("installed Xray hash differs from the prepared plan")
    result = subprocess.run([str(path), "version"], capture_output=True, text=True, timeout=10, check=True)
    match = re.match(r"Xray ([0-9]+\.[0-9]+\.[0-9]+)(?:\s|$)", result.stdout)
    if not match or match[1] != expected["version"]:
        raise ValueError("installed Xray version differs from the prepared plan")


def stage(config, runtime, route_id, plan, template, observations=None, now=None):
    candidate = copy.deepcopy(config)
    route, _, _ = bound_plan(candidate, route_id, plan)
    if route.get("presentation", {}).get("published"):
        raise ValueError("prepared route publication is not yet integrated with SaaS admission; unpublish before installation")
    route["prepared_plan"] = copy.deepcopy(plan)
    # The binding includes current private configuration and route metadata.
    # The stored plan is identical on install/reconcile, with no revision churn.
    _, _, binding = bound_plan(candidate, route_id, plan)
    observed = copy.deepcopy(runtime or {})
    previous = observed.setdefault("prepared_routes", {}).get(route_id, {})
    state = select_paths(plan, binding, previous, observations, now) if observations is not None else {"binding": binding, "selected": None}
    observed["prepared_routes"][route_id] = state
    result, _ = compile_template(candidate, route_id, plan, template, state, now)
    return candidate, observed, result, binding


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("install", "reconcile", "probe-input", "verify"))
    parser.add_argument("--config", required=True)
    parser.add_argument("--route-id", required=True)
    parser.add_argument("--plan")
    parser.add_argument("--template", required=True)
    parser.add_argument("--runtime")
    parser.add_argument("--observations")
    parser.add_argument("--engine-binary")
    parser.add_argument("--output", required=True, help="Private transaction directory")
    args = parser.parse_args()
    load = runtime_state.load
    config = load(args.config)
    route = next(r for r in config["routes"] if r["id"] == args.route_id)
    plan = load(args.plan) if args.command == "install" else route.get("prepared_plan")
    if not plan:
        raise ValueError("prepared plan is required")
    directory = Path(args.output)
    if args.command == "verify":
        if not args.engine_binary:
            raise ValueError("installed engine readback check is required")
        check_engine(plan, args.engine_binary)
        desired = load(directory/"prepared.xray.json")
        if desired != load(args.template):
            raise ValueError("prepared route readback differs from the complete candidate")
        xray_template.validate_eu_only(desired)
        return
    if not args.engine_binary:
        raise ValueError("installed engine check is required")
    check_engine(plan, args.engine_binary)
    if args.command == "probe-input":
        _, outbounds, binding = bound_plan(config, args.route_id, plan)
        for path, outbound in zip(plan["paths"], outbounds):
            runtime_state.atomic(directory/f"probe-{path['id']}.json", outbound)
        runtime_state.atomic(directory/"probe-input.json", {"schema_version": 1, "binding": binding, "engine": plan["channel"]["engine"], "at": int(time.time()), "paths": {p["id"]: False for p in plan["paths"]}})
        return
    now = int(time.time())
    observations = load(args.observations) if args.observations else None
    if args.command == "reconcile" and observations is None:
        raise ValueError("reconcile requires complete fresh path observations")
    candidate, runtime, template, binding = stage(config, load(args.runtime, {}), args.route_id, plan, load(args.template), observations, now)
    runtime_state.atomic(directory/"prepared.config.json", candidate)
    runtime_state.atomic(directory/"prepared.runtime.json", runtime)
    runtime_state.atomic(directory/"prepared.xray.json", template)
    runtime_state.atomic(directory/"prepared.descriptor.json", public_descriptor(plan))
    runtime_state.atomic(directory/"prepared.binding.json", {"binding": binding, "capability": CAPABILITY})
    # Parse the exact candidate with the same installed engine before mutation.
    subprocess.run([args.engine_binary, "run", "-test", "-config", str(directory/"prepared.xray.json")], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15, check=True)


if __name__ == "__main__":
    main()
