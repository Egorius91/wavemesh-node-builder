# Private prepared journal: one historical reader

The authoritative pure stdlib reader is scripts/lib/prepared_journal.py.
`prepared_status(transaction: Path) -> None | "active" | "committed"` performs
bounded private-file reads only. It does not collect execution, repair/fsync,
write, dispatch, publish health, or grant capability. The Agent owner can install
these same frozen bytes; do not implement another interpretation of its schema.

None means ordinary journal handling remains applicable. Any pending presence
(including dangling symlink) is active. Completed presence with malformed/missing
proof is active; a result carrying prepared_commit without retained origin is
also active. Committed requires the exact version1 hash-bound completed intent,
plan, ordinary owner-result receipt, four private candidate artifacts, matching
selector transition and actual local runtime postimage. It is historical audit,
not current execution/readiness. Generic mark cannot erase this provenance;
idempotent committed does not rewrite it. Snapshot rollback always refuses any
prepared origin, including a proof with missing markers, via the read-only guard.

Frozen intent has version/transaction/route/binding; expected generation, epoch,
engine instance, before/after/normalized native hashes and owned rule/inbound/
target; admitted absolute storage paths; local preimage hashes (runtime absence
is distinct); and hashes of existing private candidate files. Its full copy in
result contains hashes/context, never credential/config payload. Candidate and
runtime postimage files stay private in the same transaction directory.

The ordinary private owner reply binds version, transaction id and raw intent
hash to exact operation-local Go result flags. Only a future trusted owner
adapter may retain it. A caller verified=true/force CLI is not provided. Lost,
partial, foreign, no-op or unproven writer replies cannot finish existing debt.
Fresh exact execution collection and separate template/process readbacks still
belong to the completion writer. No such transport/caller is installed here.

Pre-apply prepared samples retain their original generation/epoch/full hash.
The merged runtime postimage may preserve changed unrelated Root fields, but its
owned state must equal the frozen pre-apply candidate; a new epoch must obtain
its own fresh sample. Native no-op cannot manufacture a committed transition.

Pure visibility cannot prove that a prior directory barrier succeeded. Existing
mutating begin/prune/idempotent completion therefore repeat directory fsync under
the existing node flock before proceeding; a failed barrier stops the action.
The Agent owner must do the same at its mutation boundary. No new lock/store is
introduced. Shared-runtime writes remain closed until ROOT's health writer lock
and unresolved-journal fence are accepted. This reader alone performs no runtime
write and does not authorize publication/live/6004/physical deny.

The portable tests prove structure, origin retention, foreign/stale/duplicate/
corrupt refusal, metadata-only idempotency and barrier failure propagation.
Actual Linux fsync/flock/Bash execution is a separate native packet. Current
producer remains d8eea105; full producer/core/service integration is NOT RUN.

The mutation-only API is transaction_state.admit(root), exposed privately as
`admit --root ROOT` for existing lock-owning health callers. begin uses the same
function. Read-only check/list do not repair barriers. The intent copy is itself
type-validated before comparison (Python true must not compare as schema1).
