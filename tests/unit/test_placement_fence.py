"""Real durable-file proof of v2 binding in the existing access fence."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location('placement_runtime', Path(__file__).resolve().parents[2] / 'agent/access_runtime.py')
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


class PlacementFenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.effects = []
        self.barrier = mock.patch.object(runtime, 'quiesce_placement_panel'); self.barrier.start(); self.addCleanup(self.barrier.stop)

        @runtime.fenced_access('access.update_entitlements')
        def apply(value, config, root):
            self.effects.append(copy.deepcopy(value))
            if config.get('crash'):
                raise RuntimeError('synthetic mutation-boundary crash')
            return {'accepted': True}
        self.apply = apply
        self.request = dict(schema_version=2, access_id='access001', placement_id='legacy:access001',
            tenant_id='tenant001', node_id='node0001', command_id='command001', generation=1,
            entitlement_revision=4, plan_hash='a'*64, expected_version=0,
            desired_version=1, enabled=False, expires_at='2026-11-30T00:00:00Z', device_limit=1, quota_bytes='0')

    def test_v2_command_replay_keeps_exact_binding_and_durable_fence(self):
        self.apply(self.request, {}, self.root)
        fence = json.loads((self.root/'access001.fence').read_text())
        for name in ('placement_id','tenant_id','node_id','generation','entitlement_revision','command_id','plan_hash'):
            self.assertEqual(fence[name], self.request[name])
        self.apply(self.request, {}, self.root)
        for name, value in [('command_id','different'),('plan_hash','b'*64),('generation',2)]:
            changed = dict(self.request, **{name:value})
            with self.assertRaises(runtime.ProvisionError): self.apply(changed, {}, self.root)

    def test_legacy_writer_cannot_override_v2_even_with_a_larger_version(self):
        self.apply(self.request, {}, self.root)
        old = {k:v for k,v in self.request.items() if k not in runtime.placement_binding(self.request)}
        old['desired_version'] = 99
        with self.assertRaisesRegex(runtime.ProvisionError,'Legacy access writer is fenced'):
            self.apply(old, {}, self.root)
        self.assertEqual(len(self.effects),1)

    def test_generation_revision_and_identity_cannot_regress(self):
        first = dict(self.request,generation=2,entitlement_revision=7)
        self.apply(first, {}, self.root)
        for field, value in [('generation',1),('entitlement_revision',6),('placement_id','another'),
                             ('tenant_id','another'),('node_id','another')]:
            with self.assertRaises(runtime.ProvisionError):
                self.apply(dict(first,desired_version=2,expected_version=1,command_id='next',**{field:value}), {}, self.root)
        self.assertEqual(len(self.effects),1)

    def test_crash_burns_old_version_and_same_command_can_resume(self):
        with self.assertRaises(RuntimeError): self.apply(self.request, {'crash':True}, self.root)
        with self.assertRaises(runtime.ProvisionError):
            self.apply(dict(self.request,command_id='different'), {}, self.root)
        self.apply(self.request, {}, self.root)
        self.assertEqual(len(self.effects),2)

    def test_disable_can_fence_uncertain_version_but_enable_must_reconcile(self):
        self.apply(self.request, {}, self.root)
        newer = dict(self.request,desired_version=2,command_id='nextcmd1',entitlement_revision=5)
        with self.assertRaisesRegex(runtime.ProvisionError,'reconciled local version'):
            self.apply(dict(newer,enabled=True), {}, self.root)
        self.apply(newer, {}, self.root)
        self.apply(dict(newer,desired_version=3,expected_version=2,command_id='restorecmd1',enabled=True), {}, self.root)

    def test_bad_metadata_fails_before_any_effect(self):
        for name, value in [('generation',True),('entitlement_revision',0),('plan_hash','x'),
                             ('expected_version',1),('placement_id','../other'),('schema_version',True)]:
            with self.assertRaises(runtime.ProvisionError): self.apply(dict(self.request,**{name:value}), {}, self.root)
        self.assertEqual(self.effects,[])

    def test_unknown_or_partial_durable_fence_cannot_downgrade_to_legacy(self):
        self.apply(self.request, {}, self.root)
        path = self.root/'access001.fence'
        original = json.loads(path.read_text())
        for mutate in [lambda f: f.update(schema_version=3), lambda f: f.pop('schema_version'),
                       lambda f: f.pop('plan_hash'), lambda f: f.update(unrecognized='value')]:
            changed = copy.deepcopy(original); mutate(changed)
            path.write_text(json.dumps(changed))
            with self.assertRaises(runtime.ProvisionError): self.apply(self.request, {}, self.root)
        self.assertEqual(len(self.effects),1)


if __name__ == '__main__': unittest.main()
