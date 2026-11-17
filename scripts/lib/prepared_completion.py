"""Private owner-reply retention; no transport or completion caller is installed.

Only the future trusted owning adapter may call record_owner_result, while
holding the existing node mutation lock. Caller-supplied verified/force CLI
inputs are deliberately absent. Retention never settles the pending operation.
"""
from pathlib import Path
import os

import prepared_journal as reader
import transaction_state as journal


class CompletionUnavailable(RuntimeError):
    """Fixed diagnostic; no private reply/config/exception payload is exported."""


def record_owner_result(transaction, reply):
    """Durably retain this exact reply before fresh collect or local commit.

    Partial/no-op flags are retained as facts but cannot establish completion.
    A conflicting subsequent reply never replaces the original evidence. This
    function performs no native call and cannot resolve a lost writer reply.
    """
    try:
        directory = Path(transaction)
        if directory.is_symlink() or os.path.lexists(directory / "prepared.apply.completed.json"):
            raise ValueError("unresolved journal origin")
        plan = reader.decode(reader.read_private(directory / "plan.json"))
        if (type(plan.get("schema_version")) is not int or plan["schema_version"] != 1
                or plan.get("operation") != "auto-prepared-reconcile"):
            raise ValueError("unsupported owner context")
        intent_raw = reader.read_private(directory / "prepared.apply.pending.json")
        bound = reader.validate_owner_result(reply, directory.name, reader.sha(intent_raw))
        path = directory / "prepared.owner-result.json"
        if os.path.lexists(path):
            original = reader.decode(reader.read_private(path))
            reader.validate_owner_result(original, directory.name, reader.sha(intent_raw))
            if original != bound:
                raise ValueError("conflicting owner result")
            # The earlier atomic write may have failed its directory barrier.
            # Repeat metadata durability, never the native/business operation.
            journal._sync_directory(directory)
        else:
            journal.atomic_json(path, bound)
        return reader.sha(reader.read_private(path))
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
        raise CompletionUnavailable("OWNER_RESULT_NOT_RETAINED") from None
