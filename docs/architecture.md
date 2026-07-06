# Architecture

WaveMesh Node Builder deploys a node with three public-facing roles on one domain:

1. Web Identity website on `/`.
2. VLESS + XHTTP public path on a random `/api/<token>/` path.
3. Subscription URL on a random `/sub/<token>/` path.

Only nginx listens publicly on 80/443. Xray and panel services must bind to loopback or otherwise remain inaccessible from the Internet.

## Correct XHTTP topology

```text
client -> https://domain.com:443/api/random/ -> nginx -> http://127.0.0.1:random_local_port -> Xray inbound
```

Inbound requirements:

- protocol: VLESS;
- transport: XHTTP;
- listen address: 127.0.0.1;
- port: random 10000-30000;
- inbound security: none;
- public security in links: tls;
- public address in links: domain.com:443.

## Subscription rule

The subscription URL itself is public:

```text
https://domain.com/sub/random/
```

Links inside it must never expose internal implementation details.

Forbidden values in subscription output:

- 127.0.0.1;
- localhost;
- raw server IP when a domain is configured;
- local XHTTP port;
- panel port;
- `security=none` in public links.
