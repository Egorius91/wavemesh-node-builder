#!/usr/bin/env python3
"""Pure helpers for the 3X-UI native subscription backend."""

import argparse
import base64
import copy
import ipaddress
import json
import re
from pathlib import Path
import sys
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit
import uuid
sys.path.insert(0, str(Path(__file__).resolve().parent))
from route_presentation import route_is_public
from auto_route import enabled_exit_tags


BACKENDS = {"generated", "xui-native"}
PATH_RE = re.compile(r"^/(?!.*(?:\.\.|%2[fF]|\s)).{10,126}/$")


def as_object(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return {}
    return value if isinstance(value, dict) else {}


def backend(config):
    subscription = config.get("network", {}).get("subscription", {})
    value = subscription.get("backend") or subscription.get("mode") or "generated"
    if value not in BACKENDS:
        raise ValueError(f"unsupported subscription backend: {value}")
    return value


def normalized_path(value):
    value = str(value or "")
    if not value.startswith("/"):
        value = "/" + value
    if not value.endswith("/"):
        value += "/"
    if not PATH_RE.fullmatch(value):
        raise ValueError("subscription path must be an opaque absolute path ending in /")
    return value


def set_backend(config, value, path=None):
    if value not in BACKENDS:
        raise ValueError(f"unsupported subscription backend: {value}")
    result = copy.deepcopy(config)
    subscription = result.setdefault("network", {}).setdefault("subscription", {})
    subscription["backend"] = value
    subscription["mode"] = value
    subscription["path"] = normalized_path(path or subscription.get("path"))
    if value == "xui-native":
        subscription["local_port"] = 2096
    else:
        subscription.setdefault("local_port", 2096)
    return result


def native_settings(current, config):
    result = copy.deepcopy(current)
    # Current 3X-UI uses remarkTemplate. Its default appends EMAIL, which
    # exposes WaveMesh's technical route identity in client-visible names.
    result.pop("remarkModel", None)
    path = normalized_path(config["network"]["subscription"]["path"])
    domain = config["server"]["domain"]
    result.update(
        {
            "subEnable": True,
            "subJsonEnable": False,
            "subClashEnable": False,
            "subListen": "127.0.0.1",
            "subPort": 2096,
            "subPath": path,
            "subDomain": domain,
            "subURI": f"https://{domain}{path}",
            "subShowInfo": False,
            "remarkTemplate": "{{INBOUND}}",
        }
    )
    return result


def openapi_capabilities(document):
    paths = document.get("paths", {}) if isinstance(document, dict) else {}
    return {
        "openapi": bool(paths),
        "clients_api": all(
            path in paths
            for path in (
                "/panel/api/clients/list",
                "/panel/api/clients/subLinks/{subId}",
            )
        ),
        "settings_api": all(
            path in paths
            for path in ("/panel/api/setting/all", "/panel/api/setting/update")
        ),
        "inbounds_api": "/panel/api/inbounds/list" in paths,
    }


def visible_inbounds(response):
    values = response.get("obj", []) if isinstance(response, dict) else []
    public = []
    hidden = []
    rejected = []
    for inbound in values if isinstance(values, list) else []:
        if not isinstance(inbound, dict):
            continue
        remark = str(inbound.get("remark") or "")
        stream = as_object(inbound.get("streamSettings"))
        enabled = bool(inbound.get("enable", True))
        if remark.startswith("--!"):
            hidden.append(remark)
            continue
        if enabled and inbound.get("protocol") == "vless" and stream.get("network") == "xhttp":
            public.append(remark)
        elif enabled:
            rejected.append(remark)
    return {"public": public, "hidden": hidden, "rejected": rejected}


def decode_subscription(content):
    text = content.strip()
    if "vless://" in text:
        return text
    try:
        padding = "=" * ((4 - len(text) % 4) % 4)
        return base64.urlsafe_b64decode((text + padding).encode()).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        raise ValueError("subscription response is neither raw links nor valid base64")


def validate_content(content, domain, forbidden, expected_profiles=None):
    decoded = decode_subscription(content)
    lines = [line.strip() for line in decoded.splitlines() if line.strip()]
    profiles = [line for line in lines if line.startswith("vless://")]
    if not profiles:
        raise ValueError("native subscription contains no VLESS profiles")
    if expected_profiles is not None and len(profiles) != expected_profiles:
        raise ValueError(
            f"native subscription profile count differs from Clients API: {len(profiles)} != {expected_profiles}"
        )
    lowered = decoded.lower()
    if "--!" in decoded:
        raise ValueError("native subscription exposes a hidden inbound")
    for value in forbidden:
        value = str(value or "").strip()
        if value and value.lower() in lowered:
            raise ValueError("native subscription exposes an internal endpoint")
    for line in profiles:
        try:
            profile = urlsplit(line)
            host = profile.hostname
            port = profile.port
        except ValueError:
            raise ValueError("native subscription contains an invalid profile address") from None
        if (host is None or host.lower() != domain.lower() or port != 443
                or not profile.username or profile.password is not None
                or any(ord(char) < 32 or ord(char) == 127 for char in line)):
            raise ValueError("native subscription does not publish the configured domain on port 443")
        try:
            if ipaddress.ip_address(host).is_private:
                raise ValueError("native subscription exposes a private address")
        except ValueError as exc:
            if str(exc) == "native subscription exposes a private address":
                raise
    return {"profiles": len(profiles)}


def placement_public_profiles(config, client_uuid, response):
    """Validate a complete native profile set for a managed placement.

    This is a configuration/identity projection, not proof of executing TLS,
    egress or failover. No panel mutation or upstream URL fetch belongs here.
    Preserve supported native transport values; never silently drop an unknown
    parameter or return a successful subset of the published routes.
    """
    def refuse():
        # Neither native links nor configuration/credential values enter errors.
        raise ValueError("placement public profiles are not verified")

    if config.get("node", {}).get("role") not in {"entry", "standalone"} or backend(config) != "xui-native":
        refuse()
    try:
        if str(uuid.UUID(client_uuid)) != client_uuid:
            refuse()
    except (ValueError, TypeError, AttributeError):
        refuse()
    domain = config.get("server", {}).get("domain")
    if (not isinstance(domain, str) or len(domain) > 253 or not re.fullmatch(
            r"(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
            r"[A-Za-z](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", domain)
            or domain.lower().endswith((".localhost", ".local", ".internal"))):
        refuse()
    domain = domain.lower()
    if config.get("network", {}).get("public_port", 443) != 443:
        refuse()
    exits = {e.get("id"): e for e in config.get("exits", []) if e.get("enabled", True)}
    balancers = {b.get("id"): b for b in config.get("balancers", []) if b.get("enabled", True)}
    routes = []
    for route in config.get("routes", []):
        if not route_is_public(config, route, manual_default=True):
            continue
        if route.get("prepared_plan") is not None:
            # Prepared-channel qualification is a distinct PR4 contract.
            refuse()
        if route.get("kind") == "cascade":
            if route.get("exit_id") not in exits:
                refuse()
        elif route.get("kind") == "auto":
            balancer = balancers.get(route.get("balancer_id"), {})
            selectors = balancer.get("selector")
            try:
                allowed_selectors = enabled_exit_tags(config, [])
            except ValueError:
                refuse()
            if (not isinstance(selectors, list) or not selectors or
                    any(s not in allowed_selectors for s in selectors)
                    or route.get("routing", {}).get("balancer_tag") != balancer.get("balancer_tag")):
                refuse()
        else:
            # A direct RU route must never become a fallback profile.
            refuse()
        entry = route.get("entry", {})
        path, name = entry.get("public_path"), route.get("display_name")
        if (not isinstance(path, str) or not PATH_RE.fullmatch(path) or "?" in path or "#" in path
                or "\\" in path or "%" in path or entry.get("xhttp_mode", "stream-one") != "stream-one"
                or not isinstance(name, str) or not 1 <= len(name) <= 128
                or any(ord(c) < 32 or ord(c) == 127 for c in name)):
            refuse()
        routes.append((route.get("sort_order", 0), name, str(route.get("id", "")), path))
    if not 1 <= len(routes) <= 32 or len({r[3] for r in routes}) != len(routes):
        refuse()
    value = response.get("obj") if isinstance(response, dict) and response.get("success") is True else None
    if isinstance(value, list):
        if not 1 <= len(value) <= 32 or any(not isinstance(v, str) or "\n" in v or "\r" in v for v in value):
            refuse()
        content = "\n".join(value)
    elif isinstance(value, str):
        content = value
    else:
        refuse()
    if len(content) > 131072:
        refuse()
    try:
        lines = decode_subscription(content).splitlines()
        if len(lines) != len(routes) or any(not line or len(line) > 4096 for line in lines):
            refuse()
        # Retain the existing every-authority/hidden-marker checks as well.
        validate_content("\n".join(lines), domain, [], len(routes))
        by_path = {}
        allowed = {"encryption", "security", "type", "host", "sni", "path", "mode", "fp", "alpn", "extra"}
        for line in lines:
            if (any(ord(c) < 33 or ord(c) == 127 for c in line) or
                    re.search(r"%(?![0-9a-fA-F]{2})", line)):
                refuse()
            parsed = urlsplit(line)
            if (parsed.scheme != "vless" or parsed.username != client_uuid or parsed.password is not None
                    or parsed.path or parsed.hostname.lower() != domain or parsed.port != 443):
                refuse()
            pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True,
                              encoding="utf-8", errors="strict", max_num_fields=16)
            params = dict(pairs)
            if (len(pairs) != len(params) or set(params) - allowed or
                    any(any(ord(c) < 32 or ord(c) == 127 for c in v) for v in params.values()) or
                    any(params.get(k) != v for k, v in {"encryption": "none", "security": "tls",
                        "type": "xhttp", "host": domain, "sni": domain, "mode": "stream-one"}.items()) or
                    params.get("path") not in {r[3] for r in routes}):
                refuse()
            if "fp" in params and params["fp"] not in {"chrome", "firefox", "safari", "ios", "android",
                    "edge", "360", "qq", "random", "randomized"}:
                refuse()
            if "alpn" in params and params["alpn"] not in {"h2", "http/1.1", "h2,http/1.1", "http/1.1,h2"}:
                refuse()
            if "extra" in params and not re.fullmatch(r'\{\s*"mode"\s*:\s*"stream-one"\s*\}', params["extra"]):
                refuse()
            if "extra" in params:
                params["extra"] = '{"mode":"stream-one"}'
            path = params["path"]
            if path in by_path:
                refuse()
            # Canonical ordering removes irrelevant native ordering differences.
            # Native remarks can contain email/private metadata; do not export them.
            by_path[path] = urlunsplit(("vless", f"{client_uuid}@{domain}:443", "",
                urlencode(sorted(params.items())), ""))
        if set(by_path) != {r[3] for r in routes}:
            refuse()
        return [by_path[path] + "#" + quote(name, safe="") for _, name, _, path in sorted(routes)]
    except (ValueError, TypeError, AttributeError, UnicodeError):
        refuse()


