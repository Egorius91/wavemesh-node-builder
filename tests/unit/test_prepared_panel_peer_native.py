"""Real Linux peer/socket/FD primitive; not full approved Producer/core proof."""
from contextlib import contextmanager
import os
from pathlib import Path
import select
import signal
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'scripts/lib'))
import prepared_execution as execution

CHILD = '''import os,select,socket,sys
from pathlib import Path
root=Path(sys.argv[1]);held=[];replacement=None
unix=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);unix.bind(str(root/'snapshot.sock'));os.chmod(root/'snapshot.sock',0o600);unix.listen()
tcp=socket.socket(socket.AF_INET,socket.SOCK_STREAM);tcp.bind(('127.0.0.1',0));tcp.listen()
print(tcp.getsockname()[1],flush=True)
try:
 while True:
  ready,_,_=select.select([tcp,unix,sys.stdin],[],[],10)
  if not ready:continue
  if tcp in ready:
   conn,_=tcp.accept();held.append(conn);print('accepted',flush=True)
  if unix in ready:
   conn,_=unix.accept();held.append(conn)
  if sys.stdin in ready:
   command=sys.stdin.readline().strip()
   if command=='quit' or not command:break
   if command=='replace':
    fd=held[0].fileno();held[0].close();replacement=os.open('/dev/null',os.O_RDONLY)
    if replacement!=fd:os.dup2(replacement,fd);os.close(replacement);replacement=fd
    print('replaced',flush=True)
   if command=='probe':
    ready,_,_=select.select([held[0]],[],[],0.1)
    print(len(held[0].recv(256,socket.MSG_PEEK)) if ready else 0,flush=True)
finally:
 for item in held:item.close()
 if replacement is not None:os.close(replacement)
 unix.close();tcp.close()
'''


@unittest.skipUnless(sys.platform=='linux' and getattr(os,'geteuid',lambda:1)()==0,'actual Linux root SO_PEERCRED/proc socket binding required')
class PreparedPanelPeerNative(unittest.TestCase):
    def line(self,process):
        ready,_,_=select.select([process.stdout],[],[],5)
        self.assertTrue(ready,'own peer fixture did not report readiness')
        return process.stdout.readline(128).decode().strip()

    @contextmanager
    def peer(self):
        with tempfile.TemporaryDirectory(prefix='wm-panel-peer-') as name:
            root=Path(name);script=root/'peer.py';script.write_text(CHILD,encoding='utf-8')
            process=subprocess.Popen([sys.executable,'-u',str(script),str(root)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,bufsize=0)
            connection=None
            try:
                port=int(self.line(process));connection=socket.socket(socket.AF_INET,socket.SOCK_STREAM);connection.settimeout(3);connection.connect(('127.0.0.1',port))
                self.assertEqual(self.line(process),'accepted')
                with mock.patch.object(execution,'DIRECTORY',root),mock.patch.object(execution,'SNAPSHOT',root/'snapshot.sock'):
                    with execution._connection() as (_,pid,_):
                        self.assertEqual(pid,process.pid)
                        producer=execution._process(pid)
                        with open(f'/proc/{pid}/exe','rb') as executable,open(sys.executable,'rb') as expected:
                            self.assertEqual(execution._hash(executable),execution._hash(expected),'fixture executable pin changed')
                            identity=os.fstat(executable.fileno())
                            yield process,connection,producer
                            if process.poll() is None:
                                self.assertEqual(execution._process(pid),producer)
                                self.assertTrue(execution._same(identity,os.stat(f'/proc/{pid}/exe')))
            finally:
                if connection is not None:connection.close()
                if process.poll() is None:
                    try:process.stdin.write(b'quit\n');process.stdin.flush()
                    except BrokenPipeError:pass  # Exit status below still exposes an unexpected crash.
                timed_out=False
                try:process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    timed_out=True;process.terminate();process.wait(timeout=5)
                finally:
                    for stream in (process.stdin,process.stdout,process.stderr):stream.close()
                self.assertFalse(timed_out,'own peer fixture failed graceful cleanup')
                self.assertIn(process.returncode,(0,-signal.SIGTERM),'own peer fixture crashed')

    def test_actual_accepted_fd_binds_same_root_peercred_process_and_exec(self):
        with self.peer() as (process,connection,producer):
            first=execution._accepted_panel_peer(connection,producer)
            self.assertEqual(first,execution._accepted_panel_peer(connection,producer))
            self.assertEqual(os.readlink(first[1]),f'socket:[{first[0]}]')
            process.stdin.write(b'probe\n');process.stdin.flush();self.assertEqual(self.line(process),'0')
            connection.sendall(b'fixture-only')
            process.stdin.write(b'probe\n');process.stdin.flush();self.assertEqual(self.line(process),str(len(b'fixture-only')))

    def test_actual_different_pid_reused_start_fd_and_exited_peer_refuse(self):
        with self.peer() as (process,connection,producer):
            for changed in (execution._process(os.getpid()),{**producer,'start_ticks':'0'}):
                with self.assertRaises(execution.ExecutionUnavailable):execution._accepted_panel_peer(connection,changed)
            process.stdin.write(b'replace\n');process.stdin.flush();self.assertEqual(self.line(process),'replaced')
            with self.assertRaises((execution.ExecutionUnavailable,OSError)):execution._accepted_panel_peer(connection,producer)
            process.terminate();process.wait(timeout=5)
            with self.assertRaises((execution.ExecutionUnavailable,OSError)):execution._accepted_panel_peer(connection,producer)


if __name__=='__main__':unittest.main()
