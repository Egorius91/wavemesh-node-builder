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
import uuid
from urllib.parse import urlsplit

DEFAULT_ROOT = Path("/var/lib/wavemesh-agent/panel-requests")
MAINTENANCE_PROTOCOL = "local-maintenance-v2"
INSTALLATION_PROTOCOL = "panel-install-intent-v3"
STOP_PROTOCOL = "panel-stop-intent-v4"
PANEL_UNIT = "x-ui.service"
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
        if isinstance(value, dict) and value.get("schema_version") in (2, 3, 4):
            version = value["schema_version"]
            expected = {"schema_version", "request", "maintenance"}
            if version in (3, 4):
                expected.add("installation")
            if version == 4:
                expected.add("stop")
            if (type(value["schema_version"]) is not int
                    or set(value) != expected):
                raise PanelRequestError("PANEL_JOURNAL_INVALID")
            hold = value["maintenance"]
            if (not isinstance(hold, dict) or set(hold) != {"operation_id", "generation", "phase"}
                    or hold["phase"] not in {"HELD", "CANCELLED"}):
                raise PanelRequestError("PANEL_JOURNAL_INVALID")
            validate_hold_identity(hold["operation_id"], hold["generation"])
            if value["request"] is not None:
                self.validate_request(value["request"])
            if version in (3, 4):
                self.validate_installation(value["installation"])
                if (hold["phase"] != "HELD" or (value["request"] is not None
                        and value["request"]["phase"] != "RESPONSE_ACCEPTED")):
                    raise PanelRequestError("PANEL_JOURNAL_INVALID")
            if version == 4:
                self.validate_stop(value["stop"])
        else:
            self.validate_request(value)
        return value

    @staticmethod
    def validate_request(value):
        if (not isinstance(value, dict) or set(value) != {"schema_version", "phase", "attempt_id", "request_digest"}
                or type(value["schema_version"]) is not int or value["schema_version"] != 1
                or value["phase"] not in {"DISPATCH_INTENT", "RESPONSE_ACCEPTED"}
                or any(not isinstance(value[k], str) or not re.fullmatch(r"[a-f0-9]{64}", value[k])
                       for k in ("attempt_id", "request_digest"))):
            raise PanelRequestError("PANEL_JOURNAL_INVALID")
        return value

    @staticmethod
    def request_state(value):
        return value["request"] if value and value["schema_version"] in (2, 3, 4) else value

    @staticmethod
    def hold_state(value):
        return value["maintenance"] if value and value["schema_version"] in (2, 3, 4) else None

    @staticmethod
    def validate_stop(value):
        keys = {"phase", "unit", "boot_id", "invocation_id", "control_group", "cgroup_inode", "contract_sha256"}
        if (not isinstance(value, dict) or set(value) != keys
                or value["phase"] != "STOP_INTENT" or value["unit"] != PANEL_UNIT
                or not isinstance(value["boot_id"], str)
                or not re.fullmatch(r"[a-f0-9]{8}(-[a-f0-9]{4}){3}-[a-f0-9]{12}", value["boot_id"])
                or not isinstance(value["invocation_id"], str)
                or not re.fullmatch(r"(?:[a-f0-9]{32})?", value["invocation_id"])
                or value["control_group"] != "/system.slice/" + PANEL_UNIT
                or type(value["cgroup_inode"]) is not int or value["cgroup_inode"] < 0
                or not isinstance(value["contract_sha256"], str)
                or not re.fullmatch(r"[a-f0-9]{64}", value["contract_sha256"])):
            raise PanelRequestError("PANEL_STOP_INVALID")

    @staticmethod
    def validate_installation(value):
        if (not isinstance(value, dict)
                or set(value) != {"phase", "candidate_sha256", "rollback_manifest_sha256"}
                or value["phase"] != "INSTALL_INTENT"
                or any(not isinstance(value[key], str) or not re.fullmatch(r"[a-f0-9]{64}", value[key])
                       for key in ("candidate_sha256", "rollback_manifest_sha256"))):
            raise PanelRequestError("PANEL_INSTALLATION_INVALID")

    @contextmanager
    def installation_intent(self, operation_id, generation, candidate_sha256,
                            rollback_manifest_sha256, node_lock=None):
        """Internal pre-effect boundary, not artifact verification or recovery.

        Caller must independently verify artifact/backup and exclusion/drain.
        Both locks remain owned until the context exits. Exit or process death
        never clears the durable intent. Re-entering the same binding is only
        permission to reconcile; it must never trigger a blind replay of effects.
        There is deliberately no CLI/remote entry point or release operation.
        """
        validate_hold_identity(operation_id, generation)
        installation = {"phase": "INSTALL_INTENT", "candidate_sha256": candidate_sha256,
                        "rollback_manifest_sha256": rollback_manifest_sha256}
        self.validate_installation(installation)
        with maintenance_node_lock(node_lock or Path("/run/lock/wavemesh-node.lock")), self.locked():
            value = self.load()
            hold = self.hold_state(value)
            if (not hold or hold["phase"] != "HELD"
                    or (hold["operation_id"], hold["generation"]) != (operation_id, generation)):
                raise PanelRequestError("PANEL_INSTALLATION_CONFLICT")
            request = self.request_state(value)
            if request and request["phase"] != "RESPONSE_ACCEPTED":
                raise PanelRequestError("PANEL_REQUEST_RECONCILIATION_REQUIRED")
            replay = value["schema_version"] in (3, 4)
            if replay:
                if value["installation"] != installation:
                    raise PanelRequestError("PANEL_INSTALLATION_CONFLICT")
            else:
                self.save({**value, "schema_version": 3, "installation": installation})
            # A replay is explicitly distinguishable from the initial transition.
            # It conveys no claim that an earlier external effect did/didn't run.
            yield {"installation": installation, "reconciliation_required": replay,
                   "local_admission": "CLOSED", "quiescence": "NOT_PROVEN"}

    def assert_open(self, value, maintenance_only=False):
        hold = self.hold_state(value)
        if hold and hold["phase"] == "HELD":
            raise PanelRequestError("PANEL_LOCAL_MAINTENANCE_HELD")
        request = self.request_state(value)
        if not maintenance_only and request and request["phase"] != "RESPONSE_ACCEPTED":
            raise PanelRequestError("PANEL_REQUEST_RECONCILIATION_REQUIRED")

    def check_maintenance(self):
        # Called while holding the Node lock. Do not create global state for a
        # node that has never used the journal, but reject unsafe ancestors.
        for path in (self.root, *self.root.parents):
            if path.is_symlink():
                raise PanelRequestError("PANEL_JOURNAL_UNSAFE")
        try:
            self.root.lstat()
        except FileNotFoundError:
            return
        with self.locked():
            self.assert_open(self.load(), maintenance_only=True)

    def check_startup(self, node_lock=None):
        """Admission for a NEW systemd activation, not backend drain proof.

        Unlike transport bootstrap, missing durable state must fail closed.
        Neither this check nor loss of the volatile locks initializes/releases
        journal state. A start admitted before a hold still requires stop/drain.
        """
        if sys.platform != "linux" or os.geteuid() != 0 or not self.root.is_absolute():
            raise PanelRequestError("PANEL_STARTUP_UNSUPPORTED")
        for path in (self.root, *self.root.parents):
            info = path.lstat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0
                    or info.st_mode & 0o022):
                raise PanelRequestError("PANEL_STARTUP_STORAGE_UNSAFE")
        with maintenance_node_lock(node_lock or Path("/run/lock/wavemesh-node.lock")), self.locked():
            value = self.load()
            if value is None:
                raise PanelRequestError("PANEL_STARTUP_STATE_REQUIRED")
            self.assert_open(value)

    def maintenance(self, action, operation_id=None, generation=None):
        """Caller must hold the Node lock for prepare/cancel, then journal lock.

        This changes local admission only. It never stops a service, reconciles
        an uncertain request, or establishes external writer exclusion/drain.
        """
        if action not in {"prepare", "cancel", "status"}:
            raise PanelRequestError("PANEL_MAINTENANCE_INVALID")
        if action != "status":
            validate_hold_identity(operation_id, generation)
        value = self.load()
        if value and value["schema_version"] in (3, 4) and action != "status":
            raise PanelRequestError("PANEL_INSTALLATION_RECONCILIATION_REQUIRED")
        hold = self.hold_state(value)
        request = self.request_state(value)
        same = hold and (hold["operation_id"], hold["generation"]) == (operation_id, generation)
        if action == "prepare":
            if not same:
                if (hold and hold["phase"] == "HELD") or generation != (hold["generation"] + 1 if hold else 1):
                    raise PanelRequestError("PANEL_MAINTENANCE_CONFLICT")
                hold = {"operation_id": operation_id, "generation": generation, "phase": "HELD"}
                value = {"schema_version": 2, "request": request, "maintenance": hold}
                self.save(value)
        elif action == "cancel":
            if not same:
                raise PanelRequestError("PANEL_MAINTENANCE_CONFLICT")
            if hold["phase"] == "HELD":
                if request and request["phase"] != "RESPONSE_ACCEPTED":
                    raise PanelRequestError("PANEL_REQUEST_RECONCILIATION_REQUIRED")
                hold = {**hold, "phase": "CANCELLED"}
                value = {"schema_version": 2, "request": request, "maintenance": hold}
                self.save(value)
        result = {"local_admission": "CLOSED" if hold and hold["phase"] == "HELD" else "NOT_HELD",
                "maintenance": hold, "request_pending": bool(request and request["phase"] != "RESPONSE_ACCEPTED"),
                "quiescence": "NOT_PROVEN"}
        if value and value["schema_version"] in (3, 4):
            result["installation"] = value["installation"]
        if value and value["schema_version"] == 4:
            # Boot/cgroup/invocation identity is private reconciliation state.
            result["stop"] = {"phase": value["stop"]["phase"]}
        return result

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
                self.assert_open(previous)
                # Per-attempt salt prevents a stable identity fingerprint. Neither
                # headers/credentials nor request paths/payloads are persisted.
                nonce = secrets.token_hex(32)
                digest = hashlib.sha256(json.dumps([nonce, method, path, target, payload],
                                                  separators=(",", ":")).encode()).hexdigest()
                pending = {"schema_version": 1, "phase": "DISPATCH_INTENT",
                           "attempt_id": nonce, "request_digest": digest}
                def recorded(request):
                    return {**previous, "request": request} if self.hold_state(previous) else request
                pending = recorded(pending)
                self.save(pending)  # No network dispatch until file and directory fsync succeed.
                result = dispatch()
                if not response_accepted(result[1]):
                    raise PanelRequestError("PANEL_RESPONSE_UNCERTAIN")
                try:
                    self.save(recorded({**self.request_state(pending), "phase": "RESPONSE_ACCEPTED"}))
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


