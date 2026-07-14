#!/usr/bin/env python3
"""Redacted multi-Exit and Auto Route E2E verification for an installed Entry node."""

import argparse
import json
import sys
import urllib.parse
from pathlib import Path


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def fail(message):
    raise ValueError(message)


def enabled_manual_routes(config):
    exits = {item["id"]: item for item in config.get("exits", []) if item.get("enabled", True)}
    routes = [
        item
        for item in config.get("routes", [])
        if item.get("kind") == "cascade"
        and item.get("enabled", True)
        and item.get("exit_id") in exits
    ]
    routes.sort(key=lambda item: (item.get("sort_order", 0), item.get("display_name", ""), item["id"]))
    if len(routes) < 2 or len({item["exit_id"] for item in routes}) < 2:
        fail("at least two enabled cascade routes to distinct Exits are required")
    return exits, routes


def published_auto_routes(config, exits):
    balancers = {item.get("id"): item for item in config.get("balancers", [])}
    known_outbounds = {
        item.get("xray", {}).get("outbound_tag")
        for item in exits.values()
        if item.get("xray", {}).get("outbound_tag")
    }
    routes = []
    for route in config.get("routes", []):
        if route.get("kind") != "auto" or not route.get("enabled", True):
            continue
        if not route.get("presentation", {}).get("published", False):
            continue
        balancer = balancers.get(route.get("balancer_id"))
        if not balancer or not balancer.get("enabled", True):
            fail(f"published Auto Route has no enabled balancer: {route.get('id')}")
        if balancer.get("strategy") != "leastPing":
            fail(f"published Auto Route uses unsupported strategy: {route.get('id')}")
        selectors = list(dict.fromkeys(balancer.get("selector", [])))
        if not selectors:
            fail(f"published Auto Route has no selectors: {route.get('id')}")
        missing = [selector for selector in selectors if selector not in known_outbounds]
        if missing:
            fail(f"published Auto Route selects unknown managed outbounds: {', '.join(missing)}")
        if route.get("routing", {}).get("balancer_tag") != balancer.get("balancer_tag"):
            fail(f"published Auto Route balancer tag differs from desired state: {route.get('id')}")
        routes.append((route, balancer))
    routes.sort(key=lambda item: (item[0].get("sort_order", 0), item[0].get("display_name", ""), item[0]["id"]))
    return routes


def ordered_published_routes(manual_routes, auto_routes):
    routes = list(manual_routes) + [route for route, _ in auto_routes]
    routes.sort(key=lambda item: (item.get("sort_order", 0), item.get("display_name", ""), item["id"]))
    return routes


def expected_client_routes(client, routes):
    credentials = {
        item["route_id"]: item
        for item in client.get("credentials", [])
        if item.get("enabled", True)
    }
    return [(route, credentials[route["id"]]) for route in routes if route["id"] in credentials]


def validate_profile(profile, domain, route, credential):
    parsed = urllib.parse.urlsplit(profile)
    query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    expected = {
        "security": "tls",
        "type": "xhttp",
        "host": domain,
        "sni": domain,
        "path": route["entry"]["public_path"],
        "mode": "stream-one",
    }
    if parsed.scheme != "vless" or parsed.hostname != domain or parsed.port != 443:
        fail("profile does not terminate at the Entry domain on port 443")
    if parsed.username != credential["uuid"]:
        fail("profile credential does not match its managed route")
    if any(query.get(key, [None])[0] != value for key, value in expected.items()):
        fail("profile transport or Entry routing parameters differ from desired state")
    if urllib.parse.unquote(parsed.fragment) != (route.get("display_name") or route["id"]):
        fail("profile display name or ordering differs from desired state")


