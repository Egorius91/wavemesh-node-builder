#!/usr/bin/env python3
"""Installed prepared reconcile adapter; one existing FD9/intent/send owner.

No force/verified inputs, retries, credential creation or ordinary restore.
Historical completion is never a claim about current execution/readiness.
"""
import argparse
from pathlib import Path
import sqlite3

import prepared_completion as completion
import prepared_execution as execution
import prepared_intent as writer
import prepared_journal as reader
import prepared_owner as owner
import transaction_state as journal


class ReconcileUnavailable(RuntimeError):
    """Fixed diagnostic without credentials, paths, config or response payloads."""


def reconcile(transaction, route_id, files, observations, credential_file):
    try:
        completion._require_node_owner()
        directory = Path(transaction)
        reader.prepared_origin(directory)
        if reader.prepared_status(directory) == "committed":
            journal.mark(directory, "committed")
            return "historical-committed"
        intent = writer.prepare_reconcile_once(directory, route_id, files, observations)
        if intent.get("kind") == "health-only":
            if reader.prepared_status(directory) != "committed":
                raise ValueError("health-only completion required")
            return "health-only"
        # This replaces the legacy apply, never follows it. Owner itself keeps
        # request-before-send provenance and invokes independent completion.
        if owner.apply_once(directory, credential_file) != "committed":
            raise ValueError("prepared update remains pending")
        if reader.prepared_status(directory) != "committed":
            raise ValueError("prepared commit proof required")
        return "committed"
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError,
            RecursionError, StopIteration, RuntimeError, sqlite3.Error,
            execution.ExecutionUnavailable):
        raise ReconcileUnavailable("PREPARED_RECONCILE_NOT_VERIFIED_RETAIN_JOURNAL") from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("transaction", "route-id", "config", "runtime", "database", "process", "observations", "credential-file"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    try:
        observations = reader.decode(reader.read_private(args.observations))
        result = reconcile(args.transaction, args.route_id,
            {key: getattr(args, key) for key in ("config", "runtime", "database", "process")},
            observations, args.credential_file)
    except (OSError, ValueError, RuntimeError):
        raise SystemExit("PREPARED_RECONCILE_NOT_VERIFIED_RETAIN_JOURNAL") from None
    print(result)


if __name__ == "__main__":
    main()
