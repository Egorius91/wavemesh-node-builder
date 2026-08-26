#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TEMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TEMP_DIR"' EXIT

BIN_DIR="$TEMP_DIR/bin"
DESTDIR="$TEMP_DIR/root"
SYSTEMCTL_LOG="$TEMP_DIR/systemctl.log"
mkdir -p "$BIN_DIR" "$DESTDIR/etc/wavemesh-agent"

cat > "$BIN_DIR/wavemesh" <<'SH'
#!/usr/bin/env bash
exit 0
SH
cat > "$BIN_DIR/systemctl" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$WAVEMESH_TEST_SYSTEMCTL_LOG"
exit 0
SH
chmod +x "$BIN_DIR/wavemesh" "$BIN_DIR/systemctl"

token="wvn_$(printf 'a%.0s' {1..40})"
cat > "$DESTDIR/etc/wavemesh-agent/agent.env" <<EOF
WAVEMESH_API_BASE=https://api.example.invalid/api
WAVEMESH_NODE_ID=node_12345678
WAVEMESH_TENANT_ID=tenant_12345678
WAVEMESH_AGENT_TOKEN=$token
WAVEMESH_AGENT_TOKEN_EXPIRES_AT=2030-01-01T00:00:00Z
EOF
chmod 0600 "$DESTDIR/etc/wavemesh-agent/agent.env"

run_installer() {
  PATH="$BIN_DIR:$PATH" \
  WAVEMESH_AGENT_DESTDIR="$DESTDIR" \
  WAVEMESH_AGENT_SYSTEMCTL="$BIN_DIR/systemctl" \
  WAVEMESH_AGENT_PYTHON="${WAVEMESH_TEST_PYTHON:-/usr/bin/python3}" \
  WAVEMESH_TEST_SYSTEMCTL_LOG="$SYSTEMCTL_LOG" \
    bash "$ROOT_DIR/agent/install.sh" >/dev/null
}

run_rollback() {
  PATH="$BIN_DIR:$PATH" \
  WAVEMESH_AGENT_DESTDIR="$DESTDIR" \
  WAVEMESH_AGENT_SYSTEMCTL="$BIN_DIR/systemctl" \
  WAVEMESH_AGENT_PYTHON="${WAVEMESH_TEST_PYTHON:-/usr/bin/python3}" \
  WAVEMESH_TEST_SYSTEMCTL_LOG="$SYSTEMCTL_LOG" \
    bash "$DESTDIR/usr/local/sbin/wavemesh-node-agent-rollback" "$@"
}

run_installer
printf '\n# rollback-latest-marker\n' >> "$DESTDIR/usr/local/lib/wavemesh-agent/node_agent.py"
run_installer

backup_root="$DESTDIR/var/lib/wavemesh-agent/backups"
mkdir -p "$backup_root/stale-upstream-release-20990101T000000Z"

run_rollback --latest >/dev/null
grep -Fq 'rollback-latest-marker' "$DESTDIR/usr/local/lib/wavemesh-agent/node_agent.py"

mkdir -p "$backup_root/20991231T235959Z-99999"
if run_rollback --latest >/dev/null 2>&1; then
  echo "rollback accepted canonical backup without a manifest" >&2
  exit 1
fi

echo "node agent latest rollback selection tests: OK"
