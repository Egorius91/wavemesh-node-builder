# Public Entry TLS evidence

`python3 scripts/lib/public_entry_tls.py --config /etc/wavemesh-node/config.json`
performs one read-only TLS handshake to the admitted literal `server.public_ip`
on port443, using `server.domain` as SNI and certificate hostname. It performs no
DNS discovery, HTTP request, panel call, key read, certificate issuance, config
write or service change. The operator must already have permission to probe that
target. Source availability does not authorize a live probe.

The Python platform verifier must validate the chain, hostname and validity;
there is no insecure mode. The platform trust configuration, including normal
`SSL_CERT_FILE` / `SSL_CERT_DIR` overrides, belongs in the admitted security
inputs. The result hashes the roots actually loaded in the verifying context;
this is not an inventory of every lazy on-disk CA-directory entry. A synthetic
fixture CA does not prove public trust on a real client.

On Linux the input must be a root-owned regular single-link0600 file without a
symlink leaf. Reads are bounded and refuse duplicate JSON keys. The file is
checked before and after the handshake. The target must be a public unicast
IPv4/IPv6 literal; private, reserved, multicast, mapped and scoped addresses are
refused. The socket verifies its connected peer and TLS uses at least1.2.
Connect and handshake share a10-second network budget; the caller must also
bound the child process for scheduler or filesystem stalls.

Only `VERIFIED_TLS_ONLY` is positive. The private JSON result binds probe UUID,
timestamps, full configuration hash, target, address family, negotiated version,
served leaf fingerprint/expiry and loaded trust roots. Failures use fixed reasons
without raw TLS errors, source paths, private config or credentials. The report
does not contain `healthy` or capability fields. Consumers must check freshness
against their admitted clock/policy and current source/config/security inputs.

This evidence proves neither Nginx's loaded configuration/worker identity nor a
particular XHTTP path, route publication, subscription compatibility, VPN traffic,
Exit health, fail-closed, Node mTLS, fleet or beta acceptance. It does not relax
the prepared execution owner's inline-TLS guard. Those boundaries remain separate.

Portable unit tests cover target/privacy/composition contracts. Native tests run
as root in a fresh network namespace containing only loopback, inside an admitted
disposable Linux environment. Local Linux checks use the project offline VM;
the final hosted CI runs the same namespace fixture. The tests temporarily add
two synthetic namespace-local IP aliases, generate their own short-lived CA/leaf,
serve TLS on443 and remove aliases/files/listeners afterward. No real targets or
owner keys are used. Invoke with `sudo unshare --net -- python3 -B
tests/native/test_public_entry_tls_native.py --require-native -v`. Require-native
returns77 rather than accepting a non-native or default-network environment.
