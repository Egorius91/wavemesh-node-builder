"""Optional delivery boundaries plus real POSIX collection on synthetic inputs."""
from copy import deepcopy
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "agent"))
import node_agent as agent
import placement_profiles as profiles
import runtime_findings as findings

CLIENT = "11111111-2222-4333-8444-555555555555"
ACCESS, NODE, TENANT = "access_fixture", "node_fixture", "tenant_fixture"
URI = f"vless://{CLIENT}@entry.example.com:443?" + urlencode({"encryption": "none", "security": "tls", "type": "xhttp",
    "host": "entry.example.com", "sni": "entry.example.com", "path": "/api/public-route-one/", "mode": "stream-one"}) + "#PrivateCanary"


class AgentProfileCycle(unittest.TestCase):
    def config(self, directory, enabled="false", auth="mtls", mode="shadow"):
        p = Path(directory) / "agent.env"
        p.write_text("\n".join(["WAVEMESH_API_BASE=https://api.example.invalid/api", "WAVEMESH_NODE_ID=" + NODE,
            "WAVEMESH_TENANT_ID=" + TENANT, "WAVEMESH_AGENT_AUTH_MODE=" + auth, "WAVEMESH_AGENT_MTLS_MODE=" + mode,
            "WAVEMESH_AGENT_MTLS_API_BASE=https://mtls.example.invalid/api", "WAVEMESH_AGENT_PUBLIC_PROFILES_ENABLED=" + enabled,
            "WAVEMESH_AGENT_RUNTIME_PATH=" + str(Path(directory) / "runtime.json"),
            "WAVEMESH_AGENT_TOKEN=wvn_" + "a" * 40, "WAVEMESH_AGENT_TOKEN_EXPIRES_AT=2030-01-01T00:00:00Z"]))
        return agent.AgentConfig.load(p)

    def test_default_and_mtls_gates_do_not_collect_or_send(self):
        with tempfile.TemporaryDirectory() as directory:
            instance = agent.NodeAgent(self.config(directory), mtls_runtime=mock.Mock())
            with mock.patch.object(agent.subprocess, "run") as run:
                instance.run_public_profile_cycle(); run.assert_not_called(); instance.mtls_runtime.api_json.assert_not_called()
                instance.config.public_profiles_enabled = True
                instance.run_public_profile_cycle(); run.assert_not_called()
                self.assertEqual(instance.last_profile_status, {"state": "MTLS_NOT_READY"})
            for enabled, auth, mode in [("observe", "mtls", "shadow"), ("true", "bearer", "shadow"),
                                        ("true", "bootstrap-mtls", "shadow"), ("true", "mtls", "disabled")]:
                with self.assertRaises(agent.AgentError): self.config(directory, enabled, auth, mode)

    def test_timeout_and_private_exception_are_isolated_without_output_in_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            instance = agent.NodeAgent(self.config(directory, "true"), mtls_runtime=mock.Mock())
            instance.last_mtls_status = {"state": "SHADOW_ACTIVE"}
            error = subprocess.TimeoutExpired("synthetic", 20, output=URI, stderr="private_canary")
            with mock.patch.object(agent.subprocess, "run", side_effect=error) as run, self.assertLogs(agent.LOG, "WARNING") as logs:
                instance.run_public_profile_cycle()
            self.assertNotIn(URI, str(logs.output)); self.assertNotIn("private_canary", str(logs.output))
            self.assertEqual(run.call_args.kwargs["timeout"], 20)
            self.assertEqual(run.call_args.kwargs["stdout"], subprocess.DEVNULL)
            self.assertEqual(run.call_args.kwargs["stderr"], subprocess.DEVNULL)
            instance.mtls_runtime.api_json.assert_not_called()
            self.assertEqual(list(Path(directory).glob(".public-profile.*")), [])

    def test_private_result_uses_dedicated_legacy_placement_route_without_financial_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            api = mock.Mock(); api.api_json.return_value = {"accepted": True, "profile_hash": "c" * 64}
            instance = agent.NodeAgent(self.config(directory, "true"), mtls_runtime=api)
            instance.last_mtls_status = {"state": "SHADOW_ACTIVE"}; instance.profile_priority_access_id = ACCESS
            body = {"schema_version": 1, "access_id": ACCESS, "placement_id": "legacy:" + ACCESS,
                "node_version": instance.config.observed_version, "profiles": [URI]}
            with mock.patch.object(agent.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run, \
                    mock.patch.object(findings, "read_json", return_value={"cursor": 1, "profile": body}):
                instance.run_public_profile_cycle()
            api.api_json.assert_called_once_with("POST", f"internal/v1/nodes/{NODE}/accesses/{ACCESS}/placements/legacy:{ACCESS}/public-profile", body, expected=(200,))
            self.assertNotIn(URI, str(run.call_args))
            self.assertIsNone(instance.profile_priority_access_id)
            self.assertEqual(instance.runtime, {})
            self.assertEqual(list(Path(directory).glob(".public-profile.*")), [])

    def test_lost_metadata_response_recollects_without_restarting_access_command(self):
        with tempfile.TemporaryDirectory() as directory:
            api = mock.Mock(); api.api_json.side_effect = [RuntimeError("private_canary"), {"accepted": True, "profile_hash": "c" * 64}]
            instance = agent.NodeAgent(self.config(directory, "true"), mtls_runtime=api)
            instance.last_mtls_status = {"state": "SHADOW_ACTIVE"}; instance.profile_priority_access_id = ACCESS
            body = {"schema_version": 1, "access_id": ACCESS, "placement_id": "placement_fixture", "node_version": instance.config.observed_version, "profiles": [URI]}
            with mock.patch.object(agent.subprocess, "run", return_value=SimpleNamespace(returncode=0)), \
                    mock.patch.object(findings, "read_json", return_value={"cursor": 1, "profile": body}), \
                    mock.patch.object(instance, "execute_access_runtime") as execute, \
                    mock.patch.object(instance, "report_access_command_failure") as failure, self.assertLogs(agent.LOG, "WARNING") as logs:
                instance.run_public_profile_cycle(); self.assertEqual(instance.profile_priority_access_id, ACCESS)
                instance.run_public_profile_cycle(); execute.assert_not_called(); failure.assert_not_called()
            self.assertEqual(api.api_json.call_args_list[0], api.api_json.call_args_list[1])
            self.assertNotIn("private_canary", str(logs.output)); self.assertEqual(instance.runtime, {})

    def test_read_only_adapters_keep_distinct_path_permissions(self):
        cfg = {"panel": {"listen_port": 23456, "path": "/fixture", "api_auth": {"token": "synthetic_token_123"}}}
        old, new = findings.ReadOnlyPanel(cfg), profiles.ProfilePanel(cfg)
        path = "/panel/api/clients/subLinks/synthetic_private_123"
        self.assertFalse(old._allowed_path(path)); self.assertTrue(new._allowed_path(path))
        for method, target, body in [("POST", path, None), ("GET", path, {}), ("GET", path + "/extra", None),
                                     ("GET", path + "?secret", None), ("GET", "/arbitrary", None)]:
            with self.assertRaises(findings.FindingError): new.call(method, target, body)


@unittest.skipUnless(os.name == "posix", "Real private files and node flock require Linux")
class NativeProfileCollection(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name); self.access = self.root / "access"; self.access.mkdir(mode=0o700)
        self.config_path = self.root / "config.json"; self.lock = self.root / "node.lock"
        self.config = {"node": {"id": "external-inventory-id", "role": "entry"}, "server": {"domain": "entry.example.com"},
            "network": {"public_port": 443, "subscription": {"backend": "xui-native"}},
            "exits": [{"id": "ch", "enabled": True}], "routes": [{"id": "route-ch", "kind": "cascade", "exit_id": "ch",
                "display_name": "Switzerland", "entry": {"public_path": "/api/public-route-one/"}}]}
        self.state = {"access_id": ACCESS, "desired_version": 1, "client_uuid": CLIENT, "panel_email": "synthetic_client", "sub_id": "synthetic_private_123"}
        self.fence = {"schema_version": 2, "access_id": ACCESS, "version": 1, "tenant_id": TENANT, "node_id": NODE,
            "placement_id": "legacy:" + ACCESS, "command_id": "command_fixture", "generation": 1, "entitlement_revision": 2,
            "plan_hash": "a" * 64, "placement_payload_hash": "b" * 64, "expected_version": 0, "execution_complete": True}
        self.fence_path = self.access / (ACCESS + ".fence"); self.state_path = self.access / (ACCESS + ".1.json")
        for p, v in [(self.config_path, self.config), (self.fence_path, self.fence), (self.state_path, self.state)]: findings.write_json(p, v)
        self.client = {"client": {"email": self.state["panel_email"], "uuid": CLIENT, "subId": self.state["sub_id"], "enable": True}, "inboundIds": [9]}
        self.calls = []; self.drift = False
        outer = self
        class Panel:
            def __init__(self, config): self.reads = 0
            def call(self, method, path, payload=None):
                assert method == "GET" and payload is None; outer.calls.append(path)
                if "/clients/get/" in path: return {"success": True, "obj": deepcopy(outer.client)}
                if path == "/panel/api/inbounds/list": return {"success": True, "obj": [{"id": 9, "enable": True, "protocol": "vless", "remark": "Public"}]}
                if "/subLinks/" in path:
                    self.reads += 1
                    return {"success": True, "obj": [URI.replace("#PrivateCanary", "#Drift" if outer.drift and self.reads == 2 else "#PrivateCanary")]}
                raise AssertionError("Unexpected panel operation")
        self.panel_factory = Panel

    def collect(self, **kwargs):
        return profiles.collect_profile(self.config_path, self.access, NODE, TENANT, 3, 0,
            panel_factory=self.panel_factory, mutation_lock_path=self.lock, **kwargs)

    def test_completed_v2_and_v3_export_current_binding_without_modifying_state(self):
        for schema in (2, 3):
            self.fence["schema_version"] = schema; findings.write_json(self.fence_path, self.fence)
            before = {p: p.read_bytes() for p in (self.config_path, self.fence_path, self.state_path)}
            result = self.collect(priority_access_id=ACCESS)
            self.assertEqual(result["profile"]["placement_id"], "legacy:" + ACCESS)
            self.assertEqual(result["profile"]["node_version"], 3)
            self.assertEqual(result["profile"]["config_sha256"], findings.digest(self.config))
            self.assertNotIn("PrivateCanary", str(result)); self.assertIn("#Switzerland", result["profile"]["profiles"][0])
            self.assertEqual(before, {p: p.read_bytes() for p in before})

    def test_incomplete_wrong_scope_and_newer_state_prevent_export(self):
        for key, value in [("execution_complete", False), ("node_id", "other_node"), ("tenant_id", "other_tenant")]:
            fence = {**self.fence, key: value}; findings.write_json(self.fence_path, fence)
            with self.assertRaises(profiles.ProfileError): self.collect()
        findings.write_json(self.fence_path, self.fence); findings.write_json(self.access / (ACCESS + ".2.json"), {**self.state, "desired_version": 2})
        with self.assertRaisesRegex(profiles.ProfileError, "PROFILE_FENCE_BEHIND"): self.collect()
        self.assertEqual(self.calls, [])

    def test_observable_drift_and_disabled_client_return_no_successful_profile(self):
        self.drift = True
        with self.assertRaisesRegex(profiles.ProfileError, "PROFILE_OBSERVATION_DRIFT"): self.collect()
        self.drift = False; self.client["client"]["enable"] = False
        self.assertIsNone(self.collect()["profile"])

    def test_busy_node_lock_and_unsafe_state_refuse_before_panel_read(self):
        from access_runtime import node_mutation_lock, ProvisionError
        with node_mutation_lock(self.lock), self.assertRaises(ProvisionError): self.collect()
        self.state_path.chmod(0o644)
        with self.assertRaises(findings.FindingError): self.collect()
        self.assertEqual(self.calls, [])

    def test_legacy_and_empty_inventory_do_not_create_or_repair_access_state(self):
        findings.write_json(self.fence_path, {"access_id": ACCESS, "version": 1, "payload_hash": "a" * 64})
        self.assertIsNone(self.collect()["profile"]); self.fence_path.unlink(); self.state_path.unlink(); self.access.rmdir()
        self.assertIsNone(self.collect()["profile"]); self.assertFalse(self.access.exists()); self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
