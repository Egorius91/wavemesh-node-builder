"""First real entitlement update over retained legacy identity/state; synthetic panel."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import test_access_entitlements as entitlements
from test_placement_legacy_binary import module, OLD_SOURCE, OLD_SHA

runtime, FakePanel = entitlements.runtime, entitlements.FakePanel


class PlacementLegacyAdoptionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        fixture = entitlements.AccessEntitlementTests(); fixture.setUp()
        self.identity = fixture.seed(self.root, expiry_ms=1800000000000, limit_ip=1, total_gb=1000)
        FakePanel.clients[self.identity['panel_email']].update(up=450, down=50)
        self.request = dict(fixture.request(device_limit=1, quota_bytes='1000'), schema_version=2,
            placement_id='legacy:access_12345678', tenant_id='tenant001', node_id='node0001',
            command_id='adoption001', generation=1, entitlement_revision=4, plan_hash='a'*64, expected_version=3)
        self.config = fixture.config()
        patch = mock.patch.object(runtime, 'PanelClient', FakePanel); patch.start(); self.addCleanup(patch.stop)
        patch = mock.patch.object(runtime, 'quiesce_placement_panel'); self.barrier = patch.start(); self.addCleanup(patch.stop)

    def test_first_v2_update_and_replay_keep_legacy_identity_counters_and_history(self):
        legacy_bytes = (self.root/'access_12345678.3.json').read_bytes()
        before = copy.deepcopy(FakePanel.clients[self.identity['panel_email']])
        deny = dict(self.request, enabled=False, command_id='adoptiondeny')
        proof = runtime.update_entitlements(deny, self.config, self.root)
        self.assertFalse(proof['enabled']); self.assertEqual(proof['disabled_identity_count'], 1)
        self.assertFalse(FakePanel.clients[self.identity['panel_email']]['enable'])
        self.request.update(desired_version=5, expected_version=4, command_id='adoptionenable')
        material = runtime.update_entitlements(self.request, self.config, self.root)
        self.assertEqual(material['client_uuid'], self.identity['client_uuid'])
        self.assertEqual(material['sub_id'], self.identity['sub_id'])
        self.assertEqual(material['placement_id'], self.request['placement_id'])
        self.assertEqual(len(FakePanel.clients), 1)
        for key in ('id', 'subId', 'up', 'down', 'totalGB'):
            self.assertEqual(FakePanel.clients[self.identity['panel_email']][key], before[key])
        self.assertEqual((self.root/'access_12345678.3.json').read_bytes(), legacy_bytes)
        self.assertTrue(json.loads((self.root/'access_12345678.fence').read_text())['execution_complete'])
        updates = len(FakePanel.updates)
        self.assertEqual(runtime.update_entitlements(self.request, self.config, self.root), material)
        self.assertEqual(len(FakePanel.updates), updates); self.barrier.assert_called_once()
        self.assertEqual(hashlib.sha256(OLD_SOURCE.read_bytes()).hexdigest(), OLD_SHA)
        old = module(OLD_SOURCE, 'legacy_adoption_old_runtime')
        request = {k: v for k, v in self.request.items() if k not in runtime.placement_binding(self.request)}
        with mock.patch.object(old, 'PanelClient') as panel:
            with self.assertRaisesRegex(old.ProvisionError, 'Access command fence is invalid'):
                old.update_entitlements(dict(request, desired_version=999), self.config, self.root)
            panel.assert_not_called()

    def test_lost_first_v2_panel_response_reconciles_same_identity_without_counter_reset(self):
        original = FakePanel.call
        def lost(panel, method, path, payload=None):
            result = original(panel, method, path, payload)
            if method == 'POST': raise runtime.ProvisionError('synthetic lost update response')
            return result
        with mock.patch.object(FakePanel, 'call', lost):
            # The existing reconciler independently reads back an applied update
            # after a lost response; it should complete, not manufacture UNKNOWN.
            first = runtime.update_entitlements(self.request, self.config, self.root)
        self.assertTrue(json.loads((self.root/'access_12345678.fence').read_text())['execution_complete'])
        updates = len(FakePanel.updates)
        result = runtime.update_entitlements(self.request, self.config, self.root)
        self.assertEqual(result, first)
        self.assertEqual(result['client_uuid'], self.identity['client_uuid'])
        self.assertEqual(len(FakePanel.clients), 1); self.assertEqual(len(FakePanel.updates), updates)
        client = FakePanel.clients[self.identity['panel_email']]
        self.assertEqual((client['up'], client['down'], client['totalGB']), (450, 50, 1000))
        self.assertEqual(FakePanel.adjustments, [])


if __name__ == '__main__': unittest.main(verbosity=2)
