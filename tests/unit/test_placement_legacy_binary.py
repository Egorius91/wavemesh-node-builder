"""Exercise the actual pinned old executor, not a v1 payload in the new one."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
OLD_SOURCE = ROOT / 'tests/fixtures/access_runtime_dc4e66d.py'
OLD_SHA = '6134d7f5cb9c864429092216ce1b3e6e56b5f42f066ce032db5f49bfcdaf1223'


def module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


class PlacementLegacyBinaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        assert hashlib.sha256(OLD_SOURCE.read_bytes()).hexdigest() == OLD_SHA
        cls.old = module(OLD_SOURCE, 'pinned_pre_placement_runtime')
        cls.current = module(ROOT / 'agent/access_runtime.py', 'guarded_placement_runtime')

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.effects = []
        self.request = dict(schema_version=2, access_id='access001', placement_id='legacy:access001', tenant_id='tenant001',
            node_id='node0001', command_id='command001', generation=1, entitlement_revision=4, plan_hash='a'*64,
            expected_version=0, desired_version=1, enabled=False, expires_at='2026-11-30T00:00:00Z', device_limit=1, quota_bytes='0')
        patch = mock.patch.object(self.current, 'quiesce_placement_panel'); patch.start(); self.addCleanup(patch.stop)

        @self.current.fenced_access('access.update_entitlements')
        def current_apply(value, config, root):
            self.effects.append(copy.deepcopy(value)); return {'accepted': True}
        self.current_apply = current_apply

    def legacy_request(self, version):
        return {k: v for k, v in dict(self.request, desired_version=version, enabled=True).items()
                if k not in self.current.placement_binding(self.request)}

    def test_actual_old_executor_works_on_legacy_state_then_refuses_guarded_v2(self):
        old_effects = []

        @self.old.fenced_access('access.update_entitlements')
        def old_apply(value, config, root):
            old_effects.append(value['desired_version']); return {'accepted': True}

        old_apply(self.legacy_request(1), {}, self.root)
        self.assertEqual(old_effects, [1])
        legacy_fence = json.loads((self.root/'access001.fence').read_text())
        self.assertEqual(set(legacy_fence), {'access_id', 'version', 'payload_hash'})
        self.request.update(desired_version=2, expected_version=1)
        self.current_apply(self.request, {}, self.root)
        before = (self.root/'access001.fence').read_bytes()
        fence = json.loads(before)
        self.assertNotIn('payload_hash', fence); self.assertRegex(fence['placement_payload_hash'], r'^[a-f0-9]{64}$')
        with self.assertRaisesRegex(self.old.ProvisionError, 'Access command fence is invalid'):
            old_apply(self.legacy_request(999), {}, self.root)
        self.assertEqual(old_effects, [1]); self.assertEqual((self.root/'access001.fence').read_bytes(), before)
        self.current_apply(self.request, {}, self.root)  # exact current replay still works

    def test_actual_old_process_cannot_update_or_cleanup_a_guarded_placement(self):
        self.current_apply(self.request, {}, self.root)
        before = (self.root/'access001.fence').read_bytes()
        code = r'''
import importlib.util,json,sys
from pathlib import Path
spec=importlib.util.spec_from_file_location('pinned_old_runtime',sys.argv[1])
old=importlib.util.module_from_spec(spec);spec.loader.exec_module(old)
root=Path(sys.argv[2]); request=json.loads(sys.argv[3]); operation=sys.argv[4]
@old.fenced_access(operation)
def mutate(value,config,state_root):
    (state_root/'unexpected-effect').write_text('MUTATED')
try:
    mutate(request,{},root)
except old.ProvisionError as error:
    if str(error)!='Access command fence is invalid': raise
    print(json.dumps({'status':'LEGACY_WRITER_FENCED'}))
else:
    raise SystemExit('Old writer unexpectedly crossed the mutation boundary')
'''
        for operation in ('access.update_entitlements', 'cleanup'):
            child = subprocess.run([sys.executable, '-B', '-c', code, str(OLD_SOURCE), str(self.root),
                json.dumps(self.legacy_request(999)), operation], capture_output=True, text=True, timeout=10)
            self.assertEqual(child.returncode, 0, child.stderr)
            self.assertEqual(json.loads(child.stdout), {'status': 'LEGACY_WRITER_FENCED'})
            self.assertFalse((self.root/'unexpected-effect').exists())
            self.assertEqual((self.root/'access001.fence').read_bytes(), before)

    def test_unqualified_old_v2_fence_is_preserved_and_refused_before_any_effect(self):
        self.current_apply(self.request, {}, self.root)
        path = self.root/'access001.fence'
        old = json.loads(path.read_text()); old['payload_hash'] = old.pop('placement_payload_hash')
        self.current.atomic_json(path, old); before = path.read_bytes()
        request = dict(self.request, desired_version=2, expected_version=1, command_id='command002')
        with self.assertRaisesRegex(self.current.ProvisionError, 'Access command fence is invalid'):
            self.current_apply(request, {}, self.root)
        self.assertEqual(len(self.effects), 1); self.assertEqual(path.read_bytes(), before)


if __name__ == '__main__': unittest.main()
