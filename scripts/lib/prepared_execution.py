"""Private read-only execution evidence. No health publication or runtime mutation."""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import socket
import stat
import struct
import sys
import time

import prepared_channels as channels

DIRECTORY = Path("/run/wavemesh-xray")
SNAPSHOT = DIRECTORY / "snapshot.sock"
APPROVAL = Path("/etc/wavemesh/prepared-producer-approval.json")
UPSTREAM = "c621ba9014a6aa21fd7c9ecf45655e44e24ebf13"
CORE = "a27e3f95ab6ce270956d0cecd32eda4686a811afc71c64a6a734a3dede14883e"
LIMIT = 4 << 20


class ExecutionUnavailable(RuntimeError):
    """Fixed diagnostic only; private response/configuration never enters errors."""


def _fail(code):
    raise ExecutionUnavailable(code)


def _integer(text):
    if len(text) > 20 or text == "-0":
        _fail("UNSUPPORTED_JSON_INTEGER")
    return int(text)


def _non_integer(_):
    _fail("UNSUPPORTED_JSON_NUMBER")


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            _fail("AMBIGUOUS_JSON")
        value[key] = item
    return value


def _canonical_input(value):
    if isinstance(value, str):
        if "\u2028" in value or "\u2029" in value:
            _fail("UNSUPPORTED_JSON_UNICODE")
        value.encode("utf-8", errors="strict")
    elif isinstance(value, dict):
        for key, item in value.items():
            _canonical_input(key)
            _canonical_input(item)
    elif isinstance(value, list):
        for item in value:
            _canonical_input(item)
    elif type(value) is int:
        _integer(str(value))
    elif value is not None and type(value) is not bool:
        _fail("UNSUPPORTED_JSON_VALUE")


def _decode(raw):
    if not isinstance(raw, bytes) or len(raw) > LIMIT:
        _fail("SNAPSHOT_SIZE")
    try:
        value = json.loads(raw.decode("utf-8", errors="strict"), object_pairs_hook=_object,
                           parse_int=_integer, parse_float=_non_integer, parse_constant=_non_integer)
        _canonical_input(value)
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise ExecutionUnavailable("SNAPSHOT_JSON") from None


def _shape(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= set(value) or set(value) - set(required) - set(optional):
        _fail("SNAPSHOT_SHAPE")


def _approval(value):
    _shape(value, ("schema_version", "artifact_kind", "source_commit", "upstream_commit",
                   "producer_sha256", "core_sha256", "release_manifest_sha256"))
    if type(value["schema_version"]) is not int or value["schema_version"] != 1 or value["artifact_kind"] != "full-producer":
        _fail("FULL_PRODUCER_APPROVAL_REQUIRED")
    if value["upstream_commit"] != UPSTREAM or value["core_sha256"] != CORE:
        _fail("UNAPPROVED_ENGINE")
    if not isinstance(value["source_commit"], str) or not re.fullmatch(r"[a-f0-9]{40}", value["source_commit"]):
        _fail("APPROVAL_SHAPE")
    for key in ("producer_sha256", "release_manifest_sha256"):
        if not isinstance(value[key], str) or not channels.HASH.fullmatch(value[key]):
            _fail("APPROVAL_SHAPE")
    return value


def _private(metadata, kind, mode):
    if metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) != mode or not kind(metadata.st_mode):
        _fail("PRIVATE_CUSTODY_REQUIRED")


