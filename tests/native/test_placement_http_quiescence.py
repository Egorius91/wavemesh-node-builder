"""Root-only disposable VM: kill CLI while its panel HTTP handler is pending."""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.request import urlopen

if sys.platform != 'linux' or os.geteuid() != 0:
    raise SystemExit('Requires the owned disposable Linux VM as root')
ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('placement_http_runtime',ROOT/'agent/access_runtime.py')
runtime=importlib.util.module_from_spec(spec);spec.loader.exec_module(runtime)


def server(root,port):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def send(self,status,value):
            body=json.dumps(value).encode();self.send_response(status);self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        def do_GET(self):
            if self.path=='/ready': return self.send(200,{'success':True})
            if self.path.endswith('/panel/api/inbounds/list'):
                return self.send(200,{'success':True,'obj':[{'id':3,'enable':True,'protocol':'vless','remark':'Public'}]})
            if self.path.endswith('/panel/api/clients/list'):
                rows=json.loads((root/'clients.json').read_text()) if (root/'clients.json').exists() else []
                return self.send(200,{'success':True,'obj':rows})
            self.send(404,{'success':False})
        def do_POST(self):
            if not self.path.endswith('/panel/api/clients/add'): return self.send(404,{'success':False})
            value=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            (root/'handler-started').touch()
            deadline=time.monotonic()+60
            while not (root/'release-handler').exists():
                if time.monotonic()>deadline: return self.send(503,{'success':False})
                time.sleep(.05)
            (root/'clients.json').write_text(json.dumps([value['client']]))
            (root/'late-mutation').touch()
            self.send(200,{'success':True})
    ThreadingHTTPServer(('127.0.0.1',port),Handler).serve_forever()


def systemctl(*args):
    return subprocess.run(['/usr/bin/systemctl',*args],capture_output=True,text=True,timeout=30,check=False)


def unit():
    result=systemctl('show','x-ui.service','--property=MainPID,InvocationID,ActiveState')
    if result.returncode: raise AssertionError('Cannot read owned fixture unit')
    return dict(line.split('=',1) for line in result.stdout.splitlines())


