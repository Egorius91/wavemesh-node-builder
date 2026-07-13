#!/usr/bin/env bash
WM_SUBSCRIPTION_RENDERER="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/subscription_renderer.py"

wm_subscription_prepare() {
  local source_config="$1" output_config="$2" output_dir="$3" metadata="$4"
  python3 "$WM_SUBSCRIPTION_RENDERER" --config "$source_config" --output-config "$output_config" --output-dir "$output_dir" --metadata "$metadata"
}

wm_subscription_install_files() {
  local prepared_dir="$1" backup_dir="$2"
  mkdir -p "$backup_dir" "$WM_SUB_DIR/users"
  cp -a "$WM_SUB_DIR/." "$backup_dir/"
  find "$WM_SUB_DIR/users" -maxdepth 1 -type f -name '*.txt' -delete
  install -m 0644 "$prepared_dir/sub.txt" "$WM_SUB_DIR/sub.txt"
  local file
  for file in "$prepared_dir"/users/*.txt; do [[ -e "$file" ]] || continue; install -m 0644 "$file" "$WM_SUB_DIR/users/$(basename "$file")"; done
}

wm_subscription_restore_files() {
  local backup_dir="$1"
  rm -rf "$WM_SUB_DIR/users"; mkdir -p "$WM_SUB_DIR/users"
  cp -a "$backup_dir/." "$WM_SUB_DIR/"
}

wm_subscription_validate_public() {
  local metadata="$1" item path sub_id expected response_file status
  while IFS= read -r item; do
    path="$(printf '%s' "$item" | python3 -c 'import json,sys; print(json.load(sys.stdin)["path"])')"
    sub_id="$(printf '%s' "$item" | python3 -c 'import json,sys; print(json.load(sys.stdin)["subscription_id"])')"
    expected="$WM_SUB_DIR/users/${sub_id}.txt"
    response_file="$(mktemp)"
    status="$(curl -sSk --max-time 10 -o "$response_file" -w '%{http_code}' "https://${DOMAIN}${path}" 2>/dev/null || true)"
    if [[ "$status" != "200" ]]; then rm -f "$response_file"; wm_warn "Public subscription validation returned HTTP ${status:-transport-error} for ${path}"; return 1; fi
    if ! cmp -s "$response_file" "$expected"; then rm -f "$response_file"; wm_warn "Public subscription content differs from generated file for ${path}"; return 1; fi
    rm -f "$response_file"
  done < <(python3 -c 'import json,sys; [print(json.dumps(x,separators=(",",":"))) for x in json.load(open(sys.argv[1]))]' "$metadata")
}
