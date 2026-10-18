"""Portable orchestration/refusal tests; no native ownership/service/lock action."""
import copy
import importlib.util
import pathlib
import sys
import unittest
from unittest import mock
import contextlib
import stat
from types import SimpleNamespace

PATH = pathlib.Path(__file__).parents[2] / 'operations' / 'xui_owner_repair.py'
spec = importlib.util.spec_from_file_location('owner_repair', PATH)
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)


def manifest():
    return {'schema': 1, 'target': m.TARGETS[0], 'operation': 'a' * 32,
            'objects': [dict(path=p, kind='directory' if p in m.DIRECTORIES else 'regular',
                             dev=7, ino=i+100, mode=0o755, size=16,
                             mtime_ns=22, flags=0, nlink=2 if p in m.DIRECTORIES else 1,
                             sha256=None if p in m.DIRECTORIES else 'b'*64)
                        for i, p in enumerate(m.MEMBERS)]}


class MemoryJournal:
    def __init__(self): self.records = []; self.binding=None
    def append(self, event, **fields): self.records.append(dict(event=event, **fields))
    def events(self, event): return [r for r in self.records if r['event'] == event]


class Objects:
    def __init__(self, document):
        self.pins = {p['path']: copy.deepcopy(p) for p in document['objects']}
        self.owners = {p: (1001, 1001) for p in self.pins}
        self.writes = []
        self.fail_after_change = None
        self.drift = None
    def check(self, pin, *, root_allowed=False):
        if self.drift == pin['path']: raise m.Stop('OBJECT_DRIFT')
        if self.owners[pin['path']] not in (((1001, 1001), (0, 0)) if root_allowed else ((1001, 1001),)):
            raise m.Stop('UNEXPECTED_OWNER')
        return self.owners[pin['path']]
    def chown(self, pin):
        path=pin['path']; self.writes.append(path); self.owners[path]=(0,0)
        if path == self.fail_after_change: raise OSError('synthetic uncertain outcome')
    def close(self): pass


class Admission:
    """Explicit mock, never a production admission adapter."""
    def __init__(self): self.calls=0; self.fail_at=None
    def assert_closed(self):
        self.calls+=1
        if self.calls == self.fail_at: raise m.Stop('ADMISSION_CHANGED')


