# Phase 11.5 — Auto Route E2E verification

`wavemesh cascade verify-e2e` now verifies published Auto Routes together with manual cascade routes.

The command checks:

- at least two enabled manual cascade routes point to distinct Exits;
- every published Auto Route has an enabled `leastPing` balancer;
- every Auto selector references a known managed `wm-exit-*` outbound;
- the route and balancer tags agree;
- every enabled client has one enabled credential for every published manual or Auto route;
- every generated VLESS profile terminates at the Entry domain and uses the expected UUID, XHTTP path, TLS, host, SNI, mode, display name, and ordering;
- subscription files contain no Exit domains, relay UUIDs, relay paths, or expected Exit public IPs;
- manual route runtime health and routeTest still select their expected managed outbounds.

Run:

```bash
sudo wavemesh cascade verify-e2e
sudo wavemesh cascade verify-e2e --json
```

This is a control-plane and subscription E2E verification. It does not replace a real client egress test. A client-side external-IP check remains the final proof that traffic exits through the selected Exit node.
