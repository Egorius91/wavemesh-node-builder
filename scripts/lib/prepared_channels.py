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
import uuid
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


def validate_engine_instance(value):
    """PID alone is reusable; the qualified collector supplies the boot too."""
    shape(value, ("boot_id", "pid", "start_ticks"), "engine process instance")
    boot = value["boot_id"]
    if not isinstance(boot, str) or str(uuid.UUID(boot)) != boot:
        raise ValueError("engine boot identity is not canonical")
    if type(value["pid"]) is not int or value["pid"] <= 0:
        raise ValueError("engine PID is not a positive integer")
    ticks = value["start_ticks"]
    if not isinstance(ticks, str) or not re.fullmatch(r"0|[1-9][0-9]*", ticks):
        raise ValueError("engine start_ticks are not canonical")
    return copy.deepcopy(value)


def validate_runtime_view(value):
    """An independently collected SETTLED view, never copied from saved state.

    Pending/ambiguous transitions have no qualified view. The existing Agent
    collector owns this input; a heartbeat does not create a path observation.
    """
    shape(value, ("engine_instance", "effective_config_sha256"), "current engine view")
    validate_engine_instance(value["engine_instance"])
    if not isinstance(value["effective_config_sha256"], str) or not HASH.fullmatch(value["effective_config_sha256"]):
        raise ValueError("effective engine readback hash is required")
    return copy.deepcopy(value)


def state_is_current(state, binding, runtime_view, now, max_age):
    try:
        view = validate_runtime_view(runtime_view)
        return (state.get("binding") == binding
                and state.get("engine_instance") == view["engine_instance"]
                and state.get("effective_config_sha256") == view["effective_config_sha256"]
                and type(state.get("sample_at")) is int
                and 0 <= now-state["sample_at"] <= max_age)
    except (KeyError, TypeError, ValueError, AttributeError):
        return False


def qualified_effective_config(effective_config, runtime_view):
    view = validate_runtime_view(runtime_view)
    if not isinstance(effective_config, dict) or digest(effective_config) != view["effective_config_sha256"]:
        raise ValueError("qualified executing configuration is required, not panel cache")
    return effective_config


def health_config_fingerprint(config, route_id, plan, effective_config, runtime_view):
    """Ignore exactly our selector when accumulating same-process samples.

    Each sample still binds the full current digest. Generated inbounds, rule
    order and every other byte of semantic JSON participate in continuity.
    This does not qualify a disk file or settle a pending engine mutation.
    """
    effective = qualified_effective_config(effective_config, runtime_view)
    route, outbounds, _ = bound_plan(config, route_id, plan)
    xray_template.validate_log_policy(effective)
    xray_template.validate_eu_only(effective, config)
    selectors = [item["tag"] for item in outbounds]
    xray_template.validate_exit_selectors(effective, selectors)
    for outbound in outbounds:
        if next(item for item in effective["outbounds"] if item["tag"] == outbound["tag"]) != outbound:
            raise ValueError("executing prepared path differs from its approved definition")
    rules = effective.get("routing", {}).get("rules", [])
    matches = [rule for rule in rules if rule.get("ruleTag") == route["routing"]["rule_tag"]]
    if len(matches) != 1:
        raise ValueError("executing prepared selector is missing or duplicated")
    rule = matches[0]
    if (set(rule) != {"type", "inboundTag", "ruleTag", "outboundTag"}
            or rule["type"] != "field" or rule["inboundTag"] != [route["entry"]["inbound_tag"]]
            or rule["outboundTag"] not in [xray_template.BLOCK_TAG, *selectors]):
        raise ValueError("executing prepared selector is not the owned unconditional rule")
    normalized = copy.deepcopy(effective)
    next(r for r in normalized["routing"]["rules"] if r.get("ruleTag") == rule["ruleTag"])["outboundTag"] = xray_template.BLOCK_TAG
    return digest(normalized)


