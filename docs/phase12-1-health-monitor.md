# Phase 12.1 — periodic health monitoring and transition log

This increment adds an operational monitoring foundation without changing routing or attempting automatic remediation.

## Commands

```bash
sudo wavemesh monitor run
sudo wavemesh monitor run --json
sudo wavemesh monitor status
sudo wavemesh monitor status --json
sudo wavemesh monitor events --limit 20
sudo wavemesh monitor events --limit 20 --json
sudo wavemesh monitor install
sudo wavemesh monitor uninstall
```

`monitor run` refreshes manual cascade health and Auto Route health, stores the current normalized snapshot in:

```text
/var/lib/wavemesh-node/health-state.json
```

It appends transition-only events to:

```text
/var/log/wavemesh-node/health-events.jsonl
```

The first observation of each route is recorded as `baseline`. Later entries are written only when a route status changes or a previously observed route disappears. Repeated healthy checks do not grow the event log.

## Timer

`wavemesh monitor install` installs and enables:

```text
wavemesh-health-monitor.service
wavemesh-health-monitor.timer
```

The timer runs approximately once per minute with a small randomized delay and persists missed runs across reboots.

## Safety boundary

This phase is observation-only:

- no Exit is disabled or removed;
- no Auto Route selector or override is changed;
- no subscription is rewritten beyond the normal health command behavior;
- no notification is sent yet;
- no automatic failover policy is introduced beyond Xray's existing `leastPing` behavior.

The next increments can consume the transition log to add notification delivery, hysteresis, incident state, and carefully bounded remediation.
