import copy
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import test_access_entitlements as entitlements
runtime, FakePanel = entitlements.runtime, entitlements.FakePanel


class PlacementDisableTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        FakePanel.clients = {}; FakePanel.updates = []; FakePanel.update_attempts = 0
        FakePanel.adjustments = []; FakePanel.sticky_update_fields = set()
        self.request = dict(entitlements.AccessEntitlementTests.request(), schema_version=2, enabled=False,
            placement_id='legacy:access_12345678', tenant_id='tenant001', node_id='node0001',
            command_id='command001', generation=1, entitlement_revision=8, plan_hash='a'*64, expected_version=0)
        self.patch = mock.patch.object(runtime, 'PanelClient', FakePanel); self.patch.start(); self.addCleanup(self.patch.stop)
        self.barrier = mock.patch.object(runtime, 'quiesce_placement_panel'); self.barrier.start(); self.addCleanup(self.barrier.stop)

    def seed(self):
        return entitlements.AccessEntitlementTests().seed(self.root, expiry_ms=2000000000000, limit_ip=1, total_gb=1000)

    def test_absent_disable_writes_tombstone_without_identity_or_create(self):
        self.request['desired_version'] = 1
        result = runtime.update_entitlements(self.request, {}, self.root)
        self.assertFalse(result['client_exists']); self.assertFalse(result['enabled'])
        self.assertEqual(result['command_id'], self.request['command_id'])
        self.assertEqual(list(self.root.glob('*.json')), [])
        self.assertTrue((self.root/'access_12345678.fence').is_file())
        self.assertEqual(FakePanel.updates, [])
        self.assertEqual(runtime.update_entitlements(self.request, {}, self.root), result)

    def test_disables_every_historical_identity_and_preserves_budget_and_counters(self):
        state = self.seed()
        another = dict(state, desired_version=2, panel_email='wm_access_12345678_2',
            client_uuid='467d034f-0221-4092-85b2-d888f791c970', sub_id='another_subscription_1234')
        runtime.atomic_json(self.root/'access_12345678.2.json', another)
        FakePanel.clients[another['panel_email']] = {**FakePanel.clients[state['panel_email']],
            'email':another['panel_email'], 'id':another['client_uuid'], 'subId':another['sub_id']}
        for c in FakePanel.clients.values(): c.update(up=450, down=50)
        before = copy.deepcopy(FakePanel.clients)
        result = runtime.update_entitlements(self.request, {}, self.root)
        self.assertTrue(result['client_exists']); self.assertEqual(result['disabled_identity_count'],2)
        for email, c in FakePanel.clients.items():
            self.assertFalse(c['enable'])
            for k in ('id','subId','limitIp','totalGB','expiryTime','up','down'): self.assertEqual(c[k],before[email][k])
        updates = len(FakePanel.updates)
        runtime.update_entitlements(self.request, {}, self.root)
        self.assertEqual(len(FakePanel.updates),updates)
        self.assertEqual(FakePanel.adjustments,[])

    def test_unknown_inventory_does_not_mint_absence_proof_or_touch_foreign_client(self):
        self.seed()
        FakePanel.clients['wm_access_12345678_999'] = {'email':'wm_access_12345678_999','enable':True}
        with self.assertRaisesRegex(runtime.ProvisionError,'Untracked'): runtime.update_entitlements(self.request, {}, self.root)
        self.assertEqual(FakePanel.updates,[])

    def test_foreign_identity_stops_before_mutation(self):
        state = self.seed(); FakePanel.clients[state['panel_email']]['subId']='foreign_subscription'
        with self.assertRaises(runtime.ProvisionError): runtime.update_entitlements(self.request, {}, self.root)
        self.assertEqual(FakePanel.updates,[])

    def test_partial_failure_keeps_fence_and_same_command_resumes_remaining_disable(self):
        state = self.seed(); original = FakePanel.call
        def fail_once(panel,method,path,payload=None):
            if method=='POST': raise runtime.ProvisionError('synthetic failure before update')
            return original(panel,method,path,payload)
        with mock.patch.object(FakePanel,'call',fail_once):
            with self.assertRaises(runtime.ProvisionError): runtime.update_entitlements(self.request, {}, self.root)
        self.assertTrue((self.root/'access_12345678.fence').exists())
        result = runtime.update_entitlements(self.request, {}, self.root)
        self.assertFalse(result['enabled']); self.assertFalse(FakePanel.clients[state['panel_email']]['enable'])

    def test_bad_or_unavailable_inventory_refuses_absence(self):
        for obj in [None, {}, [{'email':''}], [{'email':'same'},{'email':'SAME'}]]:
            with mock.patch.object(FakePanel,'call',return_value={'success':True,'obj':obj}):
                with self.assertRaises(runtime.ProvisionError): runtime.update_entitlements(self.request, {}, self.root)


if __name__ == '__main__': unittest.main()
