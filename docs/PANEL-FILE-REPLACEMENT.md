# Stopped panel file transaction

`agent/panel_replace.py` supplies an internal file replacement/rollback stage.
The caller must already hold the operation-bound admission state, verified
candidate, sealed rollback snapshot, exact API isolation and drained panel
service in the same boot. This module observes those prerequisites under both
Node and journal locks; it does not stop/start services, restore a database or
release admission. It has no command-line or arbitrary-command interface.

## Durable exchange

The current panel inventory must still equal the sealed snapshot. Before making
an activation copy, journal version 6 records `PREPARE_INTENT`. This prevents the
older recovery-start path from bypassing an interrupted file transaction.
Prepared candidate and snapshot remain immutable; only a private activation copy
receives the validated artifact modes. A durable receipt binds operation,
generation, candidate/archive/manifest/Builder identity, stopped-service contract
and both trees' device, inode, mode and content inventory hashes.

After durable `REPLACE_INTENT`, one Linux `renameat2(RENAME_EXCHANGE)` exchanges
the existing directories. There is no two-rename fallback on unsupported
filesystems. The former live tree remains in the private transaction directory.
Both parents are synced, the exact inode/content orientation is checked and the
stopped-service/isolation proof is repeated before recording `REPLACED`.

Re-entry never repeats an uncertain exchange. It compares both trees to the
receipt and independently verified candidate/snapshot. A completed exchange with
a lost response or sync result can be reconciled. An intent whose forward
exchange did not take effect requires explicit rollback; that operation can
accept the original pair without any exchange. Incomplete preparation remains
blocked and retained for separate reconciliation.

## File rollback and startup boundary

Rollback saves `ROLLBACK_INTENT` before exchanging a verified candidate/original
pair back. Its lost result is reconciled in the same way, without redispatch.
If rollback did not take effect, replay remains blocked rather than repeating
the syscall. Corruption, changed identity, a new boot or unproven orientation
also retain the interlock. `ROLLED_BACK` means original file inode, bytes and
modes were restored; it does not mean a working or commercially safe service.

All version-6 phases continue to deny ordinary startup, the older recovery-start
API, maintenance cancellation and Agent/CLI writes. No database is restored or
edited by this stage. Startup after replacement/rollback still needs a distinct
verified transition, current SaaS ownership/revocation reconciliation, safe
runtime exposure and eventual admission release. API isolation alone is not a
VPN data-plane fence. These remain required for usable Entry/Exit replacement
and expansion and the full commercial service.

## Evidence scope

Root Linux tests exercise actual directory exchange, restoration of different
fixture bytes/modes/inodes, both locks, process death, lost responses in both
directions, sync failure, partial preparation, corrupt receipts/trees, changed
boot and startup denial. The full packaged panel/Xray workflow runs an additional
disposable systemd scenario: drain real panel/Xray processes, exchange and restore
the actual prepared tree after injected lost results, preserve DB bytes, and
prove startup and writers remain denied. A sentinel distinguishes the original
tree from the prepared tree. Both contain the same built panel executable; this
does not prove compatibility between different production panel versions.

The existing recovery/VLESS scenario runs separately. Its success does not prove
startup after a file transaction. Sanitized reports bind the exact Builder head
and say deployment `NONE`. Tests cover process crashes and injected sync faults,
not power-loss durability on every filesystem. Source/CI evidence is not staging
or production acceptance.
