#!/usr/bin/env python3
"""Remove legacy WaveMesh subscription locations from the main nginx site."""

import argparse
import json
from pathlib import Path


def location_blocks(lines):
    index = 0
    while index < len(lines):
        stripped = lines[index].strip()
        if not stripped.startswith("location ") or "{" not in stripped:
            index += 1
            continue
        depth = lines[index].count("{") - lines[index].count("}")
        end = index + 1
        while end < len(lines) and depth > 0:
            depth += lines[end].count("{") - lines[end].count("}")
            end += 1
        yield index, end
        index = end


def sanitize(text, subscription_path):
    path = subscription_path if subscription_path.endswith("/") else subscription_path + "/"
    lines = text.splitlines(keepends=True)
    remove = set()
    blocks = list(location_blocks(lines))
    owned_paths = {path}
    for start, end in blocks:
        header = lines[start].strip()
        block = "".join(lines[start:end])
        owned = "try_files /sub.txt" in block or "proxy_pass http://127.0.0.1:2096" in block
        if owned:
            parts = header.replace("{", "").replace("=", " ").split()
            owned_paths.update(value for value in parts if value.startswith("/"))
            remove.update(range(start, end))
            if end < len(lines) and not lines[end].strip():
                remove.add(end)
    for start, end in blocks:
        block = "".join(lines[start:end])
        if "return 301" in block and any(value.rstrip("/") in block for value in owned_paths):
            remove.update(range(start, end))
            if end < len(lines) and not lines[end].strip():
                remove.add(end)
    return "".join(line for index, line in enumerate(lines) if index not in remove)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--site", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    path = config["network"]["subscription"]["path"]
    source = Path(args.site).read_text(encoding="utf-8")
    Path(args.output).write_text(sanitize(source, path), encoding="utf-8")


if __name__ == "__main__":
    main()
