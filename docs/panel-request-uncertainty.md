# Local panel request uncertainty barrier

The Agent Python transport and Node Builder shell transport share a private,
durable journal. If the panel receives a mutation but its response is lost,
another local mutation must not be sent just because the process or flock ended.
The shell transaction engine also checks this journal before beginning work or
restoring snapshots, and Agent source rollback checks it before restoring files.

## Request contract

`agent/panel_request_guard.py` is used by `PanelClient.call` and
`wm_xui_request`. POST mutations are allow-listed for the existing client,
inbound, settings/API-token and Xray template operations. GET/HEAD and the
explicit existing read-only POST endpoints remain available for diagnosis.
Authentication login/CSRF requests remain outside this mutation protocol.
The endpoint classifications require verification against the installed panel
version before rollout; the upstream `latest` label is not a version contract.

For a mutation, the transport takes a nonblocking journal flock, validates the
prior record, and persists `DISPATCH_INTENT` before making the network request.
The file and directory entries, including directory ancestry during initialization,
are fsynced. Only a bounded, unambiguous JSON object with `success: true` from a
successful HTTP call permits recording `RESPONSE_ACCEPTED`.

Timeouts, rejected/malformed responses and process death leave intent pending.
Pending or malformed state rejects subsequent mutations before dispatch. A
failed completion attempts to restore the durable intent; persistent storage
failure requires maintenance and cannot be treated as a reliable journal.
There is no timeout-based release, automatic replay or force-clear command.

`RESPONSE_ACCEPTED` is only a transport observation. It does not prove that
background panel work has drained, that Xray applied the configuration, or that
a user can connect. A crash after recording acceptance but before delivering
the result still requires the caller's idempotency/reconciliation protocol.
This module alone does not establish exactly-once provisioning or billing.

## Storage and rollback

The canonical root is `/var/lib/wavemesh-agent/panel-requests` (0700); its lock
and state files are owned by the executor and private (0600). Symlinks,
hard-linked/special state files, unknown schemas and duplicate JSON keys fail
closed. The record contains a random attempt ID and salted request digest, not
URLs, client identifiers, request bodies, credentials or provider error text.

`WAVEMESH_PANEL_REQUEST_STATE_DIR` is a local process override for isolated test
roots. All cooperating transports must use the same root. Node command payloads
cannot select it. Production override drift is not automatically detected across
independent processes and must be excluded by the deployment contract. The
Agent rollback utility requires the canonical deployment-root journal.

The installer includes the guard module in change detection, backups and source
installation. The journal is never restored from a source backup. Agent rollback
holds the Node lock and journal lock for its entire operation. With journal
history present, it rejects targets without panel request protection. Snapshot
rollback in the CLI rejects pending requests before restoring DB/configuration
or changing services. CLI callers must retain the existing Node mutation lock.

Do not delete or edit live pending state to unblock a request. Preserve it and
the associated access/transaction state for backend-specific reconciliation.
A supported release protocol still needs evidence that the old request cannot
complete later, desired/observed state comparison and an explicit durable
recovery decision. Until implemented and accepted, unresolved requests require
maintenance; this source slice is not ready for unattended commercial rollout.

## Coverage and remaining authority work

Synthetic Linux tests exercise Python/shell coordination, concurrent processes,
crash after dispatch intent, lost responses, malformed/unsafe files, fsync
failures, normal success and rollback refusal with state preservation. Windows
skips POSIX cases and cannot establish Linux runtime acceptance.

Older deployed binaries, remote SaaS/Bot HTTP writers, direct panel UI/DB edits
and privileged operators do not participate automatically. Exclusive Agent
authority, credential/listener migration, complete ownership exclusion,
backend drain, backup/restore fencing and executable quarantine remain separate
requirements. Website and Bot purchase/renewal must migrate to working SaaS
desired-state commands before those legacy writers are retired.

No feature gate, deployed Agent, panel client or runtime configuration is changed
by publishing this source. Staging acceptance must additionally cover the exact
panel binary, existing accesses, real VPN traffic, restart/crash recovery and
rollback under the final authority/recovery protocol.
