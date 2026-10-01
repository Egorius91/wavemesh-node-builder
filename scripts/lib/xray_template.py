#!/usr/bin/env python3
"""Deterministic merge/remove operations for WaveMesh-managed Xray objects."""

import argparse
import json
from pathlib import Path

MATCH_KEYS = {"domain", "ip", "port", "sourcePort", "localPort", "network", "source", "sourceIP", "user", "inboundTag", "protocol", "attrs"}
XRAY_API_PORT = 62789
AUTO_PROBE_URL = "https://www.google.com/generate_204"
AUTO_PROBE_INTERVAL = "30s"
PRIVATE_ERROR_LOG = "/var/log/wavemesh-node/xray-error.log"
LOG_LEVELS = {"debug", "info", "warning", "error", "none"}


def validate_log_shape(template, allow_missing_policy_fields=False):
    """Reject unsupported Xray log shapes without ever rendering their values."""
    log = template.get("log")
    allowed = {"access", "error", "loglevel", "dnsLog", "maskAddress"}
    required = {"access", "error", "loglevel"}
    if not isinstance(log, dict) or not required.issubset(log) or not set(log).issubset(allowed):
        raise ValueError("Xray log policy has an unsupported shape")
    if not allow_missing_policy_fields and set(log) != allowed:
        raise ValueError("Xray log policy is incomplete")
    if not isinstance(log["access"], str) or not log["access"].strip():
        raise ValueError("Xray access log stream is omitted or empty")
    if not isinstance(log["error"], str) or not log["error"].strip():
        raise ValueError("Xray error log stream is omitted or empty")
    if not isinstance(log["loglevel"], str) or log["loglevel"] not in LOG_LEVELS:
        raise ValueError("Xray log level is unknown")
    if "dnsLog" in template or "maskAddress" in template:
        raise ValueError("Xray privacy log settings must be nested in log")
    if "dnsLog" in log and not isinstance(log["dnsLog"], bool):
        raise ValueError("Xray DNS log setting has an unsupported shape")
    if "maskAddress" in log and not isinstance(log["maskAddress"], str):
        raise ValueError("Xray address masking setting has an unsupported shape")


def normalize_log_policy(template):
    """Return a copy with the selected private log policy applied."""
    log = template.get("log")
    allowed = {"access", "error", "loglevel", "dnsLog", "maskAddress"}
    if not isinstance(log, dict) or "loglevel" not in log or not set(log).issubset(allowed):
        raise ValueError("Xray log policy has an unsupported migration shape")
    if "dnsLog" in template or "maskAddress" in template:
        raise ValueError("Xray privacy log settings must be nested in log")
    if not isinstance(log["loglevel"], str) or log["loglevel"] not in LOG_LEVELS:
        raise ValueError("Xray log level is unknown")
    for stream in ("access", "error"):
        if stream in log and not isinstance(log[stream], str):
            raise ValueError("Xray log stream has an unsupported shape")
    if "dnsLog" in log and not isinstance(log["dnsLog"], bool):
        raise ValueError("Xray DNS log setting has an unsupported shape")
    if "maskAddress" in log and not isinstance(log["maskAddress"], str):
        raise ValueError("Xray address masking setting has an unsupported shape")
    result = json.loads(json.dumps(template))
    result["log"].update({
        "access": "none",
        "error": PRIVATE_ERROR_LOG,
        "loglevel": "error" if template["log"]["loglevel"] == "error" else "warning",
        "dnsLog": False,
        "maskAddress": "full",
    })
    return result


def validate_log_policy(template):
    validate_log_shape(template)
    if template["log"]["access"] != "none":
        raise ValueError("Xray access logging must be disabled")
    if template["log"]["error"] != PRIVATE_ERROR_LOG:
        raise ValueError("Xray error log sink does not match the private policy")
    if template["log"]["loglevel"] not in {"warning", "error"}:
        raise ValueError("Xray log level is less restrictive than warning")
    if template["log"].get("dnsLog") is not False:
        raise ValueError("Xray DNS logging must be disabled")
    if template["log"].get("maskAddress") != "full":
        raise ValueError("Xray addresses must be fully masked")


