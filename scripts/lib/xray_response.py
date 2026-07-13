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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--response", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    response = json.loads(Path(args.response).read_text(encoding="utf-8"))
    Path(args.output).write_text(json.dumps(extract_template(response), indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
