"""Placement wire compatibility and installed-runtime admission, no Node I/O."""
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import test_node_agent as legacy

agent = legacy.agent


def envelope():
    return dict(command_id='command_12345678', attempt=3, schema_version=2,
        target_node_id='node_12345678', tenant_id='tenant_12345678', expected_version=5,
        type='access.update_entitlements', payload=dict(access_id='access_12345678',
            desired_version=6, enabled=False, expires_at='2027-01-01T00:00:00Z',
            device_limit=1, quota_bytes='1073741824', placement_id='legacy:access_12345678',
            generation=2, entitlement_revision=9, plan_hash='a'*64, command_id='command_12345678'))


def payload():
    c = envelope()
    return agent.validate_access_command(c, c['target_node_id'], c['tenant_id'])[3]


def material():
    p = payload()
    return {k: p[k] for k in ('schema_version', 'desired_version', 'placement_id',
        'generation', 'entitlement_revision', 'plan_hash', 'command_id', 'enabled')} | dict(
        client_exists=False, disabled_identity_count=0, inventory_hash='b'*64)


class PlacementTransportTests(unittest.TestCase):
    def instance(self, root):
        instance = object.__new__(agent.NodeAgent)
        instance.config = SimpleNamespace(node_id='node_12345678', tenant_id='tenant_12345678',
            command_mode='access', env_path=root/'agent.env', access_state_root=root/'access',
            access_runtime_path=root/'runtime.py')
        instance.last_mtls_status = {'state':'SHADOW_ACTIVE'}
        return instance

    def test_v2_checks_scope_exact_payload_and_expected_version(self):
        c = envelope()
        self.assertEqual(payload()['expected_version'], 5)
        damages = [dict(tenant_id='foreign'), dict(target_node_id='foreign'), dict(schema_version=True),
            dict(expected_version=6), dict(expected_version=True), dict(type='access.prepare_replacement'),
            *[dict(payload={**c['payload'], **d}) for d in [dict(generation=True), dict(entitlement_revision=0),
                dict(command_id='other_command'), dict(plan_hash='no'), dict(placement_id='../escape'), dict(shell='id')]]]
        for d in damages:
            with self.subTest(d=d), self.assertRaises(agent.AgentError):
                agent.validate_access_command({**c, **d}, c['target_node_id'], c['tenant_id'])

    def test_v1_keeps_original_shape_and_rejects_v2_fields(self):
        c = envelope()
        c['schema_version'] = 1
        for k in ('placement_id', 'generation', 'entitlement_revision', 'plan_hash', 'command_id'):
            del c['payload'][k]
        self.assertEqual(agent.validate_access_command(c,c['target_node_id'])[3],c['payload'])
        c['payload']['placement_id'] = 'legacy:access_12345678'
        with self.assertRaises(agent.AgentError): agent.validate_access_command(c,c['target_node_id'])

    def test_material_requires_actual_runtime_binding_and_complete_disable_proof(self):
        p, m = payload(), material()
        agent.validate_placement_material(m,p)
        for d in [dict(generation=1), dict(entitlement_revision=8), dict(command_id='foreign'),
            dict(plan_hash='c'*64), dict(enabled=True), dict(client_exists=True), dict(inventory_hash=''),
            dict(disabled_identity_count=True), dict(client_uuid='unexpected')]:
            with self.subTest(d=d), self.assertRaises(agent.AgentError):
                agent.validate_placement_material({**m,**d},p)
        with self.assertRaises(agent.AgentError):
            agent.validate_placement_material({'desired_version':6},p)

    def test_cycle_preserves_attempt_and_placement_path_on_mtls_only(self):
        with tempfile.TemporaryDirectory() as directory:
            instance = self.instance(Path(directory)); c = envelope(); calls = []
            def api(method,path,body,**kw):
                calls.append((method,path,copy.deepcopy(body)))
                return c if method == 'GET' else {}
            instance.mtls_runtime = SimpleNamespace(api_json=api)
            instance.placement_runtime_ready = mock.Mock(return_value=True)
            instance.execute_access_runtime = mock.Mock(return_value=material())
            instance.report_access_command_failure = mock.Mock()
            instance.run_access_command_cycle()
            self.assertEqual(len(calls),4)
            self.assertEqual(calls[1][2],{'attempt':3})
            self.assertTrue(calls[2][1].endswith('/accesses/access_12345678/placements/legacy:access_12345678/materialize'))
            self.assertEqual(calls[2][2],material())
            self.assertEqual(calls[3][2]['attempt'],3)
            instance.report_access_command_failure.assert_not_called()
            self.assertEqual(instance.execute_access_runtime.call_args.args[1],payload())

    def test_old_runtime_refused_before_started_or_physical_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            instance=self.instance(Path(directory)); instance.mtls_runtime=mock.Mock()
            instance.mtls_runtime.api_json.return_value=envelope()
            instance.placement_runtime_ready=mock.Mock(return_value=False)
            instance.execute_access_runtime=mock.Mock(); instance.report_access_command_failure=mock.Mock()
            instance.run_access_command_cycle()
            self.assertEqual(instance.mtls_runtime.api_json.call_count,1)
            instance.execute_access_runtime.assert_not_called()
            instance.report_access_command_failure.assert_called_once()

    def test_old_runtime_output_cannot_be_upgraded_by_agent(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); instance=self.instance(root); instance.config.access_runtime_path.write_text('old runtime')
            def execute(args,**kw):
                Path(args[args.index('--output')+1]).write_text(json.dumps({'desired_version':6}))
                return SimpleNamespace(returncode=0)
            with mock.patch.object(agent.subprocess,'run',side_effect=execute), self.assertRaises(agent.AgentError):
                instance.execute_access_runtime('access.update_entitlements',payload())

    def test_runtime_capability_probe_is_readonly_cached_and_invalidated_by_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); instance=self.instance(root); path=instance.config.access_runtime_path
            self.assertFalse(instance.placement_runtime_ready())
            path.write_text('old runtime')
            with mock.patch.object(agent,'run_json_command',return_value={
                'access_command_schemas':[1,2], 'placement_fence':2, 'disable_absent':True}) as probe:
                self.assertFalse(instance.placement_runtime_ready())
                self.assertFalse(instance.placement_runtime_ready()); self.assertEqual(probe.call_count,1)
            path.write_text('new runtime')
            with mock.patch.object(agent,'run_json_command',return_value={
                'access_command_schemas':[1,2], 'placement_fence':2, 'disable_absent':True, 'unknown_effect_barrier':1}) as probe:
                self.assertTrue(instance.placement_runtime_ready()); self.assertTrue(instance.placement_runtime_ready())
                self.assertEqual(probe.call_count,1)
                self.assertEqual(probe.call_args.args[0],['/usr/bin/python3',str(path),'--capabilities'])
            path.write_text('broken runtime')
            with mock.patch.object(agent,'run_json_command',side_effect=RuntimeError):
                self.assertFalse(instance.placement_runtime_ready())


if __name__ == '__main__': unittest.main()
