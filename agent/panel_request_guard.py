#!/usr/bin/env python3
"""Durable uncertainty barrier for cooperating local panel transports.

RESPONSE_ACCEPTED is a transport observation, never runtime/quiescence proof.
There is intentionally no reset/retry/reconciliation command in this module.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit

DEFAULT_ROOT = Path("/var/lib/wavemesh-agent/panel-requests")
MAX_STATE = 4096
MAX_RESPONSE = 8 * 1024 * 1024
READ_POSTS = {"/panel/api/xray/", "/panel/api/xray/testOutbound",
              "/panel/api/xray/routeTest", "/panel/api/setting/all", "/panel/setting/all"}
WRITE_PATH = re.compile(
    r"^/panel/(?:api/)?(?:clients/(?:add|addDisabled|bulkAdjust|(?:update|del)/[^/\s?#]+)"
    r"|inbounds/(?:add|(?:update|setEnable|del)/[0-9]+)"
    r"|setting/(?:update|apiTokens/create)|xray/update)$"
)


class PanelRequestError(RuntimeError):
    pass


def mutation(method: str, path: str) -> bool:
    if method in {"GET", "HEAD"}:
        return False
    if method == "POST" and path in READ_POSTS:
        return False
    if method != "POST" or not WRITE_PATH.fullmatch(path):
        raise PanelRequestError("PANEL_REQUEST_UNSUPPORTED")
    return True


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PanelRequestError("PANEL_JSON_INVALID")
        result[key] = value
    return result


def response_accepted(raw: bytes) -> bool:
    if len(raw) > MAX_RESPONSE:
        return False
    try:
        value = json.loads(raw, object_pairs_hook=unique_object)
        return isinstance(value, dict) and value.get("success") is True
    except (ValueError, UnicodeError, PanelRequestError):
        return False


class PanelRequestGuard:
    def __init__(self, root: Path | None = None):
        self.root = root or Path(os.environ.get("WAVEMESH_PANEL_REQUEST_STATE_DIR", str(DEFAULT_ROOT)))

    @contextmanager
    def locked(self, inherited_fd=None):
        if os.name != "posix" or not self.root.is_absolute():
            raise PanelRequestError("PANEL_JOURNAL_UNSUPPORTED")
        import fcntl
        # Do not follow a symlink in any ancestor, even for a not-yet-created root.
        for path in (self.root, *self.root.parents):
            if path.is_symlink():
                raise PanelRequestError("PANEL_JOURNAL_UNSAFE")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.root.stat()
        if info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise PanelRequestError("PANEL_JOURNAL_UNSAFE")
        # mkdir(parents=True) is not durable until every new parent entry is
        # synced. Sync existing ancestors too: a previous initialization may
        # have failed after mkdir, so existence alone is not durability proof.
        for path in (self.root, *self.root.parents):
            directory = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        fd = (os.dup(inherited_fd) if inherited_fd is not None else
              os.open(self.root / ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600))
        try:
            self.safe_file(fd)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise PanelRequestError("PANEL_REQUEST_BUSY") from None
            current = (self.root / ".lock").lstat()
            held = os.fstat(fd)
            if (current.st_dev, current.st_ino) != (held.st_dev, held.st_ino):
                raise PanelRequestError("PANEL_JOURNAL_UNSAFE")
            yield
        finally:
            os.close(fd)

    @staticmethod
    def safe_file(fd):
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != os.geteuid() or info.st_mode & 0o077):
            raise PanelRequestError("PANEL_JOURNAL_UNSAFE")

    def load(self):
        try:
            fd = os.open(self.root / "state.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return None
        try:
            self.safe_file(fd)
            raw = os.read(fd, MAX_STATE + 1)
        finally:
            os.close(fd)
        if len(raw) > MAX_STATE:
            raise PanelRequestError("PANEL_JOURNAL_INVALID")
        try:
            value = json.loads(raw, object_pairs_hook=unique_object)
        except (ValueError, UnicodeError):
            raise PanelRequestError("PANEL_JOURNAL_INVALID") from None
        if (not isinstance(value, dict) or set(value) != {"schema_version", "phase", "attempt_id", "request_digest"}
                or type(value["schema_version"]) is not int or value["schema_version"] != 1
                or value["phase"] not in {"DISPATCH_INTENT", "RESPONSE_ACCEPTED"}
                or any(not isinstance(value[k], str) or not re.fullmatch(r"[a-f0-9]{64}", value[k])
                       for k in ("attempt_id", "request_digest"))):
            raise PanelRequestError("PANEL_JOURNAL_INVALID")
        return value

    def save(self, value):
        fd, temporary = tempfile.mkstemp(prefix=".state-", dir=self.root)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.root / "state.json")
            directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def execute(self, method, path, target, payload, dispatch):
        if not mutation(method, path):
            return dispatch()
        try:
            with self.locked():
                previous = self.load()
                if previous and previous["phase"] != "RESPONSE_ACCEPTED":
                    raise PanelRequestError("PANEL_REQUEST_RECONCILIATION_REQUIRED")
                # Per-attempt salt prevents a stable identity fingerprint. Neither
                # headers/credentials nor request paths/payloads are persisted.
                nonce = secrets.token_hex(32)
                digest = hashlib.sha256(json.dumps([nonce, method, path, target, payload],
                                                  separators=(",", ":")).encode()).hexdigest()
                pending = {"schema_version": 1, "phase": "DISPATCH_INTENT",
                           "attempt_id": nonce, "request_digest": digest}
                self.save(pending)  # No network dispatch until file and directory fsync succeed.
                result = dispatch()
                if not response_accepted(result[1]):
                    raise PanelRequestError("PANEL_RESPONSE_UNCERTAIN")
                try:
                    self.save({**pending, "phase": "RESPONSE_ACCEPTED"})
                except Exception:
                    # A failed completion must not silently reopen admission.
                    # Re-publish the already durable intent before surfacing it.
                    self.save(pending)
                    raise
                return result
        except PanelRequestError:
            raise
        except Exception:
            raise PanelRequestError("PANEL_REQUEST_UNCERTAIN") from None


def curl_transport(envelope):
    """Internal shell adapter: one fixed curl executable, never a shell command."""
    if not isinstance(envelope, dict) or set(envelope) != {"method", "path", "url", "args"}:
        raise PanelRequestError("PANEL_ADAPTER_INVALID")
    method, path, url, args = (envelope[k] for k in ("method", "path", "url", "args"))
    if not all(isinstance(x, str) for x in (method, path, url)) or not isinstance(args, list):
        raise PanelRequestError("PANEL_ADAPTER_INVALID")
    parsed = urlsplit(url)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.username or parsed.password or parsed.query or parsed.fragment or not parsed.path.endswith(path):
        raise PanelRequestError("PANEL_ADAPTER_INVALID")
    flags = {"--silent", "--show-error"}
    values = {"--connect-timeout", "--max-time", "--output", "--write-out", "--request", "-H", "-b", "-c", "--data-binary"}
    options = {}
    index = 0
    while index < len(args):
        option = args[index]
        if not isinstance(option, str):
            raise PanelRequestError("PANEL_ADAPTER_INVALID")
        if option in flags:
            index += 1
            continue
        if option not in values or index + 1 >= len(args) or not isinstance(args[index + 1], str):
            raise PanelRequestError("PANEL_ADAPTER_INVALID")
        if option != "-H" and option in options:
            raise PanelRequestError("PANEL_ADAPTER_INVALID")
        options[option] = args[index + 1]
        index += 2
    if options.get("--request") != method or options.get("--write-out") != "%{http_code}" or not options.get("--output"):
        raise PanelRequestError("PANEL_ADAPTER_INVALID")

    def dispatch():
        result = subprocess.run(["curl", *args, url], capture_output=True, check=False)
        status = result.stdout.decode("ascii", errors="strict").strip()
        if result.returncode != 0 or not re.fullmatch(r"2[0-9]{2}", status):
            raise PanelRequestError("PANEL_REQUEST_UNCERTAIN")
        fd = os.open(options["--output"], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            PanelRequestGuard.safe_file(fd)
            raw = os.read(fd, MAX_RESPONSE + 1)
        finally:
            os.close(fd)
        return status, raw

    return PanelRequestGuard().execute(method, path, url, options.get("--data-binary", ""), dispatch)[0]


if __name__ == "__main__":
    try:
        if sys.argv[1:] in (["--check-open"], ["--check-open-held-lock"]):
            guard = PanelRequestGuard()
            with guard.locked(10 if sys.argv[1:] == ["--check-open-held-lock"] else None):
                state = guard.load()
                if state and state["phase"] != "RESPONSE_ACCEPTED":
                    raise PanelRequestError("PANEL_REQUEST_RECONCILIATION_REQUIRED")
            sys.exit(0)
        if sys.argv[1:]:
            raise PanelRequestError("PANEL_ADAPTER_INVALID")
        raw = sys.stdin.buffer.read(MAX_RESPONSE + 1)
        if len(raw) > MAX_RESPONSE:
            raise PanelRequestError("PANEL_ADAPTER_INVALID")
        print(curl_transport(json.loads(raw, object_pairs_hook=unique_object)))
    except Exception:
        print("PANEL_REQUEST_FAILED_RECONCILE_BEFORE_RETRY", file=sys.stderr)
        sys.exit(1)
