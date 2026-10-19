"""Virtual cgroup I/O only: no host /sys, /proc, fstatfs or service call."""
import errno
import importlib.util
import pathlib
import stat
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

SOURCE=pathlib.Path(__file__).parents[2]/'operations/xui_owner_repair.py'
spec=importlib.util.spec_from_file_location('owner_cgroup_regression',SOURCE)
m=importlib.util.module_from_spec(spec);sys.modules[spec.name]=m;spec.loader.exec_module(m)
ROOT='/sys/fs/cgroup'
GROUP='/system.slice/'+m.UNITS[0]
LEAF=ROOT+GROUP


class VirtualCgroup:
    def __init__(self,scenario='empty'):
        self.scenario=scenario;self.nextfd=10;self.opened={};self.offset={};self.closed=[]
        self.read_started=False;self.stat_reads=0;self.negative_seen=False
        owner=self
        class VirtualPath:
            def __init__(self,path):self.path=str(path)
            def __str__(self):return self.path
            def __truediv__(self,name):return VirtualPath(self.path+'/'+name)
            def exists(self):return scenario not in {'missing_root','missing_parent','missing_leaf','permission_root',
                                                      'absent_then_created','negative_parent_rebind','negative_root_rebind'}
            def is_symlink(self):return scenario=='leaf_symlink' and self.path==LEAF
            def is_dir(self):return True
            def iterdir(self):
                if scenario=='descendant' and self.path==LEAF:return iter([VirtualPath(LEAF+'/child')])
                return iter([])
        self.path_class=VirtualPath
    def allocate(self,path):
        fd=self.nextfd;self.nextfd+=1
        self.opened[fd]=(path,self.read_started or self.negative_seen);self.offset[fd]=0
        return fd
    def anchor(self,path,**kw):
        if path!=ROOT:raise AssertionError('unexpected native prefix')
        if self.scenario=='missing_root':raise FileNotFoundError(errno.ENOENT,'fixture')
        if self.scenario=='permission_root':raise PermissionError(errno.EACCES,'fixture')
        if self.scenario=='root_symlink':raise OSError(errno.ELOOP,'fixture')
        return self.allocate(ROOT)
    def resolve(self,name,dir_fd=None):
        if dir_fd is not None:return self.opened[dir_fd][0]+'/'+str(name)
        return str(name)
    def missing_or_symlink(self,path):
        if path==ROOT+'/system.slice' and self.scenario=='missing_parent':raise FileNotFoundError(errno.ENOENT,'fixture')
        if path==LEAF and self.scenario=='missing_leaf':raise FileNotFoundError(errno.ENOENT,'fixture')
        if path==LEAF and self.scenario=='leaf_symlink':raise OSError(errno.ELOOP,'fixture')
        if path==LEAF+'/cgroup.events' and self.scenario=='file_symlink':raise OSError(errno.ELOOP,'fixture')
    def open(self,name,flags,mode=0o777,*,dir_fd=None):
        path=self.resolve(name,dir_fd)
        if path==LEAF and self.scenario in {'absent_then_created','negative_parent_rebind','negative_root_rebind'}:
            self.negative_seen=True;raise FileNotFoundError(errno.ENOENT,'fixture')
        self.missing_or_symlink(path)
        if self.scenario=='read_error' and path.endswith('/cgroup.procs'):raise PermissionError(errno.EACCES,'fixture')
        return self.allocate(path)
    def info(self,path,*,named=False,generation=False):
        file=path.rsplit('/',1)[-1] in {'cgroup.events','cgroup.procs','cgroup.stat'}
        number={ROOT:1,ROOT+'/system.slice':2,LEAF:3,LEAF+'/cgroup.events':4,
                LEAF+'/cgroup.procs':5,LEAF+'/cgroup.stat':6}.get(path,7)
        if self.scenario=='leaf_rebind' and named and path==LEAF and self.read_started:number=99
        if self.scenario=='root_rebind' and path==ROOT and generation:number=98
        if self.scenario=='negative_root_rebind' and path==ROOT and generation:number=98
        if self.scenario=='negative_parent_rebind' and path==ROOT+'/system.slice' and named and self.negative_seen:number=88
        if self.scenario=='file_rebind' and path==LEAF+'/cgroup.events' and named and self.read_started:number=144
        stamp=20 if self.scenario=='parent_changed' and path==ROOT+'/system.slice' and self.read_started else 10
        return SimpleNamespace(st_dev=77,st_ino=number,st_mode=(stat.S_IFREG|0o644) if file else (stat.S_IFDIR|0o755),
                               st_uid=0,st_gid=0,st_nlink=1 if file else 2,st_size=0,
                               st_mtime_ns=stamp,st_ctime_ns=stamp)
    def fstat(self,fd):
        path,generation=self.opened[fd]
        return self.info(path,generation=generation)
    def stat(self,name,*,dir_fd=None,follow_symlinks=True):
        path=self.resolve(name,dir_fd)
        if path==LEAF and self.scenario=='leaf_named_missing':raise FileNotFoundError(errno.ENOENT,'fixture')
        if path==LEAF and self.scenario in {'negative_parent_rebind','negative_root_rebind'}:raise FileNotFoundError(errno.ENOENT,'fixture')
        self.missing_or_symlink(path)
        return self.info(path,named=True,generation=self.read_started)
    def lseek(self,fd,offset,whence):self.offset[fd]=offset;return offset
    def read(self,fd,count):
        path,_=self.opened[fd]
        if self.scenario=='io_read_error' and path.endswith('/cgroup.procs'):raise OSError(errno.EIO,'fixture')
        if self.offset[fd]==0:
            if path.endswith('/cgroup.stat'):self.stat_reads+=1
            self.read_started=True
        if path.endswith('/cgroup.stat'):
            descendants=int(self.scenario=='descendant' or (self.scenario=='created_descendant' and self.stat_reads>1))
            dying=int(self.scenario=='dying_descendant')
            raw=f'nr_descendants {descendants}\nnr_dying_descendants {dying}\n'.encode()
        elif path.endswith('/cgroup.events'):
            populated=int(self.scenario in {'active','recursive_only','descendant'})
            raw=f'populated {populated}\nfrozen 0\n'.encode()
            if self.scenario=='malformed_event':raw=b'populated 2\nfrozen 0\n'
            if self.scenario=='duplicate_event':raw=b'populated 0\npopulated 0\n'
            if self.scenario=='oversize_event':raw=b'populated 0\n'+b'x'*40000
            if self.scenario=='events_changed' and self.stat_reads>1:raw=b'populated 1\nfrozen 0\n'
            if self.scenario=='short_control':raw=b''
        else:
            raw=b'27\n' if self.scenario=='active' else b''
            if self.scenario=='malformed_procs':raw=b'not-a-pid\n'
        offset=self.offset[fd];data=raw[offset:offset+count];self.offset[fd]+=len(data)
        return data
    def close(self,fd):self.closed.append(fd)
    def filesystem(self,fd):
        if self.scenario=='wrong_fs':raise m.Stop('CGROUP2_FILESYSTEM_REQUIRED')
    def patches(self):
        return [mock.patch.object(m.os,'O_NOFOLLOW',0x20000,create=True),
                mock.patch.object(m.os,'O_DIRECTORY',0x10000,create=True),
                mock.patch.object(m.os,'O_CLOEXEC',0x80000,create=True),
                mock.patch.object(m,'Path',self.path_class),
                mock.patch.object(m,'open_absolute_directory',self.anchor),
                mock.patch.object(m,'require_cgroup2_filesystem',self.filesystem,create=True),
                mock.patch.object(m.os,'open',self.open),mock.patch.object(m.os,'stat',self.stat),
                mock.patch.object(m.os,'fstat',self.fstat),mock.patch.object(m.os,'read',self.read),
                mock.patch.object(m.os,'lseek',self.lseek),mock.patch.object(m.os,'close',self.close)]


