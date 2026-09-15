# Operation-bound panel rollback snapshot

`agent/panel_backup.py` supplies the missing backup prerequisite for the internal
installation/stop/recovery controllers. It has no CLI or remote command and is
not wired into the live installer. The fixed production source paths are
`/usr/local/x-ui` and `/etc/x-ui/x-ui.db`; only isolated tests relocate them.

`prepare` requires Linux root, the canonical journal, the matching HELD operation
and generation, and no unresolved panel request. It owns both the Node lock and
the journal lock through capture and verification. It never clears HELD or
changes the journal's installation state.

## Capture and durable identity

The operation directory is exclusively created beneath the private journal. An
intent identifies the operation, generation, candidate archive digest and source
paths, with a random nonce. Copies contain the complete validated panel tree,
including private generated configuration. Original file modes are recorded;
stored files are 0600 and directories 0700. Root ownership, safe ancestor modes,
regular files, unique links, bounded sizes and names are required. Symlinks and
special files are rejected. The source tree is compared after capture to detect
changes during copying.

SQLite's backup API captures committed WAL data into a standalone database; it
does not copy only the live main file or combine it with separately copied WAL.
The source connection is read-only, although SQLite may maintain its normal
shared-memory sidecar. Copy progress is time/size bounded. The closed snapshot
must pass integrity checking and contain application tables. Files and directory
entries are fsynced. Only then is a sealed manifest atomically published and
independently read back with all copied bytes verified.

The returned manifest SHA-256 is the rollback binding for installation intent.
On repeat calls, an existing sealed snapshot is verified and returned, never
recaptured from a newer database. Once installation intent exists, verification
also requires its persisted manifest digest. Partial directories, failed file
copies, a pending manifest and unexpected entries remain untouched and require
reconciliation. A complete manifest whose final directory sync failed can be
verified and synced on re-entry. No cleanup/reset/retry command is provided.

The database, copied configuration, intent and manifest are private recovery
material: do not upload or log them. Public CI evidence contains only allow-listed
artifact identity and pass/fail status, never client rows or snapshot contents.
Local private storage is not encryption, off-host disaster recovery or a retention
policy; these remain separate operational requirements.

## What this does not authorize

A consistent database snapshot is not an atomic snapshot of all files and DB,
proof that external writers have stopped, current SaaS authority, or permission
to restore an old database. HELD excludes cooperating Agent/CLI writes; API
packet isolation and process drain remain separate steps. Root is trusted.

Restoring an old DB can resurrect revoked access. Closing the panel API alone
does not contain VPN traffic. Before any restore/release path is made callable,
current SaaS ownership and durable revocation/tombstone reconciliation must be
enforced together with runtime exposure controls. This change neither restores
files/DB nor opens access, restarts live services or changes firewall rules.

## Evidence

The dedicated root Linux workflow exercises committed WAL capture while a writer
connection remains open, replay after a lost result, both locks, unsafe links,
changed sources, partial capture after process death, failed publication/sync,
tampering and persisted installation binding. Windows executes the portable
manifest checks and explicitly skips Linux/root scenarios.

The existing full packaged panel/Xray systemd CI now uses a real snapshot and
manifest binding instead of a placeholder. It compares private client identities
and disabled state in the snapshot, verifies the snapshot while stopped, then
continues the real VLESS and lost-start-result recovery checks. It does not
restore the snapshot. CI proves the tested disposable environment only, not
staging/production acceptance or a completed commercial service.