class OwnerRepairTests(unittest.TestCase):
    def setUp(self):
        self.doc=manifest(); self.objects=Objects(self.doc)
        self.ledger=MemoryJournal(); self.admission=Admission()

    def apply(self):
        return m.apply_owner_only(self.doc, self.objects, self.ledger,
                                  self.admission.assert_closed)

    def test_default_is_inert_even_with_execute_until_live_adapter_exists(self):
        self.assertEqual(m.main([]), 77)
        self.assertEqual(m.main(['--execute']), 77)

    def test_unbound_admission_precedes_every_native_runtime_probe(self):
        with mock.patch.object(m,'check_operator_source') as source,mock.patch.object(m,'namespace_identity') as namespace:
            with self.assertRaises(m.Stop):m.NativeRuntime(m.UnboundAdmission(),{}, {},'c'*64)
            source.assert_not_called();namespace.assert_not_called()

    def test_fake_quiescence_arguments_cannot_enable_native_entrypoint(self):
        with mock.patch.object(m.os,'open') as opened,mock.patch.object(m.subprocess,'run') as command:
            self.assertEqual(m.main(['--execute','--quiescent','true','--receipt','fabricated.json']),77)
            opened.assert_not_called();command.assert_not_called()

    def test_finite_scope_refusals(self):
        for change in ('missing','extra','duplicate','target','traversal','hash','mode','flags'):
            d=manifest()
            if change=='missing': d['objects'].pop()
            if change=='extra': d['objects'].append(copy.deepcopy(d['objects'][0]))
            if change=='duplicate': d['objects'][2]['path']=d['objects'][3]['path']
            if change=='target': d['target']='another-host'
            if change=='traversal': d['objects'][2]['path']+='/../x'
            if change=='hash': d['objects'][2]['sha256']='x'*64
            if change=='mode': d['objects'][2]['mode']=0o4777
            if change=='flags': d['objects'][2]['flags']=0x10
            with self.subTest(change=change), self.assertRaises(m.Stop): m.validate_manifest(d)

    def test_all_owner_changes_are_forward_ordered_and_journaled_before_effect(self):
        self.apply()
        self.assertEqual(self.objects.writes, list(m.MEMBERS))
        self.assertEqual(len(self.ledger.events('INTENT')),17)
        self.assertEqual(len(self.ledger.events('APPLIED')),17)
        self.assertEqual(self.ledger.records[0]['event'],'INTENT')

    def test_admission_failure_before_effect_is_inert(self):
        self.admission.fail_at=1
        with self.assertRaises(m.Stop): self.apply()
        self.assertEqual(self.objects.writes,[])

    def test_admission_loss_stops_dependent_changes(self):
        self.admission.fail_at=4
        with self.assertRaises(m.Stop): self.apply()
        self.assertEqual(len(self.objects.writes),1)

    def test_unknown_fchown_is_not_retried_and_root_subset_is_retained(self):
        self.objects.fail_after_change=m.MEMBERS[1]
        with self.assertRaises(m.Stop) as result: self.apply()
        self.assertEqual(result.exception.code,'OWNER_OUTCOME_UNKNOWN')
        self.assertEqual(self.objects.owners[m.MEMBERS[1]],(0,0))
        before=list(self.objects.writes)
        self.assertEqual(len(before),2)
        self.objects.fail_after_change=None
        # Explicit reconciliation records actual same-object state before continuation.
        m.reconcile_objects(self.doc,self.objects,self.ledger,self.admission.assert_closed)
        self.apply()
        self.assertEqual(self.objects.writes.count(m.MEMBERS[1]),1)
        self.assertEqual(len(self.objects.writes),17)

    def test_root_without_intent_is_not_adopted(self):
        self.objects.owners[m.MEMBERS[0]]=(0,0)
        with self.assertRaises(m.Stop): self.apply()
        self.assertEqual(self.objects.writes,[])

    def test_drift_refuses_without_chown(self):
        self.objects.drift=m.MEMBERS[0]
        with self.assertRaises(m.Stop): self.apply()
        self.assertEqual(self.objects.writes,[])

    def test_pending_intent_requires_explicit_reconciliation(self):
        self.ledger.append('INTENT',path=m.MEMBERS[0],pin=self.doc['objects'][0])
        with self.assertRaises(m.Stop): self.apply()
        self.assertEqual(self.objects.writes,[])

    def test_verifier_rejects_partial_owner_set(self):
        with self.assertRaises(m.Stop): m.verify_objects(self.doc,self.objects,self.admission.assert_closed)
        self.apply()
        self.assertEqual(m.verify_objects(self.doc,self.objects,self.admission.assert_closed),17)

    def test_ledger_chain_detects_corruption_and_truncation(self):
        first=m.encode_record(0,'0'*64,{'event':'BEGIN'})
        second=m.encode_record(1,m.digest(first),{'event':'INTENT'})
        self.assertEqual(len(m.decode_records(first+second)),2)
        for damaged in (first+second[:-1], first+second.replace(b'INTENT',b'INTENX')):
            with self.assertRaises(m.Stop):m.decode_records(damaged)

    def test_unit_parser_rejects_missing_duplicate_and_unknown_properties(self):
        text='\n'.join(k+'=0' for k in m.UNIT_PROPS)
        self.assertEqual(len(m.parse_unit(text)),len(m.UNIT_PROPS))
        for bad in (text+'\nJob=0', text+'\nEnvironment=secret', text.replace('Job=0','')):
            with self.assertRaises(m.Stop):m.parse_unit(bad)

    def test_known_sticky_lock_parent_exception_is_narrow(self):
        def info(mode,uid=0):return SimpleNamespace(st_mode=stat.S_IFDIR|mode,st_uid=uid,st_gid=0)
        m.safe_lock_parent(info(0o1777));m.safe_lock_parent(info(0o755))
        for value in (info(0o777),info(0o1777,1001)):
            with self.assertRaises(m.Stop):m.safe_lock_parent(value)
        with self.assertRaises(m.Stop):m.safe_root_directory(info(0o1777))

    def test_quiescence_is_rechecked_for_skipped_root_and_each_verifier_member(self):
        self.apply()
        before=self.admission.calls
        self.apply()
        self.assertGreaterEqual(self.admission.calls-before,18)
        before=self.admission.calls
        m.verify_objects(self.doc,self.objects,self.admission.assert_closed)
        self.assertGreaterEqual(self.admission.calls-before,19)

    def test_unknown_stop_request_is_durable_and_cannot_be_repeated(self):
        runtime=m.NativeRuntime.__new__(m.NativeRuntime)
        runtime.assert_admission=lambda:None
        runtime.command_pins={'/usr/bin/busctl':'b'*64}
        with mock.patch.object(m,'checked_public_command',side_effect=TimeoutError) as command:
            with self.assertRaises(m.Stop) as outcome:runtime.request('StopUnit',m.UNITS[0],self.ledger)
            self.assertEqual(outcome.exception.code,'SERVICE_OUTCOME_UNKNOWN_RECONCILE')
            with self.assertRaises(m.Stop):runtime.request('StopUnit',m.UNITS[0],self.ledger)
            self.assertEqual(command.call_count,1)
        self.assertEqual(self.ledger.records[0]['event'],'SERVICE_INTENT')