def validate_hold_identity(operation_id, generation):
    try:
        valid = isinstance(operation_id, str) and str(uuid.UUID(operation_id)) == operation_id
    except (ValueError, AttributeError):
        valid = False
    if not valid or type(generation) is not int or not 1 <= generation <= 2_147_483_647:
        raise PanelRequestError("PANEL_MAINTENANCE_INVALID")


@contextmanager
def maintenance_node_lock(path=Path("/run/lock/wavemesh-node.lock")):
    if os.name != "posix":
        raise PanelRequestError("PANEL_JOURNAL_UNSUPPORTED")
    import fcntl
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != os.geteuid() or info.st_mode & 0o022):
            raise PanelRequestError("PANEL_NODE_LOCK_UNSAFE")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PanelRequestError("PANEL_NODE_BUSY") from None
        current = path.lstat()
        if (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino):
            raise PanelRequestError("PANEL_NODE_LOCK_UNSAFE")
        yield
    finally:
        os.close(fd)


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
        if sys.argv[1:] == ["--check-startup"]:
            # systemd must consult the canonical journal even when a service
            # environment contains a transport/fixture state-dir override.
            PanelRequestGuard(DEFAULT_ROOT).check_startup()
            sys.exit(0)
        if sys.argv[1:] == ["--check-maintenance"]:
            PanelRequestGuard().check_maintenance()
            sys.exit(0)
        if len(sys.argv) >= 2 and sys.argv[1] == "maintenance":
            guard = PanelRequestGuard()
            if sys.argv[2:] == ["status"]:
                with guard.locked():
                    print(json.dumps(guard.maintenance("status")))
            elif len(sys.argv) == 5 and sys.argv[2] in {"prepare", "cancel"}:
                # Positional typed inputs only; no runtime URL/path/shell payload.
                with maintenance_node_lock():
                    with guard.locked():
                        print(json.dumps(guard.maintenance(sys.argv[2], sys.argv[3], int(sys.argv[4]))))
            else:
                raise PanelRequestError("PANEL_MAINTENANCE_INVALID")
            sys.exit(0)
        if sys.argv[1:] == ["--check-v1-held-lock"]:
            guard = PanelRequestGuard()
            with guard.locked(10):
                state = guard.load()
                if state and state["schema_version"] != 1:
                    raise PanelRequestError("PANEL_MAINTENANCE_PROTOCOL_REQUIRED")
            sys.exit(0)
        if sys.argv[1:] in (["--check-open"], ["--check-open-held-lock"]):
            guard = PanelRequestGuard()
            with guard.locked(10 if sys.argv[1:] == ["--check-open-held-lock"] else None):
                guard.assert_open(guard.load())
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
