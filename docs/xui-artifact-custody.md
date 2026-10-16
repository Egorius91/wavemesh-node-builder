# 3X-UI executable artifact custody

The root installer extracts TAR releases without restoring archive UID/GID or
privileged permission behavior. TAR, ZIP and direct extracted input then use the
same install boundary: the copied tree (including its root directory) becomes
numeric `0:0` and loses group/other write permission. Normal binary execution bits
are retained; immediate files in `bin`, including symlinks resolving inside the
installed tree, still receive execution bits. An internally symlinked `bin`
directory is supported as well.
Release symlinks are preserved, but ownership/permission normalization does not
follow them to change external targets.

Release selection, download URLs/tags, service configuration and the existing
active-install shortcut are unchanged. This source change does not repair an
already installed/running tree and does not add transactional stop/rollback.

Focused native proof: `sudo bash tests/unit/test_xui_artifact_custody.sh`.
It needs Linux, GNU tar/coreutils, Python 3, unzip and UID 0 with permission to
assign synthetic UID/GID 1001. It uses only a fresh temporary fixture and never
executes the installer or a release binary. Exit 77 means NOT_RUN. The existing
native ownership/PKI step in `.github/workflows/tests.yml` is the minimal CI
integration point; this patch does not modify or dispatch CI.
