# Node credential recovery

Status: staging-only controlled operation

## Purpose

Recover an existing Node Agent when neither its bearer nor its mTLS credential can authenticate. The operation is explicitly authorized by a short-lived, node-scoped `wvr_` token issued by SaaS.

## Installed tools

The Agent installer provides:

```text
/usr/local/lib/wavemesh-agent/node_recovery.py
/usr/local/sbin/wavemesh-node-agent-recover
```

They are inert unless a private recovery token is present.

## Secret handling

Install the one-time token as:

```text
/etc/wavemesh-agent/recovery.token
```

Required properties:

- root-owned regular file;
- mode `0600`;
- contains only the single `wvr_` token;
- not a symlink;
- never supplied as a command-line argument;
- never printed by the recovery client or wrapper.

The replacement `wvn_` bearer is generated locally. Only its SHA-256 is sent to SaaS.

## Controlled command

Set only the non-secret external Node ID and run:

```bash
WAVEMESH_RECOVERY_EXTERNAL_NODE_ID=ru-spb1 \
  /usr/local/sbin/wavemesh-node-agent-recover
```

The wrapper:

1. acquires a host lock;
2. validates the Agent environment and recovery-token permissions;
3. creates a private backup of Agent environment and TLS state;
4. runs a secret-free recovery preflight;
5. stops `wavemesh-node-agent.service`;
6. invokes the recovery API;
7. atomically installs the locally generated bearer and expiry;
8. retains all old immutable certificate generations but removes the active selector;
9. clears stale rotation and acknowledgement state;
10. resets the mTLS runtime to `BEARER_ONLY`;
11. starts the Agent;
12. waits for a newly issued identity and `SHADOW_ACTIVE`;
13. destroys the one-time recovery token and pending bearer file.

## Crash safety

Before the network request, the client writes the locally generated replacement bearer to mode-0600 `recovery.pending`.

After server acceptance, it writes a mode-0600 `recovery.accepted.json` containing only hashes and bounded identifiers. No raw credential is stored in the marker.

This closes the server-commit/local-activation window:

- a network failure keeps the same pending bearer and recovery token;
- rerunning sends the same hash;
- a lost successful response is handled by SaaS idempotent replay;
- a crash after the acceptance marker finishes locally without another API request;
- a conflicting local pending hash fails closed.

## Failure behavior

When the apply phase is ambiguous, the wrapper:

- leaves the Agent stopped;
- preserves the token, pending bearer and backup;
- prints only the private diagnostic path;
- reports that rerunning the same command is safe.

Do not restore the old `agent.env` after the server has accepted recovery. The server transaction revokes the old credentials. Re-run the recovery command with the retained private state instead.

## Acceptance

Success requires:

```text
NODE_SCOPED_CREDENTIAL_RECOVERY=PASS
```

and proves:

- Agent service is active;
- mTLS runtime is `SHADOW_ACTIVE`;
- one-time token is destroyed;
- pending bearer and acceptance marker are destroyed;
- old certificate generations remain available for forensic review.

Central acceptance must additionally prove fresh heartbeats, a new acknowledged mTLS credential and no new `401 UNAUTHORIZED` events.
