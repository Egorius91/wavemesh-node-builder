#!/usr/bin/env python3
"""Persistent, redacted transaction state for WaveMesh mutations."""

import argparse
import json
import os
import secrets
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import prepared_journal as prepared_reader
from prepared_journal import prepared_status

ACTIVE = {"in_progress", "recovering", "rollback_failed"}
TERMINAL = {"committed", "rolled_back"}


def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sync_directory(path):
    # Windows cannot open/fsync directories. POSIX journal durability must be
    # checked on native Linux; a failed barrier must reach the dispatch caller.
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _mkdirs(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    # An earlier failed barrier may have left *existing* but nondurable dirs.
    # Persist the entire parent chain on every successful preparation; exists
    # alone cannot tell us which directory entries survived an earlier failure.
    directory = path.absolute()
    while True:
        _sync_directory(directory)
        if directory.parent == directory:
            break
        directory = directory.parent


def _prepared_pending(path):
    # A broken symlink or malformed marker is still unresolved dispatch debt.
    return os.path.lexists(Path(path) / "prepared.apply.pending.json")


def _prepared_completed(path):
    # Origin must survive a damaged result and failures after a known commit.
    return os.path.lexists(Path(path) / "prepared.apply.completed.json")


def atomic_json(path, value):
    # Preserve the existing text-file byte format on each platform.
    data = (json.dumps(value, indent=2, sort_keys=True) + "\n").replace("\n", os.linesep).encode("utf-8")
    atomic_bytes(path, data)


def atomic_bytes(path, data):
    """Same durable journal write for an exact owner-produced private payload."""
    if not isinstance(data, bytes):
        raise TypeError("atomic bytes payload required")
    path = Path(path)
    _mkdirs(path.parent)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def records(root):
    root = Path(root)
    result = []
    if not root.exists():
        return result
    for directory in sorted((item for item in root.iterdir() if item.is_dir()), reverse=True):
        plan_path = directory / "plan.json"
        pending = _prepared_pending(directory)
        completed = _prepared_completed(directory)
        prepared = prepared_status(directory)
        if not plan_path.exists() and not pending and not completed and prepared is None:
            continue
        try:
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            if not isinstance(plan, dict):
                plan = {}
        except (OSError, ValueError):
            plan = {}
        try:
            outcome = json.loads((directory / "result.json").read_text(encoding="utf-8"))
            if not isinstance(outcome, dict):
                outcome = {}
        except (OSError, ValueError):
            outcome = {"status": "in_progress"}
        if pending or prepared == "active":
            outcome = {"status": "rollback_failed", "message": "Prepared update outcome UNKNOWN; retain dispatch evidence"}
        elif prepared == "committed":
            outcome = {"status": "committed", "finished_at": outcome.get("finished_at")}
        elif prepared == "rolled_back":
            outcome = {"status": "rolled_back", "finished_at": outcome.get("finished_at")}
        result.append({
            "id": directory.name,
            "operation": str(plan.get("operation") or "unknown")[:80],
            "started_at": plan.get("started_at"),
            "finished_at": outcome.get("finished_at"),
            "status": outcome.get("status") or "in_progress",
            "message": str(outcome.get("message") or "")[:160] or None,
        })
    return result


def incomplete(root):
    return [item for item in records(root) if item["status"] not in TERMINAL]


def resolve(root, transaction_id, active_only=False):
    if not transaction_id or Path(transaction_id).name != transaction_id:
        raise ValueError("invalid transaction id")
    root = Path(root).resolve()
    path = (root / transaction_id).resolve()
    if path.parent != root or not path.is_dir() or not ((path / "plan.json").is_file() or prepared_status(path) is not None):
        raise ValueError("transaction not found")
    if active_only:
        item = next((record for record in records(root) if record["id"] == transaction_id), None)
        if not item or item["status"] in TERMINAL:
            raise ValueError("transaction is not recoverable")
    return path


def admit(root):
    """Mutation admission only: retain debt, repair verified directory barriers.

    Caller holds the existing node flock. Read-only check/list never call this.
    """
    pending = incomplete(root)
    if pending:
        raise RuntimeError(f"incomplete transaction: {pending[0]['id']}")
    root = Path(root)
    # A previous rename barrier may have failed after becoming visible. The
    # pure reader never repairs it; mutation admission repeats that barrier.
    if root.exists():
        for directory in root.iterdir():
            if directory.is_dir() and prepared_status(directory) in ("committed", "rolled_back"):
                _sync_directory(directory)


def begin(root, operation, pid, *, prepared=False):
    if prepared:
        from prepared_completion import _require_node_owner
        _require_node_owner()
        if operation != "auto-prepared-reconcile":
            raise ValueError("prepared reconcile operation required")
    admit(root)
    root = Path(root)
    _mkdirs(root)
    os.chmod(root, 0o700)
    transaction_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(3)}"
    if prepared:
        transaction_id += prepared_reader.PREPARED_SUFFIX
    path = root / transaction_id
    path.mkdir(mode=0o700)
    _sync_directory(root)
    plan = {"schema_version": 1, "operation": operation[:80], "pid": int(pid), "started_at": utc_now()}
    if prepared:
        plan["prepared_protocol"] = prepared_reader.PROTOCOL
        plan["prepared_phase"] = "bootstrap"
    atomic_json(path / "plan.json", plan)
    atomic_json(path / "result.json", {"status": "in_progress"})
    if prepared:
        atomic_json(path / prepared_reader.ORIGIN, {"protocol": prepared_reader.PROTOCOL,
                    "transaction_id": path.name, "status": "NO_CONFIG_MUTATION_DISPATCHED"})
        prepared_reader.prepared_origin(path)
    return path


