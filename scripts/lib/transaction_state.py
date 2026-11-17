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
    missing = []
    ancestor = path
    while not ancestor.exists():
        missing.append(ancestor)
        ancestor = ancestor.parent
    path.mkdir(parents=True, exist_ok=True)
    # Persist each new directory entry, including a new journal root, before
    # writing a record that could authorize a later native effect.
    for directory in reversed(missing):
        _sync_directory(directory.parent)


def _prepared_pending(path):
    # A broken symlink or malformed marker is still unresolved dispatch debt.
    return os.path.lexists(Path(path) / "prepared.apply.pending.json")


def atomic_json(path, value):
    path = Path(path)
    _mkdirs(path.parent)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
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
        if not plan_path.exists() and not pending:
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
        if pending:
            outcome = {"status": "rollback_failed", "message": "Prepared update outcome UNKNOWN; retain dispatch evidence"}
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
    if path.parent != root or not path.is_dir() or not ((path / "plan.json").is_file() or _prepared_pending(path)):
        raise ValueError("transaction not found")
    if active_only:
        item = next((record for record in records(root) if record["id"] == transaction_id), None)
        if not item or item["status"] in TERMINAL:
            raise ValueError("transaction is not recoverable")
    return path


def begin(root, operation, pid):
    pending = incomplete(root)
    if pending:
        raise RuntimeError(f"incomplete transaction: {pending[0]['id']}")
    root = Path(root)
    _mkdirs(root)
    os.chmod(root, 0o700)
    transaction_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(3)}"
    path = root / transaction_id
    path.mkdir(mode=0o700)
    _sync_directory(root)
    atomic_json(path / "plan.json", {"schema_version": 1, "operation": operation[:80], "pid": int(pid), "started_at": utc_now()})
    atomic_json(path / "result.json", {"status": "in_progress"})
    return path


def mark(path, status, message=""):
    if status not in ACTIVE | TERMINAL:
        raise ValueError("unsupported transaction status")
    if status in TERMINAL and _prepared_pending(path):
        raise RuntimeError("Prepared update outcome UNKNOWN; terminal status is forbidden")
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
        if not _prepared_pending(path):
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
    for name in ("check", "list", "latest", "prune", "prune-backups"):
        child = sub.add_parser(name)
        child.add_argument("--root", required=True)
        if name == "list": child.add_argument("--json", action="store_true")
        if name in ("prune", "prune-backups"): child.add_argument("--keep", type=int, default=20)
    begin_parser = sub.add_parser("begin")
    begin_parser.add_argument("--root", required=True); begin_parser.add_argument("--operation", required=True); begin_parser.add_argument("--pid", required=True, type=int)
    mark_parser = sub.add_parser("mark")
    mark_parser.add_argument("--transaction", required=True); mark_parser.add_argument("--status", required=True); mark_parser.add_argument("--message", default="")
    resolve_parser = sub.add_parser("resolve")
    resolve_parser.add_argument("--root", required=True); resolve_parser.add_argument("--id", required=True); resolve_parser.add_argument("--active-only", action="store_true")
    install_parser = sub.add_parser("atomic-install")
    install_parser.add_argument("--source", required=True); install_parser.add_argument("--target", required=True)
    args = parser.parse_args()

    if args.command == "check":
        pending = incomplete(args.root)
        if pending: print(pending[0]["id"]); raise SystemExit(2)
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
        try: print(begin(args.root, args.operation, args.pid))
        except RuntimeError as error: raise SystemExit(str(error)) from error
    elif args.command == "mark": mark(args.transaction, args.status, args.message)
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
