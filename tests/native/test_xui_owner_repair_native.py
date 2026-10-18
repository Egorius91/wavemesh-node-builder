#!/usr/bin/env python3
"""Prepared POSIX fixture proof. Default 77; never targets a live Node tree.

Explicit future invocation requires Linux UID0 and --require-native. It changes
ownership ONLY inside its newly created tempfile tree. It never runs Node,
systemctl, busctl, flock, network, installer, service or real repair entrypoint.
"""
import sys

if '--require-native' not in sys.argv:
    print('NOT_RUN: explicit --require-native and separately admitted Linux root fixture required')
    raise SystemExit(77)

import importlib.util
import os
import pathlib
import tempfile
import unittest
from unittest import mock

if sys.platform != 'linux' or os.geteuid() != 0:
    print('NOT_RUN: Linux UID0 required')
    raise SystemExit(77)

source=pathlib.Path(__file__).parents[2]/'operations/xui_owner_repair.py'
spec=importlib.util.spec_from_file_location('owner_fixture_module',source)
m=importlib.util.module_from_spec(spec);sys.modules[spec.name]=m;spec.loader.exec_module(m)


class MemoryJournal:
    def __init__(self):self.records=[]
    def append(self,event,**fields):self.records.append({'event':event,**fields})


class NativeFixtures(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='wavemesh-owner-fixture-')
        self.root=pathlib.Path(self.temp.name)
        self.local=self.root/'usr/local';self.home=self.local/'x-ui'
        (self.home/'bin').mkdir(parents=True)
        for path in m.MEMBERS:
            relative=path[len(m.HOME):].lstrip('/')
            actual=self.home/relative
            if path not in m.DIRECTORIES:actual.write_bytes(('synthetic public '+relative+'\n').encode())
            os.chmod(actual,0o700)
            os.chown(actual,1001,1001)
        self.excluded=self.home/'not-a-candidate.private'
        self.excluded.write_bytes(b'synthetic excluded content, never opened by operator\n')
        self.ledger_root=self.root/'ledger'
        def anchor(path,create=False,**kw):
            if path=='/usr/local':return os.open(self.local,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
            if path.startswith(str(self.ledger_root)+'/'):
                if create:pathlib.Path(path).mkdir(parents=True,mode=0o700,exist_ok=True)
                return os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
            raise AssertionError('fixture must not open a live native prefix')
        self.patch=mock.patch.object(m,'open_absolute_directory',anchor);self.patch.start()
        identities={path:(os.stat(self.home/path[len(m.HOME):].lstrip('/')).st_dev,
                          os.stat(self.home/path[len(m.HOME):].lstrip('/')).st_ino) for path in m.MEMBERS}
        self.identities=identities
        self.doc=m.NativeObjects.capture(m.TARGETS[0],'a'*32,identities,lambda:None)
    def tearDown(self):self.patch.stop();self.temp.cleanup()
    def test_real_fchown_preserves_same_bytes_mode_inode_and_exclusion(self):
        ledger=MemoryJournal();objects=m.NativeObjects(self.doc)
        try:m.apply_owner_only(self.doc,objects,ledger,lambda:None)
        finally:objects.close()
        independent=m.NativeObjects(self.doc)
        try:self.assertEqual(m.verify_objects(self.doc,independent,lambda:None),17)
        finally:independent.close()
        self.assertEqual(self.excluded.read_bytes(),b'synthetic excluded content, never opened by operator\n')
    def test_renamed_home_or_bin_cannot_validate_leaf_in_old_directory(self):
        objects=m.NativeObjects(self.doc)
        old=self.local/'old-tree'
        os.rename(self.home,old);self.home.mkdir()
        try:
            with self.assertRaises(m.Stop):objects.check(self.doc['objects'][2])
        finally:objects.close()
    def test_symlink_and_external_hardlink_refuse(self):
        path=self.home/'x-ui';before=path.read_bytes();path.unlink()
        outside=self.root/'sentinel';outside.write_bytes(before);os.chown(outside,1001,1001)
        path.symlink_to(outside)
        with self.assertRaises((OSError,m.Stop)):m.NativeObjects(self.doc)
        path.unlink();os.link(outside,path)
        with self.assertRaises(m.Stop):m.NativeObjects.capture(m.TARGETS[0],'a'*32,self.identities,lambda:None)
    def test_protected_ledger_reopens_intent_and_rejects_torn_record(self):
        binding={'target':m.TARGETS[0],'operation':'a'*32,'manifest_sha256':m.digest(m.canonical(self.doc)),
                 'operator_sha256':'c'*64,'namespace':{'fixture':True}}
        with mock.patch.object(m,'JOURNAL_ROOT',str(self.ledger_root)):
            path=str(self.ledger_root/('a'*32))
            journal=m.Journal(path,binding)
            journal.append('INTENT',path=self.doc['objects'][0]['path'],pin=self.doc['objects'][0]);journal.close()
            journal=m.Journal(path,binding);self.assertEqual(journal.records[-1]['event'],'INTENT');journal.close()
            with open(pathlib.Path(path)/'ledger.jsonl','ab') as stream:stream.write(b'{')
            with self.assertRaises(m.Stop):m.Journal(path,binding)


if __name__=='__main__':unittest.main(argv=[sys.argv[0]])
