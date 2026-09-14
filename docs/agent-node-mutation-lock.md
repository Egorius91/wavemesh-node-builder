# Agent and CLI mutation exclusion

The production `access_runtime.py` entrypoint takes the existing CLI lock at
`/run/lock/wavemesh-node.lock` before reading configuration or transaction state.
It retains the lock through panel reads, mutation, verification and durable
output, including credential replacement cleanup. Lock order is node then access;
the existing per-access version/payload fence remains in force.

This prevents a CLI snapshot/rollback transaction from overlapping an Agent
access operation. After CLI process death, an incomplete transaction still blocks
Agent writes: only known `committed` and `rolled_back` records are accepted.
Missing, corrupt and unknown transaction states require operator reconciliation.
The Agent does not recover or delete CLI journals automatically.

The installer delivers a tmpfiles `f` rule, creates the shared file without
replacing its inode, and includes the rule in installation backups. Boot-time
tmpfiles creates it before Agent startup. The hardened service can write only
this file under `/run/lock`, not the entire directory. Existing CLI versions use
the same file, so the lock path must not be renamed. Never remove/replace the lock
file to clear a busy condition; the kernel releases flock on process death.

Installation does not restart the Agent or change command/mTLS gates. Activation
must include the new unit and tmpfiles rule, not just a copied Python file.
Rollback restores the old rule (or removes a previously absent rule), preserves
the runtime lock inode and remains compatible with older backup manifests.
Restoring an old executor also restores its old concurrency limitations.

This is a prerequisite for unmanaged-client quarantine, not its implementation.
It does not fence direct panel/UI writes, external automation, setup scripts or
arbitrary root commands that do not take the CLI lock. Those writers require
maintenance exclusion before any future unmanaged-client action. No customer
ownership, quarantine decision, client disablement or runtime traffic acceptance
is introduced here. Python lifecycle functions are internal helpers; production
execution must use the guarded entrypoint, not call them directly.
