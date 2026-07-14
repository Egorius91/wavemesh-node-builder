# Phase 11.4 — Auto Route publication

Status: implemented, pending CI and live verification.

## Commands

```bash
wavemesh cascade auto publish [--id auto-europe]
wavemesh cascade auto unpublish [--id auto-europe]
```

Publication is transactional. It updates desired state, regenerates all subscriptions, applies nginx, validates public subscription output, then commits `config.json`.

A published Auto Route contributes one additional VLESS/XHTTP profile per enabled client. The profile uses only the Entry domain, TLS SNI/Host, the Auto public path, and the client-specific Auto credential.

Exit domains, Exit IPs, relay UUIDs, relay paths, loopback ports, and internal local ports remain forbidden in generated subscriptions.

Unpublishing removes only the Auto nginx location and Auto subscription profile. Manual cascade profiles remain available.

A disabled Auto Route cannot be published. A published Auto Route must be unpublished before it can be disabled.

## Live verification

After merge and installation on the Entry node:

```bash
set -Eeuo pipefail
cd ~/wavemesh-node-builder
git checkout main
git pull origin main
sudo install -m 755 bin/wavemesh /usr/local/bin/wavemesh
sudo rm -rf /usr/local/lib/wavemesh
sudo mkdir -p /usr/local/lib/wavemesh
sudo cp -a scripts/. /usr/local/lib/wavemesh/

wavemesh 2>&1 | grep -A14 'cascade auto'
sudo wavemesh cascade auto health --json
sudo wavemesh cascade auto publish
sudo wavemesh cascade auto status --json
sudo wavemesh subscription validate
sudo wavemesh cascade health
sudo wavemesh cascade auto health --json
```

Verify that each client subscription now contains the Auto profile and still contains the manual profiles. Then test the Auto profile in a real client and confirm its public IP matches one of the configured Exit nodes, never the Entry node.

Rollback test:

```bash
sudo wavemesh cascade auto unpublish
sudo wavemesh cascade auto status --json
sudo wavemesh subscription validate
sudo wavemesh cascade health
sudo wavemesh cascade auto health --json
```
