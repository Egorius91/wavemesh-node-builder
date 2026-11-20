"""The merged periodic owner keeps full health and optional profile independent."""
import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0,str(Path(__file__).resolve().parent))
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'agent'))
import test_prepared_agent_observation as health_cases
import runtime_findings as findings


class PreparedProfileCoexistence(unittest.TestCase):
    def setUp(self):
        self.fixture=health_cases.PreparedAgentObservation()
        self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        f=self.fixture
        f.config.public_profiles_enabled=True
        f.config.env_path=f.directory/'agent.env'
        f.config.access_state_root=f.directory/'access'
        self.profile={'schema_version':1,'access_id':'access_fixture','placement_id':'placement_fixture',
            'node_version':f.config.observed_version,'profiles':['SYNTHETIC_PRIVATE_PROFILE']}
        self.profile_api=mock.Mock(return_value={'accepted':True,'profile_hash':'c'*64})
        f.instance.mtls_runtime=SimpleNamespace(api_json=self.profile_api)

    def run_once(self):
        with mock.patch.object(health_cases.agent.subprocess,'run',return_value=SimpleNamespace(returncode=0)), \
                mock.patch.object(findings,'read_json',return_value={'cursor':1,'profile':self.profile}):
            self.fixture.run_once()

    def assert_profile(self):
        f=self.fixture
        self.profile_api.assert_called_once_with('POST',
            f'internal/v1/nodes/{f.config.node_id}/accesses/access_fixture/placements/placement_fixture/public-profile',
            self.profile,expected=(200,))
        self.assertEqual(f.instance.last_profile_status,{'state':'CONFIGURATION_REPORTED'})
        heartbeat=f.instance.send_heartbeat.call_args.args[0]
        self.assertIs(heartbeat['capabilities']['prepared_eu_paths_v1'],False)
        self.assertNotIn('SYNTHETIC_PRIVATE_PROFILE',json.dumps(heartbeat))
        self.assertEqual(list(f.directory.glob('.public-profile.*')),[])

    def test_same_tick_keeps_full_negative_health_separate_from_private_profile(self):
        f=self.fixture;before=f.node_path.read_bytes();self.run_once()
        self.assertEqual(len(f.calls),1)
        state=f.calls[0][2]['state'];self.assertIn('routes',state);self.assertIn('auto_routes',state)
        self.assertEqual(f.row()['state'],'unqualified')
        self.assertNotIn('SYNTHETIC_PRIVATE_PROFILE',json.dumps(state))
        self.assertNotIn('pending_health_observation',f.persisted())
        self.assertEqual(f.node_path.read_bytes(),before);self.assert_profile()

    def test_lost_health_reply_retains_original_envelope_and_does_not_block_profile(self):
        f=self.fixture
        def lost(*args,**kwargs):
            f.accept(*args,**kwargs)
            raise health_cases.agent.ApiError(503,'PRIVATE_ERROR',True,'PRIVATE_EXCEPTION_CANARY')
        f.instance.api_json.side_effect=lost
        with self.assertLogs(health_cases.agent.LOG,'WARNING') as logs:self.run_once()
        self.assertEqual(len(f.calls),1)
        self.assertEqual(f.persisted()['pending_health_observation'],copy.deepcopy(f.calls[0][2]))
        self.assertNotIn('PRIVATE_EXCEPTION_CANARY',str(logs.output))
        self.assertNotIn('PRIVATE_ERROR',str(logs.output));self.assert_profile()


if __name__=='__main__':unittest.main(verbosity=2)
