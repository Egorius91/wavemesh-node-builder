#!/usr/bin/env python3
"""Deterministic merge/remove operations for WaveMesh-managed Xray objects."""

import argparse
import json
from pathlib import Path

MATCH_KEYS = {"domain", "ip", "port", "sourcePort", "localPort", "network", "source", "sourceIP", "user", "inboundTag", "protocol", "attrs"}


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def is_catch_all(rule):
    return not any(key in rule and rule[key] not in (None, "", []) for key in MATCH_KEYS)


def ensure_unique(items, key, label):
    values = [item.get(key) for item in items if item.get(key)]
    duplicates = sorted({value for value in values if values.count(value) > 1})
    if duplicates:
        raise ValueError(f"duplicate {label}: {', '.join(duplicates)}")


def merge(template, outbound, inbound_tag, outbound_tag, rule_tag):
    if not outbound_tag.startswith("wm-exit-") or outbound.get("tag") != outbound_tag:
        raise ValueError("managed outbound tag mismatch")
    if not inbound_tag.startswith("wm-route-") or not rule_tag.startswith("wm-rule-"):
        raise ValueError("managed route tags must use wm- prefixes")

    result = json.loads(json.dumps(template))
    outbounds = result.setdefault("outbounds", [])
    outbounds[:] = [item for item in outbounds if item.get("tag") != outbound_tag]
    outbounds.append(outbound)

    routing = result.setdefault("routing", {})
    rules = routing.setdefault("rules", [])
    rules[:] = [item for item in rules if item.get("ruleTag") != rule_tag and not (item.get("inboundTag") == [inbound_tag] and item.get("outboundTag") == outbound_tag)]
    rule = {"type": "field", "inboundTag": [inbound_tag], "outboundTag": outbound_tag, "ruleTag": rule_tag}
    catch_index = next((index for index, item in enumerate(rules) if is_catch_all(item)), len(rules))
    rules.insert(catch_index, rule)

    ensure_unique(outbounds, "tag", "outbound tags")
    ensure_unique(rules, "ruleTag", "routing rule tags")
    return result


def remove(template, inbound_tag, outbound_tag, rule_tag):
    result = json.loads(json.dumps(template))
    result["outbounds"] = [item for item in result.get("outbounds", []) if item.get("tag") != outbound_tag]
    routing = result.setdefault("routing", {})
    routing["rules"] = [item for item in routing.get("rules", []) if item.get("ruleTag") != rule_tag and not (item.get("inboundTag") == [inbound_tag] and item.get("outboundTag") == outbound_tag)]
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
    args = parser.parse_args()
    template = read_json(args.template)
    if args.command == "merge":
        result = merge(template, read_json(args.outbound), args.inbound_tag, args.outbound_tag, args.rule_tag)
    else:
        result = remove(template, args.inbound_tag, args.outbound_tag, args.rule_tag)
    Path(args.output).write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
