#!/usr/bin/env bash
set -Eeuo pipefail

DESTDIR="${WAVEMESH_AGENT_DESTDIR:-}"
SYSTEMCTL="${WAVEMESH_AGENT_SYSTEMCTL:-systemctl}"
PYTHON="${WAVEMESH_AGENT_PYTHON:-/usr/bin/python3}"
SERVICE="wavemesh-node-agent.service"
if [[ -z "$DESTDIR" ]]; then
  SYSTEMCTL=systemctl
  PYTHON=/usr/bin/python3
fi
RESTART=false
BACKUP_ID=""

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

ok() {
  echo "OK: $*"
}

usage() {
  echo "Usage: wavemesh-node-agent-rollback [--latest | --backup ID] [--restart]"
}

while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --latest)
      [[ -z "$BACKUP_ID" ]] || fail "Select exactly one backup"
      BACKUP_ID=latest
      shift
      ;;
    --backup)
      [[ -z "$BACKUP_ID" && "$#" -ge 2 ]] || fail "Missing or duplicate backup ID"
      BACKUP_ID="$2"
      shift 2
      ;;
    --restart)
      RESTART=true
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage >&2
      fail "Unknown rollback argument"
      ;;
  esac
done

[[ -n "$BACKUP_ID" ]] || fail "Select --latest or --backup ID"
[[ -z "$DESTDIR" || "$DESTDIR" == /* ]] || fail "WAVEMESH_AGENT_DESTDIR must be absolute"
[[ "$DESTDIR" != "/" ]] || fail "WAVEMESH_AGENT_DESTDIR must not be /"
[[ -z "$DESTDIR" || ! -L "$DESTDIR" ]] || fail "WAVEMESH_AGENT_DESTDIR must not be a symlink"
if [[ -z "$DESTDIR" ]]; then
  [[ "${EUID}" -eq 0 ]] || fail "Run rollback as root"
fi
command -v "$PYTHON" >/dev/null 2>&1 || fail "python3 is required"
command -v "$SYSTEMCTL" >/dev/null 2>&1 || fail "systemctl is required"

ETC_DIR="$DESTDIR/etc/wavemesh-agent"
INSTALL_DIR="$DESTDIR/usr/local/lib/wavemesh-agent"
UNIT_PATH="$DESTDIR/etc/systemd/system/$SERVICE"
ROLLBACK_PATH="$DESTDIR/usr/local/sbin/wavemesh-node-agent-rollback"
BACKUP_ROOT="$DESTDIR/var/lib/wavemesh-agent/backups"
ENV_FILE="${WAVEMESH_AGENT_ENV:-$ETC_DIR/agent.env}"

if [[ "$BACKUP_ID" == latest ]]; then
  shopt -s nullglob
  backups=()
  for candidate in "$BACKUP_ROOT"/*; do
    backup_name="${candidate##*/}"
    [[ -d "$candidate" && ! -L "$candidate" ]] || continue
    [[ "$backup_name" =~ ^[0-9]{8}T[0-9]{6}Z-[0-9]+$ ]] || continue
    backups+=("$candidate")
  done
  shopt -u nullglob
  [[ "${#backups[@]}" -gt 0 ]] || fail "No canonical Agent backups are available"
  backup_dir="${backups[${#backups[@]}-1]}"
else
  [[ "$BACKUP_ID" =~ ^[0-9]{8}T[0-9]{6}Z-[0-9]+$ ]] || fail "Backup ID is invalid"
  backup_dir="$BACKUP_ROOT/$BACKUP_ID"
fi
[[ -d "$backup_dir" && ! -L "$backup_dir" ]] || fail "Selected Agent backup is missing or unsafe"
[[ -f "$backup_dir/manifest" && ! -L "$backup_dir/manifest" ]] || fail "Selected Agent backup has no safe manifest"

install_directory() {
  local mode="$1" path="$2"
  [[ ! -L "$path" ]] || fail "Refusing to use a symlink directory"
  if [[ -z "$DESTDIR" ]]; then
    install -o root -g root -d -m "$mode" "$path"
  else
    install -d -m "$mode" "$path"
  fi
}

atomic_restore() {
  local source="$1" target="$2" mode="$3" parent_mode="$4" temporary
  [[ -f "$source" && ! -L "$source" ]] || fail "Backup file is missing or unsafe"
  install_directory "$parent_mode" "$(dirname "$target")"
  [[ ! -e "$target" || -f "$target" ]] || fail "Refusing to replace a non-file rollback target"
  temporary="$(mktemp "$(dirname "$target")/.${target##*/}.rollback.XXXXXX")"
  if [[ -z "$DESTDIR" ]]; then
    install -o root -g root -m "$mode" "$source" "$temporary"
  else
    install -m "$mode" "$source" "$temporary"
  fi
  mv -fT "$temporary" "$target"
}

restore_or_remove() {
  local label="$1" target="$2" mode="$3" parent_mode="$4"
  if [[ -f "$backup_dir/$label" && ! -L "$backup_dir/$label" ]]; then
    atomic_restore "$backup_dir/$label" "$target" "$mode" "$parent_mode"
  elif [[ -f "$backup_dir/$label.absent" && ! -L "$backup_dir/$label.absent" ]]; then
    [[ ! -L "$target" ]] || fail "Refusing to remove a symlink rollback target"
    rm -f -- "$target"
  else
    fail "Backup entry is incomplete"
  fi
}

unit_changed=true
if [[ -f "$backup_dir/$SERVICE" && -f "$UNIT_PATH" ]] && cmp -s "$backup_dir/$SERVICE" "$UNIT_PATH"; then
  unit_changed=false
elif [[ -f "$backup_dir/$SERVICE.absent" && ! -e "$UNIT_PATH" ]]; then
  unit_changed=false
fi

restore_or_remove node_agent.py "$INSTALL_DIR/node_agent.py" 0755 0755
restore_or_remove node_mtls_client.py "$INSTALL_DIR/node_mtls_client.py" 0644 0755
restore_or_remove node_mtls_runtime.py "$INSTALL_DIR/node_mtls_runtime.py" 0644 0755
restore_or_remove node_mtls_state.py "$INSTALL_DIR/node_mtls_state.py" 0644 0755
restore_or_remove acceptance.py "$INSTALL_DIR/acceptance.py" 0755 0755
restore_or_remove access_runtime.py "$INSTALL_DIR/access_runtime.py" 0755 0755
restore_or_remove "$SERVICE" "$UNIT_PATH" 0644 0755
restore_or_remove wavemesh-node-agent-rollback "$ROLLBACK_PATH" 0755 0755
restore_or_remove agent.env "$ENV_FILE" 0600 0700

if [[ -f "$ENV_FILE" ]] && grep -Eq -- '-----BEGIN [A-Z0-9 ]+-----' "$ENV_FILE"; then
  fail "Restored Agent environment contains PEM material"
fi
if [[ -f "$INSTALL_DIR/node_agent.py" && -f "$ENV_FILE" ]]; then
  "$PYTHON" "$INSTALL_DIR/node_agent.py" check --env-file "$ENV_FILE" >/dev/null
fi
if [[ "$unit_changed" == true ]]; then
  "$SYSTEMCTL" daemon-reload
fi
if [[ "$RESTART" == true ]]; then
  "$SYSTEMCTL" restart "$SERVICE"
fi

ok "Selected Agent backup restored atomically"
if [[ "$RESTART" == true ]]; then
  ok "Only $SERVICE was restarted"
else
  ok "Service was not restarted; activation remains an explicit operator step"
fi
