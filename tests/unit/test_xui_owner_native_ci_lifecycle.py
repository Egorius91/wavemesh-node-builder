#!/usr/bin/env python3
"""Portable lifecycle checks: AST functions only, all native effects mocked."""
import ast
import pathlib
import stat
import subprocess
import textwrap
import types
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[2]
FIXTURE_SOURCE = (ROOT / 'tests/native/test_xui_owner_repair_native.py').read_text()
WORKFLOW_SOURCE = (ROOT / '.github/workflows/tests.yml').read_text()


def functions_only(source, names, namespace):
    tree = ast.parse(source)
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    exec(compile(ast.Module(body=functions, type_ignores=[]), '<mocked-lifecycle>', 'exec'), namespace)
    return namespace


class FixtureCleanup(unittest.TestCase):
    def setup_case(self, failure):
        tree = ast.parse(FIXTURE_SOURCE)
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'NativeFixtures')
        method = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == 'setUp')
        fake_os, fake_temp, fake_paths, fake_module, fake_mock = [mock.MagicMock() for _ in range(5)]
        fake_os.umask.return_value = 0o022
        path = fake_paths.Path.return_value
        path.stat.return_value.st_mode = stat.S_IFDIR | 0o700
        path.__truediv__.return_value = path
        fake_module.MEMBERS = ()
        if failure == 'constructor':
            fake_temp.TemporaryDirectory.side_effect = OSError('constructor')
        elif failure == 'mkdir':
            path.mkdir.side_effect = OSError('mkdir')
        else:
            fake_module.NativeObjects.capture.side_effect = OSError('capture')
        namespace = dict(os=fake_os, tempfile=fake_temp, pathlib=fake_paths, m=fake_module, mock=fake_mock, stat=stat)
        exec(compile(ast.Module(body=[method], type_ignores=[]), '<mocked-setup>', 'exec'), namespace)
        case = unittest.TestCase()
        with self.assertRaises(OSError):
            namespace['setUp'](case)
        case.doCleanups()
        if failure != 'constructor':
            fake_temp.TemporaryDirectory.return_value.cleanup.assert_called_once_with()
        fake_os.umask.assert_has_calls([mock.call(0o077), mock.call(0o022)])
        if failure == 'capture':
            fake_mock.patch.object.return_value.stop.assert_called_once_with()
        return path

    def test_constructor_failure_restores_umask(self):
        self.setup_case('constructor')

    def test_setup_failure_cleans_temp_and_restores_umask(self):
        path = self.setup_case('mkdir')
        path.mkdir.assert_called_once_with(parents=True, mode=0o700)

    def test_late_setup_failure_stops_patch_and_cleans_temp(self):
        self.setup_case('capture')