def copy_policy_safe_template(template):
    validate_log_policy(template)
    return json.loads(json.dumps(template))


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def is_catch_all(rule):
    return not any(key in rule and rule[key] not in (None, "", []) for key in MATCH_KEYS)


def ensure_unique(items, key, label):
    values = [item.get(key) for item in items if item.get(key)]
    duplicates = sorted({value for value in values if values.count(value) > 1})
    if duplicates:
        raise ValueError(f"duplicate {label}: {', '.join(duplicates)}")


def ensure_xray_api(template):
    api = template.setdefault("api", {})
    api["tag"] = "api"
    services = api.setdefault("services", [])
    for service in ("HandlerService", "StatsService", "RoutingService"):
        if service not in services:
            services.append(service)

    inbounds = template.setdefault("inbounds", [])
    api_inbounds = [item for item in inbounds if item.get("tag") == "api"]
    if len(api_inbounds) > 1:
        raise ValueError("multiple Xray API inbounds")
    if not api_inbounds:
        if any(int(item.get("port", -1)) == XRAY_API_PORT for item in inbounds):
            raise ValueError(f"Xray API port {XRAY_API_PORT} is occupied")
        inbounds.insert(0, {"listen": "127.0.0.1", "port": XRAY_API_PORT, "protocol": "tunnel", "settings": {"rewriteAddress": "127.0.0.1"}, "tag": "api"})
    else:
        inbound = api_inbounds[0]
        if inbound.get("listen") != "127.0.0.1" or int(inbound.get("port", 0)) <= 0:
            raise ValueError("existing Xray API inbound is not loopback with a valid port")

    routing = template.setdefault("routing", {})
    rules = routing.setdefault("rules", [])
    has_api_rule = any(item.get("inboundTag") in (["api"], "api") and item.get("outboundTag") == "api" for item in rules)
    if not has_api_rule:
        rules.insert(0, {"type": "field", "inboundTag": ["api"], "outboundTag": "api", "ruleTag": "wm-api-rule"})
    template.setdefault("stats", {})
    return template


def insert_before_catch_all(rules, rule):
    catch_index = next((index for index, item in enumerate(rules) if is_catch_all(item)), len(rules))
    rules.insert(catch_index, rule)


def merge(template, outbound, inbound_tag, outbound_tag, rule_tag):
    if not outbound_tag.startswith("wm-exit-") or outbound.get("tag") != outbound_tag:
        raise ValueError("managed outbound tag mismatch")
    if not inbound_tag.startswith("wm-route-") or not rule_tag.startswith("wm-rule-"):
        raise ValueError("managed route tags must use wm- prefixes")

    result = copy_policy_safe_template(template)
    ensure_xray_api(result)
    outbounds = result.setdefault("outbounds", [])
    outbounds[:] = [item for item in outbounds if item.get("tag") != outbound_tag]
    outbounds.append(outbound)

    routing = result.setdefault("routing", {})
    rules = routing.setdefault("rules", [])
    rules[:] = [item for item in rules if item.get("ruleTag") != rule_tag and not (item.get("inboundTag") == [inbound_tag] and item.get("outboundTag") == outbound_tag)]
    insert_before_catch_all(rules, {"type": "field", "inboundTag": [inbound_tag], "outboundTag": outbound_tag, "ruleTag": rule_tag})

    ensure_unique(outbounds, "tag", "outbound tags")
    ensure_unique(rules, "ruleTag", "routing rule tags")
    return result


