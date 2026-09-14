#!/usr/bin/env bash

# Source checkouts keep the implementation in agent/. Installed CLI libraries
# carry an exact copy beside this adapter, without depending on an Agent install.
WM_PANEL_GUARD_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
case "$WM_PANEL_GUARD_LIB_DIR" in
  */scripts/lib)
    WM_PANEL_REQUEST_GUARD="$(cd "$WM_PANEL_GUARD_LIB_DIR/../.." && pwd)/agent/panel_request_guard.py"
    ;;
  *) WM_PANEL_REQUEST_GUARD="$WM_PANEL_GUARD_LIB_DIR/panel_request_guard.py" ;;
esac

wm_panel_guard_run() {
  [[ -f "$WM_PANEL_REQUEST_GUARD" && ! -L "$WM_PANEL_REQUEST_GUARD" ]] || return 1
  python3 "$WM_PANEL_REQUEST_GUARD" "$@"
}
