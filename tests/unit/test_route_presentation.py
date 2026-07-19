import copy
import importlib.util
import json
from pathlib import Path

root = Path(__file__).resolve().parents[2]
tool = root / "scripts" / "lib" / "route_presentation.py"
spec = importlib.util.spec_from_file_location("route_presentation", tool)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

config = {
    "network": {"subscription": {}},
    "exits": [{"id": "de-1", "enabled": True}, {"id": "de-2", "enabled": True}],
    "routes": [
        {
            "id": "route-auto-auto-europe",
            "kind": "auto",
            "enabled": True,
            "presentation": {"published": True},
            "entry": {"inbound_id": 30, "inbound_tag": "wm-route-auto-auto-europe"},
        },
        {
            "id": "route-de-1",
            "kind": "cascade",
            "enabled": True,
            "exit_id": "de-1",
            "entry": {"inbound_id": 10, "inbound_tag": "wm-route-de-1"},
        },
        {
            "id": "route-de-2",
            "kind": "cascade",
            "enabled": True,
            "exit_id": "de-2",
            "entry": {"inbound_id": 20, "inbound_tag": "wm-route-de-2"},
        },
    ],
}

assert module.publication_mode(config) == "all"
assert module.public_route_ids(config) == [
    "route-auto-auto-europe", "route-de-1", "route-de-2"
]

auto_only = module.set_publication_mode(config, "auto-only")
assert module.public_route_ids(auto_only) == ["route-auto-auto-europe"]
assert set(module.describe(auto_only)["hidden_enabled_route_ids"]) == {"route-de-1", "route-de-2"}

unpublished = copy.deepcopy(config)
unpublished["routes"][0]["presentation"]["published"] = False
try:
    module.set_publication_mode(unpublished, "auto-only")
except ValueError as error:
    assert "published Auto Route" in str(error)
else:
    raise AssertionError("auto-only accepted without a published Auto Route")

actual = {
    "obj": [
        {
            "id": 10,
            "settings": {"clients": [
                {"id": "manual-a", "email": "user-a", "subId": "sub-a", "enable": True},
                {"id": "manual-disabled", "email": "user-disabled", "subId": "sub-disabled", "enable": False},
            ]},
        },
        {
            "id": 20,
            "settings": {"clients": [
                {"id": "manual-b", "email": "user-b", "subId": "sub-b", "enable": True},
            ]},
        },
        {
            "id": 30,
            "settings": {"clients": [
                {"id": "auto-a", "email": "user-a", "subId": "sub-a", "enable": True},
            ]},
        },
    ]
}
report = module.native_transition_report(config, auto_only, actual)
assert report["ready"] is False
assert report["source_profile_count"] == 2
assert report["targets"][0]["missing_profile_count"] == 1

actual["obj"][2]["settings"]["clients"].append(
    {"id": "auto-b", "email": "user-b", "subId": "sub-b", "enable": True}
)
report = module.native_transition_report(config, auto_only, actual)
assert report["ready"] is True
assert report["targets"][0]["missing_profile_count"] == 0

print("route presentation tests: OK")
