"""Bounded optional profile observation; never provisioning or financial replay.

The only output containing credentials is a private caller-owned result file.
The Agent delivers it independently after collection. Recollection is read-only;
lost delivery never requires repeating an access lifecycle command.
"""
from __future__ import annotations

import argparse
from itertools import islice
import os
from pathlib import Path
import re
import sys
from urllib.parse import quote

from access_runtime import (SAFE_ID, assert_no_pending_node_transaction, assert_matching_client,
                            node_mutation_lock, validate_state, visible_vless_inbound_ids)
from runtime_findings import ReadOnlyPanel, digest, private_directory, read_json, write_json

# Reuse the Builder's installed codec and route policy. Staged old packages
# lacking that codec refuse collection; they do not invent a generated profile.
source_library = Path(__file__).resolve().parents[1] / "scripts/lib"
sys.path.insert(0, str(source_library if source_library.is_dir() else Path("/usr/local/lib/wavemesh/lib")))
from native_subscription import placement_public_profiles


class ProfileError(RuntimeError):
    """Fixed codes only; no panel/configuration/URI contents in exceptions."""


class ProfilePanel(ReadOnlyPanel):
    def _allowed_path(self, path: str) -> bool:
        return bool(super()._allowed_path(path) or re.fullmatch(r"/panel/api/clients/subLinks/[A-Za-z0-9_-]{8,128}", path))


def collect_profile(config_path: Path, access_root: Path, node_id: str, tenant_id: str,
                    node_version: int, cursor: int, *, panel_factory=ProfilePanel,
                    mutation_lock_path: Path | None = None, priority_access_id: str | None = None) -> dict:
    if (os.name != "posix" or not all(isinstance(v, str) and SAFE_ID.fullmatch(v) for v in (node_id, tenant_id))
            or type(node_version) is not int or not 0 <= node_version <= 2147483647
            or type(cursor) is not int or not 0 <= cursor <= 2147483647
            or priority_access_id is not None and not SAFE_ID.fullmatch(priority_access_id)):
        raise ProfileError("PROFILE_SCOPE_INVALID")
    # The node lock is shared with the existing Builder/access writer. No access
    # command is executed, no fence/state is repaired, and no API call holds it.
    with node_mutation_lock(mutation_lock_path):
        assert_no_pending_node_transaction(config_path.parent / "transactions")
        result = {"cursor": (cursor + 1) % 2147483648, "profile": None}
        if not access_root.exists():
            return result
        private_directory(access_root)  # Existing directory: validate, never repair.
        entries = list(islice(access_root.iterdir(), 100001))
        if len(entries) > 100000:
            raise ProfileError("PROFILE_INVENTORY_LIMIT")
        fences = sorted(p for p in entries if p.name.endswith(".fence"))
        if len(fences) > 4096:
            raise ProfileError("PROFILE_INVENTORY_LIMIT")
        if not fences:
            return result
        path = next((p for p in fences if p.name == priority_access_id + ".fence"), None) if priority_access_id else None
        path = path or fences[cursor % len(fences)]
        access_id = path.name.removesuffix(".fence")
        if not SAFE_ID.fullmatch(access_id):
            raise ProfileError("PROFILE_FENCE_INVALID")
        fence = read_json(path)
        if fence.get("schema_version") not in {2, 3}:
            return result  # Legacy credentials remain on their existing path.
        version = fence.get("version")
        if (fence.get("access_id") != access_id or fence.get("tenant_id") != tenant_id or fence.get("node_id") != node_id
                or type(version) is not int or not 1 <= version <= 2147483647
                or fence.get("execution_complete") is not True
                or not isinstance(fence.get("command_id"), str) or not SAFE_ID.fullmatch(fence["command_id"])
                or not isinstance(fence.get("placement_id"), str) or not re.fullmatch(r"(?:legacy:)?[A-Za-z0-9_-]{1,128}", fence["placement_id"])
                or any(type(fence.get(k)) is not int or not 1 <= fence[k] <= 2147483647 for k in ("generation", "entitlement_revision"))
                or not re.fullmatch(r"[a-f0-9]{64}", str(fence.get("plan_hash", "")))):
            raise ProfileError("PROFILE_FENCE_INVALID")
        for retained in entries:
            if retained.name.startswith(access_id + ".") and retained.name.endswith(".json"):
                match = re.fullmatch(re.escape(access_id) + r"\.([1-9][0-9]*)\.json", retained.name)
                if not match or int(match[1]) > version:
                    raise ProfileError("PROFILE_FENCE_BEHIND")
        state_path = access_root / f"{access_id}.{version}.json"
        state = read_json(state_path)
        validate_state(state, access_id, version)
        config = read_json(config_path, private=False)
        # Builder node.id is an external inventory ID; SaaS nodeId is a distinct
        # identity. The completed durable fence and mTLS context bind that scope.
        panel = panel_factory(config)
        client_path = "/panel/api/clients/get/" + quote(state["panel_email"], safe="")
        client = panel.call("GET", client_path).get("obj")
        inbounds = panel.call("GET", "/panel/api/inbounds/list")
        visible = visible_vless_inbound_ids(inbounds)
        if not visible:
            raise ProfileError("PROFILE_PUBLIC_INBOUND_MISSING")
        assert_matching_client(client, state, visible)
        if not isinstance(client, dict) or client.get("client", client).get("enable") is not True:
            return result
        sub_path = "/panel/api/clients/subLinks/" + quote(state["sub_id"], safe="")
        native = panel.call("GET", sub_path)
        profiles = placement_public_profiles(config, state["client_uuid"], native)
        if (panel.call("GET", client_path).get("obj") != client or panel.call("GET", sub_path) != native
                or panel.call("GET", "/panel/api/inbounds/list") != inbounds
                or read_json(path) != fence or read_json(state_path) != state or read_json(config_path, private=False) != config):
            raise ProfileError("PROFILE_OBSERVATION_DRIFT")
        result["profile"] = {"schema_version": 1, "access_id": access_id, "placement_id": fence["placement_id"],
            "generation": fence["generation"], "desired_version": version, "entitlement_revision": fence["entitlement_revision"],
            "plan_hash": fence["plan_hash"], "command_id": fence["command_id"], "node_version": node_version,
            "config_sha256": digest(config), "profiles": profiles}
        return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect one private placement profile; no API delivery or panel writes")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--access-root", type=Path, required=True)
    parser.add_argument("--node-id", required=True)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--node-version", type=int, required=True)
    parser.add_argument("--cursor", type=int, required=True)
    parser.add_argument("--priority-access-id")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        private_directory(args.output.parent)
        result = collect_profile(args.config, args.access_root, args.node_id, args.tenant_id, args.node_version, args.cursor,
                                 priority_access_id=args.priority_access_id)
        write_json(args.output, result)
        return 0
    except Exception:
        # Even CLI diagnostics cannot echo paths, client identities or URLs.
        print("PROFILE_COLLECTION_BLOCKED")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
