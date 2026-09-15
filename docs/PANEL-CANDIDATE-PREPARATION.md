# Private candidate preparation

`agent/panel_candidate.py` provides the internal preparation prerequisite for
transactional installation. It does not execute the candidate, replace the live
panel tree or database, alter systemd/network policy, or open admission.

The caller must supply the exact Builder commit and a manifest SHA-256 obtained
through a separate trusted approval path. Deriving the expected digest from an
untrusted download does not authenticate it. This module is a byte/inventory
verifier, not a signature service, commercial authorization or release approval.
The full CI producer already verifies the pinned upstream dependencies, source
patch, GPL corresponding source, static build and real runtime behavior. A future
promotion controller must bind that evidence to the exact manifest and operation.

## Admission and files

Preparation requires Linux root, the canonical journal, a matching HELD operation
and generation, no unresolved request and both Node/journal locks. It creates a
private operation directory next to the fixed `/usr/local/x-ui` tree so future
replacement can use the same filesystem. A mounted live tree on another device
is rejected. Files are 0600, directories 0700; executable modes are retained only
in the verified manifest. The live tree is unchanged.

The manifest must identify linux-amd64, the exact Builder commit/version and the
current fixed 17-member panel layout. It binds archive and corresponding-source
checksums, bounded sizes, types, modes and per-file checksums. Input paths require
root ownership, safe ancestors and regular single-link files. Read bytes are
pinned before extraction, avoiding a second read of a changed source pathname.

Extraction reads fixed 512-byte tar headers before payloads. Only the exact
regular-file/directory inventory is accepted. It never uses generic tar
extraction, ownership restoration or link handling. Links, PAX/GNU extensions,
sparse entries, duplicates, traversal, oversized entries, inconsistent checksums
and nonzero trailing content are rejected. Source archive bytes are retained
unchanged and hash-checked; source review/provenance remains the producer and
approval path's responsibility.

An operation/source/manifest intent is durably written before extraction. Every
file and directory is synced before `ready.json` is atomically published and
read back. Interrupted directories or `ready.pending` remain untouched; they are
not deleted or re-extracted automatically. A published result with a lost final
sync/result can be verified and synced again. Re-entry needs no incoming archive
and verifies the complete prepared tree and saved archives. Existing installation
intent must still bind the same candidate archive. All material stays private.

## Evidence and remaining installation work

Dedicated Linux tests cover trusted digest/commit mismatch, unsafe paths/types,
both locks, missing admission, source/payload corruption, process death, failed
publication/sync and replay without extraction. The full packaged panel/Xray CI
prepares the actual built bundle, verifies its panel digest against the running
image, and uses the prepared archive identity for snapshot, isolation, stop and
recovery. It verifies the staged payload again under the stopped-service locks.
The fixture does not replace the running image with the private prepared tree.

Atomic live-tree replacement, current SaaS ownership/revocation reconciliation,
safe restored-runtime exposure, rollback and admission release remain required.
Preparing valid bytes alone does not establish any of these properties. Neither
source/CI success nor a trusted manifest substitutes for staging/production and
the complete commercial VPN lifecycle acceptance.
