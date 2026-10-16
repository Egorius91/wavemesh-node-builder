#!/usr/bin/env bash
set -Eeuo pipefail

# Run explicitly with sudo on Linux. Never execute the installer or a binary.
# Exit 77 means that native ownership proof was NOT_RUN, not PASS.
[[ "$(uname -s)" == Linux && "$(id -u)" == 0 ]] || {
  echo "NOT_RUN: Linux UID 0 and real POSIX ownership/modes required" >&2
  exit 77
}
for tool in tar cp chown chmod stat find readlink cmp python3 unzip; do
  command -v "$tool" >/dev/null || { echo "NOT_RUN: missing $tool" >&2; exit 77; }
done
tar --version | grep -q 'GNU tar' || exit 77
cp --version | grep -q 'GNU coreutils' || exit 77
umask 0022

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
source "$ROOT_DIR/scripts/06_3xui.sh"
wm_fail() { echo "$*" >&2; exit 1; }

tmp="$(mktemp -d "${TMPDIR:-/tmp}/wavemesh-xui-custody.XXXXXXXX")"
trap 'rm -rf -- "$tmp"' EXIT
mkdir -p "$tmp/source/x-ui/bin" "$tmp/source/x-ui/lib"
printf 'synthetic executable, never run\n' > "$tmp/source/x-ui/x-ui"
printf 'synthetic xray, never run\n' > "$tmp/source/x-ui/bin/xray"
printf 'release data\n' > "$tmp/source/x-ui/data"
printf 'internal linked binary, never run\n' > "$tmp/source/x-ui/lib/xray-linked"
chmod 0644 "$tmp/source/x-ui/lib/xray-linked"
ln -s ../lib/xray-linked "$tmp/source/x-ui/bin/internal-link"
chmod 0777 "$tmp/source/x-ui" "$tmp/source/x-ui/x-ui"
chmod 0775 "$tmp/source/x-ui/bin"
chmod 0666 "$tmp/source/x-ui/bin/xray"
chmod 0644 "$tmp/source/x-ui/data"
if ! chown -R 1001:1001 "$tmp/source"; then
  echo "NOT_RUN: cannot set synthetic foreign ownership" >&2
  exit 77
fi
tar -czf "$tmp/release.tar.gz" -C "$tmp/source" x-ui

# Negative control: reproduce the old root extraction/copy behavior with real tools.
mkdir "$tmp/legacy-extract" "$tmp/legacy-home"
tar -xzf "$tmp/release.tar.gz" -C "$tmp/legacy-extract"
cp -a "$tmp/legacy-extract/x-ui"/. "$tmp/legacy-home"/
[[ "$(stat -c '%u:%g:%a' "$tmp/legacy-home")" == 1001:1001:777 ]]
[[ "$(stat -c '%u:%g:%a' "$tmp/legacy-home/x-ui")" == 1001:1001:777 ]]
echo 'PASS: old behavior imports foreign ownership and writable modes'

# Exercise the production extraction helper, including its source-root metadata.
(umask 0027; wm_extract_xui_release_archive "$tmp/release.tar.gz" release.tar.gz "$tmp/extract")
[[ "$(stat -c '%u:%g:%a' "$tmp/extract/x-ui")" == 0:0:750 ]]
[[ "$(stat -c '%u:%g:%a' "$tmp/extract/x-ui/x-ui")" == 0:0:750 ]]
echo 'PASS: tar extraction does not restore foreign ownership or permissions'

