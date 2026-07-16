#!/usr/bin/env python3
"""Normalize version-dependent 3X-UI Xray template responses."""

import argparse
import json
from pathlib import Path


def extract_template(response):
    raw = response.get("obj")
    for _ in range(8):
        if isinstance(raw, str):
            raw = json.loads(raw)
            continue
        if isinstance(raw, dict) and "xraySetting" in raw:
            raw = raw["xraySetting"]
            continue
        break
    if not isinstance(raw, dict):
        raise ValueError("3X-UI returned an unsupported Xray template format")
    return raw


def extract_route_outbound(response):
    obj = response.get("obj")
    if isinstance(obj, str):
        obj = json.loads(obj)
    if not isinstance(obj, dict):
        raise ValueError("3X-UI returned an unsupported routeTest format")
    if obj.get("matched") is not True:
        return ""
    outbound = obj.get("outboundTag")
    if not isinstance(outbound, str):
        raise ValueError("3X-UI routeTest response has no outboundTag")
    return outbound


def _decode_obj(response):
    raw = response.get("obj")
    for _ in range(4):
        if not isinstance(raw, str):
            break
        raw = json.loads(raw)
    return raw


def extract_test_outbound(response):
    """Normalize the real probe result inside a successful 3X-UI envelope."""
    raw = _decode_obj(response)

    # Older 3X-UI releases returned the measured delay directly.
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return {
            "success": raw >= 0,
            "delay": int(raw) if raw >= 0 else None,
            "error": None if raw >= 0 else "outbound probe failed",
        }

    if not isinstance(raw, dict):
        raise ValueError("3X-UI returned an unsupported testOutbound format")

    success = raw.get("success") is True
    endpoints = raw.get("endpoints")
    if isinstance(endpoints, list) and endpoints:
        # A top-level API success is not data-plane success. For TCP mode at
        # least one endpoint must have been reached by the actual probe.
        success = success and any(
            isinstance(endpoint, dict) and endpoint.get("success") is True
            for endpoint in endpoints
        )

    delay = raw.get("delay")
    if not isinstance(delay, (int, float)) or isinstance(delay, bool) or delay < 0:
        delay = None
    error = raw.get("error")
    if not isinstance(error, str) or not error.strip():
        error = None if success else "outbound endpoint is unreachable"

    return {
        "success": success,
        "delay": int(delay) if delay is not None else None,
        "error": error,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--response", required=True)
    parser.add_argument("--output")
    parser.add_argument("--kind", choices=("template", "test-outbound"), default="template")
    args = parser.parse_args()
    response = json.loads(Path(args.response).read_text(encoding="utf-8"))
    if args.kind == "test-outbound":
        result = extract_test_outbound(response)
        print(json.dumps(result, separators=(",", ":"), sort_keys=True))
        raise SystemExit(0 if result["success"] else 1)
    if not args.output:
        parser.error("--output is required for template responses")
    Path(args.output).write_text(json.dumps(extract_template(response), indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