def merge_balancer(template, selectors, inbound_tag, balancer_tag, rule_tag, strategy="leastPing"):
    selectors = list(dict.fromkeys(selectors))
    if not selectors:
        raise ValueError("Auto Route selector must contain at least one Exit outbound")
    if any(not tag.startswith("wm-exit-") for tag in selectors):
        raise ValueError("Auto Route selectors must use wm-exit- outbound tags")
    if not inbound_tag.startswith("wm-route-auto-"):
        raise ValueError("Auto Route inbound tag must use wm-route-auto- prefix")
    if not balancer_tag.startswith("wm-balancer-") or not rule_tag.startswith("wm-rule-auto-"):
        raise ValueError("Auto Route balancer and rule tags must use managed prefixes")
    if strategy != "leastPing":
        raise ValueError("only leastPing is supported for Auto Route")

    result = copy_policy_safe_template(template)
    ensure_xray_api(result)
    available = {item.get("tag") for item in result.get("outbounds", [])}
    missing = [tag for tag in selectors if tag not in available]
    if missing:
        raise ValueError(f"Auto Route selector references missing outbounds: {', '.join(missing)}")

    routing = result.setdefault("routing", {})
    rules = routing.setdefault("rules", [])
    balancers = routing.setdefault("balancers", [])
    rules[:] = [item for item in rules if item.get("ruleTag") != rule_tag and item.get("inboundTag") != [inbound_tag]]
    balancers[:] = [item for item in balancers if item.get("tag") != balancer_tag]
    balancers.append({"tag": balancer_tag, "selector": selectors, "strategy": {"type": strategy}})
    insert_before_catch_all(rules, {"type": "field", "inboundTag": [inbound_tag], "balancerTag": balancer_tag, "ruleTag": rule_tag})

    observatory = result.setdefault("observatory", {})
    observatory.update({
        "subjectSelector": selectors,
        "probeURL": AUTO_PROBE_URL,
        "probeInterval": AUTO_PROBE_INTERVAL,
        "enableConcurrency": True,
    })

    ensure_unique(balancers, "tag", "balancer tags")
    ensure_unique(rules, "ruleTag", "routing rule tags")
    return result


def verify_balancer(template, selectors, inbound_tag, balancer_tag, rule_tag, strategy="leastPing"):
    validate_log_policy(template)
    selectors = list(dict.fromkeys(selectors))
    routing = template.get("routing", {})
    balancers = [item for item in routing.get("balancers", []) if item.get("tag") == balancer_tag]
    if len(balancers) != 1:
        raise ValueError("managed Auto Route balancer missing or duplicated")
    balancer = balancers[0]
    if balancer.get("selector") != selectors or balancer.get("strategy") != {"type": strategy}:
        raise ValueError("managed Auto Route balancer differs from desired state")
    rules = [item for item in routing.get("rules", []) if item.get("ruleTag") == rule_tag]
    if len(rules) != 1:
        raise ValueError("managed Auto Route routing rule missing or duplicated")
    rule = rules[0]
    if rule.get("inboundTag") != [inbound_tag] or rule.get("balancerTag") != balancer_tag or "outboundTag" in rule:
        raise ValueError("managed Auto Route routing rule differs from desired state")
    catch_index = next((index for index, item in enumerate(routing.get("rules", [])) if is_catch_all(item)), len(routing.get("rules", [])))
    rule_index = routing.get("rules", []).index(rule)
    if rule_index >= catch_index:
        raise ValueError("managed Auto Route routing rule is shadowed by catch-all")
    observatory = template.get("observatory", {})
    if observatory.get("subjectSelector") != selectors:
        raise ValueError("Auto Route observatory selectors differ from desired state")
    return True


def remove(template, inbound_tag, outbound_tag, rule_tag):
    result = copy_policy_safe_template(template)
    result["outbounds"] = [item for item in result.get("outbounds", []) if item.get("tag") != outbound_tag]
    routing = result.setdefault("routing", {})
    routing["rules"] = [item for item in routing.get("rules", []) if item.get("ruleTag") != rule_tag and not (item.get("inboundTag") == [inbound_tag] and item.get("outboundTag") == outbound_tag)]
    return result


