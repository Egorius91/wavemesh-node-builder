# Node request log privacy

Subscription identifiers and connection paths are credentials. Both nginx
access logs and upstream error messages can contain full request URIs. The
managed location include therefore disables access logging and directs request
error logging to `/dev/null` at server scope. The bootstrap HTTP site and the
HTTP redirect server apply the same policy. TLS continues to be validated.

This deliberately removes per-request nginx diagnostics for the WaveMesh
virtual host, including unknown paths and upstream failures. Use bounded health
probes, service status and SaaS/Agent metrics for operational evidence; never
turn on raw URI logging to investigate a customer subscription.

When the configured panel base is a known literal path, the managed renderer
also emits exact-prefix 403 locations for the native panel API log families
`panel/api/server/logs/` and `panel/api/server/xraylogs/`. A managed route may
not overlap either reserved family. These denials apply to public nginx ingress;
they do not remove the panel APIs or restrict direct loopback access and trusted
SSH forwarding. The renderer preserves the other generated subscription,
relay, and VPN locations.

CI runs a real nginx instance with a synthetic subscription path and query,
checks successful proxy delivery and a refused upstream, and first proves the
unprotected control logs those values. The protected configuration must omit
both secrets from all test logs. This does not prove third-party panel/Xray
logging policy or clean up historical logs. Existing nodes need a validated
nginx configuration update and reload before the request-log policy or public
panel-log denials take effect. Source tests are not host ingress acceptance.
