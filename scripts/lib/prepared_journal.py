"""Read-only historical prepared-journal validation, shared with the Agent.

No execution collection, mutation, durability repair or readiness projection.
Only stdlib imports: an installer can copy these exact bytes to the Agent.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid

LIMIT = 4 << 20
HASH = re.compile(r"[0-9a-f]{64}\Z")
ARTIFACTS = {"config": "prepared.config.json", "runtime": "prepared.runtime.json",
             "template": "prepared.xray.json", "process": "prepared.process.json"}
OUTCOME = {"NativeAttempted", "NativeSucceeded", "TemplatePersisted", "ConfigPersisted", "KnownCommit", "NoOp"}


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate journal key")
        result[key] = value
    return result


def _invalid_number(_):
    raise ValueError("invalid journal number")


def decode(data):
    value = json.loads(data, object_pairs_hook=_pairs, parse_constant=_invalid_number)
    if not isinstance(value, dict):
        raise ValueError("journal object required")
    return value


def read_private(path):
    """Bounded regular-file read; never follow a dangling/redirected marker."""
    path = Path(path)
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or path.is_symlink()
                or (os.name == "posix" and (before.st_uid != os.geteuid() or before.st_mode & 0o077))):
            raise ValueError("private regular journal file required")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read(LIMIT + 1)
        after, current = os.fstat(fd), path.lstat()
        identity = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
        if len(data) > LIMIT or identity(before) != identity(after) or identity(after) != identity(current):
            raise ValueError("journal file changed")
        return data
    finally:
        os.close(fd)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def digest(value):
    return sha(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode())


def _shape(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ValueError("unsupported journal shape")


def _hash(value):
    if not isinstance(value, str) or not HASH.fullmatch(value):
        raise ValueError("journal hash required")


def validate_intent(intent, transaction_id):
    _shape(intent, ("schema_version", "transaction_id", "route_id", "binding", "expected", "files", "before", "candidate"))
    if type(intent["schema_version"]) is not int or intent["schema_version"] != 1 or intent["transaction_id"] != transaction_id:
        raise ValueError("foreign journal intent")
    if not isinstance(intent["route_id"], str) or not intent["route_id"].startswith("route-"):
        raise ValueError("route binding required")
    _hash(intent["binding"])
    expected = intent["expected"]
    _shape(expected, ("generation", "epoch", "engine_instance", "before_sha256", "after_sha256", "normalized_sha256", "rule_tag", "inbound_tag", "target"))
    for value in (expected["generation"], expected["engine_instance"].get("boot_id")):
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError("canonical execution generation required")
    epoch = expected["epoch"]
    if not isinstance(epoch, str) or not re.fullmatch(r"0|[1-9][0-9]{0,19}", epoch) or int(epoch) >= 2**64-1:
        raise ValueError("bounded next epoch required")
    _shape(expected["engine_instance"], ("boot_id", "pid", "start_ticks"))
    if type(expected["engine_instance"]["pid"]) is not int or expected["engine_instance"]["pid"] <= 0:
        raise ValueError("process identity required")
    if not isinstance(expected["engine_instance"]["start_ticks"], str) or not re.fullmatch(r"0|[1-9][0-9]*", expected["engine_instance"]["start_ticks"]):
        raise ValueError("process start required")
    for key in ("before_sha256", "after_sha256", "normalized_sha256"):
        _hash(expected[key])
    if expected["before_sha256"] == expected["after_sha256"]:
        raise ValueError("native no-op is not a prepared commit")
    for key, prefix in (("rule_tag", "wm-rule-"), ("inbound_tag", "wm-route-")):
        if not isinstance(expected[key], str) or not expected[key].startswith(prefix):
            raise ValueError("owned selector required")
    if not isinstance(expected["target"], str) or not (expected["target"] == "wm-eu-only-block" or expected["target"].startswith("wm-exit-path-")):
        raise ValueError("owned target required")
    _shape(intent["files"], ("config", "runtime", "database", "process"))
    if any(not isinstance(value, str) or not Path(value).is_absolute() for value in intent["files"].values()):
        raise ValueError("admitted absolute storage paths required")
    if len(set(intent["files"].values())) != 4:
        raise ValueError("distinct storage paths required")
    _shape(intent["before"], ("config", "runtime", "owned", "template", "process"))
    for key, value in intent["before"].items():
        if value is not None or key not in ("runtime", "owned"):
            _hash(value)
    _shape(intent["candidate"], ARTIFACTS)
    for value in intent["candidate"].values():
        _hash(value)
    return intent


def expected_transition(intent):
    value = intent["expected"]
    return {"generation": value["generation"], "from_epoch": value["epoch"], "to_epoch": str(int(value["epoch"]) + 1),
            "before_sha256": value["before_sha256"], "after_sha256": value["after_sha256"],
            "normalized_sha256": value["normalized_sha256"], "rule_tag": value["rule_tag"]}


def validate_owner_result(value, transaction_id, intent_sha256):
    _shape(value, ("schema_version", "transaction_id", "intent_sha256", "outcome"))
    if type(value["schema_version"]) is not int or value["schema_version"] != 1 or value["transaction_id"] != transaction_id or value["intent_sha256"] != intent_sha256:
        raise ValueError("foreign owner result")
    _shape(value["outcome"], OUTCOME)
    if any(type(flag) is not bool for flag in value["outcome"].values()):
        raise ValueError("exact owner outcome required")
    return value


def known_owner_result(value):
    outcome = value["outcome"]
    return outcome["NoOp"] is False and all(outcome[key] is True for key in OUTCOME - {"NoOp"})


def prepared_status(directory):
    """None: ordinary journal; active: retain; committed: verified history only."""
    directory = Path(directory)
    if directory.is_symlink():
        return "active"
    pending, completed = directory / "prepared.apply.pending.json", directory / "prepared.apply.completed.json"
    if os.path.lexists(pending):
        return "active"
    origin = os.path.lexists(completed)
    try:
        result = decode(read_private(directory / "result.json"))
        if not origin and "prepared_commit" not in result:
            return None
        if not origin:
            return "active"
        plan = decode(read_private(directory / "plan.json"))
        if plan.get("schema_version") != 1 or type(plan["schema_version"]) is not int or plan.get("operation") != "auto-prepared-reconcile":
            return "active"
        _shape(result, ("status", "finished_at", "prepared_commit"))
        if result["status"] != "committed" or not isinstance(result["finished_at"], str):
            return "active"
        proof = result["prepared_commit"]
        _shape(proof, ("schema_version", "transaction_id", "intent_sha256", "intent", "owner_result_sha256", "selector_transition", "local_post"))
        if type(proof["schema_version"]) is not int or proof["schema_version"] != 1 or proof["transaction_id"] != directory.name:
            return "active"
        raw = read_private(completed)
        intent = validate_intent(decode(raw), directory.name)
        if sha(raw) != proof["intent_sha256"] or intent != proof["intent"] or proof["selector_transition"] != expected_transition(intent):
            return "active"
        reply_raw = read_private(directory / "prepared.owner-result.json")
        reply = validate_owner_result(decode(reply_raw), directory.name, sha(raw))
        if sha(reply_raw) != proof["owner_result_sha256"] or not known_owner_result(reply):
            return "active"
        candidates = {}
        for kind, filename in ARTIFACTS.items():
            data = read_private(directory / filename)
            if sha(data) != intent["candidate"][kind]:
                return "active"
            candidates[kind] = decode(data)
        _shape(proof["local_post"], ("config", "runtime", "template", "process"))
        for value in proof["local_post"].values():
            _hash(value)
        runtime_post = read_private(directory / "prepared.runtime.post.json")
        post = decode(runtime_post)
        route = intent["route_id"]
        state = candidates["runtime"].get("prepared_routes", {}).get(route)
        if (not isinstance(state, dict) or state.get("binding") != intent["binding"]
                or state.get("native_generation") != intent["expected"]["generation"]
                or state.get("mutation_epoch") != intent["expected"]["epoch"]
                or state.get("effective_config_sha256") != intent["expected"]["before_sha256"]):
            return "active"
        if (proof["local_post"]["config"] != intent["candidate"]["config"] or proof["local_post"]["runtime"] != sha(runtime_post)
                or proof["local_post"]["template"] != digest(candidates["template"]) or proof["local_post"]["process"] != digest(candidates["process"])
                or post.get("prepared_routes", {}).get(route) != state):
            return "active"
        return "committed"
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
        if origin:
            return "active"
        # Losing both marker names cannot make an unreadable prepared result
        # an ordinary snapshot rollback. Valid ordinary journals stay unchanged.
        try:
            plan = decode(read_private(directory / "plan.json"))
            if str(plan.get("operation", "")).startswith("auto-prepared-"):
                return "active"
        except (OSError, ValueError, TypeError, RecursionError):
            pass
        return None
