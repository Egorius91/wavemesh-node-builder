"""New prepared protocol fences Agent writes and repeats terminal barriers."""
from contextlib import contextmanager
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0,str(Path(__file__).resolve().parent))
import test_agent_prepared_journal as agent_cases
from test_prepared_journal import reader,journal
runtime=agent_cases.runtime


def no_effect_fixture(root):
    directory=root/'20261120T103000Z-123abc-prepared-v1'
    directory.mkdir(parents=True,mode=0o700)
    candidates={'config':{'node':{'role':'entry'}},'runtime':{},'template':{'routing':{'rules':[]}}}
    preimages={'config':b'{"node":{"role":"entry"}}','template':b'{"routing":{"rules":[]}}'}
    for key,name in [('config','config.before.json'),('template','prepared.template.before.json')]:
        journal.atomic_bytes(directory/name,preimages[key])
    for key,name in reader.ARTIFACTS.items():journal.atomic_json(directory/name,candidates[key])
    intent={'schema_version':1,'transaction_id':directory.name,'route_id':'route-unit','binding':'b'*64,
        'expected':{'generation':'00000000-0000-4000-8000-000000000002','epoch':'0',
            'engine_instance':{'boot_id':'00000000-0000-4000-8000-000000000001','pid':123,'start_ticks':'100'},
            'before_sha256':'a'*64,'after_sha256':'c'*64,'normalized_sha256':'d'*64,
            'rule_tag':'wm-rule-unit','inbound_tag':'wm-route-unit','target':'wm-exit-path-unit'},
        'files':{key:str((root.parent/key).absolute()) for key in ('config','runtime','database','process')},
        'before':{'config':reader.sha(preimages['config']),'runtime':None,'owned':None,'template':reader.sha(preimages['template']),'process':None},
        'candidate':{key:reader.sha((directory/name).read_bytes()) for key,name in reader.ARTIFACTS.items()}}
    journal.atomic_json(directory/'plan.json',{'schema_version':1,'operation':'auto-prepared-reconcile','pid':123,
        'prepared_protocol':reader.PROTOCOL,'prepared_phase':'pre-effect','prepared_pre_effect':intent})
    journal.atomic_json(directory/reader.ORIGIN,{'protocol':reader.PROTOCOL,'transaction_id':directory.name,'status':'NO_CONFIG_MUTATION_DISPATCHED'})
    journal.atomic_json(directory/'result.json',{'status':'rolled_back','finished_at':'2026-11-20T10:30:00Z',
        'prepared_no_effect':{'origin_sha256':reader.sha((directory/reader.ORIGIN).read_bytes()),'intent_sha256':reader.digest(intent)}})
    return directory


class AgentPreparedOrigin(unittest.TestCase):
    invoke=agent_cases.AgentPreparedJournalTests.invoke
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(prefix='wm-agent-origin-');self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name);self.transactions=self.root/'transactions'
    def assert_debt(self):
        with self.assertRaises(runtime.ProvisionError):runtime.assert_no_pending_node_transaction(self.transactions)
    def test_reserved_empty_or_forged_ordinary_terminal_cannot_admit_agent(self):
        directory=self.transactions/'20261120T103000Z-123abc-prepared-v1';directory.mkdir(parents=True)
        self.assert_debt()
        journal.atomic_json(directory/'plan.json',{'schema_version':1})
        journal.atomic_json(directory/'result.json',{'status':'committed'})
        self.assert_debt()
    def test_verified_no_effect_is_read_only_barrier_work_without_current_runtime_claim(self):
        directory=no_effect_fixture(self.transactions);before={p.name:p.read_bytes() for p in directory.iterdir()}
        with mock.patch.object(os,'fsync',side_effect=AssertionError('read-only poll wrote')):
            self.assertEqual(runtime.assert_no_pending_node_transaction(self.transactions),[directory])
        self.assertEqual(before,{p.name:p.read_bytes() for p in directory.iterdir()})
        self.assertFalse((self.root/'runtime').exists())
    def test_lost_corrupt_or_changed_positive_proof_never_admits(self):
        directory=no_effect_fixture(self.transactions);saved={p.name:p.read_bytes() for p in directory.iterdir()}
        for name,raw in [(reader.ORIGIN,None),(reader.ORIGIN,b'broken'),('config.before.json',b'changed')]:
            with self.subTest(name=name,raw=raw):
                for key,value in saved.items():(directory/key).write_bytes(value)
                if raw is None:(directory/name).unlink()
                else:(directory/name).write_bytes(raw)
                self.assert_debt()
    def test_effect_possible_or_any_retained_dispatch_fact_overrides_rolled_back(self):
        directory=no_effect_fixture(self.transactions)
        plan=reader.decode((directory/'plan.json').read_bytes());changed=copy.deepcopy(plan);changed['prepared_phase']='effect-possible'
        journal.atomic_json(directory/'plan.json',changed);self.assert_debt();journal.atomic_json(directory/'plan.json',plan)
        for name in ['prepared.apply.pending.json','prepared.apply.completed.json','prepared.owner-request.json','prepared.owner-result.json']:
            with self.subTest(name=name):
                (directory/name).write_bytes(b'broken');self.assert_debt();(directory/name).unlink()
    def test_no_effect_writer_barrier_is_inside_node_lock_before_effects(self):
        directory=no_effect_fixture(self.transactions);events=[];held=False
        @contextmanager
        def lock():
            nonlocal held
            held=True
            try:yield
            finally:held=False
        def barrier(path):
            self.assertTrue(held);self.assertEqual(path,directory);events.append('barrier')
        def effect(*args):
            self.assertTrue(held);self.assertEqual(events,['barrier']);events.append('effect');return {}
        with mock.patch.object(runtime,'node_mutation_lock',lock),mock.patch.object(runtime,'sync_directory',side_effect=barrier),mock.patch.object(runtime,'provision',side_effect=effect),mock.patch.object(runtime,'atomic_json'):
            self.assertEqual(self.invoke()[0],0)
        self.assertEqual(events,['barrier','effect'])
        with mock.patch.object(runtime,'node_mutation_lock',lock),mock.patch.object(runtime,'sync_directory',side_effect=OSError('barrier failed')),mock.patch.object(runtime,'provision') as provision:
            code,log,output=self.invoke();self.assertEqual(code,1);self.assertEqual(log,'access_runtime=FAIL code=OSERROR\n');self.assertFalse(output.exists());provision.assert_not_called()


if __name__=='__main__':unittest.main(verbosity=2)
