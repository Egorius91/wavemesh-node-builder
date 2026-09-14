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

CI runs a real nginx instance with a synthetic subscription path and query,
checks successful proxy delivery and a refused upstream, and first proves the
unprotected control logs those values. The protected configuration must omit
both secrets from all test logs. This does not prove third-party panel/Xray
logging policy or clean up historical logs. Existing nodes need a validated
nginx configuration update and reload before this policy takes effect.
