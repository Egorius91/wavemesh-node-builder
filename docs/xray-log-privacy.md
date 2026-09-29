# Entry Xray private logging policy

WaveMesh Builder now accepts Entry Xray mutations only when the current Xray
template has this logging policy:

```json
{
  "log": {
    "access": "none",
    "error": "/var/log/wavemesh-node/xray-error.log",
    "loglevel": "warning",
    "dnsLog": false,
    "maskAddress": "full"
  }
}
```

`loglevel: error` is also accepted. Ordinary route and balancer operations
first require the baseline to pass this policy; they preserve its safe settings
and do not repair an unsafe baseline implicitly. The offline
`normalize-log-policy-candidate` command can prepare a candidate from the
known legacy shape: a `log` object with a recognized `loglevel`, known fields,
and omitted or empty access/error streams. It replaces only that `log` object
in an offline candidate; it does not contact the panel or apply the result.
Baseline validation rejects unknown log fields, omitted or empty streams,
malformed types, misplaced top-level `dnsLog` or `maskAddress`, enabled access
or DNS logging, a different error sink, and weaker log levels. Candidate
normalization rejects unknown fields and invalid types too. Diagnostics report
only the policy failure; they do not print the Xray configuration.

Every Entry transaction, including subscription and inbound changes, reads and
validates Xray before creating the transaction. Subscription transactions do
not capture or restore an Xray template because they do not call the Xray
configuration update API. Standalone route and balancer helpers validate before
creating their backup. The final Xray update function validates every candidate,
including rollback candidates. Rollback also validates a saved snapshot before
restoring any state. If an older incomplete transaction contains an unsafe
Xray snapshot, Builder marks rollback failed, retains the mode-0600 snapshot,
and requires operator repair; it does not replay that snapshot.

## Runtime preflight before enabling this policy

Source tests do not prove that the target host can safely write or rotate the
sink. Before deploying or enabling it on a Node, an operator must establish on
that exact host:

- the Xray process identity and its effective filesystem permissions;
- `/var/log/wavemesh-node` is a private directory, owned by the confirmed
  runtime identity and inaccessible to other service users;
- the file is created with private permissions and remains private after
  rotation and service restart;
- rotation is bounded to seven days of history, with one operator readership;
- a restart/readback confirms the deployed Xray version accepts `access: none`,
  the fixed error path, `dnsLog: false`, and `maskAddress: full`.

Do not enable the mutation path until these checks are complete. Choose a
rotation size limit during host preflight to bound disk use; retain at most
seven daily rotations. Do not grant panel users, subscription users, or
unrelated services access to the error log. The exact runtime UID, directory
ACL, rotation mechanism, and limits are host evidence and are intentionally not
guessed by this source change.

This covers Builder-managed Entry Xray JSON updates and their rollback paths.
It does not establish logging behavior for Xray processes on Exit nodes,
3X-UI's own panel logs, third-party panel behavior, or historical files. It
does not clean existing logs. Those remain separate runtime reviews. A passing
source test is not deployment or staging acceptance.
