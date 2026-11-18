# Private owner API and one-send caller (ROOT943)

This unpublished source path uses the existing panel API and existing authority.
It is not installed or called by `auto.sh`, another ordinary mutation command,
an Agent, or a production service. Snapshot schema3 and public channel v1 stay
unchanged. Source tests do not qualify the actual running main/core or traffic.

## Request and ownership

Exactly one POST is registered on the existing API group at
`/panel/api/xray/executionSelector` (under the panel's configured base path).
Existing auth, token scope, config-envelope and CSRF middleware still run.
Admin bearer/session users keep their existing authority; monitor, node-sync,
node-admin and verified mTLS identities remain denied by unchanged allowlists.
`XUI_PREPARED_ATTESTATION=1` remains required. No UI route, listener or credential
type is added. The panel listener's actual runtime acceptance remains separate.

Request JSON has exactly these mandatory fields, private version1:

- `schema_version`: integer1;
- `transaction_id`: ASCII alphanumeric first character, then alphanumeric,
  underscore, dot or hyphen; length1..128;
- `intent_sha256`: lowercase64hex SHA256 of retained pending intent bytes;
- `expected`: exactly `generation` (canonical lowercase UUID), `epoch` (canonical
  unsigned decimal string, less than uint64 maximum),
  `effective_config_sha256` (lowercase64hex full effective preimage);
- `template_before`: exact raw SQLite setting row string, never remarshal or
  panel getXraySetting (which can heal the DB);
- `rule_tag`: bounded identifier above, with nonempty `wm-rule-` suffix;
- `target`: bounded identifier, `wm-eu-only-block` or nonempty `wm-exit-path-` tag.

Body limit4MiB, UTF-8 JSON object, maximum nested depth32. Duplicate keys (including
escaped names), unknown/case-folded keys, missing/ambiguous types, trailing JSON
and invalid context fail before service invocation. The embedded template is
also a complete object without duplicate/trailing/deep JSON.

Service checks cancellation after its existing lock. The optional owner-local
full-hash expectation is excluded from legacy JSON DTOs; the full hash binds the
same qualified owner snapshot and the mutation lock check. A purpose-specific
context reaches the one native RPC; the owner checks it after locking and before
invalidation. Legacy internal callers keep their independent contexts and old
generation/epoch behavior. A known native success still performs persistence
and owner confirmation after client cancellation. An ambiguous native error
stays UNKNOWN, without replay, rollback, restart or compensation.

## Response contract

Invalid/disabled requests have only `{diagnostic: FIXED_CODE}` and no owner result.
HTTP400 means `INVALID_REQUEST`; HTTP503 means `PREPARED_EXECUTION_DISABLED`.
No error/config/credential details are emitted by this handler.

Once the validated request invokes the owner, the JSON body is EXACTLY the
existing journal owner-result object:

`{schema_version:1, transaction_id, intent_sha256, outcome}`

`outcome` has exactly the six booleans `NativeAttempted`, `NativeSucceeded`,
`TemplatePersisted`, `ConfigPersisted`, `KnownCommit`, `NoOp` plus
`ProcessConfigSHA256` (empty for an unproven postimage, otherwise lowercase64hex).
The validated transaction/intent binding is echoed. Fixed diagnostic is carried
in exactly one `X-WaveMesh-Execution-Diagnostic` response header, outside that
existing journal object: HTTP200/`COMMITTED` only for known commit;
HTTP200/`NO_OP` for no-op; HTTP409/`OUTCOME_UNPROVEN` for all other outcomes,
including errors with partial flags/hash. Raw service errors never enter the
response or the generic error-logging controller helper.

## Private caller admission and crash behavior

`prepared_owner.apply_once(transaction, credential_file)` has no CLI or installed
caller. It requires inherited FD9 under the existing node lock, one frozen
reconcile intent, frozen candidates, approved unpublished/core-pinned route,
unchanged local preimages and the exact existing SQLite template row hash.

The existing private SELECT-only reader provides one read transaction, closed
before network I/O. It derives explicit `webListen=127.0.0.1`, canonical port,
canonical `/segment/` base path, and empty cert/key settings; missing, duplicate,
wildcard, remote or TLS settings refuse before dispatch. No defaults are guessed.
Private credential JSON has exactly integer `schema_version:1` and
`bearer_token`. Its SHA256 must match exactly one enabled, explicitly `admin`,
unexpired existing `api_tokens` row. No token is created/fetched, and no real
credential was used in these fixtures. Other scopes and unknown/duplicate rows
refuse. Ordinary request helpers keep their existing behavior.

Before connecting, the SAME private transaction journal durably records
`prepared.owner-request.json` with only version, transaction ID, intent hash and
request hash. Existing dispatch/result/completed facts refuse any new send.
An uncertain directory barrier can conservatively leave debt without a send.
The client uses stdlib HTTP with a directly connected numeric AF_INET socket,
implicit reopening disabled, no DNS, proxies, redirects, login, cookie/auth
fallback or retries. Socket I/O timeout35s is not an independent physical traffic
deadline. Credentials and request body stay in private files/memory, never argv
or logs. Reply bound is4MiB; duplicate JSON/header, foreign context, contradictory
status or unsupported response remains pending. Response/socket are closed.

The typed bounded reply, including non2xx partial outcomes, is durably retained
through the existing completion owner BEFORE fresh collection or local writes.
Known commit alone proceeds through `complete`; partial/no-op returns pending.
Lost reply cannot be reconstructed from a snapshot/no-op or retried by sender.
Known-reply metadata resumption uses `complete` separately, without a new send.
ROOT owns the shared reader's orphan send/result guard and delivers its exact
patch for private integration; no competing Agent/health writer is introduced.

## Authenticated connected peer and snapshot context (ROOT958)

Purpose-specific snapshot/routing reads now check request cancellation after
owner locking BEFORE identity inspection, while legacy callers keep their old
snapshot behavior. Focused Windows Go9 tests cover both previous locks, the
snapshot waiter, persistence after known success and legacy identity loss.

Before HTTP request/Authorization bytes, the caller reuses the SAME private
approval/SO_PEERCRED/two-fresh-frame/sealed-core collector with its executable
FDs held open. The view must equal the frozen generation/epoch/full hash/core
instance. This connected numeric AF_INET socket's reversed exact tuple must
identify exactly one ESTABLISHED root-owned peer inode in the verified Producer
network namespace. The same Producer must hold that accepted inode in /proc/PID/fd;
process/start/executable/core evidence is rechecked, then accepted inode/FD/tuple
and the client socket inode are checked again. Listener metadata or loopback
alone never suffices. A queued inode-zero ESTABLISHED connection may wait only after the SAME
verified root Producer is proven to own the exact LISTEN inode/FD. Both accepted
peer checks and all metadata reads share one absolute monotonic3s budget; only
read-only metadata is polled on the same connected socket. No reconnect or HTTP
retry occurs. Missing/wrong UID/PID/tuple, closed/reused sockets or expired budget
fail before any HTTP/credential bytes. All evidence closes on every outcome.
The caller cannot provide a PID, alternate trusted process or verified flag.

Portable protocol tests explicitly mock this native custody boundary; fake,
different, reused and stale cases assert request() is never called and the fake
HTTP listener receives no request. Existing collect still returns only the same
(view, full config) tuple and closes its evidence, while a dedicated retained
context keeps evidence open during peer verification. Linux fixture4 is prepared
for real SO_PEERCRED/root-process/executable/namespace/accepted inode+FD behavior,
including actual FD reuse, exited/different process refusals, delayed accept
without prior data, and timeout with no bytes/reconnect. That is a
binding primitive with a synthetic Python peer, NOT an approved full Producer
or sealed core/service composition. Native actual execution is still NOT RUN
until its own receipt. Only synthetic credentials are used; ordinary activation
remains unwired and current full main/API/core acceptance remains separate.

## Compatibility and rollback limits

Old internal method signatures, readonly control/DTOs, denied API scopes,
inline TLS/6004 guards and generic auto path remain unchanged in this slice.
Remove this source package only before any actual dispatch; a pending/request/
reply/completed journal must be retained and reconciled, never generic-restored.
Current full main, real owner+collector composition, loopback ingress custody,
public TLS/loaded Nginx and new/existing IPv4/IPv6/DNS physical fail-closed remain
separate gates. No capability, publication, live server or Node action follows.