class CgroupReaderTests(unittest.TestCase):
    def members(self,scenario):
        fs=VirtualCgroup(scenario);patches=fs.patches()
        for patch in patches:patch.start()
        try:return m.NativeRuntime.__new__(m.NativeRuntime).members(GROUP)
        finally:
            for patch in reversed(patches):patch.stop()
    def test_stable_kernel_empty_is_empty(self):self.assertEqual(self.members('empty'),set())
    def test_active_direct_population_returns_baseline_members(self):self.assertEqual(self.members('active'),{27})
    def test_exact_leaf_absence_under_stable_parent_is_empty(self):self.assertEqual(self.members('missing_leaf'),set())
    def test_missing_root_does_not_mean_empty(self):
        with self.assertRaises(m.Stop):self.members('missing_root')
    def test_missing_parent_does_not_mean_empty(self):
        with self.assertRaises(m.Stop):self.members('missing_parent')
    def test_permission_error_does_not_mean_empty(self):
        with self.assertRaises(m.Stop):self.members('permission_root')
    def test_populated_recursive_only_never_means_empty(self):
        with self.assertRaises(m.Stop):self.members('recursive_only')
    def test_descendants_are_explicitly_unsupported(self):
        with self.assertRaises(m.Stop):self.members('descendant')
    def test_dying_descendants_are_not_empty(self):
        with self.assertRaises(m.Stop):self.members('dying_descendant')
    def test_durable_descendant_creation_between_brackets_stops(self):
        with self.assertRaises(m.Stop):self.members('created_descendant')
    def test_leaf_rebind_cannot_validate_old_fd(self):
        with self.assertRaises(m.Stop):self.members('leaf_rebind')
    def test_root_rebind_cannot_validate_old_anchor(self):
        with self.assertRaises(m.Stop):self.members('root_rebind')
    def test_observed_parent_metadata_change_stops(self):
        with self.assertRaises(m.Stop):self.members('parent_changed')
    def test_wrong_filesystem_cannot_spoof_kernel_files(self):
        with self.assertRaises(m.Stop):self.members('wrong_fs')
    def test_leaf_symlink_is_not_followed(self):
        with self.assertRaises(m.Stop):self.members('leaf_symlink')
    def test_control_file_symlink_is_not_followed(self):
        with self.assertRaises(m.Stop):self.members('file_symlink')
    def test_read_error_is_not_empty(self):
        with self.assertRaises(m.Stop):self.members('read_error')
    def test_actual_read_eio_is_not_empty(self):
        with self.assertRaises(m.Stop):self.members('io_read_error')
    def test_short_required_control_is_not_empty(self):
        with self.assertRaises(m.Stop):self.members('short_control')
    def test_mid_read_control_file_rebind_stops(self):
        with self.assertRaises(m.Stop):self.members('file_rebind')
    def test_named_leaf_disappearing_after_successful_open_is_not_absence(self):
        with self.assertRaises(m.Stop):self.members('leaf_named_missing')
    def test_leaf_created_after_first_negative_lookup_stops(self):
        with self.assertRaises(m.Stop):self.members('absent_then_created')
    def test_ancestor_rebinding_during_negative_leaf_check_stops(self):
        for scenario in ('negative_parent_rebind','negative_root_rebind'):
            with self.subTest(scenario=scenario),self.assertRaises(m.Stop):self.members(scenario)
    def test_root_symlink_is_not_followed(self):
        with self.assertRaises(m.Stop):self.members('root_symlink')
    def test_kernel_events_malformed_duplicate_and_oversize_stop(self):
        for scenario in ('malformed_event','duplicate_event','oversize_event','events_changed','malformed_procs'):
            with self.subTest(scenario=scenario),self.assertRaises(m.Stop):self.members(scenario)
    def test_only_two_exact_known_unit_groups_are_supported(self):
        fs=VirtualCgroup();patches=fs.patches()
        for patch in patches:patch.start()
        try:
            for group in ('/system.slice/other.service',GROUP+'/child','/system.slice/../x',GROUP+'/'):
                with self.subTest(group=group),self.assertRaises(m.Stop):
                    m.NativeRuntime.__new__(m.NativeRuntime).members(group)
        finally:
            for patch in reversed(patches):patch.stop()

    def test_success_and_refusal_close_all_opened_virtual_descriptors(self):
        for scenario in ('empty','missing_leaf','io_read_error','leaf_rebind','descendant'):
            fs=VirtualCgroup(scenario);patches=fs.patches()
            for patch in patches:patch.start()
            try:
                try:m.NativeRuntime.__new__(m.NativeRuntime).members(GROUP)
                except m.Stop:pass
            finally:
                for patch in reversed(patches):patch.stop()
            with self.subTest(scenario=scenario):self.assertCountEqual(fs.closed,list(fs.opened))


class CgroupABIGuardTests(unittest.TestCase):
    def test_unsupported_platform_stops_before_loading_or_calling_libc(self):
        with mock.patch.object(m.sys,'platform','win32'),mock.patch('ctypes.CDLL') as loader:
            with self.assertRaises(m.Stop) as outcome:m.require_cgroup2_filesystem(123)
            self.assertEqual(outcome.exception.code,'CGROUP2_ABI_UNSUPPORTED')
            loader.assert_not_called()


if __name__=='__main__':unittest.main()