def expected_client_profiles(config, subscription_id):
    client = next(
        (item for item in config.get("clients", []) if item.get("subscription_id") == subscription_id),
        None,
    )
    if not client or not client.get("enabled", True):
        return 0
    credentials = {
        item.get("route_id")
        for item in client.get("credentials", [])
        if item.get("enabled", True)
    }
    exits = {
        item.get("id")
        for item in config.get("exits", [])
        if item.get("enabled", True)
    }
    count = 0
    for route in config.get("routes", []):
        if not route.get("enabled", True) or route.get("id") not in credentials:
            continue
        kind = route.get("kind")
        eligible = (
            kind == "direct"
            or (kind == "cascade" and route.get("exit_id") in exits)
            or kind == "auto"
        )
        if eligible and route_is_public(config, route, manual_default=True):
            count += 1
    return count


def reconcile_client_subids(config, response):
    result = copy.deepcopy(config)
    values = response.get("obj", []) if isinstance(response, dict) else []
    actual = [item for item in values if isinstance(item, dict)]
    actions = []
    for client in result.get("clients", []):
        if not client.get("enabled", True):
            continue
        identities = {str(client.get("uuid") or "")}
        identities.update(str(item.get("uuid") or "") for item in client.get("credentials", []))
        identities.discard("")
        matches = [item for item in actual if str(item.get("uuid") or item.get("id") or "") in identities]
        if not matches:
            raise ValueError(f"builder client is missing from 3X-UI: {client.get('id')}")
        subids = {str(item.get("subId") or "") for item in matches if item.get("subId")}
        if len(subids) > 1:
            raise ValueError(f"builder client has conflicting native subscription IDs: {client.get('id')}")
        if subids:
            client["subscription_id"] = next(iter(subids))
            continue
        expected = str(client.get("subscription_id") or "")
        if not expected:
            raise ValueError(f"builder client has no subscription ID: {client.get('id')}")
        for item in matches:
            email = str(item.get("email") or "")
            if not email:
                raise ValueError("3X-UI client has no email")
            actions.append({"email": email, "sub_id": expected})
    return result, actions


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

    mutate = commands.add_parser("set-backend")
    mutate.add_argument("--config", required=True)
    mutate.add_argument("--output", required=True)
    mutate.add_argument("--backend", choices=sorted(BACKENDS), required=True)
    mutate.add_argument("--path")

    settings = commands.add_parser("settings")
    settings.add_argument("--config", required=True)
    settings.add_argument("--current", required=True)
    settings.add_argument("--output", required=True)

    capabilities = commands.add_parser("capabilities")
    capabilities.add_argument("--openapi", required=True)

    inbounds = commands.add_parser("inbounds")
    inbounds.add_argument("--response", required=True)

    validate = commands.add_parser("validate-content")
    validate.add_argument("--content", required=True)
    validate.add_argument("--domain", required=True)
    validate.add_argument("--forbidden", default="")
    validate.add_argument("--expected-profiles", type=int)

    expected = commands.add_parser("expected-profiles")
    expected.add_argument("--config", required=True)
    expected.add_argument("--subscription-id", required=True)

    client_plan = commands.add_parser("client-plan")
    client_plan.add_argument("--config", required=True)
    client_plan.add_argument("--clients", required=True)
    client_plan.add_argument("--output-config", required=True)
    client_plan.add_argument("--actions", required=True)

    args = parser.parse_args()
    if args.command == "set-backend":
        write_json(args.output, set_backend(read_json(args.config), args.backend, args.path))
    elif args.command == "settings":
        write_json(args.output, native_settings(read_json(args.current), read_json(args.config)))
    elif args.command == "capabilities":
        print(json.dumps(openapi_capabilities(read_json(args.openapi)), separators=(",", ":"), sort_keys=True))
    elif args.command == "inbounds":
        print(json.dumps(visible_inbounds(read_json(args.response)), separators=(",", ":"), sort_keys=True))
    elif args.command == "validate-content":
        result = validate_content(
            Path(args.content).read_text(encoding="utf-8"),
            args.domain,
            args.forbidden.split(","),
            args.expected_profiles,
        )
        print(json.dumps(result, separators=(",", ":"), sort_keys=True))
    elif args.command == "client-plan":
        config, actions = reconcile_client_subids(read_json(args.config), read_json(args.clients))
        write_json(args.output_config, config)
        write_json(args.actions, actions)
    else:
        print(expected_client_profiles(read_json(args.config), args.subscription_id))


if __name__ == "__main__":
    main()
