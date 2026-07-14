#!/usr/bin/env python3
"""Evaluate WaveMesh Auto Route health from desired and observed state."""

import argparse
import json
from pathlib import Path


def load(path, default=None):
    target = Path(path)
    if not target.exists() or target.stat().st_size == 0:
        return default
    return json.loads(target.read_text(encoding="utf-8"))


def as_object(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return {}
    return value if isinstance(value, dict) else {}


def parse_inbounds(response):
    values = response.get("obj", []) if isinstance(response, dict) else []
    return values if isinstance(values, list) else []


def evaluate(config, runtime, inbounds_response, template, overrides=None):
    overrides = overrides or {}
    inbounds = parse_inbounds(inbounds_response)
    by_tag = {item.get("tag") or item.get("remark"): item for item in inbounds}
    outbounds = {item.get("tag") for item in template.get("outbounds", [])}
    routing = template.get("routing", {})
    rules = routing.get("rules", [])
    balancers = {item.get("tag"): item for item in routing.get("balancers", [])}
    observatory = template.get("observatory", {})
    manual_status = {
        item.get("routing", {}).get("outbound_tag"): runtime.get("routes", {}).get(item.get("id"), {}).get("status", "unknown")
        for item in config.get("routes", []) if item.get("kind") == "cascade"
    }
    configured_balancers = {item.get("id"): item for item in config.get("balancers", [])}
    rows = []
    for route in config.get("routes", []):
        if route.get("kind") != "auto":
            continue
        balancer = configured_balancers.get(route.get("balancer_id"), {})
        enabled = bool(route.get("enabled", True) and balancer.get("enabled", True))
        entry = route.get("entry", {})
        routing_cfg = route.get("routing", {})
        expected_selectors = balancer.get("selector", [])
        override_exit = overrides.get(route.get("balancer_id"))
        effective_selectors = expected_selectors
        if override_exit:
            exit_obj = next((item for item in config.get("exits", []) if item.get("id") == override_exit), None)
            effective_selectors = [exit_obj.get("xray", {}).get("outbound_tag")] if exit_obj else []
        inbound = by_tag.get(entry.get("inbound_tag"))
        inbound_present = inbound is not None
        inbound_enabled = bool(inbound.get("enable", True)) if inbound else False
        actual_balancer = balancers.get(routing_cfg.get("balancer_tag"))
        balancer_present = actual_balancer is not None
        selector_matches = balancer_present and actual_balancer.get("selector", []) == effective_selectors
        strategy_matches = balancer_present and as_object(actual_balancer.get("strategy")).get("type") == balancer.get("strategy")
        rule_present = any(
            rule.get("ruleTag") == routing_cfg.get("rule_tag")
            and rule.get("balancerTag") == routing_cfg.get("balancer_tag")
            and entry.get("inbound_tag") in ([rule.get("inboundTag")] if isinstance(rule.get("inboundTag"), str) else rule.get("inboundTag", []))
            for rule in rules
        )
        observatory_present = set(observatory.get("subjectSelector", [])) >= set(effective_selectors)
        selectors_present = bool(effective_selectors) and all(tag in outbounds for tag in effective_selectors)
        statuses = [manual_status.get(tag, "unknown") for tag in effective_selectors]
        healthy_count = sum(status == "healthy" for status in statuses)
        known_count = sum(status in ("healthy", "unhealthy", "misconfigured", "disabled") for status in statuses)
        structural = inbound_present and inbound_enabled == enabled and balancer_present and selector_matches and strategy_matches and rule_present and observatory_present and selectors_present
        if not enabled:
            status = "disabled"
        elif not structural:
            status = "misconfigured"
        elif healthy_count == len(effective_selectors):
            status = "healthy"
        elif healthy_count > 0:
            status = "degraded"
        elif known_count == len(effective_selectors):
            status = "unhealthy"
        else:
            status = "unknown"
        rows.append({
            "id": route.get("id"),
            "display_name": route.get("display_name"),
            "status": status,
            "enabled": enabled,
            "published": route.get("presentation", {}).get("published", False),
            "strategy": balancer.get("strategy"),
            "selectors": effective_selectors,
            "override_exit_id": override_exit,
            "inbound": "present" if inbound_present else "missing",
            "inbound_enabled": inbound_enabled,
            "balancer": "present" if balancer_present else "missing",
            "routing_rule": "present" if rule_present else "missing",
            "observatory": "present" if observatory_present else "missing",
            "healthy_exits": healthy_count,
            "total_exits": len(effective_selectors),
        })
    return rows


def render(rows, as_json=False):
    if as_json:
        return json.dumps({"auto_routes": rows}, indent=2, ensure_ascii=False)
    if not rows:
        return "No Auto Routes"
    lines = ["AUTO ROUTE\tSTATUS\tHEALTHY EXITS\tOVERRIDE\tPUBLISHED"]
    for row in rows:
        lines.append(f"{row['display_name']}\t{row['status']}\t{row['healthy_exits']}/{row['total_exits']}\t{row['override_exit_id'] or '-'}\t{str(row['published']).lower()}")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--runtime", required=True)
    parser.add_argument("--inbounds", required=True)
    parser.add_argument("--template", required=True)
    parser.add_argument("--overrides", required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    rows = evaluate(load(args.config, {}), load(args.runtime, {}), load(args.inbounds, {}), load(args.template, {}), load(args.overrides, {}))
    print(render(rows, args.json))


if __name__ == "__main__":
    main()
