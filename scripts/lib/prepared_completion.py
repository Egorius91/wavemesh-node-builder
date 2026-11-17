"""Private prepared journal completion; no transport/caller is installed.

Only the future trusted owning adapter may call record_owner_result, while
holding the existing node mutation lock. Caller-supplied verified/force CLI
inputs are deliberately absent. Retention never settles the pending operation.
"""
from pathlib import Path
from contextlib import closing, contextmanager
import copy
import os
import sqlite3
import stat

import prepared_channels as channels
import prepared_execution as execution
import prepared_journal as reader
import transaction_state as journal

NODE_MUTATION_LOCK = Path("/run/lock/wavemesh-node.lock")


class CompletionUnavailable(RuntimeError):
    """Fixed diagnostic; no private reply/config/exception payload is exported."""


def _require_node_owner():
    # Builder already holds FD9. Do not reopen, replace or unlock its inode.
    if os.name != "posix" or os.geteuid() != 0:
        raise ValueError("existing node mutation owner required")
    import fcntl
    handle, current = os.fstat(9), NODE_MUTATION_LOCK.lstat()
    if (not stat.S_ISREG(handle.st_mode) or handle.st_nlink != 1 or handle.st_uid != 0
            or handle.st_mode & 0o022 or (handle.st_dev, handle.st_ino) != (current.st_dev, current.st_ino)):
        raise ValueError("existing node mutation owner required")
    fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _optional(path):
    try:
        data = reader.read_private(path)
    except FileNotFoundError:
        if os.path.lexists(path):
            raise ValueError("ambiguous local state") from None
        return None, {}
    return data, reader.decode(data)


def _owned(runtime, route_id):
    routes = runtime.get("prepared_routes", {})
    if not isinstance(routes, dict):
        raise ValueError("prepared runtime namespace is not an object")
    if route_id not in routes:
        return None
    if not isinstance(routes[route_id], dict):
        raise ValueError("prepared runtime state is not an object")
    return reader.digest(routes[route_id])


def _unowned(runtime, route_id):
    result = copy.deepcopy(runtime)
    routes = result.get("prepared_routes", {})
    if not isinstance(routes, dict):
        raise ValueError("prepared runtime namespace is not an object")
    routes.pop(route_id, None)
    if not routes:
        result.pop("prepared_routes", None)
    return result


@contextmanager
def _database_reader(path):
    path = Path(path)
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or path.is_symlink() or info.st_nlink != 1
            or (os.name == "posix" and (info.st_uid != os.geteuid() or info.st_mode & 0o077))):
        raise ValueError("private admitted database required")
    with closing(sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True, timeout=0)) as database:
        database.execute("PRAGMA query_only=ON")
        database.execute("BEGIN")
        yield database


def _template_row(database):
    rows = database.execute("SELECT value FROM settings WHERE key=? LIMIT 2", ("xrayTemplateConfig",)).fetchall()
    if len(rows) != 1 or not isinstance(rows[0][0], str) or len(rows[0][0].encode()) > reader.LIMIT:
        raise ValueError("one bounded existing template required")
    reader.decode(rows[0][0])
    return rows[0][0]


def _stored_template_raw(path):
    with _database_reader(path) as database:
        return _template_row(database)


def _stored_template(path):
    return reader.decode(_stored_template_raw(path))


def _approved_plan(intent, config):
    route = next(route for route in config["routes"] if route["id"] == intent["route_id"])
    plan = route["prepared_plan"]
    if (plan["channel"]["engine"] != {"name": "xray", "version": "26.10.21", "sha256": execution.CORE}
            or route.get("presentation", {}).get("published")):
        raise ValueError("unpublished pinned prepared route required")
    _, outbounds, binding = channels.bound_plan(config, intent["route_id"], plan)
    expected = intent["expected"]
    if (binding != intent["binding"] or expected["rule_tag"] != route["routing"]["rule_tag"]
            or expected["inbound_tag"] != route["entry"]["inbound_tag"]
            or expected["target"] not in {"wm-eu-only-block", *(item["tag"] for item in outbounds)}):
        raise ValueError("approved selector binding required")
    return plan


def _current_execution(intent, config):
    view, effective = execution.collect()
    view = channels.validate_runtime_view(view)
    expected = intent["expected"]
    if (view["engine_instance"] != expected["engine_instance"] or view["native_generation"] != expected["generation"]
            or view["mutation_epoch"] != str(int(expected["epoch"]) + 1)
            or view["effective_config_sha256"] != expected["after_sha256"]
            or view["selector_transitions"] != [reader.expected_transition(intent)]):
        raise ValueError("current execution is not this operation")
    plan = _approved_plan(intent, config)
    if (not reader.selector_matches(effective, expected)
            or channels.health_config_fingerprint(config, intent["route_id"], plan, effective, view) != expected["normalized_sha256"]):
        raise ValueError("current execution does not bind the approved selector")
    return view


def _readbacks(intent, candidate_template, owner_reply):
    stored = _stored_template(intent["files"]["database"])
    raw = reader.read_private(intent["files"]["process"])
    process = reader.decode(raw)
    if (reader.digest(stored) != reader.digest(candidate_template)
            or reader.sha(raw) != owner_reply["outcome"]["ProcessConfigSHA256"]
            or not reader.selector_matches(stored, intent["expected"])
            or not reader.selector_matches(process, intent["expected"])):
        raise ValueError("independent storage readback differs")
    return raw


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


