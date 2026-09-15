import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'agent'))
import panel_candidate as module
import panel_request_guard as journal

OP = '00000000-0000-4000-8000-000000000001'
HEAD = 'a' * 40


def fixture(extra=None):
    raw = io.BytesIO()
    members = {}
    with tarfile.open(fileobj=raw, mode='w:gz', format=tarfile.USTAR_FORMAT) as archive:
        for name in sorted(module.DIRECTORIES) + sorted(module.FILES):
            entry = tarfile.TarInfo(name)
            directory = name in module.DIRECTORIES
            data = b'' if directory else ('fixture ' + name).encode()
            entry.type = tarfile.DIRTYPE if directory else tarfile.REGTYPE
            entry.mode = 0o755 if directory or name in module.EXECUTABLES else 0o644
            entry.size = len(data)
            archive.addfile(entry, None if directory else io.BytesIO(data))
            members[name] = {'kind': 'directory' if directory else 'file', 'mode': entry.mode, 'size': len(data)}
            if not directory:
                members[name]['sha256'] = hashlib.sha256(data).hexdigest()
        if extra is not None:
            archive.addfile(extra)
    compressed = raw.getvalue()
    source = b'private fixture corresponding-source bytes'
    manifest = {'schema': 1, 'status': 'CI_CANDIDATE_NOT_DEPLOYED', 'platform': 'linux-amd64',
                'builder_commit': HEAD, 'version': '3.4.2-wavemesh.' + HEAD[:12], 'upstream': {},
                'runtime_release_sha256': 'b'*64, 'source_sha256': hashlib.sha256(source).hexdigest(),
                'archive_sha256': hashlib.sha256(compressed).hexdigest(), 'members': members,
                'frontend': {}, 'toolchain': 'fixture', 'workflow_run': '1', 'workflow_attempt': '1'}
    return manifest, compressed, source


class ContractTest(unittest.TestCase):
    def test_trusted_digest_and_source_commit_are_required(self):
        value, _, _ = fixture()
        raw = json.dumps(value).encode()
        module.manifest(raw, module.digest(raw), HEAD)
        for digest, head in (('c'*64, HEAD), (module.digest(raw), 'd'*40), ('', HEAD), (module.digest(raw), True)):
            with self.assertRaises(module.CandidateError):
                module.manifest(raw, digest, head)

    def test_manifest_rejects_extra_paths_wrong_modes_and_platform(self):
        value, _, _ = fixture()
        bad = [{**value, 'platform': 'linux-arm64'}, {**value, 'builder_commit': None},
               {**value, 'schema': True}, {**value, 'unexpected': True},
               {**value, 'members': {**value['members'], '../escape': value['members']['x-ui/x-ui']}},
               {**value, 'members': {**value['members'], 'x-ui/x-ui': {**value['members']['x-ui/x-ui'], 'mode': 0o4755}}}]
        for item in bad:
            raw = json.dumps(item).encode()
            with self.assertRaises(module.CandidateError):
                module.manifest(raw, module.digest(raw), HEAD)


@unittest.skipUnless(sys.platform == 'linux' and getattr(os, 'geteuid', lambda: -1)() == 0,
                     'dedicated root Linux CI')
class PreparationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='wm-candidate-', dir='/run')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / 'live-panel'; self.home.mkdir(mode=0o755)
        (self.home / 'x-ui').write_bytes(b'existing running image')
        self.bundle = self.root / 'incoming'; self.bundle.mkdir(mode=0o700)
        self.guard = journal.PanelRequestGuard(self.root / 'journal')
        self.lock = self.root / 'node.lock'
        self.addCleanup(patch.stopall)
        patch.object(module, 'PANEL_HOME', self.home).start()
        patch.object(journal, 'DEFAULT_ROOT', self.guard.root).start()
        with journal.maintenance_node_lock(self.lock), self.guard.locked():
            self.guard.maintenance('prepare', OP, 1)
        self.install_bundle()
        self.stage = module.location(OP, 1)

    def install_bundle(self, extra=None):
        value, archive, source = fixture(extra)
        raw = json.dumps(value).encode()
        self.manifest_sha = module.digest(raw)
        self.value = value
        for name, data in (('manifest.json', raw), (module.ARCHIVE, archive), ('source.tar.gz', source)):
            (self.bundle / name).write_bytes(data)
            (self.bundle / name).chmod(0o600)

    def prepare(self):
        return module.prepare(self.guard, OP, 1, self.bundle, self.manifest_sha, HEAD, self.lock)

    def test_prepares_private_exact_tree_and_replays_without_source_or_extraction(self):
        original = (self.home / 'x-ui').read_bytes()
        guard_before = (self.guard.root / 'state.json').read_bytes()
        result = self.prepare()
        self.assertFalse(result['reconciliation_required'])
        self.assertEqual(result['candidate_sha256'], self.value['archive_sha256'])
        self.assertEqual((result['prepared_home'] / 'x-ui').read_bytes(), b'fixture x-ui/x-ui')
        for path in self.stage.rglob('*'):
            self.assertEqual(path.stat().st_mode & 0o777, 0o700 if path.is_dir() else 0o600)
        (self.bundle / module.ARCHIVE).unlink()
        with patch.object(module, 'extract', side_effect=AssertionError('SECOND_EXTRACTION')):
            self.assertTrue(self.prepare()['reconciliation_required'])
        self.assertEqual((self.home / 'x-ui').read_bytes(), original)
        self.assertEqual((self.guard.root / 'state.json').read_bytes(), guard_before)

    def test_both_locks_cover_extraction(self):
        extract = module.extract
        def locked(*args):
            for lock in (journal.maintenance_node_lock(self.lock), self.guard.locked()):
                with self.assertRaises(journal.PanelRequestError):
                    with lock:
                        self.fail('preparation lock missing')
            return extract(*args)
        with patch.object(module, 'extract', side_effect=locked):
            self.prepare()

    def test_unresolved_request_and_cancelled_hold_reject_before_copy(self):
        with self.guard.locked():
            held = self.guard.load()
        for state in ({**held, 'maintenance': {**held['maintenance'], 'phase': 'CANCELLED'}},
                      {**held, 'request': {'schema_version': 1, 'phase': 'DISPATCH_INTENT',
                                          'attempt_id': 'b'*64, 'request_digest': 'c'*64}}):
            with self.guard.locked():
                self.guard.save(state)
            with self.assertRaisesRegex(module.CandidateError, 'CANDIDATE_ADMISSION_REQUIRED'):
                self.prepare()
            self.assertFalse(self.stage.exists())

    def test_mismatched_artifact_and_unsafe_source_link_reject_before_stage(self):
        source = self.bundle / 'source.tar.gz'
        source.write_bytes(b'corrupt')
        with self.assertRaisesRegex(module.CandidateError, 'CANDIDATE_CONTENT_MISMATCH'):
            self.prepare()
        self.assertFalse(self.stage.exists())
        source.unlink(); source.symlink_to(self.home / 'x-ui')
        with self.assertRaises(OSError):
            self.prepare()
        self.assertFalse(self.stage.exists())

    def test_process_death_retains_stage_without_retry(self):
        import multiprocessing
        def crash():
            with patch.object(module, 'extract', side_effect=lambda *args: os._exit(86)):
                self.prepare()
        child = multiprocessing.get_context('fork').Process(target=crash)
        child.start(); child.join(timeout=5)
        if child.is_alive():
            child.kill(); child.join(timeout=5); self.fail('crash did not terminate')
        self.assertEqual(child.exitcode, 86)
        before = (self.stage / 'intent.json').read_bytes()
        with patch.object(module, 'extract', side_effect=AssertionError('SECOND_EXTRACTION')):
            with self.assertRaisesRegex(module.CandidateError, 'CANDIDATE_RECONCILIATION_REQUIRED'):
                self.prepare()
        self.assertEqual((self.stage / 'intent.json').read_bytes(), before)

    def test_failed_publication_retains_pending_file(self):
        with patch.object(module.os, 'rename', side_effect=OSError('synthetic')):
            with self.assertRaises(OSError):
                self.prepare()
        self.assertTrue((self.stage / 'ready.pending').exists())
        with self.assertRaisesRegex(module.CandidateError, 'CANDIDATE_RECONCILIATION_REQUIRED'):
            self.prepare()

    def test_post_publication_sync_failure_can_verify_without_reextracting(self):
        sync = module.storage.sync_dir
        def fail(path):
            if path == self.stage and (path / 'ready.json').exists():
                raise OSError('synthetic')
            return sync(path)
        with patch.object(module.storage, 'sync_dir', side_effect=fail):
            with self.assertRaises(OSError):
                self.prepare()
        with patch.object(module, 'extract', side_effect=AssertionError('SECOND_EXTRACTION')):
            self.assertTrue(self.prepare()['reconciliation_required'])

    def test_tampered_payload_and_changed_installation_binding_reject(self):
        result = self.prepare()
        image = result['prepared_home'] / 'x-ui'
        original = image.read_bytes(); image.write_bytes(b'tampered')
        with self.assertRaisesRegex(module.CandidateError, 'CANDIDATE_CONTENT_MISMATCH'):
            self.prepare()
        image.write_bytes(original)
        with self.guard.installation_intent(OP, 1, 'f'*64, 'e'*64, self.lock):
            pass
        with self.assertRaisesRegex(module.CandidateError, 'CANDIDATE_BINDING_CHANGED'):
            self.prepare()

    def test_boolean_generation_in_private_intent_is_not_an_integer_alias(self):
        self.prepare()
        path = self.stage / 'intent.json'
        value = json.loads(path.read_bytes())
        value['generation'] = True
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(module.CandidateError, 'CANDIDATE_BINDING_CHANGED'):
            self.prepare()

    def test_tar_extensions_links_and_duplicate_entries_cannot_escape(self):
        for index, kind in enumerate((tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.XHDTYPE, tarfile.GNUTYPE_SPARSE, tarfile.REGTYPE)):
            extra = tarfile.TarInfo('x-ui/x-ui')
            extra.type = kind
            extra.linkname = '../escape' if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE) else ''
            value, data, _ = fixture(extra)
            target = self.root / ('invalid-' + str(index)); target.mkdir(mode=0o700)
            with self.assertRaises(module.CandidateError):
                module.extract(data, value['members'], target)
        value, data, _ = fixture()
        # A second hidden tar/gzip member cannot follow the valid end marker.
        target = self.root / 'trailing'; target.mkdir(mode=0o700)
        with self.assertRaises(module.CandidateError):
            module.extract(data + gzip.compress(b'not padding'), value['members'], target)


if __name__ == '__main__':
    unittest.main()
