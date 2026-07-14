#!/usr/bin/env python3
import argparse
import json
import secrets
from pathlib import Path


def random_segment(length):
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def random_path():
    return f"/{random_segment(10)}/{random_segment(18)}/"


def normalize(path):
    if not path.startswith("/"):
        path = "/" + path
    if not path.endswith("/"):
        path += "/"
    return path


def rotate(config_path, output_path, requested=None):
    cfg = json.loads(Path(config_path).read_text(encoding="utf-8"))
    old = normalize(cfg.get("network", {}).get("subscription", {}).get("path", "/sub/"))
    new = normalize(requested or random_path())
    if new == old:
        raise ValueError("new subscription path matches the current path")
    if new.startswith("/sub/"):
        raise ValueError("new subscription path must not use the legacy /sub/ prefix")
    cfg.setdefault("network", {}).setdefault("subscription", {})["path"] = new
    Path(output_path).write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"old_path": old, "new_path": new}, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--path")
    args = parser.parse_args()
    rotate(args.config, args.output, args.path)


if __name__ == "__main__":
    main()
