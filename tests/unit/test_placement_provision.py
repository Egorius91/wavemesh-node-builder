"""V2 provisioning/rotation use retained identity and the existing command fence."""
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import test_access_runtime as legacy

runtime, FakePanel = legacy.runtime, legacy.FakePanel


class PlacementProvisionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup); self.root=Path(self.tmp.name)
        FakePanel.clients={}; FakePanel.add_calls=0
        self.patch=mock.patch.object(runtime,'PanelClient',FakePanel); self.patch.start(); self.addCleanup(self.patch.stop)
        self.config={'server':{'domain':'entry.example.invalid'},
            'network':{'subscription':{'backend':'xui-native','path':'/opaque/subscription/'}}}
        self.first=dict(schema_version=2,access_id='access_12345678',placement_id='legacy:access_12345678',
            tenant_id='tenant001',node_id='node0001',command_id='command001',generation=1,entitlement_revision=4,
            plan_hash='a'*64,expected_version=0,desired_version=1,enabled=True,
            expires_at='2027-01-01T00:00:00Z',device_limit=0,quota_bytes='0',operation='access.provision')

    def replacement(self):
        return dict(self.first,desired_version=2,expected_version=1,command_id='command002',
            entitlement_revision=5,operation='access.replace_credential')

    def test_same_provision_and_rotation_resume_identity_then_cleanup_is_idempotent(self):
        first=runtime.provision(self.first,self.config,self.root)
        self.assertEqual(runtime.provision(self.first,self.config,self.root),first)
        second=runtime.provision(self.replacement(),self.config,self.root)
        self.assertNotEqual(first['client_uuid'],second['client_uuid'])
        self.assertEqual(runtime.provision(self.replacement(),self.config,self.root),second)
        self.assertEqual(FakePanel.add_calls,2)
        self.assertEqual(runtime.cleanup_previous(self.replacement(),self.config,self.root),1)
        self.assertEqual(runtime.cleanup_previous(self.replacement(),self.config,self.root),0)
        self.assertIn(second['panel_email'],FakePanel.clients)
        self.assertEqual(len(list(self.root.glob('*.json'))),2,'retained history survives physical cleanup')

    def test_provision_refuses_unknown_inventory_before_minting_credentials(self):
        FakePanel.clients['wm_access_12345678_999']={'client':{}}
        with mock.patch.object(runtime.uuid,'uuid4') as uuid, mock.patch.object(runtime.secrets,'token_urlsafe') as token:
            with self.assertRaisesRegex(runtime.ProvisionError,'Untracked'):
                runtime.provision(self.first,self.config,self.root)
            uuid.assert_not_called(); token.assert_not_called()
        self.assertEqual(FakePanel.add_calls,0); self.assertEqual(list(self.root.glob('*.json')),[])

    def test_new_provision_cannot_bypass_existing_identity_even_if_disabled(self):
        first=runtime.provision(self.first,self.config,self.root)
        FakePanel.clients[first['panel_email']]['client']['enable']=False
        with self.assertRaisesRegex(runtime.ProvisionError,'previous absence'):
            runtime.provision(dict(self.replacement(),operation='access.provision'),self.config,self.root)
        self.assertEqual(FakePanel.add_calls,1); self.assertFalse((self.root/'access_12345678.2.json').exists())

    def test_cleanup_rejects_changed_command_and_foreign_identity_before_any_delete(self):
        first=runtime.provision(self.first,self.config,self.root)
        second=runtime.provision(self.replacement(),self.config,self.root)
        with self.assertRaises(runtime.ProvisionError):
            runtime.cleanup_previous(dict(self.replacement(),command_id='command999'),self.config,self.root)
        self.assertEqual(len(FakePanel.clients),2)
        FakePanel.clients[first['panel_email']]['client']['subId']='foreign_subscription'
        with self.assertRaises(runtime.ProvisionError): runtime.cleanup_previous(self.replacement(),self.config,self.root)
        self.assertEqual(len(FakePanel.clients),2); self.assertIn(second['panel_email'],FakePanel.clients)

    def test_capabilities_probe_never_reads_config_takes_locks_or_creates_state(self):
        output=StringIO()
        with mock.patch.object(sys,'argv',
                ['access_runtime.py','--capabilities','--config',str(self.root/'absent.json')]), \
                mock.patch.object(runtime,'node_mutation_lock') as lock, \
                mock.patch.object(runtime.Path,'read_text') as read, redirect_stdout(output):
            self.assertEqual(runtime.main(),0); lock.assert_not_called(); read.assert_not_called()
        self.assertEqual(json.loads(output.getvalue()),{'access_command_schemas':[1,2],'placement_fence':2,'disable_absent':True})
        self.assertEqual(list(self.root.iterdir()),[])


if __name__ == '__main__': unittest.main()