assert_tree() {
  local path metadata mode
  while IFS= read -r -d '' path; do
    metadata="$(stat -c '%u:%g' "$path")"
    [[ "$metadata" == 0:0 ]] || wm_fail "foreign installed owner: $path"
    if [[ ! -L "$path" ]]; then
      mode="$(stat -c '%a' "$path")"
      (( (8#$mode & 0022) == 0 )) || wm_fail "writable installed path: $path"
    fi
  done < <(find "$XUI_HOME" -print0)
  [[ -x "$XUI_RUNTIME_BIN" && -x "$XUI_HOME/bin/xray" ]]
  cmp "$tmp/source/x-ui/x-ui" "$XUI_RUNTIME_BIN"
  cmp "$tmp/source/x-ui/data" "$XUI_HOME/data"
}

# Foreign direct input covers callers independent of TAR/ZIP extraction.
# A setgid destination parent and permissive umask must not leak a group/mode.
mkdir "$tmp/install-parent"
chown 0:1001 "$tmp/install-parent"
chmod 2777 "$tmp/install-parent"
printf 'external sentinel\n' > "$tmp/sentinel"
chown 1001:1001 "$tmp/sentinel"
chmod 0666 "$tmp/sentinel"
ln -s "$tmp/sentinel" "$tmp/source/x-ui/bin/external-link"
ln -s data "$tmp/source/x-ui/data-link"
sentinel_before="$(stat -c '%u:%g:%a' "$tmp/sentinel")"
XUI_HOME="$tmp/install-parent/x-ui"
XUI_RUNTIME_BIN="$XUI_HOME/x-ui"
(umask 0000; wm_install_xui_release_tree "$tmp/source/x-ui")
assert_tree
[[ "$(stat -c '%a' "$XUI_HOME")" == 755 ]]
[[ "$(stat -c '%a' "$XUI_RUNTIME_BIN")" == 755 ]]
[[ -x "$XUI_HOME/bin/internal-link" ]]
[[ "$(readlink "$XUI_HOME/bin/internal-link")" == ../lib/xray-linked ]]
[[ "$(readlink "$XUI_HOME/data-link")" == data ]]
[[ "$(stat -c '%u:%g:%a' "$tmp/sentinel")" == "$sentinel_before" ]]
[[ "$(stat -c '%u:%g:%a' "$tmp/source/x-ui")" == 1001:1001:777 ]]
[[ "$(stat -c '%u:%g:%a' "$tmp/source/x-ui/x-ui")" == 1001:1001:777 ]]
echo 'PASS: direct copy normalizes real metadata and preserves content/layout/source/external sentinel'

cp -a "$tmp/source/x-ui" "$tmp/bin-link-source"
rm -rf "$tmp/bin-link-source/bin"
ln -s lib "$tmp/bin-link-source/bin"
wm_install_xui_release_tree "$tmp/bin-link-source"
[[ -x "$XUI_HOME/bin/xray-linked" ]]
[[ "$(readlink "$XUI_HOME/bin")" == lib ]]
echo 'PASS: internal binary and bin-directory symlinks retain executable behavior'

mkdir "$tmp/external-bin"
printf 'external binary sentinel\n' > "$tmp/external-bin/xray"
chown -R 1001:1001 "$tmp/external-bin"
chmod 0666 "$tmp/external-bin/xray"
rm "$tmp/bin-link-source/bin"
ln -s "$tmp/external-bin" "$tmp/bin-link-source/bin"
wm_install_xui_release_tree "$tmp/bin-link-source"
[[ "$(stat -c '%u:%g:%a' "$tmp/external-bin/xray")" == 1001:1001:666 ]]
echo 'PASS: external bin-directory symlink target is unchanged'

# ZIP reaches the same install boundary; a normal release stays executable/readable.
python3 - "$tmp/release.zip" <<'PY'
import sys
import zipfile
with zipfile.ZipFile(sys.argv[1], "w") as z:
    for name, mode, body in [
        ("x-ui/x-ui", 0o100755, b"zip executable, never run\n"),
        ("x-ui/bin/xray", 0o100644, b"zip xray, never run\n"),
        ("x-ui/data", 0o100644, b"zip data\n"),
    ]:
        info = zipfile.ZipInfo(name)
        info.create_system = 3
        info.external_attr = mode << 16
        z.writestr(info, body)
PY
wm_extract_xui_release_archive "$tmp/release.zip" release.zip "$tmp/zip-extract"
wm_install_xui_release_tree "$tmp/zip-extract/x-ui"
[[ "$(stat -c '%u:%g:%a' "$XUI_RUNTIME_BIN")" == 0:0:755 ]]
[[ "$(stat -c '%u:%g:%a' "$XUI_HOME/bin/xray")" == 0:0:755 ]]
[[ "$(stat -c '%u:%g:%a' "$XUI_HOME/data")" == 0:0:644 ]]
cmp "$tmp/zip-extract/x-ui/x-ui" "$XUI_RUNTIME_BIN"
echo 'PASS: ordinary ZIP layout/content and executable behavior preserved'

# A failed ownership operation must stop before any success can be reported.
if (chown() { return 1; }; wm_install_xui_release_tree "$tmp/source/x-ui"; touch "$tmp/continued"); then
  wm_fail 'ownership failure was ignored'
fi
[[ ! -e "$tmp/continued" ]]
echo 'PASS: ownership failure stops installation'
echo 'xui artifact custody native proof: PASS'