def executing_intent_matches(candidate, effective, route):
    """Compare owned intent without copying generated stock state to storage.

    The full execution is still policy-validated; a preceding unrelated
    inbound-specific stock rule is allowed only by the existing EU guard.
    Array order outside our owned objects is not an editable template field.
    """
    def intent(document):
        xray_template.validate_log_policy(document)
        xray_template.validate_eu_only(document)
        routing = document.get("routing", {})
        rules = [r for r in routing.get("rules", []) if r.get("ruleTag") == route["routing"]["rule_tag"]]
        balancers = [b for b in routing.get("balancers", []) if b.get("tag") == route["routing"]["balancer_tag"]]
        if len(rules) != 1 or len(balancers) != 1:
            return None
        tags = balancers[0].get("selector", [])
        paths = [next((o for o in document.get("outbounds", []) if o.get("tag") == tag), None) for tag in tags]
        return rules, balancers, paths, document.get("observatory")
    expected = intent(candidate)
    return expected is not None and expected == intent(effective)


def template_needs_update(candidate, stored_template, runtime_view, effective_config, route):
    """A no-op tick never restarts the engine merely to refresh health.

    The collector must qualify the effective input against the executing
    instance. A panel template/cache alone does not satisfy this boundary.
    """
    effective = qualified_effective_config(effective_config, runtime_view)
    xray_template.validate_log_policy(candidate)
    xray_template.validate_eu_only(candidate)
    matches = executing_intent_matches(candidate, effective, route)
    return candidate != stored_template or not matches


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


def select_paths(plan, binding, previous, observations, now, runtime_view=None, health_config_sha256=None):
    """One distinct fresh complete-path sample per interval; unknown means block.

    Hysteresis delays recovery and ordinary failover. An all-paths-down sample
    blocks immediately; cooldown can never keep a forwarding path alive there.
    """
    view = validate_runtime_view(runtime_view)
    shape(observations, ("schema_version", "binding", "engine", "at", "paths", "engine_instance_before", "engine_instance_after", "effective_config_sha256"), "path observations")
    if type(observations["schema_version"]) is not int or observations["schema_version"] != 2 or observations["binding"] != binding or observations["engine"] != plan["channel"]["engine"]:
        raise ValueError("stale path observation binding")
    before = validate_engine_instance(observations["engine_instance_before"])
    after = validate_engine_instance(observations["engine_instance_after"])
    if before != after or after != view["engine_instance"] or observations["effective_config_sha256"] != view["effective_config_sha256"]:
        raise ValueError("engine process or effective configuration changed during/after collection")
    at = observations["at"]
    if type(at) is not int or type(now) is not int or at < 0 or now < 0 or not 0 <= now-at <= plan["health"]["max_age_seconds"]:
        raise ValueError("path observations expired or from the future")
    expected = {p["id"] for p in plan["paths"]}
    if not isinstance(observations["paths"], dict) or set(observations["paths"]) != expected or any(type(v) is not bool for v in observations["paths"].values()):
        raise ValueError("complete boolean VPN-path observations are required")
    # Only the stage owner derives the selector-normalized fingerprint from
    # qualified full input. Callers without it retain strict full-hash reset.
    continuity = health_config_sha256 or view["effective_config_sha256"]
    previous = previous if (previous.get("binding") == binding
                            and previous.get("engine_instance") == after
                            and previous.get("health_config_sha256", previous.get("effective_config_sha256")) == continuity) else {}
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
    return {"binding": binding, "sample_at": at, "switched_at": switched_at, "selected": selected, "paths": counters,
            "engine_instance": after, "effective_config_sha256": view["effective_config_sha256"], "health_config_sha256": continuity}


def compile_template(config, route_id, plan, template, state=None, now=None, runtime_view=None, effective_config=None):
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
    if runtime_view is not None:
        qualified_effective_config(effective_config, runtime_view)
    selected = state.get("selected") if state and state_is_current(state, binding, runtime_view, now, plan["health"]["max_age_seconds"]) else None
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


