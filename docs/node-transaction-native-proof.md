# Synthetic native proof for the local transaction observer

`tests/native/test_node_transaction_readonly.py` defaults to77 without creation.
The single required existing-job step invokes it with `--require-native` under
Linux UID/GID0. No workflow/job/dependency is added. The step has a one-minute
timeout; the existing job remains15 minutes and the SQL/owner/security steps
remain unchanged. Source and portable preparation do not count as native PASS.

The fixture pins exact accepted production observer and owner helper bytes,
checks the imported module paths, and rechecks the bytes after proof. The pins
are public source identities, not operational authority. It calls the actual
`observe`/NativeStatus implementation without mocking production syscalls. One
ordinary child invokes the production CLI with actual root-private regular stdin,
its external byte hash and test-only target/operation/root binding. The binding
uses the actual current mount/root namespace and all actual ancestors of its
synthetic transaction root. It is not a real Node binding or a root-set claim.

The fixture creates exactly one unique private tree under a verified safe `/root`
parent; `/tmp` is not used. All data is synthetic. It performs these eight finite
observations, without retry:

| Case | Expected production outcome |
| --- | --- |
| Empty root | Clear, zero transactions |
| Two terminal journals | Clear, one committed and one rolled_back |
| Same journals via one production CLI | Clear, same typed aggregate |
| Unknown future status | STOP_TRANSACTION_STATUS_UNKNOWN |
| Missing plan | STOP_OBSERVATION_IO |
| Result symlink to the other owned synthetic journal | STOP_OBSERVATION_IO |
| Replaced owned transaction root with stale original inode pin | STOP_ROOT_IDENTITY |
| Wrong mount-namespace inode pin | STOP_NAMESPACE_CHANGED |

Setup changes for negatives occur strictly between observations. The original
root is renamed only within the owned tree and restored after the replacement
case. The symlink points only inside that tree. Before/after **each** observation,
a separate fixture snapshot compares every owned entry's bytes or link target,
dev/inode/type/mode/uid/gid/nlink/size/mtime/ctime. Snapshot reads use O_NOATIME and
do not follow symlinks. Atime is deliberately not part of the metadata comparison;
it cannot turn this test into an accidental atime assertion. No control mutation
occurs between the before/after snapshots. The owned process's descriptor count
must also return to the same value. Read-only proof is about the observation;
setup/reset/cleanup are explicitly synthetic writes.

Limits: one outer tree; at most five directory creation intents including the
temporary replacement root, seven regular-file creation intents, nine bounded
file write intents (each at most4096B), one symlink, two intra-tree renames,
three setup unlink intents and one setup rmdir. At most five directories and
five file/link entries coexist. Final cleanup can remove only that bounded owned
tree. Snapshot visits at most12 entries with at most8 names in any directory.
The fixture scans only its own `/proc/self/fd` names, capped32, without reading
descriptor targets or exporting IDs.

The proof has a45-second cooperative budget and a separate5-second cleanup
reserve. The single child has at most8 seconds, output capped1024B per pipe, one
additional second to reap after killing only that known child, and no retry.
These checks are not a hard kernel I/O timeout; the intended local filesystem
premise remains. The GitHub step supplies the independent one-minute outer bound.
If that external bound kills the fixture or its outcome is unresolved, no cleanup
receipt may be inferred; main reconciles the actual failure before any repeat.

Cleanup first verifies exact private tree and parent identities. It requires
Python's fd-based symlink-safe rmtree and uses the verified `/root` descriptor and
unique basename. It never deletes a computed/unverified absolute path. A fresh
independently opened safe `/root` descriptor checks that the exact basename is
absent afterward. Primary failure is retained if cleanup fails. Uncertain tree
creation is not reported clean or blindly recreated. No other prefix is scanned
or removed. Sanitized output contains only fixed status/code/case aliases,
attempted/completed/CLI counts, cleanup and zero-owned-residue booleans. No raw
exception, path, ID, source/query text, metadata contents or stderr is exported.

No containers, network, services, systemd, namespaces, privileges/cgroups,
real journals/config, providers, Node/Vault or database are changed. The only
child is the owned read-only CLI. There is no production observer change,
factory/profile wiring, writer authority or new public endpoint.

Focused local preparation checks are
`tests/unit/test_node_transaction_native_preparation.py` and the existing
`tests/unit/test_ci_execution_policy.py`. They validate required single-step
execution, fixed source pins, finite native calls, default/no-create behavior,
wrapper result/snapshot/fd enforcement and sanitized primary/cleanup failure.
Those portable wrapper checks do not run the native proof. Actual Linux proof
remains NOT_RUN until main publishes and accepts the exact mandatory CI result.
