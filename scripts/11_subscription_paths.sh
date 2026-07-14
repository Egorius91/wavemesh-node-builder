#!/usr/bin/env bash

# Replace the legacy /sub/<token>/ pattern with an opaque two-segment path.
# This function is sourced after 00_common.sh and intentionally overrides
# wm_generate_random_values while preserving all other generated values.
wm_generate_random_values() {
  PANEL_PORT="${PANEL_PORT:-$(wm_random_port 48900 59999)}"
  XHTTP_LOCAL_PORT="${XHTTP_LOCAL_PORT:-$(wm_random_port 10000 30000)}"
  SUB_LOCAL_PORT="${SUB_LOCAL_PORT:-$(wm_random_port 31000 39000)}"
  PANEL_PATH="/${PANEL_PATH:-$(wm_random_alnum 18)}/"
  XHTTP_PATH="/api/$(wm_random_alnum 14)/"
  SUB_PATH="/$(wm_random_alnum 10)/$(wm_random_alnum 18)/"
  NODE_NAME="${NODE_NAME:-Node-$(wm_random_alnum 6)}"
  WEB_IDENTITY_NAME="${WEB_IDENTITY_NAME:-$(wm_random_company_name)}"
  PANEL_USERNAME="${PANEL_USERNAME:-$(wm_random_alnum 10)}"
  PANEL_PASSWORD="${PANEL_PASSWORD:-$(wm_random_alnum 18)}"
  PANEL_TOKEN=""
}