def _load_approval():
    parent = APPROVAL.parent.lstat()
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != 0 or parent.st_mode & 0o022:
        _fail("APPROVAL_CUSTODY_REQUIRED")
    fd = os.open(APPROVAL, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        _private(os.fstat(stream.fileno()), stat.S_ISREG, 0o600)
        raw = stream.read(4097)
    if len(raw) > 4096:
        _fail("APPROVAL_SIZE")
    return _approval(_decode(raw))


@contextlib.contextmanager
def _connection():
    _private(DIRECTORY.lstat(), stat.S_ISDIR, 0o700)
    before = SNAPSHOT.lstat()
    _private(before, stat.S_ISSOCK, 0o600)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(3)
        connection.connect(str(SNAPSHOT))
        pid, uid, _ = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if uid != 0 or pid <= 0 or not _same(before, SNAPSHOT.lstat()):
            _fail("SNAPSHOT_PEER")
        yield connection, pid, before


def _frame(expected_peer):
    nonce = secrets.token_hex(32)
    deadline = time.monotonic() + 3
    with _connection() as (connection, pid, before):
        if pid != expected_peer:
            _fail("SNAPSHOT_PEER_CHANGED")
        connection.sendall(json.dumps({"schema_version": 3, "challenge": nonce}, separators=(",", ":")).encode() + b"\n")
        blocks, length = [], 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _fail("SNAPSHOT_TIMEOUT")
            connection.settimeout(remaining)
            block = connection.recv(min(65536, LIMIT + 1 - length))
            if not block:
                break
            length += len(block)
            if length > LIMIT:
                _fail("SNAPSHOT_SIZE")
            blocks.append(block)
        raw = b"".join(blocks)
    if not raw.endswith(b"\n") or b"\n" in raw[:-1] or not _same(before, SNAPSHOT.lstat()):
        _fail("SNAPSHOT_FRAME")
    _private(DIRECTORY.lstat(), stat.S_ISDIR, 0o700)
    _private(SNAPSHOT.lstat(), stat.S_ISSOCK, 0o600)
    return _decode(raw), nonce, pid


def _same(left, right):
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _process(pid):
    if type(pid) is not int or pid <= 0:
        _fail("PROCESS_IDENTITY")
    raw = Path(f"/proc/{pid}/stat").read_text()
    end = raw.rfind(")")
    if end < 0 or not raw.startswith(f"{pid} ("):
        _fail("PROCESS_IDENTITY")
    fields = raw[end + 1:].split()
    if len(fields) < 20 or fields[0] in ("Z", "X") or not fields[19].isdigit():
        _fail("PROCESS_IDENTITY")
    status = Path(f"/proc/{pid}/status").read_text()
    uids = [line.split()[1:] for line in status.splitlines() if line.startswith("Uid:")]
    if uids != [["0", "0", "0", "0"]]:
        _fail("PROCESS_UID")
    return {"pid": pid, "start_ticks": fields[19], "parent_pid": int(fields[1])}


def _open(path, stack):
    return stack.enter_context(open(path, "rb", buffering=0))


def _hash(stream):
    stream.seek(0)
    return hashlib.file_digest(stream, "sha256").hexdigest()


def _seals(stream):
    import fcntl
    required = fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL
    if fcntl.fcntl(stream, fcntl.F_GET_SEALS) & required != required:
        _fail("IMMUTABLE_INPUT_REQUIRED")


def _observe(identity, peer, approval, stack, trusted_producer):
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    producer, core = _process(peer), _process(identity["pid"])
    if (identity["producer_pid"] != peer or identity["producer_start_ticks"] != producer["start_ticks"]
            or identity["boot_id"] != boot or identity["start_ticks"] != core["start_ticks"]
            or core["parent_pid"] != peer or identity["pid"] == peer):
        _fail("PROCESS_IDENTITY")
    process, stream, metadata = trusted_producer
    if producer != process or not _same(metadata, os.stat(f"/proc/{peer}/exe")):
        _fail("SNAPSHOT_PEER_CHANGED")
    files = {"producer": (f"/proc/{peer}/exe", stream, metadata)}
    for name, path in (("core", f"/proc/{core['pid']}/exe"), ("core_fd", f"/proc/{core['pid']}/fd/4")):
        stream = _open(path, stack)
        files[name] = (path, stream, os.fstat(stream.fileno()))
    if _hash(files["core"][1]) != approval["core_sha256"]:
        _fail("EXECUTABLE_PIN")
    for name in ("core", "core_fd"):
        _seals(files[name][1])
    if not _same(files["core"][2], files["core_fd"][2]):
        _fail("EXECUTABLE_OBJECT")
    for name, number in (("stdin", 0), ("config", 3)):
        path = f"/proc/{core['pid']}/fd/{number}"
        stream = _open(path, stack)
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > LIMIT:
            _fail("CONSUMED_SIZE")
        _seals(stream)
        files[name] = path, stream, metadata
    if not _same(files["stdin"][2], files["config"][2]):
        _fail("CONSUMED_OBJECT")
    if _hash(files["stdin"][1]) != identity["consumed_sha256"] or _hash(files["config"][1]) != identity["consumed_sha256"]:
        _fail("CONSUMED_HASH")
    return boot, producer, core, files


def _recheck(observed):
    boot, producer, core, files = observed
    if Path("/proc/sys/kernel/random/boot_id").read_text().strip() != boot or _process(producer["pid"]) != producer or _process(core["pid"]) != core:
        _fail("PROCESS_CHANGED")
    for path, stream, initial in files.values():
        if not _same(initial, os.fstat(stream.fileno())) or not _same(initial, os.stat(path)):
            _fail("PROCESS_CHANGED")
    for name in ("core", "core_fd", "stdin", "config"):
        _seals(files[name][1])


def _snapshot(value, nonce, peer, approval):
    _shape(value, ("schema_version", "challenge", "qualified", "reason", "identity", "epoch"),
           ("effective_config_sha256", "execution_sha256", "effective_config", "balancer_overrides", "selector_transition"))
    if type(value["schema_version"]) is not int or value["schema_version"] != 3 or value["challenge"] != nonce:
        _fail("SNAPSHOT_CHALLENGE")
    if value["qualified"] is not True or value["reason"] != "custody_attested":
        _fail("EXECUTION_UNQUALIFIED")
    if not {"effective_config_sha256", "execution_sha256", "effective_config"} <= set(value):
        _fail("SNAPSHOT_SHAPE")
    identity = value["identity"]
    _shape(identity, ("generation", "boot_id", "pid", "start_ticks", "core_sha256", "producer_pid",
                      "producer_start_ticks", "producer_sha256", "producer_upstream", "consumed_sha256"))
    if (identity["producer_pid"] != peer or type(identity["producer_pid"]) is not int
            or identity["producer_sha256"] != approval["producer_sha256"] or identity["core_sha256"] != CORE
            or identity["producer_upstream"] != UPSTREAM):
        _fail("EXECUTABLE_PIN")
    if not isinstance(identity["consumed_sha256"], str) or not channels.HASH.fullmatch(identity["consumed_sha256"]):
        _fail("CONSUMED_HASH")
    channels.validate_engine_instance({"boot_id": identity["boot_id"], "pid": identity["producer_pid"], "start_ticks": identity["producer_start_ticks"]})
    overrides = value.get("balancer_overrides", {})
    if not isinstance(overrides, dict) or overrides:
        _fail("UNSUPPORTED_RUNTIME_OVERRIDE")
    config = value["effective_config"]
    if not isinstance(config, dict) or channels.digest(config) != value["effective_config_sha256"]:
        _fail("EFFECTIVE_HASH")
    if channels.digest({"effective_config": config, "balancer_overrides": overrides}) != value["execution_sha256"]:
        _fail("EXECUTION_HASH")
    transition = value.get("selector_transition")
    view = {"schema_version": 3, "engine_instance": {"boot_id": identity["boot_id"], "pid": identity["pid"], "start_ticks": identity["start_ticks"]},
            "effective_config_sha256": value["effective_config_sha256"], "native_generation": identity["generation"],
            "mutation_epoch": value["epoch"], "selector_transitions": [] if transition is None else [transition]}
    channels.validate_runtime_view(view)
    if transition is not None and (transition["generation"] != view["native_generation"] or transition["to_epoch"] != view["mutation_epoch"] or transition["after_sha256"] != view["effective_config_sha256"]):
        _fail("SELECTOR_CONTEXT")
    return view, config


def collect():
    """Return (private runtime-view3, full executing JSON) in memory after fresh IO."""
    try:
        if not sys.platform.startswith("linux"):
            _fail("LINUX_REQUIRED")
        approval = _load_approval()
        with contextlib.ExitStack() as stack:
            with _connection() as (_, peer, _):
                pass
            producer = _process(peer)
            executable = _open(f"/proc/{peer}/exe", stack)
            if _hash(executable) != approval["producer_sha256"]:
                _fail("EXECUTABLE_PIN")
            trusted_producer = producer, executable, os.fstat(executable.fileno())
            first, nonce, first_peer = _frame(peer)
            _snapshot(first, nonce, first_peer, approval)
            observed = _observe(first["identity"], peer, approval, stack, trusted_producer)
            second, next_nonce, next_peer = _frame(peer)
            final = _snapshot(second, next_nonce, next_peer, approval)
            if nonce == next_nonce or peer != next_peer or {k: v for k, v in first.items() if k != "challenge"} != {k: v for k, v in second.items() if k != "challenge"}:
                _fail("EXECUTION_CHANGED_DURING_COLLECTION")
            _recheck(observed)
        return final
    except ExecutionUnavailable:
        raise
    except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError, AttributeError):
        raise ExecutionUnavailable("EXECUTION_NOT_VERIFIED") from None
