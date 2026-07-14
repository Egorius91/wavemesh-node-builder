#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

python3 - "$ROOT_DIR/scripts/07_xhttp_inbound.sh" <<'PY'
import ast
import re
import sys

text = open(sys.argv[1], encoding="utf-8").read()
match = re.search(r'xui\["inbound"\]\s*=\s*({.*?})\nwith open', text, re.S)
if not match:
    raise SystemExit("inbound config object not found")

tree = ast.parse(match.group(1), mode="eval")
keys = [node.value for node in tree.body.keys if isinstance(node, ast.Constant)]
if len(keys) != len(set(keys)):
    raise SystemExit("duplicate keys in inbound config object")
if "creation_mode" not in keys or "xhttp_mode" not in keys:
    raise SystemExit("inbound config must expose creation_mode and xhttp_mode")
PY

for file in "$ROOT_DIR/install.sh" "$ROOT_DIR/bin/wavemesh" "$ROOT_DIR"/scripts/*.sh; do
  bash -n "$file"
done

echo "baseline tests: OK"