class WrapperCleanup(unittest.TestCase):
    def setUp(self):
        source = WORKFLOW_SOURCE.split('# owner-native-wrapper-begin\n', 1)[1].split('# owner-native-wrapper-end', 1)[0]
        source = textwrap.dedent(source)
        self.os, self.temp, self.paths, self.sub = [mock.MagicMock() for _ in range(4)]
        self.os.umask.return_value = 0o022
        self.sub.TimeoutExpired = subprocess.TimeoutExpired
        self.sub.run.return_value.returncode = 0
        self.path = self.paths.Path.return_value
        self.temp.TemporaryDirectory.return_value.name = '/synthetic-exact-outer'
        self.path.lstat.side_effect = FileNotFoundError
        self.os.scandir.return_value.__enter__.return_value = iter(())
        self.sys = types.SimpleNamespace(version_info=(3, 12), executable='/synthetic-python')
        self.ns = functions_only(source, {'owner_outer_identity', 'run_owner_native_fixture'},
            dict(os=self.os, tempfile=self.temp, pathlib=self.paths, subprocess=self.sub, sys=self.sys, stat=stat, print=mock.Mock()))
        self.identity = mock.Mock(return_value=(1, 2, 0, 0, 0o700))
        self.ns['owner_outer_identity'] = self.identity

    def run_wrapper(self):
        result = self.ns['run_owner_native_fixture']()
        self.os.umask.assert_has_calls([mock.call(0o077), mock.call(0o022)])
        return result

    def test_success_exact_child_flags_environment_and_cleanup(self):
        self.assertEqual(self.run_wrapper(), 0)
        self.temp.TemporaryDirectory.assert_called_once_with(prefix='wavemesh-owner-native-ci-', delete=False)
        args, kwargs = self.sub.run.call_args
        self.assertEqual(args[0][:3], ['/synthetic-python', '-I', '-B'])
        self.assertEqual(args[0][-1], '--require-native')
        self.assertEqual(kwargs['env'], {'PATH':'/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL':'C.UTF-8', 'TMPDIR':str(self.path)})
        self.assertEqual(kwargs['timeout'], 120)
        self.assertFalse(kwargs['check'])
        self.assertEqual(kwargs['stdin'], self.sub.DEVNULL)
        self.assertEqual(kwargs['cwd'], str(self.path.absolute.return_value.parents[2]))
        self.temp.TemporaryDirectory.return_value.cleanup.assert_called_once_with()
        self.path.lstat.assert_called_once_with()

    def test_nonzero_child_preserved_and_cleanup_runs(self):
        self.sub.run.return_value.returncode = 7
        self.assertEqual(self.run_wrapper(), 7)
        self.temp.TemporaryDirectory.return_value.cleanup.assert_called_once_with()

    def test_inert_77_is_ci_failure_not_pass(self):
        self.sub.run.return_value.returncode = 77
        self.assertEqual(self.run_wrapper(), 77)

    def test_primary_failure_survives_cleanup_failure(self):
        self.sub.run.return_value.returncode = 7
        self.temp.TemporaryDirectory.return_value.cleanup.side_effect = OSError('cleanup')
        self.assertEqual(self.run_wrapper(), 7)
        self.assertTrue(any(call.args[0] == 'OWNER_NATIVE_OUTER_CLEANUP_FAILED' for call in self.ns['print'].call_args_list))

    def test_success_with_cleanup_failure_fails(self):
        self.temp.TemporaryDirectory.return_value.cleanup.side_effect = OSError('cleanup')
        self.assertEqual(self.run_wrapper(), 1)

    def test_residue_fails_but_finally_cleans_exact_outer(self):
        self.os.scandir.return_value.__enter__.return_value = iter((object(),))
        self.assertEqual(self.run_wrapper(), 1)
        self.temp.TemporaryDirectory.return_value.cleanup.assert_called_once_with()
        self.os.scandir.assert_called_once_with(self.path)

    def test_absence_readback_required(self):
        self.path.lstat.side_effect = None
        self.assertEqual(self.run_wrapper(), 1)

    def test_timeout_is_124_without_retry_and_cleanup_runs(self):
        self.sub.run.side_effect = subprocess.TimeoutExpired('synthetic', 120)
        self.assertEqual(self.run_wrapper(), 124)
        self.sub.run.assert_called_once()
        self.temp.TemporaryDirectory.return_value.cleanup.assert_called_once_with()

    def test_launch_failure_still_cleans(self):
        self.sub.run.side_effect = OSError('launch')
        self.assertEqual(self.run_wrapper(), 1)
        self.temp.TemporaryDirectory.return_value.cleanup.assert_called_once_with()

    def test_constructor_failure_restores_umask(self):
        self.temp.TemporaryDirectory.side_effect = OSError('constructor')
        self.assertEqual(self.run_wrapper(), 1)
        self.sub.run.assert_not_called()

    def test_rebound_outer_never_deleted(self):
        self.identity.side_effect = [(1, 2), (1, 3), (1, 3)]
        self.assertEqual(self.run_wrapper(), 1)
        self.temp.TemporaryDirectory.return_value.cleanup.assert_not_called()

    def test_unsafe_initial_outer_never_launches_or_deletes(self):
        self.identity.side_effect = RuntimeError('unsafe')
        self.assertEqual(self.run_wrapper(), 1)
        self.sub.run.assert_not_called()
        self.temp.TemporaryDirectory.return_value.cleanup.assert_not_called()

    def test_unsupported_python_fails_before_effects(self):
        self.sys.version_info = (3, 11)
        self.assertEqual(self.ns['run_owner_native_fixture'](), 1)
        self.os.umask.assert_not_called()
        self.temp.TemporaryDirectory.assert_not_called()

    def test_identity_requires_directory_root_and_private_mode(self):
        source = textwrap.dedent(WORKFLOW_SOURCE.split('# owner-native-wrapper-begin\n', 1)[1].split('# owner-native-wrapper-end', 1)[0])
        ns = functions_only(source, {'owner_outer_identity'}, {'stat':stat})
        for mode, uid, gid in [(stat.S_IFLNK | 0o700, 0, 0), (stat.S_IFDIR | 0o755, 0, 0), (stat.S_IFDIR | 0o700, 1001, 0), (stat.S_IFDIR | 0o700, 0, 1001)]:
            self.path.lstat.side_effect = None
            self.path.lstat.return_value = types.SimpleNamespace(st_mode=mode, st_uid=uid, st_gid=gid)
            with self.assertRaises(RuntimeError):ns['owner_outer_identity'](self.path)


if __name__ == '__main__':
    unittest.main()
