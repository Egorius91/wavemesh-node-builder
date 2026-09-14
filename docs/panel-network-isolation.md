# Panel API packet isolation prerequisite

`agent/panel_isolation.py` is an internal, source-only component for the future
verified panel installer. No existing Agent command, CLI or service calls it.
Do not apply it to live nodes as a complete maintenance procedure: startup
enforcement, backend drain, safe restoration and release remain unimplemented.

`PanelIsolation.isolate` takes the journal, held operation/generation, candidate
archive and rollback manifest SHA256 bindings, and one TCP panel port (1024–65535).
It enters the non-cancellable installation context before the first nft mutation
and owns the Node/journal locks through readback. One atomic nft transaction
creates the dedicated `inet wavemesh_panel_maintenance` table, two filter base
chains and two drop rules. CREATE refuses a concurrent table collision. It never
flushes, replaces or deletes other rules or repairs an unexpected existing table.

Input drops TCP traffic to the panel port arriving outside loopback. Output
drops loopback-bound TCP traffic to that port from sockets whose UID is not root.
Both IPv4 and IPv6 are covered by the inet family. Rules apply to established
connections too; no established/related exemption is inserted. Other ports and
root loopback access remain subject to the host's existing rules. A drop cannot
be undone by an accept in another chain; see the official
[nftables chain documentation](https://wiki.nftables.org/wiki-nftables/index.php/Configuring_chains).

Root is a trusted operator, not isolated by this filter. Cooperating root
Agent/CLI writers remain blocked by the journal. Manual root traffic, root-run
proxies, forwarded traffic DNATed into loopback, additional/changed API ports,
panel-local jobs and work already accepted by the backend are not proven excluded.
The future actuator must validate its actual listener/proxy/authority topology,
stop and verify backend/Xray drain, and retain the intended boundary during
installation/recovery. A single API-port filter is not a global quiescence proof.

Readback must exactly match the generated table, chains, rules, policies,
priorities and expressions, ignoring only kernel handles and metadata. The table
comment binds operation/generation, both artifact digests and port. The digests
are caller-supplied identities, not verified artifact/backup contents.

After timeout or lost response, the next call observes first. An exact matching
table can be acknowledged without another apply. Missing rules after a retained
intent require reconciliation; drift or conflicting bindings reject without
mutation. There is no automatic retry, flush, remove, reset or release operation.
Errors never include raw nft output. The return value describes current-boot
packet-filter presence and always reports `quiescence: NOT_PROVEN`.

The rules are kernel state and can disappear on reboot or privileged changes.
The durable journal alone does not prevent nginx/panel from starting with the
filter absent. A boot-time service interlock is required before deployment.

CI runs real nftables in a disposable network/PID namespace, with a nested peer
namespace connected by veth and IPv4/IPv6 echo services. It checks existing and
new local non-root/remote connections, root loopback and other-port controls,
an earlier established-accept chain, exact readback, real committed apply with
injected response loss, drift and missing rules. Only synthetic data is used;
the host network namespace and live firewall are never modified.
