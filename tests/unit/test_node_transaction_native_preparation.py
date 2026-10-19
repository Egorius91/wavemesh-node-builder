"""Portable native-fixture preparation checks; actual POSIX proof is NOT_RUN here."""
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'tests/unit'))
from test_ci_execution_policy import required_execution

spec=importlib.util.spec_from_file_location('native_status_fixture',ROOT/'tests/native/test_node_transaction_readonly.py')
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
STEP='''      - name: Native synthetic local Node transaction status proof
        timeout-minutes: 1
        run: sudo python3 -I -B tests/native/test_node_transaction_readonly.py --require-native
'''


def validate_workflow(text):
    required_execution(text)
    if text.count(STEP)!=1 or text.count('test_node_transaction_readonly.py --require-native')!=1:
        raise AssertionError('LOCAL_NATIVE_MUST_BE_EXACT_REQUIRED_SINGLE_STEP')
    if '    timeout-minutes: 15\n' not in text:
        raise AssertionError('EXISTING_JOB_BUDGET_CHANGED')


class PreparationTests(unittest.TestCase):
    def test_default_refuses_before_preconditions_source_or_creation(self):
        for args in ([],['--other'],['--require-native','--other']):
            with self.subTest(args=args),mock.patch.object(m,'Proof',side_effect=AssertionError), \
                 mock.patch.object(m,'check_sources',side_effect=AssertionError), \
                 contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(m.main(args),77)
            self.assertEqual(json.loads(out.getvalue()),{'status':'NATIVE_LOCAL_STATUS_INERT','createdTrees':0})

    def test_unsupported_host_does_not_create_tree(self):
        with mock.patch.object(m.sys,'platform','not-linux'),mock.patch.object(m,'Proof',side_effect=AssertionError), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(m.main(['--require-native']),1)
        result=json.loads(out.getvalue())
        self.assertEqual(result['code'],'NATIVE_PRECONDITIONS'); self.assertEqual(result['attempted'],0)
        self.assertTrue(result['zeroOwnedResidue'])

    def test_exact_production_source_pins(self):
        m.check_sources()
        for path,pin in m.SOURCE_PINS.items():
            self.assertEqual(hashlib.sha256((ROOT/path).read_bytes()).hexdigest(),pin)

    def test_unconditional_single_native_step_existing_budget_and_guards(self):
        text=(ROOT/'.github/workflows/tests.yml').read_text()
        validate_workflow(text)
        for changed in (text.replace(STEP,''),text.replace(STEP,STEP+STEP),
                        text.replace('    timeout-minutes: 15','    timeout-minutes: 16'),
                        text.replace(STEP,STEP.replace('timeout-minutes: 1','timeout-minutes: 4')),
                        text.replace(STEP,STEP.replace('        run:', '        if: false\n        run:')),
                        text.replace(STEP,STEP+'        continue-on-error: true\n')):
            with self.subTest(changed=changed==text),self.assertRaises(AssertionError):validate_workflow(changed)

    def test_production_cases_are_finite_real_calls_and_no_fixture_syscall_mock(self):
        self.assertEqual(len(m.CASES),8)
        self.assertEqual(set(m.EXPECTED_STOPS),{'unknown','missing','symlink','replaced_root','namespace'})
        source=(ROOT/'tests/native/test_node_transaction_readonly.py').read_text()
        for forbidden in ('unittest.mock','mock.patch','docker','systemctl','os.chown','os.chmod','os.setns'):
            self.assertNotIn(forbidden,source)
        self.assertEqual(source.count('subprocess.Popen('),1)
        self.assertIn('observer.observe(binding,',source)
        self.assertIn("dir='/root'",source)
        self.assertIn('shutil.rmtree(self.path.name,dir_fd=self.parent)',source)

    def test_exercise_must_observe_unchanged_snapshot_and_no_fd_leak(self):
        p=m.Proof(); p.path=Path('synthetic'); p.check_tree=lambda:None
        result={'status':'LOCAL_TRANSACTIONS_CLEAR_NOT_ADMISSION','counts':{'rootsObserved':1,
                'transactionsObserved':0,'committed':0,'rolledBack':0},'rootSetCompleteness':'NOT_OBSERVED',
                'operationalExclusion':'NOT_OBSERVED','liveAdmission':False,'filesystemWrites':0}
        with mock.patch.object(m,'snapshot',return_value={}),mock.patch.object(m,'fd_count',return_value=4), \
             mock.patch.object(m.observer,'observe',return_value=result) as observed:
            p.exercise('empty',{'target':'synthetic','operation':'a'*32})
        observed.assert_called_once(); self.assertEqual((p.attempted,p.completed),(1,1))
        for snapshots,fds,expected in (([{}, {'changed':1}],[4,4],'OBSERVER_CHANGED_FIXTURE'),
                                       ([{},{}],[4,5],'DESCRIPTOR_LEAK')):
            with mock.patch.object(m,'snapshot',side_effect=snapshots),mock.patch.object(m,'fd_count',side_effect=fds), \
                 mock.patch.object(m.observer,'observe',return_value=result),self.assertRaisesRegex(m.Failure,expected):
                p.exercise('empty',{'target':'synthetic','operation':'a'*32})

    def test_expected_refusal_is_not_a_synthetic_pass_or_unknown_acceptance(self):
        p=m.Proof(); p.path=Path('synthetic'); p.check_tree=lambda:None
        with mock.patch.object(m,'snapshot',return_value={}),mock.patch.object(m,'fd_count',return_value=4), \
             mock.patch.object(m.observer,'observe',side_effect=m.observer.Stop('STOP_TRANSACTION_STATUS_UNKNOWN')):
            p.exercise('unknown',{'target':'synthetic','operation':'a'*32})
        self.assertEqual((p.attempted,p.completed),(1,1))
        with mock.patch.object(m,'snapshot',return_value={}),mock.patch.object(m,'fd_count',return_value=4), \
             mock.patch.object(m.observer,'observe',side_effect=m.observer.Stop('STOP_OBSERVATION_IO')), \
             self.assertRaisesRegex(m.Failure,'EXPECTED_STOP_MISMATCH'):
            p.exercise('unknown',{'target':'synthetic','operation':'a'*32})

    def test_main_preserves_primary_and_sanitizes_even_when_cleanup_fails(self):
        for error,code in ((m.Failure('SOURCE_PIN'),'SOURCE_PIN'),(RuntimeError('PRIVATE_MARKER'),'NATIVE_LOCAL_STATUS_UNKNOWN')):
            p=SimpleNamespace(parent=None,case='unknown',attempted=4,completed=3,cli_calls=1,
                              run=mock.Mock(side_effect=error),cleanup=mock.Mock(return_value=False))
            with mock.patch.object(m.sys,'platform','linux'),mock.patch.object(m.os,'geteuid',return_value=0,create=True), \
                 mock.patch.object(m.os,'getegid',return_value=0,create=True), \
                 mock.patch.object(m.shutil.rmtree,'avoids_symlink_attacks',True), \
                 mock.patch.object(m,'check_sources'),mock.patch.object(m,'Proof',return_value=p), \
                 contextlib.redirect_stdout(io.StringIO()) as out,contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(m.main(['--require-native']),1)
            result=json.loads(out.getvalue())
            self.assertEqual(result['code'],code); self.assertFalse(result['cleanup'])
            self.assertEqual((result['attempted'],result['completed']),(4,3))
            self.assertNotIn('PRIVATE_MARKER',out.getvalue()+err.getvalue()); self.assertLess(len(out.getvalue()),1024)
            p.cleanup.assert_called_once()

    def test_uncertain_tree_creation_is_not_reported_as_clean(self):
        p=m.Proof(); p.creation_started=True
        self.assertFalse(p.cleanup())


if __name__=='__main__':
    unittest.main()
