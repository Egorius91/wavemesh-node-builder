"""Private advisory client evidence and durable mTLS delivery; never panel writes.

This module does not establish SaaS ownership and cannot authorize quarantine.
All references to actual client identity stay in the private local journal.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
import time
from datetime import datetime, timezone
from typing import Any
from urllib import parse, request
import uuid

from access_runtime import PanelClient, assert_no_pending_node_transaction, node_mutation_lock, visible_vless_inbound_ids

MAX_BYTES = 2 * 1024 * 1024
MAX_RECORDS = 128
MAX_CLIENTS = 512
MAX_ATTEMPTS = 8
EMAIL = re.compile(r"[A-Za-z0-9_.@+-]{3,128}\Z")
HEX = re.compile(r"[a-f0-9]{64}\Z")
REF = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z")


class FindingError(RuntimeError):
    """Messages are fixed codes, never input or exception text."""


def encoded(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(encoded(value)).hexdigest()


def timestamp(now: float) -> str:
    return datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def no_symlinks(path: Path) -> None:
    if not path.is_absolute() or ".." in path.parts:
        raise FindingError("UNSAFE_PATH")
    for part in (path, *path.parents):
        if part.is_symlink():
            raise FindingError("UNSAFE_PATH")


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value = {}
    for key, item in pairs:
        if key in value:
            raise FindingError("INVALID_JSON")
        value[key] = item
    return value


def read_json(path: Path, *, private: bool = True) -> dict[str, Any]:
    no_symlinks(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        forbidden = 0o077 if private else 0o022
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid()
                or info.st_mode & forbidden or info.st_size > MAX_BYTES):
            raise FindingError("UNSAFE_STATE")
        with os.fdopen(fd, "rb", closefd=False) as source:
            raw = source.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise FindingError("STATE_LIMIT")
        value = json.loads(raw, object_pairs_hook=unique_object,
                           parse_constant=lambda _v: (_ for _ in ()).throw(FindingError("INVALID_JSON")))
        if not isinstance(value, dict):
            raise FindingError("INVALID_JSON")
        return value
    finally:
        os.close(fd)


def private_directory(path: Path) -> None:
    no_symlinks(path)
    # Never recursively chmod/reown or follow an existing unsafe directory.
    if not path.exists():
        path.mkdir(mode=0o700)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise FindingError("UNSAFE_DIRECTORY")


def write_json(path: Path, value: dict[str, Any]) -> None:
    no_symlinks(path)
    raw = encoded(value)
    if len(raw) > MAX_BYTES:
        raise FindingError("STATE_LIMIT")
    if path.exists():
        read_json(path)
    fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def client_identity(record: dict[str, Any]) -> tuple[str, str, str, list[int]]:
    client = record.get("client", record)
    if not isinstance(client, dict):
        raise FindingError("INVALID_CLIENT")
    email, sub_id = client.get("email"), client.get("subId")
    identifier = client.get("uuid") or client.get("id")
    attached = record.get("inboundIds", client.get("inboundIds"))
    if (not isinstance(email, str) or not EMAIL.fullmatch(email) or not isinstance(identifier, str)
            or not isinstance(sub_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", sub_id)
            or not isinstance(attached, list) or not attached or len(attached) > MAX_CLIENTS
            or any(type(item) is not int or item <= 0 for item in attached) or len(set(attached)) != len(attached)):
        raise FindingError("INVALID_CLIENT")
    try:
        canonical = str(uuid.UUID(identifier))
    except ValueError:
        raise FindingError("INVALID_CLIENT") from None
    if canonical != identifier.lower():
        raise FindingError("INVALID_CLIENT")
    return email, identifier, sub_id, sorted(attached)


def client_names(response: dict[str, Any]) -> list[str]:
    rows = response.get("obj")
    if response.get("success") is not True or not isinstance(rows, list) or len(rows) > MAX_CLIENTS:
        raise FindingError("INVALID_INVENTORY")
    names = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("email"), str) or not EMAIL.fullmatch(row["email"]):
            raise FindingError("INVALID_INVENTORY")
        names.append(row["email"])
    if len({name.casefold() for name in names}) != len(names):
        raise FindingError("AMBIGUOUS_INVENTORY")
    return sorted(names)


class ReadOnlyPanel(PanelClient):
    """Constrain this collector even if later code accidentally supplies a write."""
    def __init__(self, config: dict[str, Any]):
        super().__init__(config, timeout=3)

    def call(self, method: str, path: str, payload=None):
        lookup = path.removeprefix("/panel/api/clients/get/") if path.startswith("/panel/api/clients/get/") else ""
        decoded = parse.unquote(lookup)
        if method != "GET" or payload is not None or not (
            path in {"/panel/api/clients/list", "/panel/api/inbounds/list"}
            or (EMAIL.fullmatch(decoded) and parse.quote(decoded, safe="") == lookup)
        ):
            raise FindingError("PANEL_OPERATION_FORBIDDEN")
        class NoRedirect(request.HTTPRedirectHandler):
            def redirect_request(self, *_args, **_kwargs):
                return None
        req = request.Request(self.base + path, method="GET", headers={"Authorization": f"Bearer {self.token}"})
        try:
            with request.build_opener(NoRedirect).open(req, timeout=self.timeout) as response:
                raw = response.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise FindingError("PANEL_RESPONSE_LIMIT")
            result = json.loads(raw, object_pairs_hook=unique_object,
                                parse_constant=lambda _v: (_ for _ in ()).throw(FindingError("INVALID_JSON")))
            if not isinstance(result, dict) or result.get("success") is not True:
                raise FindingError("PANEL_READ_FAILED")
            return result
        except Exception:
            raise FindingError("PANEL_READ_FAILED") from None


class RuntimeFindingCycle:
    def __init__(self, node_id: str, tenant_id: str, root: Path, config_path: Path,
                 access_root: Path, *, clock=time.time, panel_factory=ReadOnlyPanel,
                 mutation_lock_path: Path | None = None):
        if not all(re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", item) for item in (node_id, tenant_id)):
            raise FindingError("INVALID_SCOPE")
        self.node_id, self.tenant_id = node_id, tenant_id
        self.root, self.config_path, self.access_root = root, config_path, access_root
        self.clock, self.panel_factory, self.mutation_lock_path = clock, panel_factory, mutation_lock_path

    def cycle(self, api: Any | None) -> dict[str, Any]:
        if os.name != "posix":
            raise FindingError("POSIX_REQUIRED")
        private_directory(self.root)
        # All collector instances share this inode. API delivery deliberately does
        # not hold the CLI/node lock, but cannot race another finding sender.
        with node_mutation_lock(self.root / ".lock"):
            records = self.records()
            blocked = [r for r in records if r["delivery"]["phase"] == "BLOCKED"]
            if blocked:
                return {"state": "REVIEW_REQUIRED", "blocked": len(blocked)}
            pending = [r for r in records if r["delivery"]["phase"] != "ACCEPTED"]
            if pending:
                return self.deliver(pending[0], api) if api is not None else {"state": "QUEUED"}
            if api is not None:
                return {"state": "IDLE"}
            record = self.collect(records)
            return {"state": "QUEUED"} if record else {"state": "IDLE"}

    def records(self) -> list[dict[str, Any]]:
        entries = list(self.root.iterdir())
        if len(entries) > MAX_RECORDS + 32:
            raise FindingError("JOURNAL_LIMIT")
        records = []
        total = 0
        for path in sorted(entries):
            if path.name in {".lock", "schedule.json"} or path.name.startswith(".pending-"):
                continue
            if not path.name.endswith(".json") or not REF.fullmatch(path.stem):
                raise FindingError("UNKNOWN_JOURNAL_ENTRY")
            total += path.lstat().st_size
            if total > 8 * MAX_BYTES:
                raise FindingError("JOURNAL_LIMIT")
            record = read_json(path)
            self.validate(record, path.stem)
            records.append(record)
        return records

    def validate(self, record: dict[str, Any], revision: str) -> None:
        if set(record) != {"node_id", "tenant_id", "baseline", "report", "delivery"}:
            raise FindingError("INVALID_JOURNAL")
        report, baseline, delivery = record["report"], record["baseline"], record["delivery"]
        if (record["node_id"] != self.node_id or record["tenant_id"] != self.tenant_id
                or not isinstance(report, dict) or not isinstance(baseline, dict) or not isinstance(delivery, dict)
                or set(report) != {"schema_version", "kind", "finding_ref", "revision", "baseline_sha256", "observed_at"}
                or type(report["schema_version"]) is not int or report["schema_version"] != 1
                or report["kind"] != "UNMANAGED_CLIENT_CANDIDATE" or report["revision"] != revision
                or not REF.fullmatch(str(report["finding_ref"])) or not HEX.fullmatch(str(baseline.get("nonce")))
                or report["baseline_sha256"] != digest(baseline)
                or set(delivery) != {"phase", "attempts", "retry_at", "finding_id"}
                or delivery["phase"] not in {"PENDING", "IN_FLIGHT", "ACCEPTED", "BLOCKED"}
                or type(delivery["attempts"]) is not int or not 0 <= delivery["attempts"] <= MAX_ATTEMPTS
                or type(delivery["retry_at"]) not in {int, float} or not math.isfinite(delivery["retry_at"]) or delivery["retry_at"] < 0):
            raise FindingError("INVALID_JOURNAL")
        if (set(baseline) != {"nonce", "client_record", "public_inbound_ids", "config_sha256", "access_inventory_sha256", "panel_names_sha256", "node_id", "tenant_id"}
                or baseline["node_id"] != self.node_id or baseline["tenant_id"] != self.tenant_id
                or not isinstance(baseline["client_record"], dict)
                or any(not HEX.fullmatch(str(baseline[key])) for key in ("config_sha256", "access_inventory_sha256", "panel_names_sha256"))
                or not isinstance(baseline["public_inbound_ids"], list)
                or any(type(item) is not int or item <= 0 for item in baseline["public_inbound_ids"])):
            raise FindingError("INVALID_BASELINE")
        client_identity(baseline["client_record"])
        value = report["observed_at"]
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", value):
            raise FindingError("INVALID_JOURNAL")
        if timestamp(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()) != value:
            raise FindingError("INVALID_JOURNAL")
        if delivery["phase"] == "ACCEPTED":
            if not isinstance(delivery["finding_id"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", delivery["finding_id"]):
                raise FindingError("INVALID_RECEIPT")
        elif delivery["finding_id"] is not None:
            raise FindingError("INVALID_RECEIPT")

    def save(self, record: dict[str, Any]) -> None:
        revision = record["report"]["revision"]
        self.validate(record, revision)
        path = self.root / f"{revision}.json"
        if path.exists():
            old = read_json(path)
            self.validate(old, revision)
            if any(old[key] != record[key] for key in ("node_id", "tenant_id", "baseline", "report")):
                raise FindingError("IMMUTABLE_EVIDENCE_CHANGED")
        other_records = [item for item in self.root.glob("*.json") if item.name not in {path.name, "schedule.json"}]
        if len(other_records) >= MAX_RECORDS or sum(item.lstat().st_size for item in other_records) + len(encoded(record)) > 8 * MAX_BYTES:
            raise FindingError("JOURNAL_LIMIT")
        write_json(path, record)

    def inventory(self) -> tuple[str, str]:
        no_symlinks(self.access_root)
        if not self.access_root.is_dir():
            raise FindingError("ACCESS_INVENTORY_UNAVAILABLE")
        parts = []
        total = 0
        directory_count = 0
        def unavailable(_error):
            raise FindingError("ACCESS_INVENTORY_UNAVAILABLE") from None
        for directory, dirs, files in os.walk(self.access_root, followlinks=False, onerror=unavailable):
            directory_count += 1
            if directory_count > 2048:
                raise FindingError("INVENTORY_LIMIT")
            info = Path(directory).lstat()
            if info.st_uid != os.geteuid() or info.st_mode & 0o077 or len(dirs) + len(files) > 4096:
                raise FindingError("UNSAFE_INVENTORY")
            if any((Path(directory) / name).is_symlink() for name in dirs + files):
                raise FindingError("UNSAFE_INVENTORY")
            for name in sorted(files):
                if not name.endswith(".json"):
                    continue
                value = read_json(Path(directory) / name)
                raw = encoded(value).decode()
                total += len(raw)
                parts.append(raw)
                if len(parts) > 2048 or total > 8 * MAX_BYTES:
                    raise FindingError("INVENTORY_LIMIT")
        text = "\n".join(sorted(parts))
        return text.casefold(), hashlib.sha256(text.encode()).hexdigest()

    def collect(self, records: list[dict[str, Any]]) -> dict[str, Any] | None:
        now = self.clock()
        schedule_path = self.root / "schedule.json"
        schedule = read_json(schedule_path) if schedule_path.exists() else {"cursor": 0, "scan_after": 0}
        if (set(schedule) != {"cursor", "scan_after"} or type(schedule["cursor"]) is not int or schedule["cursor"] < 0
                or type(schedule["scan_after"]) not in {int, float} or not math.isfinite(schedule["scan_after"]) or schedule["scan_after"] < 0):
            raise FindingError("INVALID_SCHEDULE")
        if now < schedule["scan_after"]:
            return None
        # Persist cadence even on a panel outage; do not hammer the local API.
        write_json(schedule_path, {"cursor": schedule["cursor"] + 1, "scan_after": now + 60})
        if len(records) >= MAX_RECORDS:
            raise FindingError("JOURNAL_LIMIT")
        with node_mutation_lock(self.mutation_lock_path):
            assert_no_pending_node_transaction(self.config_path.parent / "transactions")
            config = read_json(self.config_path, private=False)
            inventory, inventory_hash = self.inventory()
            config_text = encoded(config).decode().casefold()
            panel = self.panel_factory(config)
            names = client_names(panel.call("GET", "/panel/api/clients/list"))
            candidates = [name for name in names if name.casefold() not in inventory and name.casefold() not in config_text]
            if not candidates:
                return None
            email = candidates[schedule["cursor"] % len(candidates)]
            path = "/panel/api/clients/get/" + parse.quote(email, safe="")
            first = panel.call("GET", path).get("obj")
            if not isinstance(first, dict):
                raise FindingError("INVALID_CLIENT")
            actual_email, identifier, sub_id, attached = client_identity(first)
            if actual_email != email:
                raise FindingError("CLIENT_MISMATCH")
            client = first.get("client", first)
            if client.get("enable") is not True:
                return None
            if any(marker.casefold() in text for marker in (email, identifier, sub_id) for text in (inventory, config_text)):
                return None
            visible = visible_vless_inbound_ids(panel.call("GET", "/panel/api/inbounds/list"))
            if not set(attached).issubset(set(visible)):
                return None
            # Detect observable drift; external panel/UI writers are NOT fenced.
            # The resulting evidence is advisory even when both reads agree.
            if (panel.call("GET", path).get("obj") != first
                    or client_names(panel.call("GET", "/panel/api/clients/list")) != names
                    or digest(read_json(self.config_path, private=False)) != digest(config)
                    or self.inventory()[1] != inventory_hash):
                raise FindingError("OBSERVATION_DRIFT")
            baseline = {"client_record": first, "public_inbound_ids": visible,
                        "config_sha256": digest(config), "access_inventory_sha256": inventory_hash,
                        "panel_names_sha256": digest(names), "node_id": self.node_id, "tenant_id": self.tenant_id}
            related = [r for r in records if client_identity(r["baseline"]["client_record"])[0] == email]
            for old in related:
                if {key: value for key, value in old["baseline"].items() if key != "nonce"} == baseline:
                    return None  # Never manufacture freshness for accepted evidence.
            refs = {r["report"]["finding_ref"] for r in related}
            if len(refs) > 1:
                raise FindingError("AMBIGUOUS_JOURNAL")
            baseline["nonce"] = secrets.token_hex(32)
            report = {"schema_version": 1, "kind": "UNMANAGED_CLIENT_CANDIDATE",
                      "finding_ref": next(iter(refs)) if refs else str(uuid.uuid4()), "revision": str(uuid.uuid4()),
                      "baseline_sha256": digest(baseline), "observed_at": timestamp(now)}
            record = {"node_id": self.node_id, "tenant_id": self.tenant_id, "baseline": baseline, "report": report,
                      "delivery": {"phase": "PENDING", "attempts": 0, "retry_at": 0, "finding_id": None}}
            self.save(record)
            return record

    def deliver(self, record: dict[str, Any], api: Any) -> dict[str, Any]:
        delivery = record["delivery"]
        now = self.clock()
        if delivery["attempts"] >= MAX_ATTEMPTS:
            delivery["phase"] = "BLOCKED"
            self.save(record)
            return {"state": "REVIEW_REQUIRED", "blocked": 1}
        if now < delivery["retry_at"]:
            return {"state": "RETRY_PENDING"}
        delivery.update(phase="IN_FLIGHT", attempts=delivery["attempts"] + 1)
        delivery["retry_at"] = now + min(900, 30 * 2 ** (delivery["attempts"] - 1))
        self.save(record)  # A crash from here MUST reuse this exact envelope.
        try:
            result = api.api_json("POST", f"internal/v1/nodes/{self.node_id}/runtime-findings",
                                  json.loads(encoded(record["report"])), expected=(202,))
            if (not isinstance(result, dict) or set(result) != {"accepted", "finding_id", "disposition", "observed_at", "report_sha256"}
                    or result["accepted"] is not True or result["disposition"] != "OBSERVED_ONLY"
                    or result["observed_at"] != record["report"]["observed_at"]
                    or result["report_sha256"] != digest(record["report"])
                    or not isinstance(result["finding_id"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", result["finding_id"])):
                raise FindingError("INVALID_RECEIPT")
        except Exception as exc:
            # No raw response/error text is retained or logged. Even a stale/conflict
            # response never replaces the original report with a new revision.
            status = getattr(exc, "status", None)
            delivery["phase"] = "BLOCKED" if (type(status) is int and 400 <= status < 500 and status not in {408, 429}) else "PENDING"
            self.save(record)
            return {"state": "REVIEW_REQUIRED" if delivery["phase"] == "BLOCKED" else "RETRY_PENDING"}
        delivery.update(phase="ACCEPTED", finding_id=result["finding_id"])
        self.save(record)
        return {"state": "OBSERVED_ONLY"}


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect one advisory runtime finding; no API delivery or panel writes")
    parser.add_argument("--node-id", required=True)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--access-root", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=Path("/etc/wavemesh-node/config.json"))
    args = parser.parse_args()
    try:
        status = RuntimeFindingCycle(args.node_id, args.tenant_id, args.state_root, args.config, args.access_root).cycle(None)
        print(json.dumps(status))
        return 0
    except Exception:
        print('{"state":"COLLECTION_BLOCKED"}')
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
