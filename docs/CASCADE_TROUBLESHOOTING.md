# Cascade VPN troubleshooting

Start with read-only checks. Do not print `config.json`, manifests, subscription files, API responses, UUIDs, tokens, or complete nginx configuration into shared logs.

## Safe initial collection

```bash
sudo systemctl is-active x-ui nginx
sudo wavemesh cascade status --json
sudo wavemesh cascade health --json
sudo wavemesh reconcile --dry-run
sudo wavemesh transaction list --json
sudo journalctl -u x-ui -n 100 --no-pager
sudo nginx -t
```

If a transaction is `in_progress`, `recovering`, or `rollback_failed`, do not start another mutation. Follow [Rollback recovery](#rollback-recovery).

## TLS error

Symptoms: manifest import rejects TLS, client reports certificate failure, or node health reports `tls: invalid`.

Checks:

```bash
getent ahosts exit.example.com
openssl s_client -connect exit.example.com:443 -servername exit.example.com </dev/null 2>/dev/null |
  openssl x509 -noout -subject -issuer -dates
sudo nginx -t
sudo systemctl status nginx --no-pager
```

Confirm that the manifest domain, `sni`, and `host` are the same public Exit domain and that the certificate is currently valid for it. Repair DNS/certificate issuance; do not set `allowInsecure` or replace the domain with a raw IP.

## DNS mismatch

Symptoms: `add-exit` reports that Exit DNS does not match expected public IPs.

```bash
getent ahostsv4 exit.example.com
getent ahostsv6 exit.example.com
```

Compare results with the Exit provider address used when the manifest was created. Remove stale A/AAAA records, wait for authoritative DNS propagation, and create a fresh manifest only if the Exit address genuinely changed. Do not use `--allow-private-target` for a public production Exit.

## testOutbound timeout

Symptoms: Exit import or health cannot complete the outbound probe.

Health requires the inner TCP probe result returned in `obj.success`; an HTTP
200 response with a top-level `success=true` only means that 3X-UI processed
the API request. It does not prove that the Exit endpoint is reachable.

On the Entry:

```bash
sudo wavemesh cascade health --json
getent ahosts exit.example.com
curl -sS --connect-timeout 5 -o /dev/null -w 'https=%{http_code}\n' https://exit.example.com/
sudo journalctl -u x-ui -n 100 --no-pager
```

On the Exit:

```bash
sudo systemctl is-active x-ui nginx
sudo nginx -t
sudo ss -ltnp
```

Check provider egress filtering, Exit port 443, nginx, Xray, TLS, and the relay peer. A working website alone does not prove the relay path works.

## routeTest mismatch

Symptoms: `testOutbound` succeeds but `routeTest` chooses another outbound.

```bash
sudo wavemesh cascade health --json
sudo wavemesh reconcile --dry-run
```

The expected mapping is `wm-route-<exit-id>` to `wm-exit-<exit-id>` through `wm-rule-<exit-id>`. A catch-all rule must remain after managed route rules. If the dry-run reports managed Xray drift, apply:

```bash
sudo wavemesh reconcile --apply
sudo wavemesh cascade health --json
```

Do not delete unrelated unmanaged Xray objects.

## Xray restart loop

```bash
sudo systemctl status x-ui --no-pager
sudo journalctl -u x-ui -n 200 --no-pager
sudo wavemesh transaction list --json
```

If a transaction is incomplete, recover it before reconciliation. Otherwise inspect the first Xray configuration error, restore the last transaction snapshot if appropriate, and verify the 3X-UI template API is readable. Repeated process restarts can interrupt active connections; stop the loop before further changes.

## nginx subscription 404

Check the managed include and public file permissions without printing the secret path or file contents:

```bash
sudo nginx -t
sudo test -f /etc/nginx/wavemesh-managed-locations.conf && echo managed_file=present
sudo nginx -T 2>/dev/null | grep -Fq /etc/nginx/wavemesh-managed-locations.conf && echo managed_include=present
sudo stat -c 'mode=%a path=%n' /var/www/wavemesh-sub /var/www/wavemesh-sub/users
sudo find /var/www/wavemesh-sub/users -maxdepth 1 -type f -name '*.txt' -printf 'mode=%m file=present\n'
```

Directories must be traversable by nginx (`0755`) and public subscription files must be readable (`0644`). Repair managed state transactionally:

```bash
sudo wavemesh subscription rebuild
sudo wavemesh subscription validate
```

If a failed recovery blocks rebuild, do not edit `result.json` casually. Preserve the failed transaction and determine whether rollback restored an already-drifted baseline or failed to restore a snapshot.

## nginx route 502

A 502 on a route path means nginx matched the public location but could not reach the loopback inbound.

```bash
sudo wavemesh cascade health --json
sudo wavemesh reconcile --dry-run
sudo ss -ltnp
sudo journalctl -u x-ui -n 100 --no-pager
```

Check that the managed inbound exists, is enabled, and listens on its desired `127.0.0.1` port. Apply reconciliation only when the dry-run identifies managed inbound or nginx drift.

## Profile connects but external IP remains RU

First confirm the selected client profile name. Then check route selection:

```bash
sudo wavemesh cascade health --json
sudo wavemesh cascade verify-e2e --json
```

The route must be `healthy` and `routeTest` must report its own `wm-exit-*` outbound. If that passes, test the external IP again while only that profile is active. A direct/catch-all rule before the managed route, stale client subscription, or selecting the legacy direct profile can leave traffic on the Entry.

## Profile missing from subscription

```bash
sudo wavemesh route list --json
sudo wavemesh subscription validate
sudo wavemesh reconcile --dry-run
```

The route, Exit, client, and client route credential must all be enabled. Rebuild after reviewing drift:

```bash
sudo wavemesh subscription rebuild
sudo wavemesh subscription validate
```

Refresh the existing subscription in the client; adding a second subscription URL is not required.

## Duplicate inbound or managed tag

Symptoms: import reports a conflict, or health reports inbound drift.

```bash
sudo wavemesh cascade status --json
sudo wavemesh reconcile --dry-run
sudo wavemesh transaction list --json
```

Managed tags use `wm-route-*`, `wm-exit-*`, `wm-rule-*`, and `wm-relay-*`. Do not rename or delete an unmanaged object just to make an import pass. If an earlier interrupted operation owns the managed object, recover its transaction first. A conflicting existing Exit id with different credentials requires explicit replacement/rotation.

## API 401, 403, or CSRF failure

Normal WaveMesh runtime calls use the root-readable Bearer token. Cookie/CSRF is used only to bootstrap a token.

```bash
sudo systemctl is-active x-ui
sudo wavemesh cascade health --json
sudo journalctl -u x-ui -n 100 --no-pager
sudo stat -c 'mode=%a path=%n' /etc/wavemesh-node/config.json
```

Confirm the panel is reachable only on loopback and the token has not been revoked during a 3X-UI upgrade. Do not print the token and do not silently fall back to SQLite writes. Restore verified API state from a node backup or rerun the supported 3X-UI bootstrap/upgrade procedure.

A single `HTTP 000` immediately after `x-ui` restart can be transient. It is acceptable only when the retry succeeds and the final transaction/health result is successful.

## Exit allowlist rejects the Entry

The manifest may restrict the relay peer to the Entry public source IP. Check the actual Entry egress address and provider NAT:

```bash
curl -4 -sS https://api.ipify.org; echo
```

If it differs from the address used with `--entry-ip`, remove the unused peer on the Exit, create a new manifest with the correct stable Entry address, transfer it securely, and import it. Do not remove the allowlist merely to hide an addressing problem.

## Rollback recovery

List transactions without opening snapshot contents:

```bash
sudo wavemesh transaction list --json
```

Recover the exact id reported by the blocked mutation:

```bash
sudo wavemesh transaction recover --id TRANSACTION_ID
sudo wavemesh subscription validate
sudo wavemesh cascade health --json
```

Or recover the newest incomplete transaction:

```bash
sudo wavemesh transaction recover --latest
```

Expected terminal state is `rolled_back`. `rollback_failed` means an operator must inspect the failed component while leaving the mutation lockout intact. Never mark a transaction terminal merely to bypass the lock unless the snapshots were demonstrably restored and the remaining issue is separately repaired and validated.

## Safe evidence to share

Share only:

- service active/inactive state;
- redacted `cascade status`, `health`, `verify-e2e`, and transaction JSON;
- nginx syntax result;
- route ids, display names, managed outbound tags, HTTP status codes, and timestamps.

Do not share manifests, complete config/runtime snapshots, subscription paths or contents, UUIDs, panel credentials, Bearer tokens, certificates' private keys, or transaction backup files.
