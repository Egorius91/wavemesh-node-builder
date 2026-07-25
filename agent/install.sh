#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AGENT_SOURCE="$PROJECT_DIR/agent/node_agent.py"
UNIT_SOURCE="$PROJECT_DIR/agent/wavemesh-node-agent.service"
ENV_FILE="${WAVEMESH_AGENT_ENV:-/etc/wavemesh-agent/agent.env}"
INSTALL_DIR="/usr/local/lib/wavemesh-agent"
UNIT_PATH="/etc/systemd/system/wavemesh-node-agent.service"

fail() {
  echo "✗ $*" >&2
  exit 1
}

ok() {
  echo "✓ $*"
}

[[ "${EUID}" -eq 0 ]] || fail "Run as root: sudo bash agent/install.sh"
command -v python3 >/dev/null 2>&1 || fail "python3 is required"
command -v wavemesh >/dev/null 2>&1 || fail "wavemesh CLI is required"
[[ -f "$AGENT_SOURCE" ]] || fail "Missing agent source: $AGENT_SOURCE"
[[ -f "$UNIT_SOURCE" ]] || fail "Missing systemd unit: $UNIT_SOURCE"
[[ -f "$ENV_FILE" ]] || fail "Missing enrolled agent environment: $ENV_FILE"

mode="$(stat -c '%a' "$ENV_FILE")"
if [[ "$mode" != "600" ]]; then
  chmod 600 "$ENV_FILE"
fi
chown root:root "$ENV_FILE"

install -d -m 0700 /etc/wavemesh-agent
install -d -m 0755 "$INSTALL_DIR"
install -m 0755 "$AGENT_SOURCE" "$INSTALL_DIR/node_agent.py"
install -m 0644 "$UNIT_SOURCE" "$UNIT_PATH"

/usr/bin/python3 "$INSTALL_DIR/node_agent.py" check --env-file "$ENV_FILE" >/dev/null
/usr/bin/python3 -m py_compile "$INSTALL_DIR/node_agent.py"

systemctl daemon-reload
systemctl enable --now wavemesh-node-agent.service
sleep 2
systemctl is-active --quiet wavemesh-node-agent.service || {
  systemctl status wavemesh-node-agent.service --no-pager --lines=40 || true
  fail "wavemesh-node-agent.service did not become active"
}

ok "Observe-only Node Agent installed and active"
ok "Environment: $ENV_FILE"
ok "Status: systemctl status wavemesh-node-agent.service"
ok "Logs: journalctl -u wavemesh-node-agent.service"