class PendingHttpTests(unittest.TestCase):
    def test_killed_cli_pending_handler_is_ended_before_a_deny_can_ack_absence(self):
        # No pre-existing installed unit may be borrowed or overwritten.
        before=systemctl('show','x-ui.service','--property=LoadState')
        self.assertEqual(before.stdout.strip(),'LoadState=not-found')
        path=Path('/run/systemd/system/x-ui.service')
        self.assertFalse(path.exists());self.assertFalse(path.is_symlink())
        root=Path(tempfile.mkdtemp(prefix='wm-placement-native-',dir='/tmp'))
        self.assertEqual(root.resolve().parent,Path('/tmp'))
        child=None;installed=False;body=None;terminal=False
        try:
            with socket.socket() as listener:
                listener.bind(('127.0.0.1',0));port=listener.getsockname()[1]
            body=('''[Unit]
Description=Disposable placement pending-HTTP fixture
[Service]
Type=simple
KillMode=control-group
SendSIGKILL=yes
Restart=no
TimeoutStopSec=5s
ExecStart=/usr/bin/python3 -B %s --server %s %s
''' % (Path(__file__).resolve(),root,port)).encode()
            fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o644)
            with os.fdopen(fd,'wb') as out: out.write(body)
            installed=True
            self.assertEqual(systemctl('daemon-reload').returncode,0)
            self.assertEqual(systemctl('start','x-ui.service').returncode,0)
            deadline=time.monotonic()+30
            while True:
                try:
                    with urlopen(f'http://127.0.0.1:{port}/ready',timeout=1) as response:
                        self.assertEqual(response.status,200);break
                except OSError:
                    if time.monotonic()>deadline: raise
                    time.sleep(.1)
            initial=unit()
            config={'panel':{'listen_port':port,'path':'/panel-fixture/','api_auth':{'token':'synthetic_token_12345678'}},
                'server':{'domain':'entry.example.invalid'},'network':{'subscription':{'backend':'xui-native','path':'/opaque/subscription/'}}}
            request=dict(schema_version=2,access_id='access_12345678',placement_id='legacy:access_12345678',tenant_id='tenant001',
                node_id='node0001',command_id='command001',generation=1,entitlement_revision=1,plan_hash='a'*64,
                expected_version=0,desired_version=1,enabled=True,expires_at='2027-01-01T00:00:00Z',
                device_limit=0,quota_bytes='0',operation='access.provision')
            runtime.atomic_json(root/'config.json',config);runtime.atomic_json(root/'request.json',request)
            child=subprocess.Popen(['/usr/bin/python3','-B',str(ROOT/'agent/access_runtime.py'),'--request',str(root/'request.json'),
                '--config',str(root/'config.json'),'--state-root',str(root/'access'),'--output',str(root/'material.json')],
                stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            deadline=time.monotonic()+30
            while not (root/'handler-started').exists():
                self.assertIsNone(child.poll(),'CLI exited before reaching pending HTTP handler')
                if time.monotonic()>deadline: self.fail('Pending HTTP handler not observed')
                time.sleep(.05)
            child.kill();child.communicate(timeout=10);self.assertEqual(child.returncode,-9)
            fence=root/'access/access_12345678.fence'
            self.assertIs(json.loads(fence.read_text())['execution_complete'],False)
            deny=dict(request,command_id='command002',desired_version=2,enabled=False,entitlement_revision=2,
                operation='access.update_entitlements')
            with runtime.node_mutation_lock(): result=runtime.update_entitlements(deny,config,root/'access')
            self.assertFalse(result['client_exists']);self.assertFalse(result['enabled'])
            self.assertEqual(result['command_id'],'command002')
            fresh=unit();self.assertNotEqual(fresh['InvocationID'],initial['InvocationID'])
            self.assertNotEqual(fresh['MainPID'],initial['MainPID'])
            self.assertFalse(Path('/proc/'+initial['MainPID']).exists())
            (root/'release-handler').touch();time.sleep(.2)
            self.assertFalse((root/'late-mutation').exists());self.assertFalse((root/'clients.json').exists())
            self.assertIs(json.loads(fence.read_text())['execution_complete'],True)
            with runtime.node_mutation_lock(): self.assertEqual(runtime.update_entitlements(deny,config,root/'access'),result)
            self.assertEqual(unit()['InvocationID'],fresh['InvocationID'],'terminal retry must not restart the panel')
            with runtime.node_mutation_lock(), self.assertRaises(runtime.ProvisionError):
                runtime.provision(request,config,root/'access')
            print(json.dumps({'proof':'PENDING_HTTP_QUIESCENCE_PASS','runtime_sha256':hashlib.sha256((ROOT/'agent/access_runtime.py').read_bytes()).hexdigest(),
                'killed_cli_pid':child.pid,'old_invocation':initial['InvocationID'],'fresh_invocation':fresh['InvocationID'],
                'old_service_pid_absent':True,'late_mutation_absent':True,'deny_replay_no_restart':True}),flush=True)
        finally:
            if child is not None and child.poll() is None: child.kill();child.communicate(timeout=10)
            if installed:
                self.assertEqual(path.read_bytes(),body,'Fixture unit changed; preserve it for reconciliation')
                self.assertEqual(systemctl('stop','x-ui.service').returncode,0)
                self.assertEqual(unit()['ActiveState'],'inactive')
                with socket.socket() as closed:
                    closed.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
                    closed.bind(('127.0.0.1',port))
                path.unlink();self.assertEqual(systemctl('daemon-reload').returncode,0)
                terminal=True
            if terminal:
                shutil.rmtree(root)
                self.assertFalse(root.exists());self.assertFalse(path.exists())
                print('PENDING_HTTP_FIXTURE_CLEANUP_PASS',flush=True)


if __name__=='__main__':
    if len(sys.argv)==4 and sys.argv[1]=='--server': server(Path(sys.argv[2]),int(sys.argv[3]))
    else: unittest.main(verbosity=2)