def abort_prepared(path):
    """Close verified pre-effect validation only. Never restore or erase facts."""
    import prepared_completion as completion
    import prepared_execution as execution
    import prepared_channels as channels
    completion._require_node_owner()
    path = Path(path)
    origin = prepared_reader.prepared_origin(path)
    if prepared_status(path) == "rolled_back":
        _sync_directory(path)
        return
    result = prepared_reader.decode(prepared_reader.read_private(path / "result.json"))
    plan = prepared_reader.decode(prepared_reader.read_private(path / "plan.json"))
    if (result != {"status": "in_progress"} or plan["prepared_phase"] != "pre-effect"
            or not prepared_reader.no_dispatch_facts(path)):
        raise RuntimeError("Prepared outcome retained; no-effect disposition is forbidden")
    intent = prepared_reader.frozen_pre_effect(path)
    files, expected = intent["files"], intent["expected"]
    config = prepared_reader.decode(prepared_reader.read_private(path / "config.before.json"))
    approved = completion._approved_plan(intent, config)
    with execution._collected_evidence(stable=True) as ((view, effective), _, recheck):
        if (view["engine_instance"] != expected["engine_instance"] or view["native_generation"] != expected["generation"]
                or view["mutation_epoch"] != expected["epoch"] or view["effective_config_sha256"] != expected["before_sha256"]
                or channels.health_config_fingerprint(config, intent["route_id"], approved, effective, view) != expected["normalized_sha256"]):
            raise RuntimeError("Prepared pre-effect execution changed; retain origin")
        recheck()
        for key in ("config", "runtime", "process"):
            raw = completion._optional(files[key])[0]
            if (None if raw is None else prepared_reader.sha(raw)) != intent["before"][key]:
                raise RuntimeError("Prepared live preimage changed; retain origin")
        if prepared_reader.sha(completion._stored_template_raw(files["database"]).encode()) != intent["before"]["template"]:
            raise RuntimeError("Prepared template preimage changed; retain origin")
        recheck()
        atomic_json(path / "result.json", {"status": "rolled_back", "finished_at": utc_now(),
            "prepared_no_effect": {"origin_sha256": origin, "intent_sha256": prepared_reader.digest(intent)}})
    if prepared_status(path) != "rolled_back":
        raise RuntimeError("Prepared no-effect disposition is not verified")


