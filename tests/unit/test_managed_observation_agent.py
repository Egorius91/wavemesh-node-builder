"""Exercise the existing Agent service/request seams without runtime/PKI work."""
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import Mock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'agent'))
import node_agent as module
import managed_observation


class ManagedObservationAgent(unittest.TestCase):
    def agent(self,observer=None):
        owner=object.__new__(module.NodeAgent)
        owner.config=SimpleNamespace(node_id='node-fixture',auth_mode='mtls')
        owner.managed_observer=observer
        owner.last_health_state={'node_status':'healthy'}
        owner.managed_observation_status='DISABLED'
        owner.mtls_runtime=Mock()
        owner.mtls_runtime.status.return_value.capability.return_value={'state':'SHADOW_ACTIVE','retry_attempts':0,'retry_at':None,'code':None}
        return owner

    def observer(self):
        observer=Mock()
        observer.status='ACTIVE'
        observer.begin.return_value={'started_at':'source-fixture-only'}
        return observer

    def test_default_send_keeps_existing_call_shape_and_one_dispatch(self):
        owner=self.agent();owner.api_json=Mock(return_value={})
        payload={'sent_at':'2026-11-21T14:00:00Z'}
        owner.send_heartbeat(payload)
        owner.api_json.assert_called_once_with('POST','internal/v1/nodes/node-fixture/heartbeat',payload,expected=(204,))

    def test_validated_receipt_is_buffered_then_recorded_after_actual_runtime_request(self):
        observer=self.observer();owner=self.agent(observer);order=[]
        def send(*args,**kwargs):
            order.append('request')
            observer.finish.assert_not_called()
            kwargs['heartbeat_receipt']({'kind':'validated transport fixture'})
            return {}
        owner.api_json=Mock(side_effect=send)
        observer.finish.side_effect=lambda *args,**kwargs:order.append('record')
        owner.send_heartbeat({'sent_at':'2026-11-21T14:00:00Z'})
        self.assertEqual(order,['request','record'])
        self.assertTrue(observer.finish.call_args.kwargs['transport_succeeded'])
        self.assertEqual(observer.finish.call_args.kwargs['receipt'],{'kind':'validated transport fixture'})
        owner.api_json.assert_called_once()

    def test_lost_reply_is_recorded_as_failed_without_replay(self):
        observer=self.observer();owner=self.agent(observer)
        failure=RuntimeError('synthetic lost response')
        owner.api_json=Mock(side_effect=failure)
        with self.assertRaises(RuntimeError) as raised:
            owner.send_heartbeat({'sent_at':'2026-11-21T14:00:00Z'})
        self.assertIs(raised.exception,failure)
        self.assertFalse(observer.finish.call_args.kwargs['transport_succeeded'])
        self.assertIsNone(observer.finish.call_args.kwargs['receipt'])
        owner.api_json.assert_called_once()

    def test_observer_begin_failure_cannot_prevent_the_ordinary_heartbeat(self):
        observer=self.observer();observer.begin.side_effect=OSError('synthetic probe failure')
        observer.block.side_effect=OSError('synthetic evidence write failure')
        owner=self.agent(observer);owner.api_json=Mock(return_value={})
        owner.send_heartbeat({'sent_at':'2026-11-21T14:00:00Z'})
        owner.api_json.assert_called_once()
        self.assertNotIn('heartbeat_receipt',owner.api_json.call_args.kwargs)
        observer.finish.assert_not_called()

    def test_finish_and_error_marker_failures_cannot_fail_or_retry_committed_request(self):
        observer=self.observer();observer.finish.side_effect=OSError('synthetic journal error')
        observer.block.side_effect=OSError('synthetic disk exhaustion')
        owner=self.agent(observer);owner.api_json=Mock(return_value={})
        owner.send_heartbeat({'sent_at':'2026-11-21T14:00:00Z'})
        owner.api_json.assert_called_once()
        self.assertEqual(owner.managed_observation_status,'BLOCKED_RETAINED')

    def test_constructor_is_inert_and_existing_run_lifecycle_owns_start_and_stop(self):
        config=SimpleNamespace(runtime_path=Path('/source-fixture/runtime.json'),
            managed_observation_manifest=Path('/source-fixture/manifest.json'))
        observer=self.observer()
        with patch.object(module,'read_json_file',return_value={}),patch.object(managed_observation,'ManagedObserver',return_value=observer) as constructor:
            owner=module.NodeAgent(config)
            constructor.assert_not_called()
            owner._run_loop=Mock()
            owner.run(once=True)
            constructor.assert_called_once_with(config.managed_observation_manifest,config)
            owner._run_loop.assert_called_once_with(True)
            observer.interrupt.assert_called_once()


if __name__=='__main__':
    unittest.main(verbosity=2)
