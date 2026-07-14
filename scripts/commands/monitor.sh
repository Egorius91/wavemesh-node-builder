#!/usr/bin/env bash

WM_HEALTH_EVENT_TOOL="${WM_HEALTH_EVENT_TOOL:-$WM_LIB_DIR/lib/health_events.py}"
WM_MONITOR_STATE_DIR="${WM_MONITOR_STATE_DIR:-/var/lib/wavemesh-node}"
WM_MONITOR_LOG_DIR="${WM_MONITOR_LOG_DIR:-/var/log/wavemesh-node}"
WM_MONITOR_STATE="${WM_MONITOR_STATE:-$WM_MONITOR_STATE_DIR/health-state.json}"
WM_MONITOR_EVENTS="${WM_MONITOR_EVENTS:-$WM_MONITOR_LOG_DIR/health-events.jsonl}"

wm_monitor_require_entry() {
  wm_load_config
  [[ "$NODE_ROLE" == "entry" ]] || wm_fail "Health monitoring currently requires an entry node"
}

wm_monitor_run() {
  local as_json=0 transaction manual auto
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --json) as_json=1; shift ;;
      *) wm_fail "Usage: wavemesh monitor run [--json]" ;;
    esac
  done
  wm_monitor_require_entry
  transaction="$(mktemp -d)"
  trap 'rm -rf "$transaction"' RETURN
  manual="$transaction/manual.json"
  auto="$transaction/auto.json"
  install -d -m 700 "$WM_MONITOR_STATE_DIR" "$WM_MONITOR_LOG_DIR"
  wavemesh cascade health --json > "$manual" || wm_fail "Manual route health collection failed"
  wavemesh cascade auto health --json > "$auto" || wm_fail "Auto Route health collection failed"
  local args=(--manual "$manual" --auto "$auto" --state "$WM_MONITOR_STATE" --events "$WM_MONITOR_EVENTS")
  (( as_json == 1 )) && args+=(--json)
  python3 "$WM_HEALTH_EVENT_TOOL" "${args[@]}"
}

wm_monitor_status() {
  local as_json=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --json) as_json=1; shift ;;
      *) wm_fail "Usage: wavemesh monitor status [--json]" ;;
    esac
  done
  [[ -f "$WM_MONITOR_STATE" ]] || wm_fail "No monitor state found; run: wavemesh monitor run"
  if (( as_json == 1 )); then
    cat "$WM_MONITOR_STATE"
  else
    python3 - "$WM_MONITOR_STATE" <<'PY'
import json,sys
state=json.load(open(sys.argv[1],encoding="utf-8"))
print(f"Observed: {state.get('observed_at','-')}")
print(f"Node: {state.get('node_status','unknown')}")
print("TYPE\tNAME\tSTATUS\tDETAIL")
for item in state.get("entities",{}).values():
    detail=""
    if item.get("kind")=="manual": detail=f"{item.get('latency_ms','-')} ms / {item.get('outbound') or '-'}"
    else: detail=f"{item.get('healthy_exits','-')}/{item.get('total_exits','-')} exits / {item.get('strategy') or '-'}"
    print(f"{item.get('kind')}\t{item.get('display_name')}\t{item.get('status')}\t{detail}")
PY
  fi
}

wm_monitor_events() {
  local limit=20 as_json=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --limit) limit="${2:-}"; shift 2 ;;
      --json) as_json=1; shift ;;
      *) wm_fail "Usage: wavemesh monitor events [--limit N] [--json]" ;;
    esac
  done
  [[ "$limit" =~ ^[1-9][0-9]*$ ]] || wm_fail "--limit must be a positive integer"
  [[ -f "$WM_MONITOR_EVENTS" ]] || { (( as_json == 1 )) && echo '[]' || echo 'No health events recorded'; return 0; }
  tail -n "$limit" "$WM_MONITOR_EVENTS" | python3 -c '
import json,sys
rows=[json.loads(line) for line in sys.stdin if line.strip()]
if sys.argv[1]=="1":
 print(json.dumps(rows,indent=2,ensure_ascii=False))
else:
 print("TIME\tEVENT\tNAME\tPREVIOUS\tSTATUS")
 for row in rows: print("{}\t{}\t{}\t{}\t{}".format(row.get("observed_at","-"),row.get("event","-"),row.get("display_name","-"),row.get("previous_status") or "-",row.get("status","-")))
' "$as_json"
}

wm_monitor_install() {
  wm_monitor_require_entry
  [[ -f "$WM_LIB_DIR/systemd/wavemesh-health-monitor.service" ]] || wm_fail "Monitor service template is missing"
  [[ -f "$WM_LIB_DIR/systemd/wavemesh-health-monitor.timer" ]] || wm_fail "Monitor timer template is missing"
  install -d -m 700 "$WM_MONITOR_STATE_DIR" "$WM_MONITOR_LOG_DIR"
  install -m 644 "$WM_LIB_DIR/systemd/wavemesh-health-monitor.service" /etc/systemd/system/wavemesh-health-monitor.service
  install -m 644 "$WM_LIB_DIR/systemd/wavemesh-health-monitor.timer" /etc/systemd/system/wavemesh-health-monitor.timer
  systemctl daemon-reload
  systemctl enable --now wavemesh-health-monitor.timer
  wm_success "Health monitor installed and timer enabled"
  systemctl --no-pager status wavemesh-health-monitor.timer || true
}

wm_monitor_uninstall() {
  systemctl disable --now wavemesh-health-monitor.timer 2>/dev/null || true
  rm -f /etc/systemd/system/wavemesh-health-monitor.timer /etc/systemd/system/wavemesh-health-monitor.service
  systemctl daemon-reload
  wm_success "Health monitor timer removed; state and event history preserved"
}

wm_monitor_command() {
  case "${1:-}" in
    run) shift; wm_monitor_run "$@" ;;
    status) shift; wm_monitor_status "$@" ;;
    events) shift; wm_monitor_events "$@" ;;
    install) shift; wm_monitor_install "$@" ;;
    uninstall) shift; wm_monitor_uninstall "$@" ;;
    *) wm_fail "Usage: wavemesh monitor run|status|events|install|uninstall" ;;
  esac
}
