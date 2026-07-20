#!/usr/bin/env python3
"""Pure helpers for deciding which managed routes are public in subscriptions."""

import argparse
import copy
import json
from pathlib import Path

PUBLICATION_MODES = {"all", "auto-only"}


def publication_mode(config):
    value = (
        config.get("network", {})
        .get("subscription", {})
        .get("publication_mode", "all")
    )
    if value not in PUBLICATION_MODES:
        raise ValueError(f"unsupported subscription publication mode: {value}")
    return value


def route_is_public(config, route, manual_default=True):
    """Return whether a route should be exposed to subscription clients.

    `manual_default` preserves each caller's existing treatment of non-Auto
    route kinds while making the auto-only policy consistent everywhere.
    """
    if not route.get("enabled", True):
        return False
    if route.get("kind") == "auto":
        return bool(route.get("presentation", {}).get("published", False))
    if publication_mode(config) == "auto-only":
        return False
    return bool(manual_default)


def public_route_ids(config):
    result = []
    enabled_exits = {
        item.get("id")
        for item in config.get("exits", [])
        if item.get("enabled", True)
    }
    for route in config.get("routes", []):
        kind = route.get("kind")
        eligible = (
            kind == "auto"
            or kind == "direct"
            or (kind == "cascade" and route.get("exit_id") in enabled_exits)
        )
        if eligible and route_is_public(config, route, manual_default=True):
            result.append(route.get("id"))
    return [item for item in result if item]



def as_object(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return {}
    return value if isinstance(value, dict) else {}


def inbound_for_route(route, response):
    values = response.get("obj", []) if isinstance(response, dict) else []
    inbound_id = route.get("entry", {}).get("inbound_id")
    tag = route.get("entry", {}).get("inbound_tag")
    display_name = route.get("display_name")
    for inbound in values if isinstance(values, list) else []:
        if not isinstance(inbound, dict):
            continue
        actual_id = inbound.get("id") or inbound.get("Id")
        if inbound_id is not None and str(actual_id) == str(inbound_id):
            return inbound
        actual_tag = inbound.get("tag") or inbound.get("remark")
        if tag and actual_tag == tag:
            return inbound
        if display_name and inbound.get("remark") == display_name:
            return inbound
    return None


def enabled_native_subids(inbound):
    if not inbound:
        return set()
    clients = as_object(inbound.get("settings")).get("clients", [])
    return {
        str(client.get("subId"))
        for client in clients if isinstance(client, dict)
        and client.get("enable", True)
        and client.get("subId")
    }


def native_transition_report(current, candidate, response):
    current_public = {
        route.get("id"): route
        for route in current.get("routes", [])
        if route.get("id") and route_is_public(current, route, manual_default=True)
    }
    candidate_public = {
        route.get("id"): route
        for route in candidate.get("routes", [])
        if route.get("id") and route_is_public(candidate, route, manual_default=True)
    }
    hidden_routes = [
        route for route_id, route in current_public.items()
        if route_id not in candidate_public
    ]
    auto_targets = [
        route for route in candidate_public.values()
        if route.get("kind") == "auto"
    ]
    source_subids = set()
    for route in hidden_routes:
        source_subids.update(enabled_native_subids(inbound_for_route(route, response)))
    target_reports = []
    ready = bool(auto_targets)
    for route in auto_targets:
        target_subids = enabled_native_subids(inbound_for_route(route, response))
        missing = source_subids - target_subids
        target_reports.append({
            "route_id": route.get("id"),
            "enabled_profile_count": len(target_subids),
            "missing_profile_count": len(missing),
        })
        if missing:
            ready = False
    return {
        "ready": ready,
        "source_profile_count": len(source_subids),
        "auto_target_count": len(auto_targets),
        "targets": target_reports,
    }

def set_publication_mode(config, mode):
    if mode not in PUBLICATION_MODES:
        raise ValueError(f"unsupported subscription publication mode: {mode}")
    result = copy.deepcopy(config)
    if mode == "auto-only":
        published_auto = [
            route
            for route in result.get("routes", [])
            if route.get("kind") == "auto"
            and route.get("enabled", True)
            and route.get("presentation", {}).get("published", False)
        ]
        if not published_auto:
            raise ValueError(
                "auto-only publication requires at least one enabled published Auto Route"
            )
    result.setdefault("network", {}).setdefault("subscription", {})[
        "publication_mode"
    ] = mode
    return result


def describe(config):
    public_ids = public_route_ids(config)
    all_enabled = [
        route.get("id")
        for route in config.get("routes", [])
        if route.get("enabled", True) and route.get("id")
    ]
    return {
        "mode": publication_mode(config),
        "public_route_ids": public_ids,
        "public_route_count": len(public_ids),
        "hidden_enabled_route_ids": [
            route_id for route_id in all_enabled if route_id not in set(public_ids)
        ],
    }


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, value):
    Path(path).write_text(
        json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)

    status = commands.add_parser("status")
    status.add_argument("--config", required=True)

    mutate = commands.add_parser("set")
    mutate.add_argument("--config", required=True)
    mutate.add_argument("--output", required=True)
    mutate.add_argument("--mode", choices=sorted(PUBLICATION_MODES), required=True)

    preflight = commands.add_parser("native-preflight")
    preflight.add_argument("--current", required=True)
    preflight.add_argument("--candidate", required=True)
    preflight.add_argument("--inbounds", required=True)

    args = parser.parse_args()
    if args.command == "status":
        print(json.dumps(describe(read_json(args.config)), ensure_ascii=False, sort_keys=True))
    elif args.command == "native-preflight":
        report = native_transition_report(
            read_json(args.current), read_json(args.candidate), read_json(args.inbounds)
        )
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        updated = set_publication_mode(read_json(args.config), args.mode)
        write_json(args.output, updated)
        print(json.dumps(describe(updated), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
