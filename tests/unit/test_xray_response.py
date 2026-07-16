import importlib.util
import json
from pathlib import Path

root = Path(__file__).resolve().parents[2]
tool = root / "scripts" / "lib" / "xray_response.py"
spec = importlib.util.spec_from_file_location("xray_response", tool)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

template = {"outbounds": [{"tag": "direct"}], "routing": {"rules": []}}
direct_string = {"success": True, "obj": json.dumps(template)}
wrapped_object = {"success": True, "obj": {"xraySetting": json.dumps(template)}}
direct_object = {"success": True, "obj": template}
settings_wrapper = {"clientReverseTags": [], "inboundTags": [], "outboundTestUrl": "https://example.com", "xraySetting": json.dumps(template)}
double_wrapped_object = {"success": True, "obj": {"xraySetting": settings_wrapper}}
double_wrapped_string = {"success": True, "obj": json.dumps({"xraySetting": settings_wrapper})}
nested_string_envelope = {"success": True, "obj": json.dumps({"xraySetting": json.dumps(settings_wrapper)})}

assert module.extract_template(direct_string) == template
assert module.extract_template(wrapped_object) == template
assert module.extract_template(direct_object) == template
assert module.extract_template(double_wrapped_object) == template
assert module.extract_template(double_wrapped_string) == template
assert module.extract_template(nested_string_envelope) == template
route = {"matched": True, "outboundTag": "wm-exit-de-fra-1", "groupTags": []}
assert module.extract_route_outbound({"obj": route}) == "wm-exit-de-fra-1"
assert module.extract_route_outbound({"obj": json.dumps(route)}) == "wm-exit-de-fra-1"
assert module.extract_route_outbound({"obj": {"matched": False, "outboundTag": ""}}) == ""

reachable = {
    "success": True,
    "obj": {
        "tag": "wm-exit-de-fra-1",
        "success": True,
        "delay": 87,
        "mode": "tcp",
        "endpoints": [{"address": "exit.example.com:443", "success": True, "delay": 87}],
    },
}
unreachable = {
    # The request itself succeeded, but the probe inside obj failed.
    "success": True,
    "obj": {
        "tag": "wm-exit-de-fra-1",
        "success": False,
        "delay": 5001,
        "error": "dial tcp: i/o timeout",
        "mode": "tcp",
        "endpoints": [{"address": "exit.example.com:443", "success": False, "delay": 5001, "error": "i/o timeout"}],
    },
}
inconsistent = {
    "success": True,
    "obj": {
        "success": True,
        "delay": 1,
        "mode": "tcp",
        "endpoints": [{"address": "exit.example.com:443", "success": False, "delay": 1}],
    },
}
assert module.extract_test_outbound(reachable) == {"success": True, "delay": 87, "error": None}
assert module.extract_test_outbound(unreachable) == {"success": False, "delay": 5001, "error": "dial tcp: i/o timeout"}
assert module.extract_test_outbound(inconsistent)["success"] is False
assert module.extract_test_outbound({"success": True, "obj": 42}) == {"success": True, "delay": 42, "error": None}
assert module.extract_test_outbound({
    "success": True,
    "obj": {"success": True, "delay": 243, "mode": "real", "httpStatus": 204},
}) == {"success": True, "delay": 243, "error": None}

for invalid in ({"obj": None}, {"obj": "[]"}, {"obj": {"xraySetting": "[]"}}, {"obj": {"xraySetting": {"xraySetting": None}}}):
    try:
        module.extract_template(invalid)
    except (ValueError, json.JSONDecodeError):
        pass
    else:
        raise AssertionError("unsupported Xray response was accepted")

for invalid in ({"obj": None}, {"obj": "[]"}, {"obj": True}):
    try:
        module.extract_test_outbound(invalid)
    except (ValueError, json.JSONDecodeError):
        pass
    else:
        raise AssertionError("unsupported testOutbound response was accepted")

print("xray response tests: OK")
