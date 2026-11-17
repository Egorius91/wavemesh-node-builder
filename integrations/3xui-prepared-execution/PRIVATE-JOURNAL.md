# Private prepared journal: one historical reader and isolated writer

Authoritative stdlib-only reader: scripts/lib/prepared_journal.py.
prepared_status(transaction: Path) -> None | "active" | "committed". It performs
bounded private-file reads only: no collect, write, fsync, capability or readiness.
The Agent installs these same frozen bytes, never another schema implementation.

None preserves ordinary handling. Any pending presence, including broken link,
is active. Completed with malformed/missing/foreign proof is active. Result proof
without retained origin is active. Generic mark cannot erase prepared_commit;
idempotent committed does not rewrite it. Snapshot rollback refuses every origin.
Mutation-only transaction_state.admit(root), CLI admit --root ROOT, repeats
verified directory barriers under the caller's existing node flock. begin reuses
it; read-only check/list remain pure. Agent owns its separate barrier integration.

Private unshipped version1 intent records transaction/route/binding, exact native
before generation/epoch/instance/hash, expected full post and normalized hash,
owned rule/inbound/target, admitted storage paths, preimage hashes and THREE
frozen client candidate hashes (config/runtime/template). Process disk preimage
is nullable, audit-only; managed Start may bypass that file entirely. It is never
an assertion about desired process memory. Do not reconstruct desired memory
from consumed native JSON or an older disk file.

The trusted future owner adapter binds an ordinary private reply to the same
transaction and raw frozen intent hash. Its outcome contains the six existing
Go operation flags plus ProcessConfigSHA256. A known result needs all success
flags, NoOp false and a strict lowercase64-hex process hash. The Go owner returns
that hash from the SAME MarshalIndent payload only after atomic file/directory
barriers and memory adoption. Failure returns no successful hash. No handler,
wire, endpoint, token privilege, server ledger or force/verified CLI is added.

Independent post-reply file read must match the owner raw hash and owned selector.
Exact bytes are retained in prepared.process.post.json, never remarshal/copied
into the template or running process. Three candidates remain frozen; the process
is a server-owned POSTIMAGE, not a pre-dispatch client candidate. Separate stored
template readback uses a read-only SQLite connection against the admitted local
source. Backend/path admission belongs to the trusted future caller.

Private complete(transaction, owner_result=None) is uninstalled source only.
It requires the existing Builder FD9 owner; retains the exact reply before fresh
collection/local commit; checks actual collector epoch+1/full hashes/receipt and
approved routing; checks storage independently before/after; writes only local
reconcile config/runtime. Config uses exact validated frozen candidate bytes.
Runtime merges only prepared_routes[route_id] into current Root fields; a third
owned state/config stops. Unrelated Root fields are preserved. Pre-apply samples
keep their original epoch/instance/hash; they never become fresh post-apply health.
Lost/partial/no-op/unproven replies cannot complete earlier debt.

Durable verified result includes full HASH-ONLY intent copy, reply hash, exact
selector receipt and local post hashes. Then the SAME pending intent is renamed
to completed and directory-synced. Failures retain origin. Retry performs only
metadata/readback; no dispatch, restart, restore or process-file rewrite. A known
result prefix is type-safe and cannot be overwritten by generic status handling.
Private runtime postimage preserves the exact merged Root document for audit.

Historical5ff portable58 plus ordinary journal script passed. ROOT942 later
accepted exact e1 Linux15 completion+4 debt+Bash and reader7/Agent1, without
skips. These prove native FD9/flock/directory/symlink filesystem behavior with
mocked collector/owner, not actual service/core/caller. ROOT955 orphan send/reply
guard adds focused Windows2/Linux2 PASS, preserving valid completed history.
Current ROOT943 adds the private unwired sender and SELECT-only exact raw
template helper; new client8 and metadata compatibility2 pass on Windows.
PRIVATE-OWNER-API.md records the protocol and remaining current API native gate.
Source0d0b processHash5 Windows/CGO tests remain a separate historical scope.
Root's health/Agent integration and grouped native packet must be accepted before
any activation. Full producer/main,6004,physical flows/watchdog,capability and
public/live/Node admission remain open. No publication or live action occurs here.
