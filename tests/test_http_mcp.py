"""Exercise the real HTTP parser/handlers using byte streams, without opening sockets."""
import io
import json
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace

from arbiter.core import Store
from arbiter.server import Handler
from arbiter.mcp import execute


class Wire:
    def __init__(self, data):
        self.input=io.BytesIO(data)
        self.output=bytearray()
    def makefile(self,*args):
        return self.input
    def sendall(self,data):
        self.output.extend(data)


class LocalHTTPClient:
    def __init__(self, server):
        self.server=server
    def exchange(self,path,body=None,token="test-token",headers=None):
        raw=json.dumps(body).encode() if body is not None else b""
        h={"Host":"127.0.0.1:7483", "Connection":"close", "Content-Length":str(len(raw))}
        if token: h["Authorization"]="Bearer "+token
        h.update(headers or {})
        request=(f"{'POST' if body is not None else 'GET'} {path} HTTP/1.1\r\n"+"".join(f"{k}: {v}\r\n" for k,v in h.items())+"\r\n").encode()+raw
        wire=Wire(request)
        Handler(wire,('127.0.0.1',1234),self.server)
        head,raw=bytes(wire.output).split(b"\r\n\r\n",1)
        code=int(head.split(b" ",2)[1])
        return code,head,raw
    def request(self,path,body=None):
        code,_,raw=self.exchange(path,body)
        result=json.loads(raw)
        if code>=400:raise RuntimeError(result["error"])
        return result
    def action(self,operation,**data):
        return self.request('/api/action',{"action":operation,"data":data})
    def snapshot(self):return self.request('/api/state')
    def inbox(self,sid):return self.request('/api/inbox?session='+sid)
    def wait(self,sid,reservation=None):
        from arbiter.client import Client
        return Client.wait(self,sid,reservation)


class HTTPMCPTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=Store(Path(self.tmp.name)/'state.db',cpu=2,memory_mb=1024)
        self.server=SimpleNamespace(store=self.store,token='test-token',server_port=7483,unix=False,telemetry={},stopping=threading.Event())
        self.client=LocalHTTPClient(self.server)
    def tearDown(self):
        self.store.db.close();self.tmp.cleanup()
    def session(self,name='worker'):
        return self.client.action('register',name=name)['id']
    def test_http_auth_and_host_origin_validation(self):
        self.assertEqual(self.client.exchange('/api/state',token=None)[0],401)
        self.assertEqual(self.client.exchange('/api/state',headers={'Host':'evil.example'})[0],403)
        body={'action':'register','data':{'name':'bad'}}
        self.assertEqual(self.client.exchange('/api/action',body,headers={'Origin':'https://evil.example'})[0],403)
        self.assertEqual(self.client.exchange('/api/action',body,headers={'Origin':'http://127.0.0.1:7483'})[0],200)
    def test_http_errors_and_internal_actions(self):
        self.assertEqual(self.client.exchange('/api/events?after=oops')[0],400)
        self.assertEqual(self.client.exchange('/api/action',{'action':'tick','data':{}})[0],400)
        self.assertEqual(self.client.exchange('/missing')[0],404)
    def test_board_assets_and_cookie(self):
        code,head,body=self.client.exchange('/',token=None)
        self.assertEqual(code,200)
        self.assertIn(b'SameSite=Strict',head)
        self.assertIn(b'HttpOnly',head)
        self.assertIn(b'Message board',body)
        self.assertIn(b"script-src 'self'",head)
        self.assertEqual(self.client.exchange('/app.js',token=None)[0],200)
    def test_cookie_auth(self):
        self.assertEqual(self.client.exchange('/api/state',token=None,headers={'Cookie':'arbiter=test-token'})[0],200)
    def test_mcp_resource_pause_resume_through_http(self):
        a,b=self.session('codex'),self.session('claude')
        ra=execute(self.client,a,'arbiter_reserve',{'cpu':1,'exclusive':['repo']})
        self.client.action('control',session=a,action='pause')
        results=[]
        t=threading.Thread(target=lambda: results.append(execute(self.client,a,'arbiter_checkpoint',{'safe':True,'status':'children stopped'})))
        t.start()
        deadline=time.time()+2
        while self.client.snapshot()['sessions'][a]['state']!='paused' and time.time()<deadline:time.sleep(.01)
        rb=execute(self.client,b,'arbiter_reserve',{'cpu':1,'exclusive':['repo']})
        self.client.action('control',session=a,action='resume')
        self.assertEqual(self.client.snapshot()['sessions'][a]['state'],'waiting')
        execute(self.client,b,'arbiter_release',{'reservation':rb['id']})
        t.join(timeout=3)
        self.assertFalse(t.is_alive())
        self.assertEqual(results[0]['state'],'running')
        self.assertEqual(self.client.snapshot()['reservations'][ra['id']]['state'],'granted')
    def test_events_are_paginated_and_resumable(self):
        a=self.session()
        for n in range(205):self.client.action('report',session=a,status=str(n))
        first=self.client.request('/api/events?after=0')
        self.assertEqual(len(first),200)
        second=self.client.request('/api/events?after='+str(first[-1]['id']))
        self.assertEqual(len(second),6)
        self.assertGreater(second[0]['id'],first[-1]['id'])
    def test_pause_during_blocked_reserve_does_not_deadlock(self):
        a,b=self.session('holder'),self.session('queued')
        held=self.client.action('reserve',session=a,exclusive=['gpu'])
        results=[]
        t=threading.Thread(target=lambda:results.append(execute(self.client,b,'arbiter_reserve',{'exclusive':['gpu']})))
        t.start()
        deadline=time.time()+2
        while self.client.snapshot()['sessions'][b]['state']!='waiting' and time.time()<deadline:time.sleep(.01)
        self.client.action('control',session=b,action='pause')
        deadline=time.time()+2
        while self.client.snapshot()['sessions'][b]['state']!='paused' and time.time()<deadline:time.sleep(.01)
        self.assertEqual(self.client.snapshot()['sessions'][b]['state'],'paused')
        self.client.action('control',session=b,action='resume')
        self.client.action('release',session=a,reservation=held['id'])
        t.join(timeout=3)
        self.assertFalse(t.is_alive())
        self.assertEqual(results[0]['state'],'granted')
    def test_pause_racing_grant_returns_notice_without_releasing_ownership(self):
        a=self.session()
        action=self.client.action
        def pause_after_grant(operation,**data):
            result=action(operation,**data)
            if operation=='reserve':action('control',session=a,action='pause')
            return result
        with patch.object(self.client,'action',side_effect=pause_after_grant):
            result=execute(self.client,a,'arbiter_reserve',{'exclusive':['gpu']})
        self.assertTrue(result['pause_requested'])
        self.assertEqual(result['state'],'granted')
        self.assertEqual(self.client.snapshot()['sessions'][a]['state'],'draining')
    def test_sse_reconnect_honors_last_event_id(self):
        a=self.session()
        self.client.action('report',session=a,status='one')
        self.client.action('report',session=a,status='two')
        class StopAfterBatch:
            done=False
            def is_set(self):return self.done
            def wait(self,seconds):self.done=True
        self.server.stopping=StopAfterBatch()
        code,head,body=self.client.exchange('/events?after=0',headers={'Last-Event-ID':'2'})
        self.assertEqual(code,200)
        self.assertIn(b'text/event-stream',head)
        self.assertIn(b'id: 3',body)
        self.assertNotIn(b'id: 1\n',body)
    def test_mcp_stdio_initialization_and_tool_calls(self):
        from arbiter.mcp import run
        a=self.session('external')
        requests=[{'jsonrpc':'2.0','id':1,'method':'initialize','params':{}},
                  {'jsonrpc':'2.0','method':'notifications/initialized'},
                  {'jsonrpc':'2.0','id':2,'method':'tools/list'},
                  {'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'arbiter_report','arguments':{'status':'MCP smoke report'}}}]
        source=io.StringIO(''.join(json.dumps(r)+'\n' for r in requests));sink=io.StringIO()
        with patch('arbiter.mcp.Client',return_value=self.client),patch.dict('os.environ',{'ARBITER_SESSION':a}),patch('sys.stdin',source),patch('sys.stdout',sink):run()
        responses=[json.loads(line) for line in sink.getvalue().splitlines()]
        self.assertEqual([r['id'] for r in responses],[1,2,3])
        self.assertEqual(responses[0]['result']['serverInfo']['name'],'arbiter')
        self.assertTrue(any(t['name']=='arbiter_checkpoint' for t in responses[1]['result']['tools']))
        self.assertEqual(self.client.snapshot()['sessions'][a]['status'],'MCP smoke report')
    def test_cli_dispatches_controls_without_argument_collisions(self):
        from arbiter.cli import main
        a=self.session()
        with patch('arbiter.cli.Client',return_value=self.client),patch('sys.argv',['arbiter','update',a]),patch('sys.stdout',io.StringIO()):main()
        self.assertEqual(self.client.inbox(a)['commands'][0]['action'],'status')
    def test_cli_approve_unblocks_observed_mcp_confirmation(self):
        from arbiter.cli import main
        a=self.session()
        req=self.client.action('approval_request',session=a,method='mcpServer/elicitation/request',params={
            'mode':'form','serverName':'arbiter','message':'Allow arbiter_report?',
            'requestedSchema':{'type':'object','properties':{}}})
        with patch('arbiter.cli.Client',return_value=self.client),patch('sys.argv',['arbiter','approve',req['id']]),patch('sys.stdout',io.StringIO()):main()
        approval=self.client.snapshot()['approvals'][req['id']]
        self.assertEqual(approval['state'],'answered')
        self.assertEqual(approval['response'],{'action':'accept','content':{}})
    def test_demo_runs_complete_loop_through_http_handlers(self):
        from arbiter.workers import demo
        with patch('arbiter.workers.Client',return_value=self.client),patch('sys.stdout',io.StringIO()) as out:demo(0)
        self.assertIn('Verified:',out.getvalue())
        sessions=list(self.client.snapshot()['sessions'].values())
        self.assertEqual(len(sessions),2)
        self.assertTrue(all(s['state']=='finished' for s in sessions))