def remove_balancer(template, inbound_tag, balancer_tag, rule_tag):
    result = copy_policy_safe_template(template)
    routing = result.setdefault("routing", {})
    routing["rules"] = [item for item in routing.get("rules", []) if item.get("ruleTag") != rule_tag and not (item.get("inboundTag") == [inbound_tag] and item.get("balancerTag") == balancer_tag)]
    routing["balancers"] = [item for item in routing.get("balancers", []) if item.get("tag") != balancer_tag]
    remaining_selectors = []
    for balancer in routing.get("balancers", []):
        remaining_selectors.extend(balancer.get("selector", []))
    if remaining_selectors:
        observatory = result.setdefault("observatory", {})
        observatory["subjectSelector"] = list(dict.fromkeys(remaining_selectors))
    else:
        result.pop("observatory", None)
    return result


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    merge_parser = sub.add_parser("merge")
    merge_parser.add_argument("--template", required=True)
    merge_parser.add_argument("--outbound", required=True)
    remove_parser = sub.add_parser("remove")
    remove_parser.add_argument("--template", required=True)
    for child in (merge_parser, remove_parser):
        child.add_argument("--inbound-tag", required=True)
        child.add_argument("--outbound-tag", required=True)
        child.add_argument("--rule-tag", required=True)
        child.add_argument("--output", required=True)

    merge_balancer_parser = sub.add_parser("merge-balancer")
    merge_balancer_parser.add_argument("--template", required=True)
    merge_balancer_parser.add_argument("--selectors", required=True, help="Comma-separated exact managed outbound tags")
    merge_balancer_parser.add_argument("--inbound-tag", required=True)
    merge_balancer_parser.add_argument("--balancer-tag", required=True)
    merge_balancer_parser.add_argument("--rule-tag", required=True)
    merge_balancer_parser.add_argument("--strategy", default="leastPing")
    merge_balancer_parser.add_argument("--output", required=True)

    verify_parser = sub.add_parser("verify-balancer")
    verify_parser.add_argument("--template", required=True)
    verify_parser.add_argument("--selectors", required=True)
    verify_parser.add_argument("--inbound-tag", required=True)
    verify_parser.add_argument("--balancer-tag", required=True)
    verify_parser.add_argument("--rule-tag", required=True)
    verify_parser.add_argument("--strategy", default="leastPing")

    remove_balancer_parser = sub.add_parser("remove-balancer")
    remove_balancer_parser.add_argument("--template", required=True)
    remove_balancer_parser.add_argument("--inbound-tag", required=True)
    remove_balancer_parser.add_argument("--balancer-tag", required=True)
    remove_balancer_parser.add_argument("--rule-tag", required=True)
    remove_balancer_parser.add_argument("--output", required=True)

    policy_parser = sub.add_parser("assert-log-policy")
    policy_parser.add_argument("--template", required=True)

    candidate_parser = sub.add_parser("normalize-log-policy-candidate")
    candidate_parser.add_argument("--template", required=True)
    candidate_parser.add_argument("--output", required=True)

    args = parser.parse_args()
    template = read_json(args.template)
    if args.command == "assert-log-policy":
        validate_log_policy(template)
        return
    if args.command == "normalize-log-policy-candidate":
        result = normalize_log_policy(template)
        Path(args.output).write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return
    if args.command == "merge":
        result = merge(template, read_json(args.outbound), args.inbound_tag, args.outbound_tag, args.rule_tag)
    elif args.command == "remove":
        result = remove(template, args.inbound_tag, args.outbound_tag, args.rule_tag)
    elif args.command == "merge-balancer":
        selectors = [item.strip() for item in args.selectors.split(",") if item.strip()]
        result = merge_balancer(template, selectors, args.inbound_tag, args.balancer_tag, args.rule_tag, args.strategy)
    elif args.command == "verify-balancer":
        selectors = [item.strip() for item in args.selectors.split(",") if item.strip()]
        verify_balancer(template, selectors, args.inbound_tag, args.balancer_tag, args.rule_tag, args.strategy)
        return
    else:
        result = remove_balancer(template, args.inbound_tag, args.balancer_tag, args.rule_tag)
    Path(args.output).write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