def values(name, active=False):
    v={k:'' for k in m.UNIT_PROPS}
    v.update(Id=name,LoadState='loaded',NeedDaemonReload='no',KillMode='control-group',
             SendSIGKILL='yes',FileDescriptorStoreMax='0',NFileDescriptorStore='0',
             FileDescriptorStorePreserve='restart',RefuseManualStart='no',RefuseManualStop='no',
             ActiveState='active' if active else 'inactive',SubState='running' if active else 'dead',
             MainPID='0',ControlPID='0',Job='0',ControlGroup='/system.slice/'+name)
    return v


class WindowAdmission(Admission):
    def __init__(self):super().__init__();self.panel_accepted=False;self.agent_restore=False
    def assert_restore_permitted(self):self.assert_closed()
    def assert_panel_restored(self):self.panel_accepted=True
    def assert_agent_restore_permitted(self):
        if not self.panel_accepted:raise m.Stop('PANEL_NOT_ACCEPTED')
        self.agent_restore=True
    def assert_restored_and_cleanup_accepted(self):self.assert_closed()


class Runtime:
    """Portable mock runtime only; no systemctl/busctl/cgroup/proc use."""
    def __init__(self):
        self.admission=WindowAdmission();self.namespace={'fixture':True};self.old_processes={}
        self.states={u:True for u in m.UNITS};self.requests=[];self.unknown_stop=False;self.failed_start=False
    def assert_admission(self):self.admission.assert_closed()
    def operation_binding(self,document):return {'fixtureBinding':m.digest(m.canonical(document))}
    def capture_baseline(self):return {u:{'active':v,'group':'/system.slice/'+u} for u,v in self.states.items()}
    def unit(self,name):return values(name,self.states[name])
    def members(self,group):return set()
    def request(self,action,name,journal,**kw):
        key=action+':'+name
        m.require(not any(r.get('request')==key for r in journal.records),'NO_REPLAY')
        journal.append('SERVICE_INTENT',request=key)
        self.requests.append((action,name))
        if action=='StopUnit':
            self.states[name]=False
            if self.unknown_stop:raise m.Stop('SERVICE_OUTCOME_UNKNOWN_RECONCILE')
        elif not self.failed_start:self.states[name]=True
        return '/org/freedesktop/systemd1/job/12'
    def observe_job(self,name,job,*,active,deadline):
        m.require(self.states[name]==active,'SERVICE_TERMINAL_MISMATCH')
    def assert_quiescent(self):
        self.assert_admission();m.require(not any(self.states.values()),'UNIT_NOT_QUIESCENT')


