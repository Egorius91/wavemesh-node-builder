#!/usr/bin/env python3
"""Idempotent local 3X-UI access lifecycle for the WaveMesh Node Agent."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from functools import wraps
import hashlib
from datetime import datetime
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any
from urllib import error, parse, request
import uuid

# The installed Agent receives these same Builder-owned source bytes. Source
# checkouts use that library directly; there is one journal schema validator.
source_library = Path(__file__).resolve().parents[1] / "scripts/lib"
if source_library.is_dir():
    sys.path.insert(0, str(source_library))
from prepared_journal import prepared_status

SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
DAY_MILLISECONDS = 24 * 60 * 60 * 1000
NODE_MUTATION_LOCK = Path("/run/lock/wavemesh-node.lock")


class ProvisionError(RuntimeError):
    pass


@contextmanager
def node_mutation_lock(path: Path | None = None):
    """Share the CLI's flock inode; never truncate, replace or unlink it."""
    if os.name != "posix":
        raise ProvisionError("Node mutation locking requires POSIX")
    import fcntl

    path = NODE_MUTATION_LOCK if path is None else path
    fd = None
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != os.geteuid() or info.st_mode & 0o022):
            raise ProvisionError("Node mutation lock is unsafe")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ProvisionError("Node mutation is busy") from None
        current = path.lstat()
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            raise ProvisionError("Node mutation lock changed")
        yield
    finally:
        if fd is not None:
            os.close(fd)


def assert_no_pending_node_transaction(root: Path) -> list[Path]:
    """A dead CLI releases flock but its pending rollback still fences Agent writes."""
    if root.is_symlink():
        raise ProvisionError("Node transaction state is unsafe")
    if not root.exists():
        return []
    if not root.is_dir():
        raise ProvisionError("Node transaction state is unsafe")
    terminal_history = []
    for directory in root.iterdir():
        if directory.is_symlink() or not directory.is_dir():
            raise ProvisionError("Node transaction state is unsafe")
        prepared = prepared_status(directory)
        if prepared == "active":
            raise ProvisionError("Node transaction reconciliation required")
        try:
            plan = load_private_state(directory / "plan.json")
            result = load_private_state(directory / "result.json")
        except (OSError, ValueError):
            raise ProvisionError("Node transaction reconciliation required") from None
        # Accept only the CLI's known terminal states. Missing, malformed and
        # future/unknown states must not silently allow a subsequent DB rollback.
        if (not plan or type(plan.get("schema_version")) is not int
                or plan["schema_version"] != 1 or not result
                or result.get("status") not in {"committed", "rolled_back"}):
            raise ProvisionError("Node transaction reconciliation required")
        if prepared in {"committed", "rolled_back"}:
            terminal_history.append(directory)
    # Read-only collectors ignore this list. Only a subsequent writer repeats
    # the directory barrier, under the node flock, before changing any state.
    return terminal_history


@contextmanager
def access_lock(state_root: Path, access_id: str):
    """Serialize executors; process death releases the operating-system lock."""
    state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if state_root.is_symlink():
        raise ProvisionError("Access state root is unsafe")
    path = state_root / f"{access_id}.lock"
    if path.is_symlink():
        raise ProvisionError("Access lock path is unsafe")
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if os.name == "nt":
            import msvcrt
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"0")
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def placement_binding(request_value: dict[str, Any]) -> dict[str, Any] | None:
    """Bind the existing local fence to the authenticated v2 command lineage."""
    schema = request_value.get("schema_version", 1)
    if type(schema) is not int or schema not in {1, 2, 3}:
        raise ProvisionError("Access command schema is invalid")
    if schema == 1:
        return None
    placement = request_value.get("placement_id")
    if not isinstance(placement, str) or not re.fullmatch(r"(?:legacy:)?[A-Za-z0-9_-]{1,128}", placement):
        raise ProvisionError("Access placement identity is invalid")
    result = {"schema_version": schema, "placement_id": placement}
    for name in ("tenant_id", "node_id", "command_id"):
        value = request_value.get(name)
        if not isinstance(value, str):
            raise ProvisionError("Access placement binding is invalid")
        result[name] = safe_id(value, name)
    for name in ("generation", "entitlement_revision"):
        value = request_value.get(name)
        if type(value) is not int:
            raise ProvisionError("Access placement revision is invalid")
        result[name] = integer(value, 1, 2_147_483_647)
    digest = request_value.get("plan_hash")
    if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise ProvisionError("Access placement plan hash is invalid")
    result["plan_hash"] = digest
    expected = request_value.get("expected_version")
    desired = request_value.get("desired_version")
    if type(expected) is not int or type(desired) is not int or not 0 <= expected < desired <= 2_147_483_647:
        raise ProvisionError("Access placement expected version is invalid")
    result["expected_version"] = expected
    if schema == 3:
        if type(request_value.get("quota_protocol_version")) is not int or request_value["quota_protocol_version"] != 2:
            raise ProvisionError("Placement quota protocol is unsupported")
        result["quota_protocol_version"] = 2
        if "traffic_budget" not in request_value:
            raise ProvisionError("Placement traffic budget is missing")
        result["traffic_budget"] = validate_traffic_budget(request_value["traffic_budget"])
    return result


def quota_bytes(value: Any) -> int:
    if not isinstance(value, str) or not re.fullmatch(r"0|[1-9][0-9]{0,18}", value) or int(value) > 9_223_372_036_854_775_807:
        raise ProvisionError("Traffic counter is invalid")
    return int(value)


