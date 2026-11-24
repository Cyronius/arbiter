import argparse
import os
import queue
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from arbiter.adapters import Claude, Codex, Transport, ROUTINE_TOOLS
from arbiter.workers import group_alive


class FakeTransport:
    def __init__(self, argv, cwd, env, track=None):
        self.argv, self.cwd, self.env = argv, cwd, env
        self.sent = []
        self.incoming = queue.Queue()
    def send(self, message):self.sent.append(message)


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.args=argparse.Namespace(cwd='/tmp',model=None,resume=None,prompt='inspect project')
        self.env={'ARBITER_SESSION':'s_test','ARBITER_URL':'http://127.0.0.1:7483','ARBITER_TOKEN':'secret-should-not-be-in-argv'}
        self.patch=patch('arbiter.adapters.Transport',FakeTransport)
        self.patch.start()
    def tearDown(self):self.patch.stop()
    def codex(self):
        c=Codex(self.args,self.env,Path('/project/bin/arbiter'))
        c.handle({'id':1,'result':{}},lambda _:None,lambda *_:None)
        c.handle({'id':2,'result':{'thread':{'id':'thread_1'}}},lambda _:None,lambda *_:None)
        c.handle({'id':3,'result':{'turn':{'id':'turn_1'}}},lambda _:None,lambda *_:None)
        return c
    def test_codex_initialization_and_active_steering(self):
        c=self.codex()
        self.assertEqual(c.t.sent[0]['method'],'initialize')
        self.assertEqual(c.t.sent[1]['method'],'initialized')
        c.message('pause at safe boundary')
        request=c.t.sent[-1]
        self.assertEqual(request['method'],'turn/steer')
        self.assertEqual(request['params']['expectedTurnId'],'turn_1')
        self.assertNotIn(self.env['ARBITER_TOKEN'],' '.join(c.t.argv))
    def test_codex_preapproval_is_scoped_to_known_coordination_tools(self):
        import tomllib
        c=self.codex()
        overrides=[c.t.argv[i+1] for i,arg in enumerate(c.t.argv) if arg=='-c']
        config=tomllib.loads('\n'.join(overrides))
        self.assertEqual(set(config),{'mcp_servers'})
        self.assertEqual(set(config['mcp_servers']),{'arbiter'})
        self.assertEqual(set(config['mcp_servers']['arbiter']['tools']),set(ROUTINE_TOOLS))
        self.assertTrue(all(v=={'approval_mode':'approve'} for v in config['mcp_servers']['arbiter']['tools'].values()))
        self.assertNotIn('arbiter_finish',config['mcp_servers']['arbiter']['tools'])
        self.assertNotIn('default_tools_approval_mode',config['mcp_servers']['arbiter'])
    def test_codex_idle_message_starts_a_turn(self):
        c=self.codex()
        c.handle({'method':'turn/completed','params':{'turn':{'status':'completed'}}},lambda _:None,lambda *_:None)
        self.assertTrue(c.done)
        c.message('new work')
        self.assertEqual(c.t.sent[-1]['method'],'turn/start')
        self.assertFalse(c.done)
    def test_codex_input_while_starting_is_preserved(self):
        c=Codex(self.args,self.env,Path('/project/bin/arbiter'))
        c.handle({'id':1,'result':{}},lambda _:None,lambda *_:None)
        c.handle({'id':2,'result':{'thread':{'id':'thread_1'}}},lambda _:None,lambda *_:None)
        count=len(c.t.sent)
        c.message('pause requested during startup')
        self.assertEqual(len(c.t.sent),count)
        c.handle({'id':3,'result':{'turn':{'id':'turn_1'}}},lambda _:None,lambda *_:None)
        self.assertEqual(c.t.sent[-1]['method'],'turn/steer')
        self.assertIn('pause requested',c.t.sent[-1]['params']['input'][0]['text'])
    def test_codex_requests_require_explicit_response(self):
        c=self.codex();requests=[];count=len(c.t.sent)
        c.handle({'id':'approval-1','method':'item/fileChange/requestApproval','params':{'reason':'edit'}},lambda _:None,lambda *x:requests.append(x))
        self.assertEqual(len(c.t.sent),count)
        self.assertEqual(requests[0][0],'approval-1')
        c.answer('approval-1',{'decision':'decline'})
        self.assertEqual(c.t.sent[-1],{'id':'approval-1','result':{'decision':'decline'}})
    def test_codex_resume_preserves_thread_identity(self):
        self.args.resume='existing-thread'
        c=Codex(self.args,self.env,Path('/project/bin/arbiter'))
        c.handle({'id':1,'result':{}},lambda _:None,lambda *_:None)
        self.assertEqual(c.t.sent[-1]['method'],'thread/resume')
        self.assertEqual(c.t.sent[-1]['params']['threadId'],'existing-thread')
    def test_codex_failure_is_not_completion(self):
        c=self.codex()
        with self.assertRaises(RuntimeError):
            c.handle({'method':'turn/completed','params':{'turn':{'status':'failed','error':{'message':'failure'}}}},lambda _:None,lambda *_:None)
    def test_claude_stream_json_and_private_config(self):
        c=Claude(self.args,self.env,Path('/project/bin/arbiter'))
        try:
            self.assertIn('--input-format',c.t.argv)
            self.assertIn('stream-json',c.t.argv)
            self.assertNotIn('--dangerously-skip-permissions',c.t.argv)
            self.assertNotIn(self.env['ARBITER_TOKEN'],' '.join(c.t.argv))
            self.assertEqual(c.config_path.stat().st_mode & 0o777,0o600)
            self.assertEqual(c.t.sent[0]['request']['subtype'],'initialize')
            c.handle({'type':'control_response','response':{'subtype':'success','request_id':'arbiter-initialize','response':{}}},lambda _:None,lambda *_:None)
            self.assertEqual(c.t.sent[1]['message']['content'],'inspect project')
            c.handle({'type':'system','subtype':'init','session_id':'claude-1'},lambda _:None,lambda *_:None)
            c.message('status please')
            self.assertEqual(c.t.sent[-1]['session_id'],'claude-1')
            c.handle({'type':'result','subtype':'success'},lambda _:None,lambda *_:None)
            self.assertTrue(c.done)
        finally:c.config_path.unlink()
    def test_claude_error_is_not_completion(self):
        c=Claude(self.args,self.env,Path('/project/bin/arbiter'))
        try:
            with self.assertRaises(RuntimeError):
                c.handle({'type':'result','subtype':'error_during_execution','is_error':True,'errors':['bad']},lambda _:None,lambda *_:None)
        finally:c.config_path.unlink()


class RealProcessTests(unittest.TestCase):
    def test_transport_round_trip_over_real_process_pipes(self):
        t=Transport([sys.executable,'-u','-c','import sys,json; print(json.dumps(json.loads(sys.stdin.readline())),flush=True)'], '/tmp',os.environ.copy())
        t.send({'id':1,'method':'hello'})
        self.assertEqual(t.incoming.get(timeout=3),{'id':1,'method':'hello'})
        self.assertEqual(t.incoming.get(timeout=3),None)
        self.assertEqual(t.proc.wait(timeout=3),0)
        t.proc.stdin.close();t.proc.stdout.close()
    @unittest.skipUnless(Path('/proc').exists(),'Linux process-group accounting')
    def test_descendants_remain_visible_after_leader_exits(self):
        code='import subprocess,sys; subprocess.Popen([sys.executable,"-c","import time; time.sleep(.4)"])'
        p=subprocess.Popen([sys.executable,'-c',code],start_new_session=True)
        p.wait(timeout=3)
        self.assertTrue(group_alive(p.pid))
        deadline=time.time()+3
        while group_alive(p.pid) and time.time()<deadline:time.sleep(.02)
        self.assertFalse(group_alive(p.pid))
