#!/usr/bin/env bash
set -Eeuo pipefail
[[ "$EUID" -eq 0 && "$#" -eq 1 ]] || exit 1
case "$1" in inspect|prepare|deliver|apply|reconcile) ;; *) exit 1 ;; esac
exec /usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin LANG=C \
  /usr/bin/python3 -E -s /usr/local/lib/wavemesh-agent/expired_node_helper.py "$1"
