# Local Node transaction-status observer

`operations/node_transaction_readonly.py` is a read-only observation component.
Its default invocation returns77 before native checks or binding/file reads.
Only `--observe-readonly` enables the fixed observation. A successful result is
`LOCAL_TRANSACTIONS_CLEAR_NOT_ADMISSION`, with aggregate counts and
`liveAdmission=false`, `rootSetCompleteness=NOT_OBSERVED` and
`operationalExclusion=NOT_OBSERVED`. No target, operation, transaction ID, path,
message or input contents are exported.

The existing repair entrypoint still returns77; UnboundAdmission still STOPs.
This component does not establish a writer fence, lease, lock, complete root set,
host authority or LIVE admission. It does not wire a repair factory, recover a
transaction, change ordinary CLI behavior or inspect config/Env/argv/backups.

## Prepared input and actual root authority

The explicit CLI takes `--target` (one of the three existing owner-repair aliases),
`--operation` (the admitted32hex operation) and `--binding-sha256`. Stdin must be
an existing root:root0600 regular single-link file, at most16KiB. Its bytes and
descriptor metadata are checked; its SHA256 must equal the external pin. The
hash fixes input identity only. A trusted native preparation must independently
establish the target/operation/namespace/root bindings and the completeness of
the actual participating roots. No preparer, private-file auto-generation or
new authority is included here. Do not derive the set from guessed CLI/Agent
defaults or accept an owner `noPending`/`closed` Boolean.

The schema1 binding has exactly these fields:

- `schema`: integer1, never bool.
- `target`, `operation`: exact admitted values matching the CLI.
- `namespace`: exactly `mount` and `root`, each `[dev, ino]`, matching the existing
  owner operator's `namespace_identity()` observation.
- `roots`: one to four objects, each exactly `path` and `chain`. The canonical
  absolute path has one to16 components (no empty/dot/dotdot/NUL; at most512 UTF8
  bytes, each component at most128). `chain` is a `[dev, ino]` pair for `/` and
  each subsequent component, including the transaction root. Pairs use bounded
  unsigned integer values; inode must be positive. The `/` pin must match the
  namespace root. Shared ancestor pins must agree. Duplicate/overlapping paths
  and duplicate root inodes are refused.

These paths and inode pairs stay inside the native process. There is no arbitrary
path discovery. Missing roots STOP: this component does not support anchored
absence because the existing layer provides no authority to declare an absent
transaction root complete. Extra binding keys, booleans as numbers, unknown
schema, mismatched target/operation and duplicate JSON keys STOP.

Run the trusted source as a normal Python script with its unchanged sibling
`xui_owner_repair.py` available, or import it as `operations.node_transaction_readonly`.
The explicit mode requires Linux and euid0 for the existing root-private
descriptor safety policy and O_NOATIME reads. No privilege is installed or
expanded. Prepared code integrity and truthful native bindings remain separate
runtime prerequisites; a successful component read does not establish them.

## Finite observation and metadata compatibility

The observer reuses `safe_root_directory`, `metadata` and `namespace_identity`
from the existing operator. It opens and retains every ancestor/root descriptor,
using O_DIRECTORY/O_NOFOLLOW/O_NONBLOCK/O_CLOEXEC/O_NOATIME. Every directory must
be root:root and not group/world writable. It compares descriptor metadata and
the anchored name at open and final verification. Root dev/ino must match the
prepared chain. Namespace is checked before opening and during final checks.

Only direct child directories of each transaction root are considered. The total
limit is128 transactions across all roots. Each child is subject to the same
safe-directory and final same-name/same-descriptor checks. Within it, only the
fixed `plan.json` and `result.json` are opened. They must be root:root0600 regular
single-link files, with O_NOFOLLOW/O_NONBLOCK/O_NOATIME and at most4096 bytes each.
Read length, before/after descriptor metadata and anchored name must agree. The
root listings are bounded and checked again with fresh directory descriptions;
all retained records and namespace are checked again before success. Changes,
symlinks, unexpected child/file types, missing/malformed files and size/count
overflow STOP. No backup, configuration or request/payload file is opened.

The current redacted producer is `scripts/lib/transaction_state.py`:
`begin()` writes schema_version, operation[:80], integer pid and started_at;
`mark()` writes status, optional finished_at and message[:160]. Backup/config
snapshots are separate files in `transaction.sh`, never part of this metadata.
With default JSON ASCII escaping, a worst supplementary Unicode codepoint takes
12 bytes. Even80 such operation codepoints and160 message codepoints yield
synthetic producer files below4KiB; focused tests execute the actual unchanged
begin/mark producers to verify this for both terminal states. The cap deliberately
refuses larger or future payload-bearing metadata rather than loading it.

To preserve the existing Agent's minimal schema1 terminal fixtures, only
`schema_version` is mandatory in plan. It must be exactly integer1. Optional
current operation/pid/started_at fields are checked for type/bounds; unknown plan
keys STOP. Result requires a string status; only current status/finished_at/message
keys are accepted, with optional fields checked for type/bounds. Only `committed`
and `rolled_back` are clear. Every ACTIVE or unknown status STOPs. The ordinary
CLI's incomplete() behavior is untouched; its exit0 is not used as admission.
Duplicate fields, invalid UTF8/JSON, nonfinite values and excessive nesting refuse.

At most452 descriptors are retained (4*17 chain plus128*3 child/files), with one
extra temporary scan descriptor. Metadata payload reads total at most1MiB, plus
EOF probes; maximum individual file4KiB. Output is under1KiB with only fixed
statuses, bounded counts and fixed booleans. A5-second monotonic deadline is
checked between operations. It is a cooperative budget, **not** a hard kernel I/O
timeout; preparation must supply the intended local filesystem. Syscalls on a
stalled filesystem cannot be forcibly interrupted by this component. No worker,
retry or new controller is added to claim a stronger guarantee.

All retained descriptors close on success/refusal. Close failure cannot erase a
primary refusal; otherwise it invalidates success. CLI errors and unexpected
exceptions produce only allowlisted STOP codes without raw error text/stderr or
traceback. No filesystem write syscall, lock write or subprocess is used.

## Validation and unproven native behavior

The focused portable suite is:

```text
python -B tests/unit/test_node_transaction_readonly.py -v
```

It exercises real redacted producers on synthetic temporary files, and a finite
stub of the new reader's exact filesystem syscalls. It covers terminal/empty,
unknown/active/malformed schemas, minimal Agent compatibility, booleans, missing
files/root, symlinks/types/hardlinks/modes, ancestor/root/child/file rebinds,
namespace/listing drift, size/count boundaries, descriptor cleanup, no payload
reads, default77 and sanitized CLI success/refusal. A legacy unknown-status RED
reproducer and the new observer's refusal demonstrate the gap without changing
the legacy CLI. Portable mocks do not prove Linux descriptor semantics.

Linux O_NOFOLLOW/O_NOATIME/dir_fd/scandir, real ownership/modes, mount namespace
binding and actual Node root completeness are NOT_RUN/NOT_PROVEN locally. The
smallest later isolated native proof needs only one root-owned private temporary
tree under a verified safe root-owned parent, the current fixed metadata files
and this observer. Prove terminal/empty clear, one unknown-status refusal, one
missing file, one symlink, one root replacement and one namespace-pin mismatch;
check file bytes/inodes/modes/mtime/ctime before/after, cleanup and zero residue.
Bound setup/reset/cleanup to that owned synthetic tree; no service, SQL, owner
repair or actual private journal is needed. Main selects any such CI step after
source review. This change adds no workflow or native fixture.
