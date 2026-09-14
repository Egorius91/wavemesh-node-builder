"""Internal current-boot panel TCP isolation; no CLI, release or service action."""
import hashlib
import json
import os
import subprocess
import sys

TABLE = "wavemesh_panel_maintenance"
NFT = "/usr/sbin/nft"
MAX_OUTPUT = 1024 * 1024


class IsolationError(RuntimeError):
    pass


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise IsolationError("ISOLATION_JSON_INVALID")
        result[key] = value
    return result


def policy(port, binding):
    if type(port) is not int or not 1024 <= port <= 65535:
        raise IsolationError("ISOLATION_PORT_INVALID")
    if not isinstance(binding, str) or len(binding) != 64 or any(c not in "0123456789abcdef" for c in binding):
        raise IsolationError("ISOLATION_BINDING_INVALID")
    def match(left, right, op="=="):
        return {"match": {"op": op, "left": left, "right": right}}
    destination = match({"payload": {"protocol": "tcp", "field": "dport"}}, port)
    objects = [{"table": {"family": "inet", "name": TABLE, "comment": "wm-install:" + binding}}]
    for hook in ("input", "output"):
        objects.append({"chain": {"family": "inet", "table": TABLE, "name": hook,
                                  "type": "filter", "hook": hook, "prio": -310, "policy": "accept"}})
        expressions = [destination]
        if hook == "input":
            expressions.append(match({"meta": {"key": "iifname"}}, "lo", "!="))
        else:
            expressions.extend([match({"meta": {"key": "oifname"}}, "lo"),
                                match({"meta": {"key": "skuid"}}, 0, "!=")])
        objects.append({"rule": {"family": "inet", "table": TABLE, "chain": hook,
                                 "expr": [*expressions, {"drop": None}], "comment": "wm-deny-" + hook}})
    return objects


def verify_readback(value, expected):
    if not isinstance(value, dict) or set(value) != {"nftables"} or not isinstance(value["nftables"], list):
        raise IsolationError("ISOLATION_READBACK_INVALID")
    observed = []
    for item in value["nftables"]:
        if not isinstance(item, dict) or len(item) != 1:
            raise IsolationError("ISOLATION_READBACK_INVALID")
        kind, fields = next(iter(item.items()))
        if kind == "metainfo":
            continue
        if kind not in {"table", "chain", "rule"} or not isinstance(fields, dict):
            raise IsolationError("ISOLATION_READBACK_INVALID")
        # Only kernel-assigned handles are irrelevant. Unknown flags, extra
        # rules/objects/expressions or changed priorities/policies fail closed.
        observed.append({kind: {key: val for key, val in fields.items() if key != "handle"}})
    if observed != expected:
        raise IsolationError("ISOLATION_READBACK_MISMATCH")


class PanelIsolation:
    def run(self, args, data=None):
        try:
            result = subprocess.run([NFT, *args], input=data, capture_output=True, timeout=10,
                                    env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"})
        except (OSError, subprocess.TimeoutExpired):
            raise IsolationError("ISOLATION_COMMAND_UNCERTAIN") from None
        if result.returncode or len(result.stdout) > MAX_OUTPUT:
            raise IsolationError("ISOLATION_COMMAND_UNCERTAIN")
        return result.stdout

    def read_json(self, args):
        try:
            return json.loads(self.run(["-j", "-n", *args]), object_pairs_hook=unique_object)
        except (ValueError, UnicodeError):
            raise IsolationError("ISOLATION_JSON_INVALID") from None

    def observe(self):
        listing = self.read_json(["list", "tables"])
        if not isinstance(listing, dict) or set(listing) != {"nftables"} or not isinstance(listing["nftables"], list):
            raise IsolationError("ISOLATION_READBACK_INVALID")
        matches = []
        for item in listing["nftables"]:
            if not isinstance(item, dict) or len(item) != 1:
                raise IsolationError("ISOLATION_READBACK_INVALID")
            if "metainfo" in item:
                continue
            table = item.get("table")
            if not isinstance(table, dict) or not isinstance(table.get("family"), str) or not isinstance(table.get("name"), str):
                raise IsolationError("ISOLATION_READBACK_INVALID")
            if table["family"] == "inet" and table["name"] == TABLE:
                matches.append(table)
        if len(matches) > 1:
            raise IsolationError("ISOLATION_READBACK_INVALID")
        return self.read_json(["list", "table", "inet", TABLE]) if matches else None

    def apply(self, expected):
        # One netlink transaction. CREATE, unlike ADD, refuses a table which
        # appeared after observation; never append to another owner's rules.
        commands = [{"create" if index == 0 else "add": item} for index, item in enumerate(expected)]
        self.run(["-j", "-f", "-"], json.dumps({"nftables": commands}).encode())

    def isolate(self, guard, operation_id, generation, candidate_sha256, rollback_manifest_sha256,
                port, node_lock=None):
        if sys.platform != "linux" or os.geteuid() != 0:
            raise IsolationError("ISOLATION_ROOT_LINUX_REQUIRED")
        # Validate the port before journaling. Remaining binding validation is
        # performed by the journal before yielding the pre-effect boundary.
        policy(port, "0" * 64)
        encoded = json.dumps([operation_id, generation, candidate_sha256, rollback_manifest_sha256, port],
                             separators=(",", ":")).encode()
        expected = policy(port, hashlib.sha256(encoded).hexdigest())
        with guard.installation_intent(operation_id, generation, candidate_sha256,
                                       rollback_manifest_sha256, node_lock) as intent:
            existing = self.observe()
            if existing is None:
                if intent["reconciliation_required"]:
                    raise IsolationError("ISOLATION_ABSENT_RECONCILIATION_REQUIRED")
                self.apply(expected)
                existing = self.observe()
            verify_readback(existing, expected)
            return {"api_packet_filter": "VERIFIED_PRESENT", "scope": "CURRENT_BOOT_ONLY",
                    "reconciliation_required": intent["reconciliation_required"],
                    "quiescence": "NOT_PROVEN"}
