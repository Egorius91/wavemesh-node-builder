"""Portable proof of native fixture preparation/lifecycle, no Docker or DB."""
import contextlib
import importlib.util
import io
import json
import pathlib
import stat
import subprocess
import types
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).parents[2]
spec = importlib.util.spec_from_file_location('central_pg_preparation',ROOT/'tests/native/test_xui_owner_central_postgres.py')
n = importlib.util.module_from_spec(spec); spec.loader.exec_module(n)


def state():
    return {'schema':1,'token':'a'*32,'name':n.PREFIX+'a'*32,'imageId':'sha256:'+'b'*64,
            'containerId':'c'*64,'phase':'CREATED'}


def result(stdout=b'',rc=0,stderr=b''):
    return types.SimpleNamespace(stdout=stdout,stderr=stderr,returncode=rc)


class NativePreparation(unittest.TestCase):
    def test_default77_before_collector_file_or_native_action(self):
        with mock.patch.object(n,'load_collector',side_effect=AssertionError), \
             mock.patch.object(n,'prepare',side_effect=AssertionError), \
             mock.patch.object(n,'finalize',side_effect=AssertionError), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(n.main([]),77)
        self.assertEqual(json.loads(output.getvalue()),{'status':'NATIVE_SQL_NOT_RUN','effects':0})

    def test_fixture_has_exact_used_source_shape_and_synthetic_metadata_only(self):
        m = n.load_collector(); schema = m.load_schema(); sql = n.fixture_sql(schema).decode()
        self.assertEqual(sql.count('CREATE TYPE public.'),6)
        self.assertEqual(sql.count('CREATE TABLE public.'),10)
        self.assertEqual(len(schema['columns']),31)
        self.assertEqual(sql.count(',now(),NULL,1)'),57)
        self.assertNotIn('CREATE TRIGGER',sql)
        self.assertNotIn('lock_node_runtime_binding',sql)
        self.assertNotIn('Tenant',sql)
        self.assertIn('timestamp(3) without time zone',sql)
        self.assertIn('NOT NULL',sql)

    def test_opaque_ids_really_have_quotes_backslashes_utf8_and_do_not_enter_sql_grammar(self):
        m = n.load_collector(); binding = n.fixture_binding(m)
        m.validate_binding(binding,m.TARGETS[0],'a'*32)
        node = binding['targets'][m.TARGETS[0]]['nodeId']
        self.assertIn("'",node); self.assertIn('\\',node); self.assertTrue(any(ord(c)>127 for c in node))
        sql = m.build_sql(binding,m.TARGETS[0],m.load_schema())
        self.assertNotIn(node.encode(),sql)
        self.assertIn(node.encode().hex().encode(),sql)

    def test_matrix_covers_every_debt_field_and_preserves_terminal_uncertainty(self):
        m = n.load_collector(); cases = n.debt_cases(n.fixture_binding(m),m.TARGETS[0])
        self.assertEqual(len(cases),35)
        self.assertEqual(len({name for name,_,_ in cases}),35)
        self.assertTrue({name for name,_,_ in cases} <= n.CASE_ALIASES)
        covered = set().union(*(expected for _,_,expected in cases))
        self.assertEqual(covered,set(m.COUNTS)-{'exact_target_count'})
        table = {name:expected for name,_,expected in cases}
        for status in ('PERMANENT_FAILED','ROLLED_BACK','ROLLBACK_FAILED','CANCELLED'):
            self.assertEqual(table['attempted_'+status],{'dispatched_unresolved':1})
        self.assertEqual(table['succeeded_attempted'],{})
        self.assertEqual(table['attempt_SUCCEEDED_null'],{'unfinished_attempts':1})
        self.assertEqual(table['delivery_expired'],{})
        self.assertEqual(table['held_fence'],{'held_binding_fences':1})

    def test_fixture_enum_assignment_uses_explicit_cast_from_hex_text(self):
        sql = n.insert('NodeCommand',status='SUCCEEDED')
        self.assertIn('::public."NodeCommandStatus"',sql)
        self.assertNotIn("'SUCCEEDED'",sql)
        with self.assertRaisesRegex(n.Failure,'FIXTURE_IDENTIFIER'):
            n.insert('not-an-identifier',status='SUCCEEDED')

    def test_owned_container_requires_name_label_id_and_image_equality(self):
        value = state(); cid = value['containerId']
        valid = (cid+' '+value['imageId']+' '+value['token']+' /'+value['name']+'\n').encode()
        run = mock.Mock(side_effect=[result((cid+'\n').encode()),result((cid+'\n').encode()),result(valid)])
        self.assertEqual(n.owned_container(value,run=run),cid)
        for outputs in ([result((cid+'\n').encode()),result(b'')],
                        [result((cid+'\n').encode()),result((cid+'\n').encode()),result(valid.replace(b'sha256:',b'bad:'))],
                        [result(('d'*64+'\n').encode()),result(('d'*64+'\n').encode())]):
            with self.subTest(outputs=len(outputs)),self.assertRaises(n.Failure):
                n.owned_container(value,run=mock.Mock(side_effect=outputs))

    def test_finalizer_removes_only_fresh_exact_owned_id_then_independently_observes_absence(self):
        path = mock.Mock(); path.exists.return_value=True
        run = mock.Mock(return_value=result())
        with mock.patch.object(n,'private_state',return_value=state()) as store, \
             mock.patch.object(n,'owned_container',side_effect=[state()['containerId'],None]) as owned:
            output = n.finalize(path,run=run)
        self.assertTrue(output['zeroOwnedContainers'])
        self.assertEqual(owned.call_count,2)
        self.assertEqual(run.call_args.args[0],['rm','--force',state()['containerId']])
        self.assertEqual(run.call_args.kwargs['timeout'],5)
        self.assertEqual(store.call_args.args[1]['phase'],'CLEANED')

    def test_finalizer_ownership_unknown_never_deletes(self):
        path = mock.Mock(); path.exists.return_value=True
        run = mock.Mock(side_effect=AssertionError)
        with mock.patch.object(n,'private_state',return_value=state()), \
             mock.patch.object(n,'owned_container',side_effect=n.Failure('FIXTURE_CONTAINER_REBOUND')), \
             self.assertRaisesRegex(n.Failure,'FIXTURE_CONTAINER_REBOUND'):
            n.finalize(path,run=run)
        run.assert_not_called()

    def test_finalizer_residue_never_marks_cleaned(self):
        path = mock.Mock(); path.exists.return_value=True
        with mock.patch.object(n,'private_state',return_value=state()) as store, \
             mock.patch.object(n,'owned_container',return_value=state()['containerId']), \
             self.assertRaisesRegex(n.Failure,'FIXTURE_RESIDUE'):
            n.finalize(path,run=mock.Mock(return_value=result()))
        self.assertEqual(store.call_count,1)

    def test_finalizer_no_receipt_never_uses_docker(self):
        path = mock.Mock(); path.exists.return_value=False
        run = mock.Mock(side_effect=AssertionError)
        self.assertEqual(n.finalize(path,run=run)['status'],'NATIVE_SQL_NOT_CREATED')
        run.assert_not_called()

    def test_prepare_pins_image_and_creates_one_isolated_resource_without_ports_volumes_or_privileged(self):
        value = state(); value['containerId']=None
        image = value['imageId']; cid = 'c'*64
        outputs = [result(),result((image+' {"/var/lib/postgresql/data":{}}\n').encode()),
                   result((cid+'\n').encode()),result(),result()]
        with mock.patch.object(n,'run_command',side_effect=outputs) as run, \
             mock.patch.object(n,'owned_container',side_effect=[None,cid]),mock.patch.object(n,'private_state'):
            self.assertEqual(n.prepare(mock.Mock(),value,n.time.monotonic()+180),(cid,image))
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(commands[0],['pull','--quiet','postgres:16'])
        create = next(args for args in commands if args[0]=='create')
        self.assertEqual(create.count('create'),1)
        self.assertIn(image,create); self.assertNotIn('postgres:16',create)
        for forbidden in ('--privileged','--publish','-p','--volume','-v','--mount'):
            self.assertNotIn(forbidden,create)
        self.assertEqual(create[create.index('--network')+1],'none')
        self.assertEqual(create[create.index('--memory')+1],'256m')
        self.assertEqual(create[create.index('--pids-limit')+1],'128')
        self.assertEqual(create.count('--tmpfs'),3)
        self.assertTrue(any('/proc/1/comm' in arg for arg in commands[-1]))

    def test_image_with_unexpected_volume_stops_before_create(self):
        value = state()
        outputs = [result(),result((value['imageId']+' {"/foreign":{}}').encode())]
        with mock.patch.object(n,'run_command',side_effect=outputs) as run, \
             mock.patch.object(n,'owned_container',return_value=None), \
             self.assertRaisesRegex(n.Failure,'FIXTURE_IMAGE_VOLUME_CONTRACT'):
            n.prepare(mock.Mock(),value,n.time.monotonic()+180)
        self.assertFalse(any(call.args[0][0]=='create' for call in run.call_args_list))

    def test_ambiguous_create_is_not_repeated(self):
        value = state()
        outputs = [result(),result((value['imageId']+' {"/var/lib/postgresql/data":{}}').encode()),
                   subprocess.TimeoutExpired('synthetic-create',20)]
        with mock.patch.object(n,'run_command',side_effect=outputs) as run, \
             mock.patch.object(n,'owned_container',return_value=None),mock.patch.object(n,'private_state'), \
             self.assertRaises(subprocess.TimeoutExpired):
            n.prepare(mock.Mock(),value,n.time.monotonic()+180)
        self.assertEqual(sum(call.args[0][0]=='create' for call in run.call_args_list),1)

    def test_main_primary_failure_preserved_and_cleanup_attempted_only_after_own_state_creation(self):
        path = ROOT/'synthetic-state-not-created.json'
        for owned in (False,True):
            with self.subTest(owned=owned),mock.patch.object(n.sys,'platform','linux'), \
                 mock.patch.object(n.os,'geteuid',create=True,return_value=0), \
                 mock.patch.object(n,'load_collector'), \
                 mock.patch.object(n,'new_state',side_effect=None if owned else n.Failure('FIXTURE_STATE_COLLISION'),return_value=state()), \
                 mock.patch.object(n,'prepare',side_effect=n.Failure('FIXTURE_CREATE_OUTCOME_UNKNOWN')), \
                 mock.patch.object(n,'finalize',return_value={'zeroOwnedContainers':True}) as finalizer, \
                 contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(n.main(['--require-native','--state',str(path)]),1)
                self.assertEqual(finalizer.call_count,int(owned))
                self.assertEqual(json.loads(output.getvalue())['reason'],
                                 'FIXTURE_CREATE_OUTCOME_UNKNOWN' if owned else 'FIXTURE_STATE_COLLISION')

    def test_workflow_has_one_bounded_proof_plus_independent_always_finalizer(self):
        workflow = (ROOT/'.github/workflows/tests.yml').read_text()
        self.assertEqual(workflow.count('test_xui_owner_central_postgres.py --require-native'),1)
        self.assertEqual(workflow.count('test_xui_owner_central_postgres.py --finalize'),1)
        tail = workflow.split('- name: Independently reconcile central SQL fixture cleanup',1)[1]
        self.assertIn('if: always()',tail)
        self.assertIn('timeout-minutes: 1',tail)
        self.assertIn('timeout-minutes: 4',workflow)
        self.assertIn('timeout-minutes: 15',workflow)
        self.assertIn('${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}.json',workflow)

    def test_successful_sql_never_hides_failed_cleanup(self):
        path = ROOT/'synthetic-state-not-created.json'
        with mock.patch.object(n.sys,'platform','linux'),mock.patch.object(n.os,'geteuid',create=True,return_value=0), \
             mock.patch.object(n,'load_collector'),mock.patch.object(n,'new_state',return_value=state()), \
             mock.patch.object(n,'prepare',return_value=(state()['containerId'],state()['imageId'])), \
             mock.patch.object(n,'PostgresFixture') as fixture, \
             mock.patch.object(n,'finalize',side_effect=n.Failure('FIXTURE_RESIDUE')), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            fixture.return_value.exercise.return_value={}
            self.assertEqual(n.main(['--require-native','--state',str(path)]),1)
        value=json.loads(output.getvalue())
        self.assertEqual(value['status'],'NATIVE_SQL_FAILED')
        self.assertEqual(value['reason'],'FIXTURE_CLEANUP_NOT_PROVEN')

    def test_primary_failure_survives_additional_cleanup_failure(self):
        path=ROOT/'synthetic-state-not-created.json'
        with mock.patch.object(n.sys,'platform','linux'),mock.patch.object(n.os,'geteuid',create=True,return_value=0), \
             mock.patch.object(n,'load_collector'),mock.patch.object(n,'new_state',return_value=state()), \
             mock.patch.object(n,'prepare',side_effect=n.Failure('FIXTURE_CREATE_OUTCOME_UNKNOWN')), \
             mock.patch.object(n,'finalize',side_effect=n.Failure('FIXTURE_RESIDUE')), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(n.main(['--require-native','--state',str(path)]),1)
        self.assertEqual(json.loads(output.getvalue())['reason'],'FIXTURE_CREATE_OUTCOME_UNKNOWN')

    def test_actual_reader_error_reports_only_fixed_case_counts_stop_and_sqlstate(self):
        m=n.load_collector(); marker='PRIVATE_MARKER_NEVER_EXPORT'
        fixture=n.PostgresFixture('c'*64,m,n.time.monotonic()+180)
        fixture.set_case('materializing')
        fixture.binding['targets'][fixture.target]['nodeId']=marker
        fixture.fingerprint=mock.Mock(return_value=b'unchanged')
        fixture.exercise=mock.Mock(side_effect=fixture.snapshot)
        error=(b'psql:<stdin>:17: ERROR:  42601\n'+marker.encode()+b' SQL INPUT ENV\n')
        path=ROOT/'synthetic-state-not-created.json'
        with mock.patch.object(n.sys,'platform','linux'),mock.patch.object(n.os,'geteuid',create=True,return_value=0), \
             mock.patch.object(n,'load_collector',return_value=m),mock.patch.object(n,'new_state',return_value=state()), \
             mock.patch.object(n,'prepare',return_value=(state()['containerId'],state()['imageId'])), \
             mock.patch.object(n,'PostgresFixture',return_value=fixture), \
             mock.patch.object(n,'run_command',return_value=result(rc=1,stderr=error)) as native, \
             mock.patch.object(n,'finalize',side_effect=n.Failure('FIXTURE_RESIDUE')), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(n.main(['--require-native','--state',str(path)]),1)
        value=json.loads(output.getvalue())
        self.assertEqual(value['reason'],'STOP_OBSERVATION_READ_FAILED')
        self.assertEqual(value['diagnostics'],{'stage':'collector_snapshot','case':'materializing',
            'snapshotsCompleted':0,'snapshotsAttempted':1,'collectorStopCode':'STOP_OBSERVATION_READ_FAILED','sqlstate':'42601'})
        self.assertFalse(value['zeroOwnedContainers'])
        self.assertNotIn(marker,output.getvalue())
        self.assertNotIn('SQL INPUT ENV',output.getvalue())
        self.assertEqual(native.call_count,1)

    def test_arbitrary_exception_and_diagnostic_fields_cannot_escape(self):
        m=n.load_collector(); marker='PRIVATE_MARKER_NEVER_EXPORT'
        self.assertEqual(n.safe_failure_code(n.Failure(marker),m),'FIXTURE_UNCLASSIFIED_FAILURE')
        self.assertEqual(n.safe_failure_code(m.Stop(marker),m),'STOP_UNCLASSIFIED')
        self.assertEqual(n.safe_failure_code(m.Stop([marker]),m),'STOP_UNCLASSIFIED')
        self.assertEqual(n.safe_failure_code(type(marker,(Exception,),{})(marker),m),'FIXTURE_UNKNOWN_ERROR')
        fake=types.SimpleNamespace(stage=marker,case=marker,last_collector_stop=marker,
                                  last_sql_error=marker.encode(),cases=True,calls=marker)
        value=n.safe_diagnostics(fake,marker)
        self.assertEqual(value,{'stage':'unknown','case':'unknown','snapshotsCompleted':0,
                                'snapshotsAttempted':0,'collectorStopCode':None,'sqlstate':None})
        self.assertNotIn(marker,json.dumps(value))

    def test_sqlstate_requires_one_complete_strict_error_code_line(self):
        for raw,expected in ((b'ERROR:  55P03\n', '55P03'),
                             (b'psql:<stdin>:2: ERROR:  42703\r\n','42703'),
                             (b'FATAL:  28P01\n','28P01'),
                             (b'ERROR: PRIVATE_MARKER\n',None),
                             (b'ERROR: 426010\n',None),(b'ERROR: 42p01\n',None),
                             (b'PRIVATE ERROR: 42601\n',None),
                             (b'ERROR: 42601\nERROR: 42703\n',None)):
            with self.subTest(raw=raw): self.assertEqual(n.sqlstate(raw),expected)

    def test_setup_sql_failure_retains_safe_sqlstate_and_success_resets_previous_case_error(self):
        m=n.load_collector();fixture=n.PostgresFixture('c'*64,m,n.time.monotonic()+180)
        with mock.patch.object(n,'run_command',return_value=result(rc=1,stderr=b'ERROR:  42P01\nPRIVATE_MARKER\n')), \
             self.assertRaisesRegex(n.Failure,'FIXTURE_COMMAND_FAILED'):
            fixture.sql('synthetic setup SQL')
        self.assertEqual(n.safe_diagnostics(fixture,'admission')['sqlstate'],'42P01')
        fixture.last_collector_stop='STOP_OBSERVATION_READ_FAILED'
        fixture.set_case('final_positive')
        diagnostics=n.safe_diagnostics(fixture,'admission')
        self.assertIsNone(diagnostics['sqlstate']);self.assertIsNone(diagnostics['collectorStopCode'])


if __name__ == '__main__':
    unittest.main()
