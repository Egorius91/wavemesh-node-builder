#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
UNIT="$ROOT_DIR/agent/wavemesh-node-agent.service"

grep -Fx 'NoNewPrivileges=true' "$UNIT" >/dev/null
grep -Fx 'CapabilityBoundingSet=CAP_SYS_PTRACE' "$UNIT" >/dev/null
grep -Fx 'RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK' "$UNIT" >/dev/null

if grep -Eq '^CapabilityBoundingSet=.*CAP_(NET_ADMIN|SYS_ADMIN|DAC_OVERRIDE)' "$UNIT"; then
  echo "node agent unit grants an excessive capability" >&2
  exit 1
fi

echo "node agent unit hardening tests: OK"
