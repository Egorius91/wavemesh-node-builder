"""Trusted-adapter receipt fixtures; no real owner transport/native acceptance."""
import copy
from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import sys
import tempfile
import sqlite3
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/lib"))
import prepared_completion as completion
import prepared_journal as reader
import transaction_state as journal
from tests.unit.test_prepared_journal import completed_fixture
from tests.unit.test_prepared_channels import fixture
import prepared_channels as channels
import prepared_execution as execution
import runtime_state


class PreparedCompletionRetention(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="wm-owner-reply-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "transactions"
        self.directory = completed_fixture(self.root)
        (self.directory / "prepared.apply.completed.json").rename(self.directory / "prepared.apply.pending.json")
        (self.directory / "prepared.owner-result.json").unlink()
        journal.atomic_json(self.directory / "result.json", {"status": "in_progress"})
        self.reply = {"schema_version": 1, "transaction_id": self.directory.name,
                      "intent_sha256": reader.sha((self.directory / "prepared.apply.pending.json").read_bytes()),
                      "outcome": {**{key: key != "NoOp" for key in reader.OUTCOME}, "ProcessConfigSHA256": reader.sha((self.directory / reader.PROCESS_POST).read_bytes())}}

    def test_known_partial_and_noop_reply_retention_never_settles_debt(self):
        for kind in ("known", "partial", "no-op"):
            with self.subTest(kind=kind):
                reply = copy.deepcopy(self.reply)
                if kind == "partial":
                    reply["outcome"]["KnownCommit"] = False
                    reply["outcome"]["ConfigPersisted"] = False
                    reply["outcome"]["ProcessConfigSHA256"] = ""
                elif kind == "no-op":
                    reply["outcome"] = {**{key: key == "NoOp" for key in reader.OUTCOME}, "ProcessConfigSHA256": ""}
                path = self.directory / "prepared.owner-result.json"
                path.unlink(missing_ok=True)  # Only this synthetic fixture is reset.
                before = (self.directory / "result.json").read_bytes()
                retained = completion.record_owner_result(self.directory, reply)
                self.assertEqual(retained, reader.sha(path.read_bytes()))
                self.assertEqual(reader.decode(path.read_bytes()), reply)
                self.assertEqual((self.directory / "result.json").read_bytes(), before)
                self.assertEqual(reader.prepared_status(self.directory), "active")
                self.assertEqual([item["id"] for item in journal.incomplete(self.root)], [self.directory.name])
                with self.assertRaises(RuntimeError):
                    journal.mark(self.directory, "committed")

    def test_foreign_context_and_verified_flag_are_refused_without_writes(self):
        for change in ("transaction", "intent", "verified", "bool-version", "string-outcome"):
            with self.subTest(change=change):
                reply = copy.deepcopy(self.reply)
                if change == "transaction": reply["transaction_id"] = "foreign"
                elif change == "intent": reply["intent_sha256"] = "f"*64
                elif change == "verified": reply["verified"] = True
                elif change == "bool-version": reply["schema_version"] = True
                elif change == "string-outcome": reply["outcome"]["KnownCommit"] = "true"
                with mock.patch.object(journal, "atomic_json", side_effect=AssertionError("foreign result wrote a file")):
                    with self.assertRaisesRegex(completion.CompletionUnavailable, "^OWNER_RESULT_NOT_RETAINED$"):
                        completion.record_owner_result(self.directory, reply)
                self.assertFalse((self.directory / "prepared.owner-result.json").exists())

    def test_duplicate_repeats_only_metadata_and_conflict_keeps_original(self):
        completion.record_owner_result(self.directory, self.reply)
        path = self.directory / "prepared.owner-result.json"
        before = path.read_bytes()
        with mock.patch.object(journal, "atomic_json", side_effect=AssertionError("duplicate rewrote evidence")):
            completion.record_owner_result(self.directory, copy.deepcopy(self.reply))
        other = copy.deepcopy(self.reply)
        other["outcome"]["KnownCommit"] = False
        with self.assertRaises(completion.CompletionUnavailable):
            completion.record_owner_result(self.directory, other)
        self.assertEqual(path.read_bytes(), before)

    def test_failed_reply_directory_barrier_retains_facts_for_metadata_retry(self):
        path = self.directory / "prepared.owner-result.json"
        real = journal._sync_directory
        def failed(directory):
            if Path(directory) == self.directory and path.exists():
                raise OSError("synthetic retained-reply barrier failed")
            real(directory)
        with mock.patch.object(journal, "_sync_directory", side_effect=failed):
            with self.assertRaises(completion.CompletionUnavailable):
                completion.record_owner_result(self.directory, self.reply)
        self.assertTrue(path.is_file())
        before = path.read_bytes()
        completion.record_owner_result(self.directory, self.reply)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(reader.prepared_status(self.directory), "active")

    def test_missing_or_completed_origin_cannot_be_replaced_by_a_reply(self):
        marker = self.directory / "prepared.apply.pending.json"
        marker.rename(self.directory / "prepared.apply.completed.json")
        with self.assertRaises(completion.CompletionUnavailable):
            completion.record_owner_result(self.directory, self.reply)
        self.assertFalse((self.directory / "prepared.owner-result.json").exists())


class PreparedCompletionWriter(unittest.TestCase):
    """Real private files/SQLite, mocked qualified collector and node-lock owner."""

    def make_case(self, suffix="case", process_before=None):
        base = Path(self.temporary.name) / suffix
        base.mkdir()
        root = base / "transactions"
        directory = journal.begin(root, "auto-prepared-reconcile", 123)
        config, plan, original = fixture()
        config["root_unicode_sentinel"] = "synthetic узел / café"
        engine = {"name":"xray", "version":"26.10.21", "sha256":execution.CORE}
        plan["channel"]["engine"] = engine
        for approval in plan["exits"]: approval["channel"]["engine"] = copy.deepcopy(engine)
        route_id = "route-auto-europe"
        config, initial_runtime, before, binding = channels.stage(config, {}, route_id, plan, original, now=100)
        before["native_generated_sentinel"] = "actual-only"
        view = {"schema_version":3, "native_generation":"00000000-0000-4000-8000-000000000002", "mutation_epoch":"0", "selector_transitions":[],
                "engine_instance":{"boot_id":"00000000-0000-4000-8000-000000000001","pid":123,"start_ticks":"1000"}, "effective_config_sha256":channels.digest(before)}
        fingerprint = channels.health_config_fingerprint(config, route_id, plan, before, view)
        observation = {"schema_version":3,"binding":binding,"engine":engine,"at":100,"paths":{p["id"]:True for p in plan["paths"]},
                       "engine_instance_before":view["engine_instance"],"engine_instance_after":view["engine_instance"],"effective_config_sha256":view["effective_config_sha256"],
                       "native_generation_before":view["native_generation"],"native_generation_after":view["native_generation"],"mutation_epoch_before":"0","mutation_epoch_after":"0"}
        first = channels.select_paths(plan,binding,{},observation,100,view,fingerprint,"wm-rule-auto-europe")
        observation["at"]=110
        state = channels.select_paths(plan,binding,first,observation,110,view,fingerprint,"wm-rule-auto-europe")
        target = "wm-exit-path-"+state["selected"]
        template,_=channels.compile_template(config,route_id,plan,original,state,now=110,runtime_view=view,effective_config=before)
        after = copy.deepcopy(before)
        next(r for r in after["routing"]["rules"] if r.get("ruleTag")=="wm-rule-auto-europe")["outboundTag"]=target
        runtime = {"root_revision":1,"root_field":{"keep":"unchanged"},"prepared_routes":{route_id:initial_runtime["prepared_routes"][route_id]}}
        candidate_runtime=copy.deepcopy(runtime);candidate_runtime["prepared_routes"][route_id]=state
        candidates={"config":config,"runtime":candidate_runtime,"template":template}
        files={key:str((base/(key+('.db' if key=='database' else '.json'))).absolute()) for key in ("config","runtime","database","process")}
        for key in ("config","runtime"): journal.atomic_json(files[key],candidates[key] if key=="config" else runtime)
        journal.atomic_bytes(directory/'config.before.json',Path(files['config']).read_bytes())
        journal.atomic_bytes(directory/'runtime.before.json',Path(files['runtime']).read_bytes())
        # Exercise the actual compiler's UTF-8 candidate format, which differs
        # from the generic journal serializer for non-ASCII configuration.
        for key,name in reader.ARTIFACTS.items(): runtime_state.atomic(directory/name,candidates[key])
        process={"routing":copy.deepcopy(template["routing"]),"inbounds":[{"tag":"desired-generated-sentinel"}],"private_sentinel":"process-only"}
        # Deliberately unlike Python's canonical candidate formatting: preserve
        # exact owner bytes rather than remarshal this postimage.
        process_raw=(json.dumps(process,ensure_ascii=False,indent='\t')+'\n').encode()
        journal.atomic_bytes(files['process'],process_raw)
        with closing(sqlite3.connect(files['database'])) as db:
            db.execute('CREATE TABLE settings (id INTEGER PRIMARY KEY, key TEXT, value TEXT)')
            db.execute('INSERT INTO settings(key,value) VALUES (?,?)',('xrayTemplateConfig',json.dumps(template)))
            db.commit()
        Path(files['database']).chmod(0o600)
        intent={"schema_version":1,"transaction_id":directory.name,"route_id":route_id,"binding":binding,
                "expected":{"generation":view['native_generation'],"epoch":"0","engine_instance":view['engine_instance'],"before_sha256":view['effective_config_sha256'],
                            "after_sha256":channels.digest(after),"normalized_sha256":fingerprint,"rule_tag":"wm-rule-auto-europe","inbound_tag":"wm-route-auto-europe","target":target},
                "files":files,"before":{"config":reader.sha(Path(files['config']).read_bytes()),"runtime":reader.sha(Path(files['runtime']).read_bytes()),
                                          "owned":completion._owned(runtime,route_id),"template":"e"*64,"process":process_before},
                "candidate":{key:reader.sha((directory/name).read_bytes()) for key,name in reader.ARTIFACTS.items()}}
        journal.atomic_json(directory/'prepared.apply.pending.json',intent)
        reply={"schema_version":1,"transaction_id":directory.name,"intent_sha256":reader.sha((directory/'prepared.apply.pending.json').read_bytes()),
               "outcome":{**{key:key!='NoOp' for key in reader.OUTCOME},"ProcessConfigSHA256":reader.sha(process_raw)}}
        post_view={**view,"mutation_epoch":"1","effective_config_sha256":channels.digest(after),"selector_transitions":[reader.expected_transition(intent)]}
        return directory,intent,reply,post_view,after

    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory(prefix='wm-completion-writer-')
        self.addCleanup(self.temporary.cleanup)

    def finish(self, case, reply=True, view=None):
        directory,intent,result,current,effective=case
        with mock.patch.object(completion,'_require_node_owner'), mock.patch.object(execution,'collect',return_value=(current if view is None else view,effective)):
            return completion.complete(directory,result if reply else None)

    def test_known_commit_preserves_root_fields_raw_process_and_preapply_epoch(self):
        for audit in (None,reader.sha(b'stale old startup disk')):
            case=self.make_case('missing' if audit is None else 'stale',audit)
            directory,intent,reply,view,effective=case
            runtime=json.loads(Path(intent['files']['runtime']).read_text());runtime['root_revision']=2;runtime['new_root_field']={'preserved':True}
            journal.atomic_json(intent['files']['runtime'],runtime)
            process_before=Path(intent['files']['process']).read_bytes()
            self.assertEqual(self.finish(case),'committed')
            self.assertFalse((directory/'prepared.apply.pending.json').exists())
            self.assertEqual(reader.prepared_status(directory),'committed')
            self.assertEqual((directory/reader.PROCESS_POST).read_bytes(),process_before)
            self.assertEqual(Path(intent['files']['process']).read_bytes(),process_before)
            result=json.loads(Path(intent['files']['runtime']).read_text())
            self.assertEqual(result['root_revision'],2);self.assertEqual(result['new_root_field'],{'preserved':True})
            self.assertEqual(result['prepared_routes'][intent['route_id']]['mutation_epoch'],'0')
            with mock.patch.object(execution,'collect',side_effect=AssertionError('idempotent metadata recollected or dispatched')),mock.patch.object(completion,'_require_node_owner'):
                self.assertEqual(completion.complete(directory),'committed')

    def test_lost_partial_or_noop_reply_never_reaches_collect_or_local_commit(self):
        for kind in ('lost','partial','no-op'):
            case=self.make_case(kind);directory,intent,reply,view,effective=case
            if kind=='partial':reply['outcome']['KnownCommit']=False
            if kind=='no-op':reply['outcome']={**{key:key=='NoOp' for key in reader.OUTCOME},'ProcessConfigSHA256':''}
            before=Path(intent['files']['runtime']).read_bytes()
            with mock.patch.object(completion,'_require_node_owner'),mock.patch.object(execution,'collect',side_effect=AssertionError('unknown reply reached collect')):
                with self.assertRaises(completion.CompletionUnavailable):completion.complete(directory,None if kind=='lost' else reply)
            self.assertEqual(Path(intent['files']['runtime']).read_bytes(),before)
            self.assertEqual(reader.prepared_status(directory),'active')

    def test_stale_execution_wrong_storage_and_third_values_do_not_overwrite(self):
        for kind in ('epoch','process','template','config-third','runtime-third'):
            case=self.make_case(kind);directory,intent,reply,view,effective=case
            if kind=='epoch':view['mutation_epoch']='2'
            if kind=='process':journal.atomic_json(intent['files']['process'],{'wrong':'postimage'})
            if kind=='template':
                with closing(sqlite3.connect(intent['files']['database'])) as db:
                    db.execute('UPDATE settings SET value=?',('{"wrong":true}',))
                    db.commit()
            if kind=='config-third':journal.atomic_json(intent['files']['config'],{'foreign':'revision'})
            if kind=='runtime-third':
                data=json.loads(Path(intent['files']['runtime']).read_text());data['prepared_routes'][intent['route_id']]={'third':True};journal.atomic_json(intent['files']['runtime'],data)
            before={key:Path(intent['files'][key]).read_bytes() for key in ('config','runtime')}
            with self.assertRaises(completion.CompletionUnavailable):self.finish(case)
            for key,value in before.items():self.assertEqual(Path(intent['files'][key]).read_bytes(),value)
            self.assertTrue((directory/'prepared.owner-result.json').is_file())
            self.assertEqual(reader.prepared_status(directory),'active')

    def test_local_commit_faults_keep_origin_and_retry_only_metadata(self):
        for kind in ('process-post','runtime-post','config','runtime','result'):
            case=self.make_case(kind);directory,intent,reply,view,effective=case
            target={'process-post':directory/reader.PROCESS_POST,'runtime-post':directory/'prepared.runtime.post.json',
                    'config':Path(intent['files']['config']),'runtime':Path(intent['files']['runtime']),'result':directory/'result.json'}[kind]
            real_json,real_bytes=journal.atomic_json,journal.atomic_bytes
            failed=False
            def write_json(path,value):
                nonlocal failed
                real_json(path,value)
                if Path(path)==target and not failed:failed=True;raise OSError('synthetic local barrier failure')
            def write_bytes(path,value):
                nonlocal failed
                real_bytes(path,value)
                if Path(path)==target and not failed:failed=True;raise OSError('synthetic local barrier failure')
            with mock.patch.object(journal,'atomic_json',side_effect=write_json),mock.patch.object(journal,'atomic_bytes',side_effect=write_bytes):
                with self.assertRaises(completion.CompletionUnavailable):self.finish(case)
            self.assertTrue(failed);self.assertEqual(reader.prepared_status(directory),'active')
            self.assertTrue((directory/'prepared.owner-result.json').is_file())
            self.assertEqual(self.finish(case,reply=False),'committed')

    def test_rename_and_directory_failures_never_authorize_snapshot_restore(self):
        for kind in ('rename','directory'):
            case=self.make_case(kind);directory,intent,reply,view,effective=case
            if kind=='rename':
                context=mock.patch.object(completion.os,'rename',side_effect=OSError('synthetic rename refusal'))
            else:
                original=journal._sync_directory
                def sync(path):
                    if Path(path)==directory and (directory/'prepared.apply.completed.json').exists():raise OSError('synthetic rename directory barrier')
                    original(path)
                context=mock.patch.object(journal,'_sync_directory',side_effect=sync)
            with context:
                with self.assertRaises(completion.CompletionUnavailable):self.finish(case)
                with self.assertRaises((RuntimeError,OSError)):journal.admit(directory.parent)
            before=(directory/'result.json').read_bytes()
            with self.assertRaises(RuntimeError):journal.mark(directory,'rolled_back')
            self.assertEqual((directory/'result.json').read_bytes(),before)
            self.assertEqual(self.finish(case,reply=False),'committed')

    def test_existing_node_owner_required_before_any_local_commit(self):
        case=self.make_case();directory,intent,reply,view,effective=case
        before=Path(intent['files']['runtime']).read_bytes()
        with mock.patch.object(completion,'_require_node_owner',side_effect=ValueError('not the existing node owner')):
            with self.assertRaises(completion.CompletionUnavailable):completion.complete(directory,reply)
        self.assertEqual(Path(intent['files']['runtime']).read_bytes(),before)

    def test_typed_prefix_proof_is_checked_before_pending_is_renamed(self):
        case=self.make_case();directory,intent,reply,view,effective=case
        with mock.patch.object(completion.os,'rename',side_effect=OSError('synthetic pre-rename stop')):
            with self.assertRaises(completion.CompletionUnavailable):self.finish(case)
        result=json.loads((directory/'result.json').read_text())
        result['prepared_commit']['intent']['schema_version']=True
        journal.atomic_json(directory/'result.json',result)
        with mock.patch.object(completion.os,'rename',side_effect=AssertionError('malformed prefix reached rename')):
            with self.assertRaises(completion.CompletionUnavailable):self.finish(case,reply=False)
        self.assertTrue((directory/'prepared.apply.pending.json').is_file())


class PreparedCompletionNativeOwner(unittest.TestCase):
    @contextmanager
    def descriptor(self, path):
        try: saved=os.dup(9)
        except OSError: saved=None
        fd=os.open(path,os.O_RDWR|os.O_CREAT|getattr(os,'O_NOFOLLOW',0),0o600)
        try:
            os.dup2(fd,9)
            yield
        finally:
            if saved is None:os.close(9)
            else:os.dup2(saved,9);os.close(saved)
            if fd!=9:os.close(fd)

    @unittest.skipUnless(sys.platform=='linux' and getattr(os,'geteuid',lambda:1)()==0,'native Linux root node lock required')
    def test_native_existing_fd9_is_held_until_caller_closes_it(self):
        import fcntl
        with tempfile.TemporaryDirectory(prefix='wm-owner-fd9-') as name:
            path=Path(name)/'node.lock'
            with self.descriptor(path),mock.patch.object(completion,'NODE_MUTATION_LOCK',path):
                inode=path.stat().st_ino
                completion._require_node_owner()
                contender=os.open(path,os.O_RDWR)
                try:
                    with self.assertRaises(BlockingIOError):fcntl.flock(contender,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    self.assertEqual(path.stat().st_ino,inode)
                finally:os.close(contender)
            contender=os.open(path,os.O_RDWR)
            try:fcntl.flock(contender,fcntl.LOCK_EX|fcntl.LOCK_NB)
            finally:os.close(contender)

    @unittest.skipUnless(sys.platform=='linux' and getattr(os,'geteuid',lambda:1)()==0,'native Linux root node lock required')
    def test_native_foreign_inode_and_weak_lock_are_refused(self):
        with tempfile.TemporaryDirectory(prefix='wm-owner-fd9-') as name:
            path=Path(name)/'node.lock';other=Path(name)/'foreign.lock'
            other.touch(mode=0o600)
            with self.descriptor(path),mock.patch.object(completion,'NODE_MUTATION_LOCK',other):
                with self.assertRaises(ValueError):completion._require_node_owner()
            with self.descriptor(path),mock.patch.object(completion,'NODE_MUTATION_LOCK',path):
                os.fchmod(9,0o666)
                with self.assertRaises(ValueError):completion._require_node_owner()


if __name__ == "__main__":
    unittest.main()