def validate_subscriptions(config, exits, routes, directory):
    domain = config.get("server", {}).get("domain", "")
    clients = [item for item in config.get("clients", []) if item.get("enabled", True)]
    if not clients:
        fail("no enabled clients are configured")
    total_profiles = 0
    for client in clients:
        subscription_id = client.get("subscription_id", "")
        path = Path(directory) / "users" / f"{subscription_id}.txt"
        if not path.is_file():
            fail("an enabled client subscription file is missing")
        profiles = [line for line in path.read_text(encoding="utf-8").splitlines() if line]
        expected = expected_client_routes(client, routes)
        if len(expected) != len(routes):
            fail("every enabled client must have one enabled credential for every published route")
        if len(profiles) != len(expected):
            fail("subscription profile count differs from enabled published client routes")
        for profile, (route, credential) in zip(profiles, expected):
            validate_profile(profile, domain, route, credential)
        content = "\n".join(profiles)
        forbidden = set()
        for item in exits.values():
            endpoint = item.get("endpoint", {})
            forbidden.update(
                str(value)
                for value in (
                    endpoint.get("domain"),
                    endpoint.get("relay_uuid"),
                    endpoint.get("relay_path"),
                    *endpoint.get("expected_public_ips", []),
                )
                if value
            )
        if any(value in content for value in forbidden):
            fail("subscription exposes an Exit endpoint or relay credential")
        total_profiles += len(profiles)
    return clients, total_profiles


def validate_runtime(runtime, routes):
    if runtime.get("node_status") != "healthy":
        fail("Entry runtime is not healthy")
    observed = runtime.get("routes", {})
    result = []
    for route in routes:
        state = observed.get(route["id"], {})
        outbound = route.get("routing", {}).get("outbound_tag")
        if state.get("status") != "healthy":
            fail("one or more cascade routes are not healthy")
        if state.get("route_test_outbound") != outbound:
            fail("routeTest did not select the expected managed outbound")
        result.append(
            {
                "route_id": route["id"],
                "display_name": route.get("display_name") or route["id"],
                "status": state["status"],
                "outbound": outbound,
            }
        )
    return result


def verify(config_path, runtime_path, subscriptions):
    config = load(config_path)
    if config.get("node", {}).get("role") != "entry":
        fail("multi-Exit E2E verification requires an Entry node")
    exits, manual_routes = enabled_manual_routes(config)
    auto_routes = published_auto_routes(config, exits)
    published_routes = ordered_published_routes(manual_routes, auto_routes)
    clients, profiles = validate_subscriptions(config, exits, published_routes, subscriptions)
    route_results = validate_runtime(load(runtime_path), manual_routes)
    auto_results = [
        {
            "route_id": route["id"],
            "display_name": route.get("display_name") or route["id"],
            "status": "published",
            "strategy": balancer.get("strategy"),
            "selectors": balancer.get("selector", []),
            "balancer_tag": balancer.get("balancer_tag"),
        }
        for route, balancer in auto_routes
    ]
    return {
        "node_status": "healthy",
        "exit_count": len({item["exit_id"] for item in manual_routes}),
        "manual_route_count": len(manual_routes),
        "auto_route_count": len(auto_routes),
        "published_route_count": len(published_routes),
        "client_count": len(clients),
        "profile_count": profiles,
        "routes": route_results,
        "auto_routes": auto_results,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--runtime", required=True)
    parser.add_argument("--subscriptions", required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        result = verify(args.config, args.runtime, args.subscriptions)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(f"E2E verification failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print("MANUAL ROUTE\tSTATUS\tOUTBOUND")
        for route in result["routes"]:
            print(f"{route['display_name']}\t{route['status']}\t{route['outbound']}")
        if result["auto_routes"]:
            print("\nAUTO ROUTE\tSTATUS\tSTRATEGY\tSELECTORS")
            for route in result["auto_routes"]:
                print(f"{route['display_name']}\t{route['status']}\t{route['strategy']}\t{len(route['selectors'])}")
        print(
            f"Verified {result['exit_count']} Exits, {result['manual_route_count']} manual routes, "
            f"{result['auto_route_count']} published Auto Routes, {result['client_count']} clients, "
            f"and {result['profile_count']} profiles"
        )


if __name__ == "__main__":
    main()