class WindowTests(unittest.TestCase):
    def setUp(self):
        self.doc=manifest();self.runtime=Runtime();self.ledger=MemoryJournal();self.fs=Objects(self.doc)
        self.ledger.binding=self.runtime.operation_binding(self.doc)
        self.lock_checks=0
        @contextlib.contextmanager
        def lock():
            def check():self.lock_checks+=1
            yield check
        self.patch=mock.patch.object(m,'existing_mutation_lock',lock);self.patch.start()
    def tearDown(self):self.patch.stop()
    def run_window(self):return m.run_window(self.doc,self.runtime,lambda d:self.fs,self.ledger,deadline=100)
    def restore(self):return m.restore_window(self.doc,self.runtime,lambda d:self.fs,self.ledger,deadline=100)
    def test_full_window_orders_panel_before_agent_and_leaves_gates_unchanged(self):
        self.assertIn('COMMAND_RESUME_NOT_PERFORMED',self.run_window())
        self.assertEqual(self.runtime.requests,[('StopUnit',m.UNITS[0]),('StopUnit',m.UNITS[1]),
                                               ('StartUnit',m.UNITS[1]),('StartUnit',m.UNITS[0])])
        self.assertTrue(self.runtime.admission.agent_restore)
        self.assertGreater(self.lock_checks,50)
    def test_stop_accepted_transport_unknown_has_executable_reconcile_restore(self):
        self.runtime.unknown_stop=True
        with self.assertRaises(m.Stop):self.run_window()
        self.assertEqual(len(self.fs.writes),0)
        observed=m.reconcile_window(self.runtime,self.ledger)
        self.assertEqual(observed[m.UNITS[0]],'inactive')
        self.runtime.unknown_stop=False
        self.assertEqual(self.restore(),
                         'PARTIAL_RESTORED_RECONCILIATION_REQUIRED')
        self.assertEqual(self.runtime.requests.count(('StopUnit',m.UNITS[0])),1)
    def test_unknown_owner_requires_reconcile_then_resume_without_stop_replay(self):
        self.fs.fail_after_change=m.MEMBERS[1]
        with self.assertRaises(m.Stop):self.run_window()
        self.fs.drift=m.MEMBERS[1]
        with self.assertRaises(m.Stop):self.restore()
        self.assertEqual(len(self.runtime.requests),2)
        self.fs.drift=None
        m.reconcile_objects(self.doc,self.fs,self.ledger,self.runtime.assert_quiescent)
        self.fs.fail_after_change=None
        m.resume_owner_interval(self.doc,self.runtime,lambda d:self.fs,self.ledger)
        self.restore()
        self.assertEqual(len(self.fs.writes),17)
        self.assertEqual(len([r for r in self.runtime.requests if r[0]=='StopUnit']),2)
    def test_failed_start_is_not_automatically_repeated(self):
        self.runtime.failed_start=True
        with self.assertRaises(m.Stop):self.run_window()
        m.reconcile_window(self.runtime,self.ledger)
        with self.assertRaises(m.Stop):self.restore()
        self.assertEqual(self.runtime.requests.count(('StartUnit',m.UNITS[1])),1)
    def test_baseline_inactive_unit_is_never_started(self):
        self.runtime.states[m.UNITS[0]]=False
        self.run_window()
        self.assertFalse(any(name==m.UNITS[0] for _,name in self.runtime.requests))

    def test_wrong_manifest_ledger_binding_stops_before_service_request(self):
        self.ledger.binding={'fixtureBinding':'wrong'}
        with self.assertRaises(m.Stop):self.run_window()
        self.assertEqual(self.runtime.requests,[])

    def test_restore_revalidates_objects_after_old_full_acceptance(self):
        self.runtime.failed_start=True
        with self.assertRaises(m.Stop):self.run_window()
        self.assertTrue(self.ledger.events('FILESYSTEM_ACCEPTED'))
        self.fs.drift=m.MEMBERS[2]
        with self.assertRaises(m.Stop):self.restore()
        self.assertEqual(self.runtime.requests.count(('StartUnit',m.UNITS[1])),1)


class NativePolicyMockTests(unittest.TestCase):
    def runtime(self,v):
        r=m.NativeRuntime.__new__(m.NativeRuntime);r.command_pins={'/usr/bin/systemctl':'b'*64}
        r.activation_pins={m.UNITS[0]:{k:v[k] for k in m.ACTIVATION_PROPS}}
        r.assert_admission=lambda:None;r.old_processes={};r.members=lambda group:set()
        return r
    def test_controlpid_and_fdstore_and_activation_drift_stop(self):
        base=values(m.UNITS[0]);r=self.runtime(base)
        for key,value in (('ControlPID','42'),('NFileDescriptorStore','1'),('PartOf','other.service')):
            v={**base,key:value}
            with self.subTest(key=key),mock.patch.object(m,'checked_public_command',return_value='\n'.join(k+'='+v[k] for k in m.UNIT_PROPS)):
                if key=='ControlPID':
                    with mock.patch.object(r,'unit',return_value=v),self.assertRaises(m.Stop):r.assert_quiescent()
                else:
                    with self.assertRaises(m.Stop):r.unit(m.UNITS[0])

    def test_corrective_start_needs_newer_live_reconciliation_and_single_reserve(self):
        r=self.runtime(values(m.UNITS[0]));r.command_pins['/usr/bin/busctl']='b'*64
        r.admission=SimpleNamespace(assert_corrective_start_permitted=lambda name:None)
        ledger=MemoryJournal();name=m.UNITS[0]
        ledger.append('SERVICE_RECONCILED',unit=name,state='inactive')
        ledger.append('SERVICE_INTENT',request='StartUnit:'+name)
        with mock.patch.object(m,'checked_public_command',return_value='o "/org/freedesktop/systemd1/job/7"') as command, \
                mock.patch.object(r,'unit',return_value=values(name)):
            with self.assertRaises(m.Stop):r.request('StartUnit',name,ledger,reserve=True)
            self.assertEqual(command.call_count,0)
            ledger.append('SERVICE_RECONCILED',unit=name,state='inactive')
            with mock.patch.object(r,'unit',return_value=values(name)):
                r.request('StartUnit',name,ledger,reserve=True)
                with self.assertRaises(m.Stop):r.request('StartUnit',name,ledger,reserve=True)
            self.assertEqual(command.call_count,1)


if __name__=='__main__': unittest.main()
