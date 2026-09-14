#!/usr/bin/env python3
"""Stopped-panel admission and SQLite-aware restore. Errors never include data."""
import argparse
from contextlib import closing
import os
from pathlib import Path, PurePosixPath
import sqlite3
import stat
import sys
import time


def verify_stopped(text, cgroup_root=Path("/sys/fs/cgroup")):
    fields = {}
    expected = {"LoadState": "loaded", "ActiveState": "inactive", "SubState": "dead",
                "MainPID": "0", "ControlPID": "0", "KillMode": "control-group",
                "SendSIGKILL": "yes"}
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if not separator or key in fields or key not in expected.keys() | {"ControlGroup"}:
            raise ValueError("invalid unit observation")
        fields[key] = value
    if set(fields) != expected.keys() | {"ControlGroup"}:
        raise ValueError("incomplete unit observation")
    if any(fields[key] != value for key, value in expected.items()):
        raise ValueError("panel stop not proven")
    group = fields["ControlGroup"]
    if group:
        path = PurePosixPath(group)
        if (not path.is_absolute() or str(path) != group or ".." in path.parts
                or group == "/" or "\\" in group):
            raise ValueError("invalid control group")
        directory = cgroup_root / group.lstrip("/")
        # If systemd reports a path, require a readable recursive cgroup-v2
        # population observation. Missing paths/unsupported cgroup-v1 fail closed.
        events = (directory / "cgroup.events").read_text(encoding="ascii")
        populated = [line.split() for line in events.splitlines()
                     if line.split()[:1] == ["populated"]]
        if populated != [["populated", "0"]]:
            raise ValueError("panel descendants may remain")


def regular(path):
    path = Path(path).absolute()
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise ValueError("unsafe database path")
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("unsafe database file")
    if os.name == "posix" and (info.st_uid != os.geteuid() or info.st_mode & 0o022):
        raise ValueError("unsafe database ownership")
    return path


def restore(source, target, timeout=15.0):
    """Replace contents using SQLite's transaction/WAL protocol, never raw copy.

    The caller must hold the Node lock and have stopped/checked x-ui. This does
    not exclude independent unmanaged writers or service activation elsewhere.
    """
    source, target = regular(source), regular(target)
    if source == target or os.path.samefile(source, target):
        raise ValueError("backup and destination must differ")
    with source.open("rb") as stream:
        if stream.read(16) != b"SQLite format 3\x00":
            raise ValueError("backup database header missing")
    for suffix in ("-wal", "-shm", "-journal"):
        side = Path(str(source) + suffix)
        if side.exists() or side.is_symlink():
            raise ValueError("backup must be a closed standalone snapshot")
        side = Path(str(target) + suffix)
        if side.exists() or side.is_symlink():
            regular(side)
    if timeout <= 0:
        raise ValueError("invalid restore deadline")
    deadline = time.monotonic() + timeout

    def progress(status, remaining, total):
        if status != sqlite3.SQLITE_DONE and time.monotonic() >= deadline:
            raise TimeoutError("restore lock deadline")

    # immutable avoids creating/changing sidecars next to the closed backup.
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro&immutable=1", uri=True)) as src:
        if src.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("invalid backup database")
        if src.execute("SELECT 1 FROM sqlite_schema WHERE type='table' LIMIT 1").fetchone() is None:
            raise ValueError("backup has no application schema")
        with closing(sqlite3.connect(target.as_uri() + "?mode=rw", uri=True, timeout=0.05)) as dst:
            dst.execute("PRAGMA synchronous=FULL")
            src.backup(dst, pages=128, progress=progress, sleep=0.05)
            if dst.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise ValueError("restored database integrity failed")
            # Let SQLite reconcile its WAL instead of unlinking journals. Busy
            # readers/unsupported recovery require attention before service start.
            checkpoint = dst.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if not checkpoint or checkpoint[0] != 0:
                raise ValueError("restored WAL checkpoint incomplete")
    if os.name == "posix":
        os.chmod(target, 0o600)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["verify-stopped", "restore"])
    parser.add_argument("--source")
    parser.add_argument("--target")
    args = parser.parse_args()
    try:
        if args.command == "verify-stopped":
            verify_stopped(sys.stdin.read(4096))
        else:
            restore(args.source, args.target)
    except (OSError, ValueError, sqlite3.Error, TypeError):
        print("PANEL_RESTORE=RECONCILIATION_REQUIRED", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