def stage(config, runtime, route_id, plan, template, observations=None, now=None, runtime_view=None, effective_config=None):
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
    fingerprint = health_config_fingerprint(candidate, route_id, plan, effective_config, runtime_view) if observations is not None else None
    state = select_paths(plan, binding, previous, observations, now, runtime_view, fingerprint) if observations is not None else {"binding": binding, "selected": None}
    observed["prepared_routes"][route_id] = state
    result, _ = compile_template(candidate, route_id, plan, template, state, now, runtime_view, effective_config)
    return candidate, observed, result, binding


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("install", "reconcile", "probe-input", "verify"))
    parser.add_argument("--config", required=True)
    parser.add_argument("--route-id", required=True)
    parser.add_argument("--plan")
    parser.add_argument("--template", required=True)
    parser.add_argument("--runtime")
    parser.add_argument("--runtime-view", help="Independent settled process/effective-config view from the owning collector")
    parser.add_argument("--effective-config", help="Full qualified executing JSON, separate from the stored mutation template")
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
        stored = load(args.template)
        if desired != stored:
            raise ValueError("prepared route readback differs from the complete candidate")
        xray_template.validate_eu_only(desired)
        view = validate_runtime_view(load(args.runtime_view) if args.runtime_view else None)
        effective = qualified_effective_config(load(args.effective_config) if args.effective_config else None, view)
        health_config_fingerprint(config, args.route_id, plan, effective, view)
        _, _, binding = bound_plan(config, args.route_id, plan)
        state = load(directory/"prepared.runtime.json", {}).get("prepared_routes", {}).get(args.route_id, {})
        now = int(time.time())
        if state.get("selected") not in {p["id"] for p in plan["paths"]} or not state_is_current(state, binding, view, now, plan["health"]["max_age_seconds"]):
            raise ValueError("current engine has no qualified fresh path observation")
        expected, _ = compile_template(config, args.route_id, plan, stored, state, now, view, effective)
        if not executing_intent_matches(expected, effective, route):
            raise ValueError("current path observation differs from executing route selection")
        return
    if not args.engine_binary:
        raise ValueError("installed engine check is required")
    check_engine(plan, args.engine_binary)
    if args.command == "probe-input":
        runtime_view = validate_runtime_view(load(args.runtime_view) if args.runtime_view else None)
        qualified_effective_config(load(args.effective_config) if args.effective_config else None, runtime_view)
        _, outbounds, binding = bound_plan(config, args.route_id, plan)
        for path, outbound in zip(plan["paths"], outbounds):
            runtime_state.atomic(directory/f"probe-{path['id']}.json", outbound)
        # Intentionally incomplete: only the owning collector may supply a
        # separately observed AFTER identity and completed path results.
        runtime_state.atomic(directory/"probe-input.json", {"schema_version": 2, "binding": binding, "engine": plan["channel"]["engine"], "at": int(time.time()),
                            "engine_instance_before": runtime_view["engine_instance"], "engine_instance_after": None,
                            "effective_config_sha256": runtime_view["effective_config_sha256"], "paths": {p["id"]: False for p in plan["paths"]}})
        return
    now = int(time.time())
    runtime_view = load(args.runtime_view) if args.runtime_view else None
    observations = load(args.observations) if args.observations else None
    if args.command == "reconcile" and observations is None:
        raise ValueError("reconcile requires complete fresh path observations")
    stored_template = load(args.template)
    effective = load(args.effective_config) if args.effective_config else None
    candidate, runtime, template, binding = stage(config, load(args.runtime, {}), args.route_id, plan, stored_template, observations, now, runtime_view, effective)
    runtime_state.atomic(directory/"prepared.config.json", candidate)
    runtime_state.atomic(directory/"prepared.runtime.json", runtime)
    runtime_state.atomic(directory/"prepared.xray.json", template)
    runtime_state.atomic(directory/"prepared.descriptor.json", public_descriptor(plan))
    runtime_state.atomic(directory/"prepared.binding.json", {"binding": binding, "capability": CAPABILITY})
    if runtime_view is not None:
        runtime_state.atomic(directory/"prepared.update.json", {"update_required": template_needs_update(template, stored_template, runtime_view, effective, route=route)})
    # Parse the exact candidate with the same installed engine before mutation.
    subprocess.run([args.engine_binary, "run", "-test", "-config", str(directory/"prepared.xray.json")], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15, check=True)


if __name__ == "__main__":
    main()