def mark(path, status, message=""):
    if status not in ACTIVE | TERMINAL:
        raise ValueError("unsupported transaction status")
    if status in TERMINAL and _prepared_pending(path):
        raise RuntimeError("Prepared update outcome UNKNOWN; terminal status is forbidden")
    if status == "committed" and prepared_status(path) == "committed":
        _sync_directory(path)
        return
    if prepared_reader.prepared_protocol(path):
        if status == "rolled_back" and prepared_status(path) == "rolled_back":
            _sync_directory(path)
            return
        raise RuntimeError("Prepared protocol requires verified disposition; generic mark is forbidden")
    if _prepared_completed(path):
        raise RuntimeError("Prepared completion requires verified metadata; retain origin and result")
    try:
        previous = json.loads((Path(path) / "result.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        previous = {}
    except (OSError, ValueError):
        if _prepared_pending(path):
            raise RuntimeError("Prepared update result is unreadable; retain origin") from None
        previous = {}
    if isinstance(previous, dict) and "prepared_commit" in previous:
        raise RuntimeError("Prepared commit proof cannot be overwritten by generic mark")
    value = {"status": status}
    if status in TERMINAL or status == "rollback_failed":
        value["finished_at"] = utc_now()
    if message:
        value["message"] = message[:160]
    atomic_json(Path(path) / "result.json", value)


def prune(root, keep):
    terminal = [item for item in records(root) if item["status"] in TERMINAL]
    for item in terminal[max(0, keep):]:
        path = resolve(root, item["id"])
        prepared = prepared_status(path)
        if prepared in ("committed", "rolled_back"):
            _sync_directory(path)
        if not _prepared_pending(path) and prepared != "active":
            shutil.rmtree(path)


def prune_backups(root, keep):
    root = Path(root)
    if not root.exists():
        return
    groups = {}
    for item in root.iterdir():
        if item.is_file():
            groups.setdefault(item.name.split(".", 1)[0], []).append(item)
    for files in groups.values():
        files.sort(key=lambda item: item.stat().st_mtime, reverse=True)
        for item in files[max(0, keep):]:
            item.unlink()


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("check", "admit", "list", "latest", "prune", "prune-backups"):
        child = sub.add_parser(name)
        child.add_argument("--root", required=True)
        if name == "list": child.add_argument("--json", action="store_true")
        if name in ("prune", "prune-backups"): child.add_argument("--keep", type=int, default=20)
    begin_parser = sub.add_parser("begin")
    begin_parser.add_argument("--root", required=True); begin_parser.add_argument("--operation", required=True); begin_parser.add_argument("--pid", required=True, type=int)
    begin_parser.add_argument("--prepared", action="store_true")
    abort_parser = sub.add_parser("prepared-abort")
    abort_parser.add_argument("--transaction", required=True)
    mark_parser = sub.add_parser("mark")
    mark_parser.add_argument("--transaction", required=True); mark_parser.add_argument("--status", required=True); mark_parser.add_argument("--message", default="")
    resolve_parser = sub.add_parser("resolve")
    resolve_parser.add_argument("--root", required=True); resolve_parser.add_argument("--id", required=True); resolve_parser.add_argument("--active-only", action="store_true")
    guard_parser = sub.add_parser("prepared-guard")
    guard_parser.add_argument("--transaction", required=True)
    install_parser = sub.add_parser("atomic-install")
    install_parser.add_argument("--source", required=True); install_parser.add_argument("--target", required=True)
    args = parser.parse_args()

    if args.command == "check":
        pending = incomplete(args.root)
        if pending: print(pending[0]["id"]); raise SystemExit(2)
    elif args.command == "admit":
        try:
            admit(args.root)
        except RuntimeError as error:
            raise SystemExit(str(error)) from None
        except (OSError, ValueError):
            raise SystemExit("Node journal durability admission not verified") from None
    elif args.command == "list":
        items = records(args.root)
        if args.json: print(json.dumps({"transactions": items}, indent=2))
        elif not items: print("No managed transactions")
        else:
            print("ID\tSTATUS\tOPERATION\tSTARTED")
            for item in items: print(f"{item['id']}\t{item['status']}\t{item['operation']}\t{item['started_at'] or '-'}")
    elif args.command == "latest":
        pending = incomplete(args.root)
        if not pending: raise SystemExit("no incomplete transaction")
        print(pending[0]["id"])
    elif args.command == "begin":
        try: print(begin(args.root, args.operation, args.pid, prepared=args.prepared))
        except RuntimeError as error: raise SystemExit(str(error)) from error
    elif args.command == "mark": mark(args.transaction, args.status, args.message)
    elif args.command == "prepared-abort":
        try: abort_prepared(args.transaction)
        except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError):
            raise SystemExit("Prepared no-effect disposition is not verified; retain origin") from None
    elif args.command == "prepared-guard":
        if prepared_status(args.transaction) is not None:
            raise SystemExit("Prepared origin retained; snapshot restore forbidden")
    elif args.command == "resolve":
        try: print(resolve(args.root, args.id, args.active_only))
        except ValueError as error: raise SystemExit(str(error)) from error
    elif args.command == "prune":
        if args.keep < 0: raise SystemExit("--keep must be non-negative")
        prune(args.root, args.keep)
    elif args.command == "prune-backups":
        if args.keep < 0: raise SystemExit("--keep must be non-negative")
        prune_backups(args.root, args.keep)
    elif args.command == "atomic-install":
        atomic_json(args.target, json.loads(Path(args.source).read_text(encoding="utf-8")))


if __name__ == "__main__": main()
