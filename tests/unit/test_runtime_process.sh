#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WM_LIB_DIR="$ROOT_DIR/scripts"
source "$ROOT_DIR/scripts/commands/runtime.sh"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/123"
touch "$tmp/123/exe"

readlink() {
  case "$XRAY_TEST_PROCESS" in
    xray) printf '%s\n' '/usr/local/bin/xray' ;;
    packaged) printf '%s\n' '/usr/local/x-ui/bin/xray-linux-amd64' ;;
    other) printf '%s\n' '/usr/sbin/nginx' ;;
    *) return 1 ;;
  esac
}

WM_PROC_ROOT="$tmp"

XRAY_TEST_PROCESS=xray
wm_xray_process_running

XRAY_TEST_PROCESS=packaged
wm_xray_process_running

XRAY_TEST_PROCESS=other
if wm_xray_process_running; then
  echo "non-Xray process was accepted" >&2
  exit 1
fi

echo "runtime process detection tests: OK"
