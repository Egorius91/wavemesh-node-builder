#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DESTDIR="${WAVEMESH_AGENT_DESTDIR:-}"
SYSTEMCTL="${WAVEMESH_AGENT_SYSTEMCTL:-systemctl}"
PYTHON="${WAVEMESH_AGENT_PYTHON:-/usr/bin/python3}"
SERVICE="wavemesh-node-agent.service"
if [[ -z "$DESTDIR" ]]; then
  SYSTEMCTL=systemctl
  PYTHON=/usr/bin/python3
fi

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

ok() {
  echo "OK: $*"
}

validate_destdir() {
  [[ -z "$DESTDIR" || "$DESTDIR" == /* ]] || fail "WAVEMESH_AGENT_DESTDIR must be absolute"
  [[ "$DESTDIR" != "/" ]] || fail "WAVEMESH_AGENT_DESTDIR must not be /"
  [[ -z "$DESTDIR" || ! -L "$DESTDIR" ]] || fail "WAVEMESH_AGENT_DESTDIR must not be a symlink"
}

root_path() {
  printf '%s%s\n' "$DESTDIR" "$1"
}

install_directory() {
  local mode="$1" path="$2"
  [[ ! -L "$path" ]] || fail "Refusing to use a symlink directory"
  if [[ -z "$DESTDIR" ]]; then
    install -o root -g root -d -m "$mode" "$path"
  else
    install -d -m "$mode" "$path"
  fi
}

atomic_install_file() {
  local source="$1" target="$2" mode="$3" temporary
  [[ -f "$source" && ! -L "$source" ]] || fail "Missing or unsafe install source"
  install_directory 0755 "$(dirname "$target")"
  [[ ! -e "$target" || -f "$target" ]] || fail "Refusing to replace a non-file target"
  if [[ -f "$target" && ! -L "$target" ]] && cmp -s "$source" "$target"; then
    chmod "$mode" "$target"
    [[ -n "$DESTDIR" ]] || chown root:root "$target"
    return
  fi
  [[ ! -L "$target" ]] || fail "Refusing to replace a symlink target"
  temporary="$(mktemp "$(dirname "$target")/.${target##*/}.install.XXXXXX")"
  if [[ -z "$DESTDIR" ]]; then
    install -o root -g root -m "$mode" "$source" "$temporary"
  else
    install -m "$mode" "$source" "$temporary"
  fi
  mv -fT "$temporary" "$target"
}

backup_file() {
  local source="$1" backup_dir="$2" label="$3" mode="$4"
  if [[ -f "$source" && ! -L "$source" ]]; then
    if [[ -z "$DESTDIR" ]]; then
      install -o root -g root -m "$mode" "$source" "$backup_dir/$label"
    else
      install -m "$mode" "$source" "$backup_dir/$label"
    fi
  elif [[ ! -e "$source" && ! -L "$source" ]]; then
    : > "$backup_dir/$label.absent"
    chmod 0600 "$backup_dir/$label.absent"
  else
    fail "Refusing to back up an unsafe path"
  fi
}

file_would_change() {
  local source="$1" target="$2"
  [[ -f "$target" && ! -L "$target" ]] && cmp -s "$source" "$target" && return 1
  return 0
}

validate_destdir
if [[ -z "$DESTDIR" ]]; then
  [[ "${EUID}" -eq 0 ]] || fail "Run as root: sudo bash agent/install.sh"
fi
command -v "$PYTHON" >/dev/null 2>&1 || fail "python3 is required"
command -v wavemesh >/dev/null 2>&1 || fail "wavemesh CLI is required"
command -v "$SYSTEMCTL" >/dev/null 2>&1 || fail "systemctl is required"

ETC_DIR="$(root_path /etc/wavemesh-agent)"
INSTALL_DIR="$(root_path /usr/local/lib/wavemesh-agent)"
UNIT_PATH="$(root_path /etc/systemd/system/$SERVICE)"
ROLLBACK_PATH="$(root_path /usr/local/sbin/wavemesh-node-agent-rollback)"
RECOVERY_PATH="$(root_path /usr/local/sbin/wavemesh-node-agent-recover)"
BACKUP_ROOT="$(root_path /var/lib/wavemesh-agent/backups)"
ENV_FILE="${WAVEMESH_AGENT_ENV:-$ETC_DIR/agent.env}"

AGENT_SOURCE="$PROJECT_DIR/agent/node_agent.py"
CLIENT_SOURCE="$PROJECT_DIR/agent/node_mtls_client.py"
RUNTIME_SOURCE="$PROJECT_DIR/agent/node_mtls_runtime.py"
STATE_SOURCE="$PROJECT_DIR/agent/node_mtls_state.py"
RECOVERY_CLIENT_SOURCE="$PROJECT_DIR/agent/node_recovery.py"
ACCEPTANCE_SOURCE="$PROJECT_DIR/agent/acceptance.py"
ACCESS_SOURCE="$PROJECT_DIR/agent/access_runtime.py"
UNIT_SOURCE="$PROJECT_DIR/agent/wavemesh-node-agent.service"
ROLLBACK_SOURCE="$PROJECT_DIR/agent/rollback.sh"
RECOVERY_SOURCE="$PROJECT_DIR/agent/recover.sh"

for source in \
  "$AGENT_SOURCE" \
  "$CLIENT_SOURCE" \
  "$RUNTIME_SOURCE" \
  "$STATE_SOURCE" \
  "$RECOVERY_CLIENT_SOURCE" \
  "$ACCEPTANCE_SOURCE" \
  "$ACCESS_SOURCE" \
  "$UNIT_SOURCE" \
  "$ROLLBACK_SOURCE" \
  "$RECOVERY_SOURCE"; do
  [[ -f "$source" && ! -L "$source" ]] || fail "Missing or unsafe installer source"
done
[[ -f "$ENV_FILE" && ! -L "$ENV_FILE" ]] || fail "Missing or unsafe enrolled agent environment"

if grep -Eq -- '-----BEGIN [A-Z0-9 ]+-----' "$ENV_FILE"; then
  fail "Agent environment must not contain PEM material"
fi
mtls_mode_lines="$(grep -Ec '^WAVEMESH_AGENT_MTLS_MODE=' "$ENV_FILE" || true)"
[[ "$mtls_mode_lines" -le 1 ]] || fail "Agent environment contains duplicate mTLS mode settings"
command_mode_lines="$(grep -Ec '^WAVEMESH_AGENT_COMMAND_MODE=' "$ENV_FILE" || true)"
[[ "$command_mode_lines" -le 1 ]] || fail "Agent environment contains duplicate command mode settings"
"$PYTHON" "$AGENT_SOURCE" check --env-file "$ENV_FILE" >/dev/null

install_directory 0700 "$ETC_DIR"
install_directory 0700 "$ETC_DIR/tls"
install_directory 0700 "$ETC_DIR/tls/pending"
install_directory 0700 "$ETC_DIR/tls/generations"
install_directory 0700 "$(root_path /var/lib/wavemesh-agent/access)"
install_directory 0700 "$(root_path /var/lib/wavemesh-agent/recovery-backups)"
install_directory 0755 "$INSTALL_DIR"
install_directory 0700 "$BACKUP_ROOT"

chmod 0600 "$ENV_FILE"
[[ -n "$DESTDIR" ]] || chown root:root "$ENV_FILE"

env_migration_required=false
if [[ "$mtls_mode_lines" -eq 0 ]]; then
  env_migration_required=true
fi
if [[ "$command_mode_lines" -eq 0 ]]; then
  env_migration_required=true
fi

changed=false
file_would_change "$AGENT_SOURCE" "$INSTALL_DIR/node_agent.py" && changed=true
file_would_change "$CLIENT_SOURCE" "$INSTALL_DIR/node_mtls_client.py" && changed=true
file_would_change "$RUNTIME_SOURCE" "$INSTALL_DIR/node_mtls_runtime.py" && changed=true
file_would_change "$STATE_SOURCE" "$INSTALL_DIR/node_mtls_state.py" && changed=true
file_would_change "$RECOVERY_CLIENT_SOURCE" "$INSTALL_DIR/node_recovery.py" && changed=true
file_would_change "$ACCEPTANCE_SOURCE" "$INSTALL_DIR/acceptance.py" && changed=true
file_would_change "$ACCESS_SOURCE" "$INSTALL_DIR/access_runtime.py" && changed=true
file_would_change "$UNIT_SOURCE" "$UNIT_PATH" && changed=true
file_would_change "$ROLLBACK_SOURCE" "$ROLLBACK_PATH" && changed=true
file_would_change "$RECOVERY_SOURCE" "$RECOVERY_PATH" && changed=true
[[ "$env_migration_required" == false ]] || changed=true

unit_changed=false
file_would_change "$UNIT_SOURCE" "$UNIT_PATH" && unit_changed=true

backup_dir=""
if [[ "$changed" == true ]]; then
  backup_id="$(date -u +%Y%m%dT%H%M%SZ)-$$"
  backup_dir="$BACKUP_ROOT/$backup_id"
  install_directory 0700 "$backup_dir"
  backup_file "$INSTALL_DIR/node_agent.py" "$backup_dir" node_agent.py 0755
  backup_file "$INSTALL_DIR/node_mtls_client.py" "$backup_dir" node_mtls_client.py 0644
  backup_file "$INSTALL_DIR/node_mtls_runtime.py" "$backup_dir" node_mtls_runtime.py 0644
  backup_file "$INSTALL_DIR/node_mtls_state.py" "$backup_dir" node_mtls_state.py 0644
  backup_file "$INSTALL_DIR/node_recovery.py" "$backup_dir" node_recovery.py 0755
  backup_file "$INSTALL_DIR/acceptance.py" "$backup_dir" acceptance.py 0755
  backup_file "$INSTALL_DIR/access_runtime.py" "$backup_dir" access_runtime.py 0755
  backup_file "$UNIT_PATH" "$backup_dir" "$SERVICE" 0644
  backup_file "$ROLLBACK_PATH" "$backup_dir" wavemesh-node-agent-rollback 0755
  backup_file "$RECOVERY_PATH" "$backup_dir" wavemesh-node-agent-recover 0755
  backup_file "$ENV_FILE" "$backup_dir" agent.env 0600
  printf 'schema_version=1\ncreated_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$backup_dir/manifest"
  chmod 0600 "$backup_dir/manifest"
  [[ -n "$DESTDIR" ]] || chown -R root:root "$backup_dir"
fi

if [[ "$env_migration_required" == true ]]; then
  env_temporary="$(mktemp "$ETC_DIR/.agent.env.migrate.XXXXXX")"
  install -m 0600 "$ENV_FILE" "$env_temporary"
  [[ "$mtls_mode_lines" -ne 0 ]] || printf '\nWAVEMESH_AGENT_MTLS_MODE=disabled\n' >> "$env_temporary"
  [[ "$command_mode_lines" -ne 0 ]] || printf 'WAVEMESH_AGENT_COMMAND_MODE=disabled\n' >> "$env_temporary"
  [[ -n "$DESTDIR" ]] || chown root:root "$env_temporary"
  mv -fT "$env_temporary" "$ENV_FILE"
fi

atomic_install_file "$AGENT_SOURCE" "$INSTALL_DIR/node_agent.py" 0755
atomic_install_file "$CLIENT_SOURCE" "$INSTALL_DIR/node_mtls_client.py" 0644
atomic_install_file "$RUNTIME_SOURCE" "$INSTALL_DIR/node_mtls_runtime.py" 0644
atomic_install_file "$STATE_SOURCE" "$INSTALL_DIR/node_mtls_state.py" 0644
atomic_install_file "$RECOVERY_CLIENT_SOURCE" "$INSTALL_DIR/node_recovery.py" 0755
atomic_install_file "$ACCEPTANCE_SOURCE" "$INSTALL_DIR/acceptance.py" 0755
atomic_install_file "$ACCESS_SOURCE" "$INSTALL_DIR/access_runtime.py" 0755
atomic_install_file "$UNIT_SOURCE" "$UNIT_PATH" 0644
atomic_install_file "$ROLLBACK_SOURCE" "$ROLLBACK_PATH" 0755
atomic_install_file "$RECOVERY_SOURCE" "$RECOVERY_PATH" 0755

if grep -Eq -- '-----BEGIN [A-Z0-9 ]+-----' "$ENV_FILE"; then
  fail "Agent environment migration produced unsafe content"
fi
"$PYTHON" "$INSTALL_DIR/node_agent.py" check --env-file "$ENV_FILE" >/dev/null
"$PYTHON" -m py_compile \
  "$INSTALL_DIR/node_agent.py" \
  "$INSTALL_DIR/node_mtls_client.py" \
  "$INSTALL_DIR/node_mtls_runtime.py" \
  "$INSTALL_DIR/node_mtls_state.py" \
  "$INSTALL_DIR/node_recovery.py" \
  "$INSTALL_DIR/acceptance.py" \
  "$INSTALL_DIR/access_runtime.py"

if [[ "$unit_changed" == true ]]; then
  "$SYSTEMCTL" daemon-reload
fi
"$SYSTEMCTL" enable "$SERVICE" >/dev/null

ok "Node Agent files prepared; service was not started or restarted"
ok "mTLS mode remains disabled unless explicitly configured by an operator"
ok "Access command mode remains disabled unless explicitly configured by an operator"
ok "Node-scoped recovery tool is installed but inert without a private one-time token"
if [[ -n "$backup_dir" ]]; then
  ok "Previous installation backed up under the private backup root"
else
  ok "Installation already matched the requested version"
fi
ok "Activation is a separate deployment step"
