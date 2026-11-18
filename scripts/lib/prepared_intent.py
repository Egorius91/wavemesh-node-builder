"""Private, unwired preparation only. No CLI, owner send or capability change.

Caller already owns FD9 and a newly begun reconcile transaction. Failed writes
retain their facts; this writer never retries, converts legacy debt or applies.
"""
import copy
import os
from pathlib import Path
import sqlite3
import stat
import time

import prepared_channels as channels
import prepared_completion as completion
import prepared_evaluation as evaluation
import prepared_execution as execution
import prepared_journal as reader
import transaction_state as journal


class IntentUnavailable(RuntimeError):
    """Fixed diagnostic, without configuration, credentials or private paths."""


def prepare_once(transaction, route_id, files, observations):
    """Freeze one schema1 intent; return only after all durability barriers.

    files are the owning adapter's admitted storage paths, not runtime evidence.
    The full executing image/identity is collected internally exactly once.
    """
    try:
        completion._require_node_owner()
        directory = Path(transaction)
        info = directory.lstat()
        if (not directory.is_absolute() or directory.resolve() != directory
                or not stat.S_ISDIR(info.st_mode)
                or (os.name == "posix" and (info.st_uid != os.geteuid() or info.st_mode & 0o077))):
            raise ValueError("one private transaction required")
        plan = reader.decode(reader.read_private(directory / "plan.json"))
        result = reader.decode(reader.read_private(directory / "result.json"))
        if (type(plan.get("schema_version")) is not int or plan["schema_version"] != 1
                or plan.get("operation") != "auto-prepared-reconcile" or result.get("status") != "in_progress"):
            raise ValueError("new reconcile transaction required")
        if any(item["id"] != directory.name for item in journal.incomplete(directory.parent)):
            raise ValueError("other unresolved transaction")
        # Do not overwrite even a partially prepared attempt or a dangling marker.
        outputs = [*reader.ARTIFACTS.values(), "config.before.json", "runtime.before.json",
                   "process.before.json", "prepared.template.before.json", "prepared.apply.pending.json",
                   "prepared.owner-request.json", "prepared.owner-result.json", "prepared.apply.completed.json"]
        if any(os.path.lexists(directory / name) for name in outputs):
            raise ValueError("existing preparation or dispatch debt")
        reader._shape(files, ("config", "runtime", "database", "process"))
        paths = {key: Path(value) for key, value in files.items()}
        if (any(not path.is_absolute() or path.resolve() != path or directory in path.parents for path in paths.values())
                or len(set(paths.values())) != 4):
            raise ValueError("distinct admitted storage paths required")
        files = {key: str(path) for key, path in paths.items()}
        config_raw = reader.read_private(paths["config"])
        config = reader.decode(config_raw)
        runtime_raw, runtime = completion._optional(paths["runtime"])
        process_raw, _ = completion._optional(paths["process"])
        template_raw = completion._stored_template_raw(paths["database"])
        template = reader.decode(template_raw)
        evaluated, view, effective = evaluation._evaluate_collected(config, runtime, route_id, template, observations)
        candidates = evaluated["candidate"]
        if (evaluated["update_required"] is not True or reader.digest(config) != reader.digest(candidates["config"])
                or reader.digest(completion._unowned(runtime, route_id))
                != reader.digest(completion._unowned(candidates["runtime"], route_id))):
            raise ValueError("one owned selector update required")
        route = next(route for route in config["routes"] if route["id"] == route_id)
        rule_tag, inbound_tag = route["routing"]["rule_tag"], route["entry"]["inbound_tag"]
        target = next(rule for rule in candidates["template"]["routing"]["rules"] if rule.get("ruleTag") == rule_tag)["outboundTag"]
        after = copy.deepcopy(effective)
        next(rule for rule in after["routing"]["rules"] if rule.get("ruleTag") == rule_tag)["outboundTag"] = target
        expected = {"generation": view["native_generation"], "epoch": view["mutation_epoch"],
                    "engine_instance": view["engine_instance"], "before_sha256": view["effective_config_sha256"],
                    "after_sha256": reader.digest(after),
                    "normalized_sha256": channels.health_config_fingerprint(config, route_id, route["prepared_plan"], effective, view),
                    "rule_tag": rule_tag, "inbound_tag": inbound_tag, "target": target}
        intent = {"schema_version": 1, "transaction_id": directory.name, "route_id": route_id,
                  "binding": evaluated["binding"], "expected": expected, "files": files,
                  "before": {"config": reader.sha(config_raw), "runtime": None if runtime_raw is None else reader.sha(runtime_raw),
                             "owned": completion._owned(runtime, route_id), "template": reader.sha(template_raw.encode()),
                             "process": None if process_raw is None else reader.sha(process_raw)},
                  "candidate": {key: "0" * 64 for key in reader.ARTIFACTS}}
        reader.validate_intent(intent, directory.name)
        completion._approved_plan(intent, candidates["config"])
        if not reader.selector_matches(candidates["template"], expected):
            raise ValueError("candidate selector differs")

        def unchanged():
            completion._require_node_owner()
            if not channels.state_is_current(candidates["runtime"]["prepared_routes"][route_id],
                                             intent["binding"], view, int(time.time()), route["prepared_plan"]["health"]["max_age_seconds"]):
                raise ValueError("path sample expired during preparation")
            if (reader.read_private(paths["config"]) != config_raw
                    or completion._optional(paths["runtime"])[0] != runtime_raw
                    or completion._optional(paths["process"])[0] != process_raw
                    or completion._stored_template_raw(paths["database"]) != template_raw):
                raise ValueError("preimage changed during preparation")

        unchanged()
        for name, raw in (("config.before.json", config_raw), ("runtime.before.json", runtime_raw),
                          ("process.before.json", process_raw), ("prepared.template.before.json", template_raw.encode())):
            if raw is not None:
                journal.atomic_bytes(directory / name, raw)
        for key, name in reader.ARTIFACTS.items():
            journal.atomic_json(directory / name, candidates[key])
            raw = reader.read_private(directory / name)
            if reader.decode(raw) != candidates[key]:
                raise ValueError("candidate storage differs")
            intent["candidate"][key] = reader.sha(raw)
        unchanged()
        reader.validate_intent(intent, directory.name)
        journal.atomic_json(directory / "prepared.apply.pending.json", intent)
        if reader.decode(reader.read_private(directory / "prepared.apply.pending.json")) != intent:
            raise ValueError("frozen intent readback differs")
        unchanged()
        return intent
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError,
            StopIteration, RuntimeError, sqlite3.Error, execution.ExecutionUnavailable):
        raise IntentUnavailable("PREPARED_INTENT_NOT_FROZEN") from None