def complete(transaction, owner_result=None):
    """Complete only this known writer's metadata under the existing node lock.

    No caller is wired to this source helper. A future trusted adapter owns the
    frozen intent and reply. This method never dispatches, restores, restarts,
    rewrites the process desired file, or relabels a pre-apply path sample.
    """
    try:
        _require_node_owner()
        directory = Path(transaction)
        if directory.is_symlink():
            raise ValueError("redirected completion directory")
        pending, completed = directory / "prepared.apply.pending.json", directory / "prepared.apply.completed.json"
        if os.path.lexists(completed):
            if reader.prepared_status(directory) != "committed":
                raise ValueError("completion origin requires reconciliation")
            journal._sync_directory(directory)
            return "committed"  # Historical metadata only, never current readiness.
        raw_intent = reader.read_private(pending)
        intent = reader.validate_intent(reader.decode(raw_intent), directory.name)
        if owner_result is not None:
            record_owner_result(directory, owner_result)
        reply_raw = reader.read_private(directory / "prepared.owner-result.json")
        reply = reader.validate_owner_result(reader.decode(reply_raw), directory.name, reader.sha(raw_intent))
        if not reader.known_owner_result(reply):
            raise ValueError("known writer completion is not proven")
        candidates, candidate_raw = {}, {}
        for kind, filename in reader.ARTIFACTS.items():
            data = reader.read_private(directory / filename)
            if reader.sha(data) != intent["candidate"][kind]:
                raise ValueError("frozen candidate changed")
            candidate_raw[kind] = data
            candidates[kind] = reader.decode(data)
        before_config = reader.read_private(directory / "config.before.json")
        if (reader.sha(before_config) != intent["before"]["config"]
                or reader.digest(reader.decode(before_config)) != reader.digest(candidates["config"])):
            raise ValueError("reconcile cannot change unrelated configuration")
        before_runtime, old_runtime = _optional(directory / "runtime.before.json")
        if ((None if before_runtime is None else reader.sha(before_runtime)) != intent["before"]["runtime"]
                or _owned(old_runtime, intent["route_id"]) != intent["before"]["owned"]
                or reader.digest(_unowned(old_runtime, intent["route_id"])) != reader.digest(_unowned(candidates["runtime"], intent["route_id"]))):
            raise ValueError("frozen runtime ownership differs")
        first = _current_execution(intent, candidates["config"])
        process_raw = _readbacks(intent, candidates["template"], reply)
        current_config = reader.read_private(intent["files"]["config"])
        if reader.sha(current_config) not in (intent["before"]["config"], intent["candidate"]["config"]):
            raise ValueError("third configuration value")
        _, current_runtime = _optional(intent["files"]["runtime"])
        route_id = intent["route_id"]
        candidate_owned = _owned(candidates["runtime"], route_id)
        if candidate_owned is None or _owned(current_runtime, route_id) not in (intent["before"]["owned"], candidate_owned):
            raise ValueError("third prepared runtime value")
        previous_result = reader.decode(reader.read_private(directory / "result.json"))
        if "prepared_commit" not in previous_result:
            merged = copy.deepcopy(current_runtime)
            merged.setdefault("prepared_routes", {})[route_id] = copy.deepcopy(candidates["runtime"]["prepared_routes"][route_id])
            journal.atomic_bytes(directory / reader.PROCESS_POST, process_raw)
            journal.atomic_json(directory / "prepared.runtime.post.json", merged)
            journal.atomic_bytes(intent["files"]["config"], candidate_raw["config"])
            journal.atomic_bytes(intent["files"]["runtime"], reader.read_private(directory / "prepared.runtime.post.json"))
        runtime_raw = reader.read_private(intent["files"]["runtime"])
        config_raw = reader.read_private(intent["files"]["config"])
        if (reader.sha(runtime_raw) != reader.sha(reader.read_private(directory / "prepared.runtime.post.json"))
                or reader.sha(config_raw) != intent["candidate"]["config"]
                or process_raw != _readbacks(intent, candidates["template"], reply)
                or reader.sha(process_raw) != reader.sha(reader.read_private(directory / reader.PROCESS_POST))
                or first != _current_execution(intent, candidates["config"])):
            raise ValueError("post-commit readback changed")
        proof = {"schema_version": 1, "transaction_id": directory.name, "intent_sha256": reader.sha(raw_intent),
                 "intent": intent, "owner_result_sha256": reader.sha(reply_raw),
                 "selector_transition": reader.expected_transition(intent),
                 "local_post": {"config": reader.sha(config_raw), "runtime": reader.sha(runtime_raw),
                                "template": reader.digest(candidates["template"]), "process": reader.sha(process_raw)}}
        if "prepared_commit" in previous_result:
            if (previous_result.get("status") != "committed"
                    or reader.digest(previous_result["prepared_commit"]) != reader.digest(proof)):
                raise ValueError("existing prepared proof differs")
        else:
            journal.atomic_json(directory / "result.json", {"status": "committed", "finished_at": journal.utc_now(), "prepared_commit": proof})
        if os.path.lexists(completed):
            raise ValueError("completed origin already exists")
        os.rename(pending, completed)
        journal._sync_directory(directory)
        if reader.prepared_status(directory) != "committed":
            raise ValueError("completion proof does not validate")
        return "committed"
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError, StopIteration, sqlite3.Error, execution.ExecutionUnavailable):
        raise CompletionUnavailable("PREPARED_COMPLETION_NOT_VERIFIED") from None
