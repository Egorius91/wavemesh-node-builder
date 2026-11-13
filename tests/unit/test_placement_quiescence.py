import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import test_placement_fence as fence_tests

runtime = fence_tests.runtime


def status(**changes):
    values=dict(Id='x-ui.service',LoadState='loaded',Type='simple',ActiveState='active',SubState='running',MainPID='123',
        ControlPID='0',InvocationID='a'*32,KillMode='control-group',SendSIGKILL='yes',NeedDaemonReload='no',Job='0',
        ControlGroup='/system.slice/x-ui.service',Triggers='',TriggeredBy='',OnFailure='',OnSuccess='',
        FileDescriptorStoreMax='0',NFileDescriptorStore='0')
    values.update(changes)
    return SimpleNamespace(returncode=0,stdout=''.join(k+'='+v+'\n' for k,v in values.items()))


class PlacementQuiescenceTests(unittest.TestCase):
    def test_fixed_service_restart_must_complete_with_a_fresh_valid_invocation(self):
        for empty in ('0',''):
            with self.subTest(job=empty), mock.patch.object(runtime.os,'name','posix'), mock.patch.object(runtime.subprocess,'run',side_effect=[
                    status(Job=empty),SimpleNamespace(returncode=0,stdout=''),status(Job=empty,InvocationID='b'*32,MainPID='456')]) as run, \
                    mock.patch.object(runtime,'PanelClient',return_value=SimpleNamespace(call=mock.Mock(return_value={'success':True,'obj':[]}))):
                runtime.quiesce_placement_panel({})
            self.assertEqual(run.call_count,3)
            self.assertEqual(run.call_args_list[1].args[0],['/usr/bin/systemctl','restart','--job-mode=fail','x-ui.service'])
            self.assertEqual(run.call_args_list[1].kwargs['timeout'],45)
            self.assertEqual(run.call_args_list[1].kwargs['env'],{'PATH':'/usr/bin:/bin','LC_ALL':'C'})

    def test_unsupported_unit_pending_jobs_and_stored_descriptors_stop_before_restart(self):
        for damage in [dict(Id='other.service'),dict(KillMode='process'),dict(SendSIGKILL='no'),dict(NeedDaemonReload='yes'),
            dict(Job='123'),dict(ControlPID='2'),dict(TriggeredBy='x-ui.socket'),dict(OnFailure='other.service'),
            dict(ControlGroup='/foreign'),dict(FileDescriptorStoreMax='1'),dict(NFileDescriptorStore='1')]:
            with self.subTest(damage=damage), mock.patch.object(runtime.os,'name','posix'), \
                    mock.patch.object(runtime.subprocess,'run',return_value=status(**damage)) as run:
                with self.assertRaises(runtime.ProvisionError): runtime.quiesce_placement_panel({})
                self.assertEqual(run.call_count,1)

    def test_failed_timed_out_or_unverified_restart_cannot_claim_quiescence(self):
        for effects in [[status(),SimpleNamespace(returncode=1,stdout='')],
                [status(),subprocess.TimeoutExpired('systemctl',45)],
                [status(),SimpleNamespace(returncode=0,stdout=''),status()],
                [status(),SimpleNamespace(returncode=0,stdout=''),status(InvocationID='b'*32,ActiveState='activating')]]:
            with self.subTest(effects=effects), mock.patch.object(runtime.os,'name','posix'), \
                    mock.patch.object(runtime.subprocess,'run',side_effect=effects):
                with self.assertRaises(runtime.ProvisionError): runtime.quiesce_placement_panel({})

    def test_uncertain_effect_is_durable_and_no_new_effect_precedes_verified_barrier(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); effects=[]
            request=dict(schema_version=2,access_id='access001',placement_id='legacy:access001',tenant_id='tenant001',
                node_id='node0001',command_id='command001',generation=1,entitlement_revision=1,plan_hash='a'*64,
                expected_version=0,desired_version=1,enabled=True,expires_at='2027-01-01T00:00:00Z',device_limit=0,quota_bytes='0')
            @runtime.fenced_access('access.update_entitlements')
            def apply(value,config,root):
                effects.append(value['enabled'])
                if config.get('fail'): raise RuntimeError('lost after effect')
                return {'desired_version':value['desired_version']}
            with self.assertRaises(RuntimeError): apply(request,{'fail':True},root)
            path=root/'access001.fence'
            self.assertIs(json.loads(path.read_text())['execution_complete'],False)
            deny=dict(request,desired_version=2,command_id='command002',enabled=False,entitlement_revision=2)
            with mock.patch.object(runtime,'quiesce_placement_panel',side_effect=runtime.ProvisionError('UNKNOWN')):
                with self.assertRaises(runtime.ProvisionError): apply(deny,{},root)
            self.assertEqual(effects,[True]); self.assertIs(json.loads(path.read_text())['execution_complete'],False)
            with mock.patch.object(runtime,'quiesce_placement_panel') as barrier:
                apply(deny,{},root); self.assertEqual(barrier.call_count,1)
                apply(deny,{},root); self.assertEqual(barrier.call_count,1,'terminal local retry does not restart the panel')
            self.assertIs(json.loads(path.read_text())['execution_complete'],True)
            self.assertEqual(effects,[True,False,False])


if __name__ == '__main__': unittest.main()