def validate_traffic_budget(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    fields = {"epoch_id", "baseline_receipt_id", "credit_receipt_id", "credit_revision", "counter_baseline_bytes",
              "counter_identity_hash", "counter_source_receipt_id", "credit_bytes"}
    if not isinstance(value, dict) or set(value) != fields:
        raise ProvisionError("Placement traffic budget shape is invalid")
    if not isinstance(value["epoch_id"], str) or not re.fullmatch(r"[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}", value["epoch_id"]):
        raise ProvisionError("Placement meter epoch is invalid")
    for name in ("baseline_receipt_id", "credit_receipt_id"):
        if not isinstance(value[name], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value[name]):
            raise ProvisionError("Placement credit receipt is invalid")
    if type(value["credit_revision"]) is not int or not 1 <= value["credit_revision"] <= 2_147_483_647:
        raise ProvisionError("Placement credit revision is invalid")
    base, credit = quota_bytes(value["counter_baseline_bytes"]), quota_bytes(value["credit_bytes"])
    if credit == 0 or base + credit > 9_223_372_036_854_775_807:
        raise ProvisionError("Placement credit is invalid")
    identity, source = value["counter_identity_hash"], value["counter_source_receipt_id"]
    if identity is None:
        if base != 0 or source is not None:
            raise ProvisionError("New placement meter must start at zero")
    elif (not isinstance(identity, str) or not re.fullmatch(r"[a-f0-9]{64}", identity) or
          not isinstance(source, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", source)):
        raise ProvisionError("Placement counter source is invalid")
    return dict(value)


def placement_quota_limit(value: dict[str, Any]) -> int:
    cap = integer(value.get("quota_bytes"), 0, 9_223_372_036_854_775_807)
    if value.get("schema_version") != 3:
        return cap
    cap = quota_bytes(value.get("quota_bytes"))
    budget = validate_traffic_budget(value.get("traffic_budget"))
    if budget is None:
        if value.get("enabled") is True and cap != 0:
            raise ProvisionError("Enabled finite access requires reserved credit")
        return cap
    if value.get("enabled") is True and (cap == 0 or quota_bytes(budget["credit_bytes"]) > cap):
        raise ProvisionError("Placement credit exceeds current rights")
    return quota_bytes(budget["counter_baseline_bytes"]) + quota_bytes(budget["credit_bytes"])


def quota_epochs(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProvisionError("Placement quota history is invalid")
    result = {}
    for key, epoch in value.items():
        if not isinstance(epoch, dict) or set(epoch) != {"budget", "identity_hash", "up_bytes", "down_bytes", "terminal"}:
            raise ProvisionError("Placement quota epoch shape is invalid")
        budget = validate_traffic_budget(epoch["budget"])
        if budget is None or key != budget["epoch_id"] or type(epoch["terminal"]) is not bool:
            raise ProvisionError("Placement quota epoch is invalid")
        identity = epoch["identity_hash"]
        if identity is not None and (not isinstance(identity, str) or not re.fullmatch(r"[a-f0-9]{64}", identity)):
            raise ProvisionError("Placement meter identity is invalid")
        up, down = quota_bytes(epoch["up_bytes"]), quota_bytes(epoch["down_bytes"])
        if up + down > 9_223_372_036_854_775_807 or identity is None and (up or down):
            raise ProvisionError("Placement meter counter is invalid")
        result[key] = {**epoch, "budget": budget}
    return result


def quota_counters(value: Any) -> dict[str, dict[str, str]]:
    """Retain every observed identity, including before its first credit."""
    if not isinstance(value, list) or len(value) > 1_000_000:
        raise ProvisionError("Placement counter history is invalid")
    result = {}
    for counter in value:
        if (not isinstance(counter, dict) or set(counter) != {"identity_hash", "up_bytes", "down_bytes"} or
                not isinstance(counter["identity_hash"], str) or not re.fullmatch(r"[a-f0-9]{64}", counter["identity_hash"]) or
                counter["identity_hash"] in result or
                quota_bytes(counter["up_bytes"]) + quota_bytes(counter["down_bytes"]) > 9_223_372_036_854_775_807):
            raise ProvisionError("Placement counter history is invalid")
        result[counter["identity_hash"]] = dict(counter)
    return result


def advance_quota_epochs(previous: dict[str, Any], budget: dict[str, Any] | None, enabled: bool) -> dict[str, Any]:
    epochs = quota_epochs(previous)
    if budget is None:
        if enabled and any(not e["terminal"] for e in epochs.values()):
            raise ProvisionError("Unsettled quota prevents an unlimited transition")
        return epochs
    old = epochs.get(budget["epoch_id"])
    if old is not None:
        if old["terminal"] and enabled:
            raise ProvisionError("A settled placement meter cannot reopen")
        if budget != old["budget"]:
            fixed = set(budget) - {"credit_receipt_id", "credit_revision", "credit_bytes"}
            if (old["terminal"] or any(budget[k] != old["budget"][k] for k in fixed) or
                    budget["credit_revision"] != old["budget"]["credit_revision"] + 1 or
                    budget["credit_receipt_id"] == old["budget"]["credit_receipt_id"] or
                    quota_bytes(budget["credit_bytes"]) <= quota_bytes(old["budget"]["credit_bytes"])):
                raise ProvisionError("Placement credit chain is invalid")
            epochs[budget["epoch_id"]] = {**old, "budget": budget}
    else:
        if enabled and any(not e["terminal"] for e in epochs.values()):
            raise ProvisionError("Previous placement credit must be disabled first")
        epochs[budget["epoch_id"]] = {"budget": budget, "identity_hash": None, "up_bytes": "0", "down_bytes": "0", "terminal": False}
    return epochs


def quiesce_placement_panel(config: dict[str, Any]) -> None:
    """End an uncertain local HTTP handler before reconciling its effect.

    Killing the CLI alone cannot cancel a request already received by 3X-UI.
    The fixed Builder-managed service must stop its whole process group before
    starting a fresh invocation. Budgeted physical denials also use this barrier
    after persisting their disables; completed non-budgeted commands do not.
    Caller holds the Node/access locks; any failure retains the incomplete fence.
    """
    if os.name != "posix":
        raise ProvisionError("Placement uncertainty requires native service reconciliation")
    properties = ("Id", "LoadState", "ActiveState", "SubState", "MainPID", "ControlPID", "InvocationID",
                  "KillMode", "SendSIGKILL", "NeedDaemonReload", "Job", "ControlGroup", "Type",
                  "Triggers", "TriggeredBy", "OnFailure", "OnSuccess", "FileDescriptorStoreMax", "NFileDescriptorStore")

    def run(arguments: list[str], timeout: int):
        try:
            result = subprocess.run(["/usr/bin/systemctl", *arguments], check=False, capture_output=True,
                text=True, timeout=timeout, env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"})
        except (OSError, subprocess.SubprocessError) as exc:
            raise ProvisionError("Placement service reconciliation outcome is unknown") from exc
        if result.returncode != 0:
            raise ProvisionError("Placement service reconciliation failed")
        return result.stdout

    def unit():
        raw = run(["show", "x-ui.service", "--no-pager", "--property=" + ",".join(properties)], 10)
        values = {}
        if len(raw) > 32768:
            raise ProvisionError("Placement service state is invalid")
        for line in raw.splitlines():
            key, separator, value = line.partition("=")
            if not separator or key not in properties or key in values:
                raise ProvisionError("Placement service state is invalid")
            values[key] = value
        # systemctl prints the null D-Bus Job as an empty value on this release.
        # Both representations mean no job; every nonzero/unknown job stays closed.
        if values.get("Job") == "":
            values["Job"] = "0"
        if (set(values) != set(properties) or values["Id"] != "x-ui.service" or values["LoadState"] != "loaded" or
                values["Type"] != "simple" or values["KillMode"] != "control-group" or values["SendSIGKILL"] != "yes" or
                values["NeedDaemonReload"] != "no" or values["Job"] != "0" or values["ControlPID"] != "0" or
                values["FileDescriptorStoreMax"] != "0" or values["NFileDescriptorStore"] != "0" or
                values["ActiveState"] not in {"active", "inactive", "failed"} or not values["MainPID"].isdigit() or
                any(values[k] for k in ("Triggers", "TriggeredBy", "OnFailure", "OnSuccess")) or
                (int(values["MainPID"]) > 0 and values["ControlGroup"] != "/system.slice/x-ui.service")):
            raise ProvisionError("Placement service cannot prove executor quiescence")
        return values

    before = unit()
    run(["restart", "--job-mode=fail", "x-ui.service"], 45)
    after = unit()
    if (after["ActiveState"] != "active" or after["SubState"] != "running" or int(after["MainPID"]) < 1 or
            not re.fullmatch(r"[a-f0-9]{32}", after["InvocationID"]) or after["InvocationID"] == before["InvocationID"]):
        raise ProvisionError("Placement service reconciliation was not verified")
    panel = PanelClient(config, timeout=1)
    deadline = time.monotonic() + 20
    while True:
        try:
            ready = panel.call("GET", "/panel/api/clients/list")
            if ready.get("success") is True and isinstance(ready.get("obj"), list):
                return
        except ProvisionError:
            pass
        if time.monotonic() >= deadline:
            raise ProvisionError("Placement service did not become ready after reconciliation")
        time.sleep(0.1)


def fenced_access(operation: str):
    def decorate(function):
        @wraps(function)
        def execute(request_value, config, state_root):
            access_id = safe_id(request_value.get("access_id"), "access_id")
            version = integer(request_value.get("desired_version"), 1, 2_147_483_647)
            binding = placement_binding(request_value)
            with access_lock(state_root, access_id):
                fence_path = state_root / f"{access_id}.fence"
                fence = load_private_state(fence_path)
                highest = 0
                for path in state_root.glob(f"{access_id}.*.json"):
                    state = load_private_state(path)
                    if state is None:
                        raise ProvisionError("Durable access state disappeared")
                    candidate = integer(state.get("desired_version"), 1, 2_147_483_647)
                    validate_state(state, access_id, candidate)
                    if path.name != f"{access_id}.{candidate}.json":
                        raise ProvisionError("Durable access state filename is invalid")
                    highest = max(highest, candidate)
                if fence is not None:
                    # A V2 fence deliberately has no legacy payload_hash. The
                    # actual pre-placement executable requires that field and
                    # must fail before mutation, even at a higher version.
                    hash_key = "payload_hash" if "schema_version" not in fence else "placement_payload_hash"
                    if (fence.get("access_id") != access_id or
                            not re.fullmatch(r"[a-f0-9]{64}", str(fence.get(hash_key) or ""))):
                        raise ProvisionError("Access command fence is invalid")
                    highest = max(highest, integer(fence.get("version"), 1, 2_147_483_647))
                    if "schema_version" not in fence:
                        if set(fence) != {"access_id", "version", "payload_hash"}:
                            raise ProvisionError("Legacy access fence shape is invalid")
                    else:
                        if type(fence["schema_version"]) is not int or fence["schema_version"] not in {2, 3}:
                            raise ProvisionError("Access placement fence schema is invalid")
                        stored_binding = placement_binding({**fence, "desired_version": fence["version"]})
                        shape = {"access_id", "version", "placement_payload_hash", *stored_binding}
                        if fence["schema_version"] == 3:
                            shape.update({"quota_epochs", "quota_counters", "quota_history_complete", "quota_material", "quota_material_hash"})
                            quota_epochs(fence.get("quota_epochs"))
                            quota_counters(fence.get("quota_counters"))
                            if type(fence.get("quota_history_complete")) is not bool:
                                raise ProvisionError("Placement quota history proof is invalid")
                            cached, cached_hash = fence.get("quota_material"), fence.get("quota_material_hash")
                            if (cached is None and cached_hash is not None or cached is not None and
                                    (not isinstance(cached, dict) or cached_hash != hashlib.sha256(json.dumps(cached, sort_keys=True).encode()).hexdigest())):
                                raise ProvisionError("Placement quota execution receipt is invalid")
                        if set(fence) not in (shape, shape | {"execution_complete"}) or (
                                "execution_complete" in fence and type(fence["execution_complete"]) is not bool):
                            raise ProvisionError("Access placement fence shape is invalid")
                    if fence.get("schema_version") in {2, 3}:
                        if binding is None:
                            raise ProvisionError("Legacy access writer is fenced")
                        if fence["schema_version"] == 3 and binding["schema_version"] != 3:
                            raise ProvisionError("Pre-budget placement writer is fenced")
                        if fence["schema_version"] == 3 and not fence["quota_history_complete"] and request_value.get("enabled") is True:
                            raise ProvisionError("Placement quota history requires reconciliation")
                        for name in ("tenant_id", "node_id", "placement_id"):
                            if fence.get(name) != binding[name]:
                                raise ProvisionError("Access placement fence identity mismatch")
                        for name in ("generation", "entitlement_revision"):
                            previous = fence.get(name)
                            if type(previous) is not int or previous < 1 or binding[name] < previous:
                                raise ProvisionError("Access placement fence revision is stale")
                if binding is not None and binding["expected_version"] > highest:
                    raise ProvisionError("Access placement local state is behind expected version")
                if binding is not None and binding["schema_version"] == 3 and request_value.get("enabled") is True and highest > 0 and fence is None:
                    raise ProvisionError("Placement quota history is missing")
                if version < highest:
                    raise ProvisionError("Access command version is stale")
                uncertain = binding is not None and highest > 0 and (not fence or fence.get("version") != highest or fence.get("execution_complete") is not True)
                if operation == "cleanup" and binding is not None and version != highest:
                    raise ProvisionError("Placement cleanup must own the current command")
                if operation != "cleanup" or binding is not None:
                    effective_operation = request_value.get("operation", "access.replace_credential" if operation == "cleanup" else operation)
                    allowed = ({"access.provision", "access.replace_credential", "access.prepare_replacement"}
                               if operation == "access.provision" else {"access.replace_credential" if operation == "cleanup" else operation})
                    if effective_operation not in allowed:
                        raise ProvisionError("Access operation mismatch")
                    enabled = request_value.get("enabled", operation == "access.provision")
                    preparing = effective_operation == "access.prepare_replacement"
                    if type(enabled) is not bool or (preparing and enabled) or (
                        operation == "access.provision" and not preparing and not enabled
                    ):
                        raise ProvisionError("Access enabled state is invalid")
                    canonical = {
                        "operation": effective_operation, "access_id": access_id,
                        "version": version, "enabled": enabled,
                        "expires_at_ms": int(parse_time(request_value.get("expires_at")).timestamp() * 1000),
                        "device_limit": integer(request_value.get("device_limit"), 0, 10_000),
                        "quota_bytes": integer(request_value.get("quota_bytes"), 0, 9_223_372_036_854_775_807),
                    }
                    if binding is not None and binding["schema_version"] == 3:
                        placement_quota_limit(request_value)
                    if binding is not None:
                        if enabled and version > highest and binding["expected_version"] != highest:
                            raise ProvisionError("Enabled access requires reconciled local version")
                        canonical.update(binding)
                    if preparing:
                        canonical["replacement_id"] = safe_id(request_value.get("replacement_id"), "replacement_id")
                    digest = hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()
                    hash_key = "placement_payload_hash" if binding is not None else "payload_hash"
                    if version == highest:
                        if fence is None or fence.get("version") != version or fence.get(hash_key) != digest:
                            raise ProvisionError("Access command replay cannot be verified")
                        if (binding is not None and binding["schema_version"] == 3 and operation != "cleanup" and
                                fence.get("execution_complete") is True and fence.get("quota_material") is not None):
                            # A durable applied receipt is not a live observation.
                            # Lost HTTP replies replay the exact material bytes,
                            # without re-enabling an exhausted/reset client.
                            return fence["quota_material"]
                    if version > highest or binding is not None:
                        epochs = {}
                        history_complete = False
                        if binding is not None and binding["schema_version"] == 3:
                            previous_epochs = (fence or {}).get("quota_epochs", {})
                            epochs = advance_quota_epochs(previous_epochs, binding["traffic_budget"], enabled)
                            history_complete = fence["quota_history_complete"] if (fence or {}).get("schema_version") == 3 else fence is not None or highest == 0
                            if not enabled and binding["traffic_budget"] is not None and binding["traffic_budget"]["epoch_id"] not in previous_epochs:
                                history_complete = False
                        atomic_json(fence_path, {"access_id": access_id, "version": version, hash_key: digest,
                                                **(binding or {}), **({"execution_complete": False} if binding is not None else {}),
                                                **({"quota_epochs": epochs, "quota_counters": (fence or {}).get("quota_counters", []),
                                                    "quota_history_complete": history_complete,
                                                    "quota_material": (fence or {}).get("quota_material") if version == highest else None,
                                                    "quota_material_hash": (fence or {}).get("quota_material_hash") if version == highest else None}
                                                   if binding is not None and binding["schema_version"] == 3 else {})})
                if uncertain:
                    quiesce_placement_panel(config)
                result = function(request_value, config, state_root)
                counters = []
                if binding is not None and binding["schema_version"] == 3 and operation != "cleanup":
                    if request_value["enabled"] is False and result.get("disabled_identity_count", 0) > 0:
                        # Panel disabled/new-auth refusal can precede the old
                        # engine's exit. End the managed process group after
                        # persisting every deny, before final counter/material
                        # acceptance or a subsequent profile can be enabled.
                        quiesce_placement_panel(config)
                    counters = record_quota_counters(request_value, config, state_root)
                if binding is not None:
                    # Persist terminal local execution before publishing output.
                    # A lost master response can replay without restarting 3X-UI.
                    completed = load_private_state(fence_path)
                    if completed is None:
                        raise ProvisionError("Placement execution fence disappeared")
                    atomic_json(fence_path, {**completed, "execution_complete": True})
                if binding is not None and operation != "cleanup":
                    # The authenticated Agent checks these against its envelope;
                    # an old runtime which ignored v2 cannot produce this proof.
                    result = {**result, "schema_version": binding["schema_version"],
                              **{k: binding[k] for k in ("placement_id", "generation", "entitlement_revision", "plan_hash", "command_id")},
                              "client_exists": result.get("client_exists", True),
                              "enabled": request_value["enabled"]}
                    if binding["schema_version"] == 3:
                        result.update(quota_protocol_version=2, traffic_budget=binding["traffic_budget"], traffic_counters=counters,
                                      quota_history_complete=completed["quota_history_complete"])
                        atomic_json(fence_path, {**completed, "execution_complete": True, "quota_material": result,
                            "quota_material_hash": hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()})
                return result
        return execute
    return decorate


class PanelClient:
    def __init__(self, config: dict[str, Any], timeout: int = 20) -> None:
        panel = object_value(config.get("panel"))
        token = str(object_value(panel.get("api_auth")).get("token") or "")
        port = integer(panel.get("listen_port"), 1, 65535)
        path = "/" + str(panel.get("path") or "").strip("/") + "/"
        if not token or not SAFE_ID.fullmatch(token):
            raise ProvisionError("3X-UI bearer API token is unavailable")
        self.base = f"http://127.0.0.1:{port}{path.rstrip('/')}"
        self.token = token
        self.timeout = timeout

    def call(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        body = None if payload is None else json.dumps(payload, separators=(",", ":")).encode()
        req = request.Request(
            self.base + path,
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "User-Agent": "wavemesh-access-runtime/1",
            },
        )
        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                raw = response.read()
        except (error.HTTPError, error.URLError) as exc:
            raise ProvisionError("3X-UI request failed") from exc
        try:
            value = json.loads(raw.decode())
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProvisionError("3X-UI returned invalid JSON") from exc
        if not isinstance(value, dict) or value.get("success") is not True:
            raise ProvisionError("3X-UI operation was rejected")
        return value


@fenced_access("access.provision")
def provision(request_value: dict[str, Any], config: dict[str, Any], state_root: Path) -> dict[str, Any]:
    access_id = safe_id(request_value.get("access_id"), "access_id")
    desired_version = integer(request_value.get("desired_version"), 1, 2_147_483_647)
    enabled = request_value.get("enabled", True)
    expires_at = parse_time(request_value.get("expires_at"))
    device_limit = integer(request_value.get("device_limit"), 0, 10_000)
    quota_bytes = placement_quota_limit(request_value)
    state_path = state_root / f"{access_id}.{desired_version}.json"
    state = load_private_state(state_path)
    panel = None
    if request_value.get("schema_version") in {2, 3}:
        # Validate retained identities/inventory before minting another identity.
        # An explicit replacement may retire known clients; initial provision
        # requires absence except for this same command's resumable identity.
        states = retained_access_states(state_root, access_id, desired_version)
        panel = PanelClient(config)
        assert_placement_inventory(panel, access_id, states)
        for email, previous in states.items():
            record = get_client(panel, email)
            if record is not None:
                assert_matching_client(record, previous, [])
                if request_value.get("operation", "access.provision") == "access.provision" and (state is None or email != state["panel_email"]):
                    raise ProvisionError("Placement provision requires verified previous absence")
                if request_value.get("schema_version") == 3 and (state is None or email != state["panel_email"]):
                    client = record.get("client") if isinstance(record.get("client"), dict) else record
                    if client.get("enable") is not False:
                        raise ProvisionError("Quota replacement requires previous identities disabled")
    if state is None:
        state = {
            "access_id": access_id,
            "desired_version": desired_version,
            "panel_email": f"wm_{access_id[:48]}_{desired_version}",
            "client_uuid": str(uuid.uuid4()),
            "sub_id": secrets.token_urlsafe(24),
        }
        atomic_json(state_path, state)
    validate_state(state, access_id, desired_version)

    subscription = object_value(object_value(config.get("network")).get("subscription"))
    if subscription.get("backend") != "xui-native":
        raise ProvisionError("xui-native subscription backend is required")
    domain = str(object_value(config.get("server")).get("domain") or "")
    sub_path = "/" + str(subscription.get("path") or "").strip("/") + "/"
    if not domain or ".." in sub_path or len(sub_path) < 12:
        raise ProvisionError("Native subscription settings are invalid")

    panel = panel or PanelClient(config)
    inbound_ids = visible_vless_inbound_ids(panel.call("GET", "/panel/api/inbounds/list"))
    if not inbound_ids:
        raise ProvisionError("No public VLESS inbound is available")
    email = str(state["panel_email"])
    existing = get_client(panel, email)
    if request_value.get("schema_version") == 3:
        prepare_quota_identity(request_value, state, existing, panel, state_root)
    if existing is None:
        panel.call(
            "POST",
            "/panel/api/clients/add",
            {
                "client": {
                    "email": email,
                    "limitIp": device_limit,
                    "totalGB": quota_bytes,
                    "expiryTime": int(expires_at.timestamp() * 1000),
                    "enable": enabled,
                    "tgId": 0,
                    "subId": state["sub_id"],
                    "reset": 0,
                    **({"resetDay": 0, "resetWeekday": 0} if request_value.get("schema_version") == 3 else {}),
                    "id": state["client_uuid"],
                    "flow": "",
                },
                "inboundIds": inbound_ids,
            },
        )
        existing = get_client(panel, email)
    assert_matching_client(existing, state, inbound_ids)
    reconcile_entitlements(panel, existing, state, inbound_ids, enabled=enabled,
                           expires_at_ms=int(expires_at.timestamp() * 1000),
                           device_limit=device_limit, quota_bytes=quota_bytes)
    if enabled:
        links = panel.call("GET", f"/panel/api/clients/subLinks/{parse.quote(str(state['sub_id']), safe='')}")
        if not subscription_links_ready(links):
            raise ProvisionError("Native subscription links are not ready")

    return {
        "desired_version": desired_version,
        "panel_email": email,
        "client_uuid": state["client_uuid"],
        "sub_id": state["sub_id"],
        "primary_inbound_id": inbound_ids[0],
        "protocol": "vless",
        "subscription_url": f"https://{domain}{sub_path}{state['sub_id']}",
    }


@fenced_access("access.update_entitlements")
def update_entitlements(
    request_value: dict[str, Any],
    config: dict[str, Any],
    state_root: Path,
) -> dict[str, Any]:
    """Update expiry and limits while preserving the accepted access identity."""
    if request_value.get("schema_version") in {2, 3} and request_value.get("enabled") is False:
        return disable_placement(request_value, config, state_root)
    access_id = safe_id(request_value.get("access_id"), "access_id")
    desired_version = integer(request_value.get("desired_version"), 2, 2_147_483_647)
    expires_at = parse_time(request_value.get("expires_at"))
    device_limit = integer(request_value.get("device_limit"), 0, 10_000)
    quota_bytes = placement_quota_limit(request_value)
    expires_at_ms = int(expires_at.timestamp() * 1000)
    enabled = request_value.get("enabled")
    if type(enabled) is not bool:
        raise ProvisionError("Access enabled state is invalid")

    state_path = state_root / f"{access_id}.{desired_version}.json"
    state = load_private_state(state_path)
    if state is None:
        previous = latest_previous_state(state_root, access_id, desired_version)
        state = {
            "access_id": access_id,
            "desired_version": desired_version,
            "panel_email": previous["panel_email"],
            "client_uuid": previous["client_uuid"],
            "sub_id": previous["sub_id"],
        }
        atomic_json(state_path, state)
    validate_state(state, access_id, desired_version)

    subscription = object_value(object_value(config.get("network")).get("subscription"))
    if subscription.get("backend") != "xui-native":
        raise ProvisionError("xui-native subscription backend is required")
    domain = str(object_value(config.get("server")).get("domain") or "")
    sub_path = "/" + str(subscription.get("path") or "").strip("/") + "/"
    if not domain or ".." in sub_path or len(sub_path) < 12:
        raise ProvisionError("Native subscription settings are invalid")

    panel = PanelClient(config)
    inbound_ids = visible_vless_inbound_ids(panel.call("GET", "/panel/api/inbounds/list"))
    if not inbound_ids:
        raise ProvisionError("No public VLESS inbound is available")
    email = str(state["panel_email"])
    existing = get_client(panel, email)
    assert_matching_client(existing, state, inbound_ids)

    if request_value.get("schema_version") == 3:
        prepare_quota_identity(request_value, state, existing, panel, state_root)

    reconcile_entitlements(panel, existing, state, inbound_ids, enabled=enabled,
                           expires_at_ms=expires_at_ms, device_limit=device_limit, quota_bytes=quota_bytes)

    if enabled:
        links = panel.call(
            "GET",
            f"/panel/api/clients/subLinks/{parse.quote(str(state['sub_id']), safe='')}",
        )
        if not subscription_links_ready(links):
            raise ProvisionError("Native subscription links are not ready")

    return {
        "desired_version": desired_version,
        "panel_email": email,
        "client_uuid": state["client_uuid"],
        "sub_id": state["sub_id"],
        "primary_inbound_id": inbound_ids[0],
        "protocol": "vless",
        "subscription_url": f"https://{domain}{sub_path}{state['sub_id']}",
    }


def retained_access_states(state_root: Path, access_id: str, version: int) -> dict[str, dict[str, Any]]:
    states: dict[str, dict[str, Any]] = {}
    for path in sorted(state_root.glob(f"{access_id}.*.json")):
        state = load_private_state(path)
        if state is None:
            raise ProvisionError("Durable access identity disappeared")
        v = integer(state.get("desired_version"), 1, version)
        validate_state(state, access_id, v)
        email = state["panel_email"]
        if email in states and any(state[k] != states[email][k] for k in ("client_uuid", "sub_id")):
            raise ProvisionError("Durable access identities conflict")
        states[email] = state
    return states


def assert_placement_inventory(panel: PanelClient, access_id: str, states: dict[str, dict[str, Any]]) -> None:
    value = panel.call("GET", "/panel/api/clients/list")
    rows = value.get("obj")
    if value.get("success") is not True or not isinstance(rows, list):
        raise ProvisionError("Placement client inventory is incomplete")
    seen = set()
    for row in rows:
        email = row.get("email") if isinstance(row, dict) else None
        if not isinstance(email, str) or not email or email.casefold() in seen:
            raise ProvisionError("Placement client inventory is ambiguous")
        seen.add(email.casefold())
        if email.casefold().startswith(f"wm_{access_id[:48]}_".casefold()) and email not in states:
            raise ProvisionError("Untracked placement credential requires reconciliation")


def disable_placement(request_value: dict[str, Any], config: dict[str, Any], state_root: Path) -> dict[str, Any]:
    """Inside fenced_access: deny every retained identity, never CREATE or reset quota.

    The existing fence is also the absent-state tombstone. Failed/partial reads
    retain that fence and the original request; no whole-placement ACK is emitted.
    """
    access_id = safe_id(request_value.get("access_id"), "access_id")
    version = integer(request_value.get("desired_version"), 1, 2_147_483_647)
    states = retained_access_states(state_root, access_id, version)
    panel = PanelClient(config)
    assert_placement_inventory(panel, access_id, states)
    existing = {}
    # Validate all identities before the first mutation. Unknown or foreign
    # material is not permission to disable someone else's client.
    for email, state in states.items():
        record = get_client(panel, email)
        if record is not None:
            assert_matching_client(record, state, [])
            existing[email] = record
    for email, record in existing.items():
        client = record.get("client") if isinstance(record.get("client"), dict) else record
        reconcile_entitlements(panel, record, states[email], [], enabled=False,
            expires_at_ms=integer(client.get("expiryTime"), 0, 9_223_372_036_854_775_807),
            device_limit=integer(client.get("limitIp"), 0, 10_000),
            quota_bytes=integer(client.get("totalGB"), 0, 9_223_372_036_854_775_807))
    assert_placement_inventory(panel, access_id, states)
    present = False
    proof = []
    for email, state in sorted(states.items()):
        record = get_client(panel, email)
        if record is not None:
            assert_matching_client(record, state, [])
            client = record.get("client") if isinstance(record.get("client"), dict) else record
            if client.get("enable") is not False:
                raise ProvisionError("Placement disable readback failed")
            present = True
        proof.append([email, state["client_uuid"], state["sub_id"], record is not None])
    return {"desired_version": version, "client_exists": present,
            "disabled_identity_count": len(states),
            "inventory_hash": hashlib.sha256(json.dumps(proof, sort_keys=True).encode()).hexdigest()}


def reconcile_entitlements(panel: PanelClient, existing: dict[str, Any] | None,
                           state: dict[str, Any], inbound_ids: list[int], *, enabled: bool,
                           expires_at_ms: int, device_limit: int, quota_bytes: int) -> None:
    """Apply and verify the same absolute entitlements for initial provision and renewal."""
    email = str(state["panel_email"])
    verified = existing
    if not entitlements_match(
        existing,
        enabled=enabled,
        expires_at_ms=expires_at_ms,
        device_limit=device_limit,
        quota_bytes=quota_bytes,
    ):
        raw_client = existing.get("client") if isinstance(existing, dict) else None
        client = dict(raw_client if isinstance(raw_client, dict) else existing or {})
        client.pop("uuid", None)
        client.pop("inboundIds", None)
        if "allowedIPs" in client:
            # ClientRecord stores CSV text; the update API binds Client.allowedIPs
            # as an array. Preserve already-decoded and JSON-encoded arrays too.
            allowed_ips = client["allowedIPs"]
            if isinstance(allowed_ips, str):
                if allowed_ips.lstrip().startswith(("[", "{")):
                    try:
                        allowed_ips = json.loads(allowed_ips)
                    except json.JSONDecodeError as exc:
                        raise ProvisionError("3X-UI client allowedIPs is invalid") from exc
                else:
                    allowed_ips = [value.strip() for value in allowed_ips.split(",") if value.strip()]
            if (not isinstance(allowed_ips, list)
                    or any(not isinstance(value, str) or not value.strip() for value in allowed_ips)):
                raise ProvisionError("3X-UI client allowedIPs is invalid")
            client["allowedIPs"] = allowed_ips
        client.update(
            {
                "email": email,
                "limitIp": device_limit,
                "totalGB": quota_bytes,
                "expiryTime": expires_at_ms,
                "enable": enabled,
                "tgId": integer(client.get("tgId") or 0, 0, 9_223_372_036_854_775_807),
                "subId": state["sub_id"],
                "reset": integer(client.get("reset") or 0, 0, 2_147_483_647),
                "id": state["client_uuid"],
                "flow": str(client.get("flow") or ""),
            }
        )
        update_error: ProvisionError | None = None
        try:
            panel.call(
                "POST",
                f"/panel/api/clients/update/{parse.quote(email, safe='')}",
                client,
            )
        except ProvisionError as exc:
            update_error = exc

        verified = get_client(panel, email)
        assert_matching_client(verified, state, inbound_ids)
        if not entitlements_match(
            verified,
            enabled=enabled,
            expires_at_ms=expires_at_ms,
            device_limit=device_limit,
            quota_bytes=quota_bytes,
        ):
            adjustment = safe_bulk_entitlement_adjustment(
                verified,
                expires_at_ms=expires_at_ms,
                device_limit=device_limit,
                quota_bytes=quota_bytes,
            ) if enabled else None
            if adjustment is not None:
                add_days, add_bytes = adjustment
                if add_days != 0 or add_bytes != 0:
                    panel.call(
                        "POST",
                        "/panel/api/clients/bulkAdjust",
                        {
                            "emails": [email],
                            "addDays": add_days,
                            "addBytes": add_bytes,
                        },
                    )
                    verified = get_client(panel, email)
                    assert_matching_client(verified, state, inbound_ids)

        try:
            assert_entitlements(
                verified,
                enabled=enabled,
                expires_at_ms=expires_at_ms,
                device_limit=device_limit,
                quota_bytes=quota_bytes,
            )
        except ProvisionError:
            if update_error is not None:
                raise update_error
            raise


def latest_previous_state(
    state_root: Path,
    access_id: str,
    desired_version: int,
) -> dict[str, Any]:
    candidates: list[tuple[int, dict[str, Any]]] = []
    for state_path in state_root.glob(f"{access_id}.*.json"):
        state = load_private_state(state_path)
        if state is None or state.get("access_id") != access_id:
            continue
        version = integer(state.get("desired_version"), 1, 2_147_483_647)
        if version >= desired_version:
            continue
        validate_state(state, access_id, version)
        candidates.append((version, state))
    if not candidates:
        raise ProvisionError("Previous durable access identity is missing")
    return max(candidates, key=lambda item: item[0])[1]


def assert_entitlements(
    record: dict[str, Any] | None,
    *,
    enabled: bool = True,
    expires_at_ms: int,
    device_limit: int,
    quota_bytes: int,
) -> None:
    if not record:
        raise ProvisionError("3X-UI entitlement verification failed")
    client = record.get("client") if isinstance(record.get("client"), dict) else record
    if (
        client.get("enable") is not enabled
        or integer(client.get("expiryTime"), 0, 9_223_372_036_854_775_807) != expires_at_ms
        or integer(client.get("limitIp"), 0, 10_000) != device_limit
        or integer(client.get("totalGB"), 0, 9_223_372_036_854_775_807) != quota_bytes
    ):
        raise ProvisionError("3X-UI entitlement update was not verified")


def entitlements_match(
    record: dict[str, Any] | None,
    *,
    enabled: bool = True,
    expires_at_ms: int,
    device_limit: int,
    quota_bytes: int,
) -> bool:
    try:
        assert_entitlements(
            record,
            enabled=enabled,
            expires_at_ms=expires_at_ms,
            device_limit=device_limit,
            quota_bytes=quota_bytes,
        )
    except ProvisionError:
        return False
    return True


def safe_bulk_entitlement_adjustment(
    record: dict[str, Any] | None,
    *,
    expires_at_ms: int,
    device_limit: int,
    quota_bytes: int,
) -> tuple[int, int] | None:
    """Return an additive 3X-UI repair only when its semantics are unambiguous."""
    if not record:
        return None
    client = record.get("client") if isinstance(record.get("client"), dict) else record
    if client.get("enable") is not True:
        return None
    if integer(client.get("limitIp"), 0, 10_000) != device_limit:
        return None

    current_expiry_ms = integer(client.get("expiryTime"), 0, 9_223_372_036_854_775_807)
    expiry_delta_ms = expires_at_ms - current_expiry_ms
    if expiry_delta_ms % DAY_MILLISECONDS != 0:
        return None
    add_days = expiry_delta_ms // DAY_MILLISECONDS
    if not -3650 <= add_days <= 3650:
        return None

    current_quota_bytes = integer(client.get("totalGB"), 0, 9_223_372_036_854_775_807)
    if current_quota_bytes == quota_bytes:
        add_bytes = 0
    elif quota_bytes == 0 and current_quota_bytes > 0:
        add_bytes = -current_quota_bytes
    else:
        return None
    return add_days, add_bytes


@fenced_access("cleanup")
def cleanup_previous(request_value: dict[str, Any], config: dict[str, Any], state_root: Path) -> int:
    """Remove only older durable identities after SaaS accepted the replacement."""
    access_id = safe_id(request_value.get("access_id"), "access_id")
    desired_version = integer(request_value.get("desired_version"), 2, 2_147_483_647)
    current_path = state_root / f"{access_id}.{desired_version}.json"
    current = load_private_state(current_path)
    if current is None:
        raise ProvisionError("Replacement durable state is missing")
    validate_state(current, access_id, desired_version)

    if request_value.get("schema_version") == 3:
        # Retain disabled clients/counters until their accounting evidence has
        # a separately qualified cleanup contract. Ordinary v2 cleanup cannot
        # delete a meter which still backs a retained reservation/settlement.
        return 0

    panel = PanelClient(config)
    if request_value.get("schema_version") in {2, 3}:
        states = retained_access_states(state_root, access_id, desired_version)
        assert_placement_inventory(panel, access_id, states)
        for email, previous in states.items():
            existing = get_client(panel, email)
            if existing is not None:
                assert_matching_client(existing, previous, [])
    removed = 0
    for state_path in sorted(state_root.glob(f"{access_id}.*.json")):
        if state_path == current_path:
            continue
        state = load_private_state(state_path)
        if state is None or state.get("access_id") != access_id:
            continue
        version = integer(state.get("desired_version"), 1, 2_147_483_647)
        if version >= desired_version:
            continue
        email = str(state.get("panel_email") or "")
        if email == current.get("panel_email"):
            continue
        if not re.fullmatch(r"[A-Za-z0-9_-]{3,128}", email):
            raise ProvisionError("Previous durable panel identity is invalid")
        if get_client(panel, email) is not None:
            panel.call(
                "POST",
                f"/panel/api/clients/del/{parse.quote(email, safe='')}",
            )
            if get_client(panel, email) is not None:
                raise ProvisionError("Previous 3X-UI client cleanup was not verified")
            removed += 1
    return removed


def visible_vless_inbound_ids(response: dict[str, Any]) -> list[int]:
    result = []
    for item in response.get("obj") or []:
        if not isinstance(item, dict) or not item.get("enable", True):
            continue
        if str(item.get("remark") or "").startswith("--!") or item.get("protocol") != "vless":
            continue
        inbound_id = item.get("id")
        if isinstance(inbound_id, int) and inbound_id > 0:
            result.append(inbound_id)
    return sorted(set(result))


def get_client(panel: PanelClient, email: str) -> dict[str, Any] | None:
    try:
        value = panel.call("GET", f"/panel/api/clients/get/{parse.quote(email, safe='')}")
    except ProvisionError as lookup_error:
        # A failed GET may be an outage or payload-building failure, not absence.
        # The unpaginated list endpoint must independently prove nonexistence.
        listing = panel.call("GET", "/panel/api/clients/list")
        rows = listing.get("obj")
        if listing.get("success") is not True or not isinstance(rows, list):
            raise ProvisionError("Client absence could not be verified") from lookup_error
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("email"), str) or not row["email"]:
                raise ProvisionError("Client list is incomplete or invalid") from lookup_error
            if row["email"].casefold() == email.casefold():
                raise ProvisionError("Existing client could not be read") from lookup_error
        return None
    obj = value.get("obj")
    if value.get("success") is not True or not isinstance(obj, dict):
        raise ProvisionError("Client readback is invalid")
    return obj


def quota_identity_hash(state: dict[str, Any]) -> str:
    # Same byte representation as JSON.stringify([email, uuid, subId]) in SaaS.
    return hashlib.sha256(json.dumps([state["panel_email"], state["client_uuid"], state["sub_id"]],
                                    separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def read_quota_counter(panel: PanelClient, state: dict[str, Any]) -> dict[str, str]:
    value = panel.call("GET", f"/panel/api/clients/traffic/{parse.quote(state['panel_email'], safe='')}")
    row = value.get("obj")
    if (not isinstance(row, dict) or row.get("email") != state["panel_email"] or
            row.get("uuid") != state["client_uuid"] or row.get("subId") != state["sub_id"]):
        raise ProvisionError("Placement traffic counter identity is invalid")
    result = {"identity_hash": quota_identity_hash(state)}
    for source, target in (("up", "up_bytes"), ("down", "down_bytes")):
        raw = row.get(source)
        if type(raw) is int:
            raw = str(raw)
        result[target] = str(quota_bytes(raw))
    if int(result["up_bytes"]) + int(result["down_bytes"]) > 9_223_372_036_854_775_807:
        raise ProvisionError("Placement traffic counter overflows")
    return result


def assert_quota_counter_progress(epoch: dict[str, Any], counter: dict[str, str]) -> None:
    if (epoch["identity_hash"] not in (None, counter["identity_hash"]) or
            quota_bytes(counter["up_bytes"]) < quota_bytes(epoch["up_bytes"]) or
            quota_bytes(counter["down_bytes"]) < quota_bytes(epoch["down_bytes"])):
        raise ProvisionError("Placement traffic counter regressed or changed identity")


def assert_no_quota_reset(record: dict[str, Any]) -> None:
    client = record.get("client") if isinstance(record.get("client"), dict) else record
    for name in ("reset", "resetDay", "resetWeekday"):
        if type(client.get(name, 0)) is not int or client.get(name, 0) != 0:
            raise ProvisionError("Automatic traffic reset is incompatible with reserved credit")


def prepare_quota_identity(value: dict[str, Any], state: dict[str, Any], record: dict[str, Any] | None,
                           panel: PanelClient, state_root: Path) -> None:
    budget = validate_traffic_budget(value.get("traffic_budget"))
    if budget is None:
        return
    states = retained_access_states(state_root, value["access_id"], value["desired_version"])
    assert_placement_inventory(panel, value["access_id"], states)
    for email, previous in states.items():
        if email == state["panel_email"]:
            continue
        other = get_client(panel, email)
        if other is not None:
            assert_matching_client(other, previous, [])
            client = other.get("client") if isinstance(other.get("client"), dict) else other
            if client.get("enable") is not False:
                raise ProvisionError("Other placement identities must be disabled before applying credit")
    path = state_root / f"{value['access_id']}.fence"
    fence = load_private_state(path)
    if fence is None or fence.get("traffic_budget") != budget:
        raise ProvisionError("Placement quota execution fence disappeared")
    epochs = quota_epochs(fence.get("quota_epochs"))
    epoch = epochs[budget["epoch_id"]]
    identity = quota_identity_hash(state)
    if (budget["counter_identity_hash"] not in (None, identity) or epoch["identity_hash"] not in (None, identity) or
            record is not None and budget["counter_identity_hash"] is None and epoch["identity_hash"] is None):
        raise ProvisionError("Placement credit is bound to another counter")
    if record is None:
        if epoch["identity_hash"] is not None or budget["counter_identity_hash"] is not None:
            raise ProvisionError("Placement quota identity disappeared; recreation would reset credit")
        counter = {"identity_hash": identity, "up_bytes": "0", "down_bytes": "0"}
    else:
        assert_matching_client(record, state, [])
        assert_no_quota_reset(record)
        counter = read_quota_counter(panel, state)
    prior = quota_counters(fence.get("quota_counters")).get(identity)
    if prior is not None:
        if record is None:
            raise ProvisionError("Retained placement traffic identity disappeared")
        assert_quota_counter_progress(prior, counter)
    assert_quota_counter_progress(epoch, counter)
    consumed = quota_bytes(counter["up_bytes"]) + quota_bytes(counter["down_bytes"])
    if consumed < quota_bytes(budget["counter_baseline_bytes"]):
        raise ProvisionError("Placement counter is behind the reserved baseline")
    epochs[budget["epoch_id"]] = {**epoch, **counter}
    atomic_json(path, {**fence, "quota_epochs": epochs})
    if consumed >= placement_quota_limit(value):
        # Do not turn a quota-disabled client on again on a retry/renewal.
        # The deny/counter path can still report the actual overrun for debt.
        raise ProvisionError("Placement credit is exhausted")


def record_quota_counters(value: dict[str, Any], config: dict[str, Any], state_root: Path) -> list[dict[str, str]]:
    """Report panel-observed counters, never assert a final settlement proof.

    A successful deny retains these observations and the local epoch. SaaS
    still owns qualification/settlement before unused credit can be reissued.
    Missing counters or rollback are UNKNOWN, never zero or another allowance.
    """
    path = state_root / f"{value['access_id']}.fence"
    fence = load_private_state(path)
    if fence is None or fence.get("schema_version") != 3:
        raise ProvisionError("Placement quota execution fence disappeared")
    epochs = quota_epochs(fence.get("quota_epochs"))
    previous = quota_counters(fence.get("quota_counters"))
    states = retained_access_states(state_root, value["access_id"], value["desired_version"])
    panel = PanelClient(config)
    counters = {}
    for state in states.values():
        record = get_client(panel, state["panel_email"])
        if record is not None:
            assert_matching_client(record, state, [])
            assert_no_quota_reset(record)
            client = record.get("client") if isinstance(record.get("client"), dict) else record
            if value["enabled"] is False and client.get("enable") is not False:
                raise ProvisionError("Placement quota deny readback failed")
        counter = read_quota_counter(panel, state)
        if counter["identity_hash"] in counters:
            raise ProvisionError("Duplicate placement traffic identity")
        counters[counter["identity_hash"]] = counter
    for identity, prior in previous.items():
        counter = counters.get(identity)
        if counter is None:
            raise ProvisionError("Retained placement traffic identity disappeared")
        assert_quota_counter_progress(prior, counter)
    for key, epoch in epochs.items():
        if epoch["identity_hash"] is None:
            # No bound identity is not a claim that an unknown HTTP effect had
            # zero consumption. Such an epoch remains reserved in SaaS.
            continue
        counter = counters.get(epoch["identity_hash"])
        if counter is None:
            raise ProvisionError("Retained placement traffic identity disappeared")
        assert_quota_counter_progress(epoch, counter)
        epochs[key] = {**epoch, **counter, "terminal": epoch["terminal"] or value["enabled"] is False}
    atomic_json(path, {**fence, "quota_epochs": epochs, "quota_counters": [counters[key] for key in sorted(counters)]})
    if value["enabled"] is True and value.get("traffic_budget") is not None:
        epoch = epochs[value["traffic_budget"]["epoch_id"]]
        if quota_bytes(epoch["up_bytes"]) + quota_bytes(epoch["down_bytes"]) >= placement_quota_limit(value):
            raise ProvisionError("Placement credit is exhausted")
    return [counters[key] for key in sorted(counters)]


def assert_matching_client(record: dict[str, Any] | None, state: dict[str, Any], inbound_ids: list[int]) -> None:
    if not record:
        raise ProvisionError("3X-UI client verification failed")
    client = record.get("client") if isinstance(record.get("client"), dict) else record
    attached = record.get("inboundIds") or client.get("inboundIds") or []
    client_uuid = client.get("uuid")
    if not client_uuid and isinstance(client.get("id"), str):
        client_uuid = client["id"]
    if (
        client.get("email") != state["panel_email"]
        or client_uuid != state["client_uuid"]
        or client.get("subId") != state["sub_id"]
        or not set(inbound_ids).issubset({int(value) for value in attached if str(value).isdigit()})
    ):
        raise ProvisionError("Existing 3X-UI client conflicts with durable access state")


def subscription_links_ready(response: dict[str, Any]) -> bool:
    value = response.get("obj")
    if isinstance(value, str):
        return bool(value.strip())
    return isinstance(value, list) and any(str(item).strip() for item in value)


def load_private_state(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise ProvisionError("Access state path is unsafe")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ProvisionError("Access state is invalid")
    return value


def validate_state(state: dict[str, Any], access_id: str, desired_version: int) -> None:
    if state.get("access_id") != access_id or state.get("desired_version") != desired_version:
        raise ProvisionError("Durable access state does not match the command")
    uuid.UUID(str(state.get("client_uuid")), version=4)
    safe_id(state.get("sub_id"), "sub_id")
    if not re.fullmatch(r"[A-Za-z0-9_-]{3,128}", str(state.get("panel_email") or "")):
        raise ProvisionError("Durable panel identity is invalid")


def sync_directory(path: Path) -> None:
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(name, 0o600)
        os.replace(name, path)
        sync_directory(path.parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def object_value(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def integer(value: Any, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise ProvisionError("Integer value is invalid")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ProvisionError("Integer value is invalid") from exc
    if result < minimum or result > maximum:
        raise ProvisionError("Integer value is outside allowed bounds")
    return result


def safe_id(value: Any, name: str) -> str:
    result = str(value or "")
    if not SAFE_ID.fullmatch(result):
        raise ProvisionError(f"{name} is invalid")
    return result


def parse_time(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProvisionError("expires_at is invalid") from exc
    if parsed.tzinfo is None:
        raise ProvisionError("expires_at must include a timezone")
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser()
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--request", type=Path)
    action.add_argument("--capabilities", action="store_true")
    parser.add_argument("--config", type=Path, default=Path("/etc/wavemesh-node/config.json"))
    parser.add_argument("--state-root", type=Path, default=Path("/var/lib/wavemesh-agent/access"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--cleanup-previous", action="store_true")
    args = parser.parse_args()
    if args.capabilities:
        print(json.dumps({"access_command_schemas": [1, 2, 3], "placement_fence": 5, "disable_absent": True,
                          "unknown_effect_barrier": 1, "traffic_budget": 1, "counter_observations": 1,
                          "quota_protocol_version": 2}))
        return 0
    try:
        # Lock order: node, then access. Hold through configuration read, panel
        # read/write/readback and durable output, including replacement cleanup.
        with node_mutation_lock():
            for directory in assert_no_pending_node_transaction(args.config.parent / "transactions"):
                sync_directory(directory)
            command = json.loads(args.request.read_text(encoding="utf-8"))
            config = json.loads(args.config.read_text(encoding="utf-8"))
            if args.cleanup_previous:
                removed = cleanup_previous(command, config, args.state_root)
                print(f"access_cleanup=PASS removed={removed}")
                return 0
            if args.output is None:
                raise ProvisionError("output is required for access lifecycle execution")
            operation = str(command.get("operation") or "access.provision")
            if operation == "access.update_entitlements":
                result = update_entitlements(command, config, args.state_root)
            elif operation in {"access.provision", "access.replace_credential", "access.prepare_replacement"}:
                result = provision(command, config, args.state_root)
            else:
                raise ProvisionError("Unsupported access lifecycle operation")
            atomic_json(args.output, result)
            print(f"access_runtime=PASS operation={operation}")
            return 0
    except Exception as exc:
        print(f"access_runtime=FAIL code={type(exc).__name__.upper()}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
