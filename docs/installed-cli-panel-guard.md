# Installed CLI panel request guard

The CLI and Agent ship the same `agent/panel_request_guard.py` source and use the
same protected journal at `/var/lib/wavemesh-agent/panel-requests` by default.
An uncertain request from either transport blocks subsequent writes from both,
including CLI transaction admission, until a supported reconciliation protocol
resolves it. A process exit or CLI reinstall does not clear that journal.

`wm_install_cli` now installs the guard beside the CLI shell/Python libraries at
`/usr/local/lib/wavemesh/lib/panel_request_guard.py`. Both `xui_api.sh` and
`transaction.sh` use `panel_guard.sh` to select that installed implementation.
Source checkouts continue using the original module under `agent/`. Installed
paths do not search for an unrelated `/usr/local/lib/agent` directory and do not
depend on an Agent installation having already occurred. Missing or symlinked
helpers fail closed; there is no unguarded curl fallback.

The optional `wm_install_cli /absolute/package/root` argument stages this same
installation layout for packaging and tests. Calling it without an argument
keeps the normal `/usr/local` destination. It does not install/restart Agent,
change panel credentials, or migrate/reset journal state.

Linux integration tests execute the real installer in a temporary packaging
root, verify the installed helper bytes/mode, invoke the installed CLI, and send
requests through its actual transport to a disposable loopback HTTP server.
They cover accepted writes, lost responses in both CLI-to-Agent and Agent-to-CLI
directions, blocked transaction snapshots, continued read-only diagnosis,
missing/symlinked helpers and reinstall over unresolved state. No real panel is
contacted. Windows skips these POSIX tests; Linux CI is the execution evidence.

CLI and Agent are separately deployed copies. Before a future rollout, verify
both copies against their approved artifact identity and compatible journal
protocol; a new source PR does not change installed servers. This packaging
correction is a prerequisite for shared maintenance admission, not proof of
exclusive remote-writer control, backend drain or a complete panel deployment.
