"""Exercise the real HTTP parser/handlers using byte streams, without opening sockets."""
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

from arbiter.core import Store
from arbiter.server import Handler, Launcher
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
        raw=body if isinstance(body, bytes) else json.dumps(body).encode() if body is not None else b""
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
    def snapshot(self,wait=None):return self.request('/api/state' if wait is None else f'/api/state?wait={wait}&timeout=1')
    def inbox(self,sid):return self.request('/api/inbox?session='+sid)
    def wait(self,sid,reservation=None):
        from arbiter.client import Client
        return Client.wait(self,sid,reservation)


class HTTPMCPTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=Store(Path(self.tmp.name)/'state.db',cpu=2,memory_mb=1024)
        self.server=SimpleNamespace(store=self.store,token='test-token',server_port=7483,unix=False,telemetry={},context_samples={},stopping=threading.Event(),launcher=Launcher())
        self.client=LocalHTTPClient(self.server)
    def tearDown(self):
        self.store.db.close();self.tmp.cleanup()
    def session(self,name='worker'):
        return self.client.action('register',name=name)['id']

    def test_context_reading_reaches_board_and_cli_for_active_session(self):
        from arbiter.server import refresh_context
        from arbiter.workers import status_text
        from tests.test_transcripts import CONVERSATION

        sid = self.client.action("register", kind="claude", name="writer", provider="claude", external_id=CONVERSATION)["id"]
        directory = Path(self.tmp.name) / "projects" / "writer"
        directory.mkdir(parents=True)
        record = {"type": "assistant", "message": {"usage": {"input_tokens": 12345}}}
        (directory / f"{CONVERSATION}.jsonl").write_text(json.dumps(record) + "\n")
        with patch.dict("os.environ", CLAUDE_CONFIG_DIR=self.tmp.name):
            refresh_context(self.server)
        state = self.client.snapshot()
        self.assertEqual(state["sessions"][sid]["context_usage"], {"input_tokens": 12345})
        self.assertIn("Context (last input): 12,345 tokens", status_text(state))
        self.client.action("finish", session=sid, safe=True)
        self.assertNotIn("context_usage", self.client.snapshot()["sessions"][sid])

    def test_operator_moves_waiters_to_the_front(self):
        from contextlib import redirect_stdout
        from arbiter.cli import main

        owner = self.session("owner")
        holder = self.client.action("reserve", session=owner, exclusive=["gpu"])
        waiters = [self.client.action("reserve", session=self.session(name), exclusive=["gpu"], priority=priority)
                   for name, priority in (("urgent", 100), ("first pick", -100), ("second pick", -100))]
        urgent, first, second = [r["id"] for r in waiters]
        request = {"action": "promote", "data": {"reservation": first}}
        token = self.store.state["sessions"][owner]["token"]
        self.assertEqual(self.client.exchange("/api/action", request, token=token)[0], 403)
        code, _, raw = self.client.exchange("/api/state", token=token)
        self.assertEqual(code, 200)
        scoped = json.loads(raw)
        self.assertNotIn("instructions", scoped["settings"])
        self.assertNotIn("presets", scoped["settings"])
        self.assertNotIn("instructions", scoped["operator"])
        self.assertEqual(self.client.exchange("/api/action", {"action": "promote", "data": {"reservation": holder["id"]}})[0], 400)
        self.client.request("/api/action", request)
        self.client.url = "http://127.0.0.1:8001"
        output = io.StringIO()
        with patch("arbiter.cli.Client", return_value=self.client), redirect_stdout(output):
            main(["promote", second])
            main([])
        status = output.getvalue()
        self.assertIn("Board: http://127.0.0.1:8001/", status)
        self.assertIn(f"1. {second}", status)
        self.assertIn(f"2. {first}", status)
        self.assertIn(f"3. {urgent}", status)
        self.client.action("release", session=owner, reservation=holder["id"])
        reservations = self.client.snapshot()["reservations"]
        self.assertEqual(reservations[second]["state"], "granted")
        self.assertEqual(reservations[first]["queue_position"], 1)
        self.assertEqual(reservations[urgent]["queue_position"], 2)

    def test_second_serve_prints_the_daemons_recorded_address(self):
        from contextlib import redirect_stdout
        from arbiter.cli import main
        from arbiter.server import lock_file

        directory = Path(self.tmp.name)
        with (directory / "daemon.lock").open("w") as lock, patch("arbiter.server.state_dir", return_value=directory):
            lock_file(lock)
            for url in ("http://127.0.0.1:8111", "unix:///tmp/test-arbiter.sock"):
                (directory / "connection.json").write_text(json.dumps({"url": url, "token": "private-test-token"}))
                output = io.StringIO()
                with redirect_stdout(output):
                    main(["serve"])
                self.assertIn(url, output.getvalue())
                self.assertNotIn("private-test-token", output.getvalue())

    def test_operator_settings_are_scoped_durable_and_can_follow_config_again(self):
        original = self.client.snapshot()["settings"]
        fields = {k: original[k] for k in ("provider", "model", "approve_inbox", "manage_resources", "preferred_phase_minutes")}
        fields.update(provider="pi", model="test-model", manage_resources=True, preferred_phase_minutes=30)
        request = {"action": "save_settings", "data": {"expected": original, "settings": fields}}
        token = self.store.state["sessions"][self.session()]["token"]
        self.assertEqual(self.client.exchange("/api/action", request, token=token)[0], 403)
        saved = self.client.request("/api/action", request)
        self.assertEqual(self.store.operator_policy()["model"], "test-model")
        self.assertTrue(saved["overridden"])
        self.assertEqual(self.client.exchange("/api/action", request)[0], 400)
        self.store.db.close()
        self.server.store = self.store = Store(Path(self.tmp.name)/"state.db")
        self.assertEqual(self.client.snapshot()["settings"], saved)
        for bad in ({"preferred_phase_minutes": True}, {"approve_inbox": 1}, {"provider": "command"}, {"command": ["custom"]}):
            with self.subTest(bad=bad), self.assertRaises(RuntimeError):
                self.client.action("save_settings", expected=saved, settings={**fields, **bad})
        self.assertEqual(self.client.action("save_settings", expected=saved, settings=None), original)
    def test_board_decision_reaches_waiting_codex_command(self):
        from contextlib import redirect_stderr
        from arbiter.adapters import run_agent

        # A stdio peer that can only finish after receiving the answer to its request.
        peer = """
import json, os, sys, threading
watchdog = threading.Timer(8, os._exit, [124])
watchdog.daemon = True
watchdog.start()
def receive():
    return json.loads(sys.stdin.readline())
def send(message):
    print(json.dumps(message), flush=True)
initialize = receive()
send({"id": initialize["id"], "result": {}})
assert receive()["method"] == "initialized"
thread = receive()
send({"id": thread["id"], "result": {"thread": {"id": "test-thread"}}})
turn = receive()
send({"id": turn["id"], "result": {"turn": {"id": "turn"}}})
send({"method": "item/commandExecution/requestApproval", "id": 3, "params": {"itemId": "cmd"}})
assert receive() == {"id": 3, "result": {"decision": "accept"}}
send({"method": "item/completed", "params": {"item": {
    "id": "cmd", "type": "commandExecution", "status": "completed", "exitCode": 0}}})
send({"method": "turn/completed", "params": {"turn": {"status": "completed"}}})
"""
        snapshot = self.client.snapshot
        def answer_from_board():
            state = snapshot()
            for aid, approval in state["approvals"].items():
                if approval["state"] == "pending":
                    self.client.action("approval_answer", session=approval["session"],
                                       approval=aid, response={"decision": "accept"})
            return state

        self.client.url = "http://127.0.0.1:7483"
        args = SimpleNamespace(provider="codex", cwd=self.tmp.name, name="test", prompt="test",
                               resume=None, model=None, once=True)
        terminal = io.StringIO()
        with patch("arbiter.adapters.Client", return_value=self.client), \
             patch("arbiter.adapters.signal.signal"), \
             patch("arbiter.adapters.codex_command", return_value=[sys.executable, "-u", "-c", peer]), \
             patch.object(self.client, "snapshot", side_effect=answer_from_board), redirect_stderr(terminal):
            run_agent(args)
        aid, = snapshot()["approvals"]
        self.assertIn(f"Sent response for {aid} to codex.", terminal.getvalue())
        self.assertIn(f"Codex command for {aid}: completed (exit 0).", terminal.getvalue())

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
    def test_board_requires_single_use_launch_secret_for_cookie(self):
        self.assertEqual(self.client.exchange('/',token=None)[0],401)
        self.assertEqual(self.client.exchange('/?k=guess',token=None)[0],401)
        link=self.client.request('/api/launch',{})['url']
        self.assertTrue(link.startswith('http://127.0.0.1:7483/?k='))
        secret=link.split('?k=')[1]
        code,head,body=self.client.exchange('/?k='+secret,token=None)
        self.assertEqual(code,200)
        self.assertIn(b'SameSite=Strict',head)
        self.assertIn(b'HttpOnly',head)
        self.assertIn(b'<div id="root"></div>',body)
        self.assertIn(b"script-src 'self'",head)
        self.assertEqual(self.client.exchange('/?k='+secret,token=None)[0],401)
        # An existing cookie keeps working without a new secret and is not reissued.
        code,head,_=self.client.exchange('/',token=None,headers={'Cookie':'arbiter=test-token'})
        self.assertEqual(code,200)
        self.assertNotIn(b'Set-Cookie',head)
        self.assertEqual(self.client.exchange('/app.js',token=None)[0],200)
    def test_launch_requires_operator_token(self):
        a=self.session()
        token=self.store.state['sessions'][a]['token']
        self.assertEqual(self.client.exchange('/api/launch',{},token=token)[0],403)
    def test_conversation_requires_operator_and_uses_registered_metadata(self):
        sid = self.client.action("register", name="editor", kind="codex")["id"]
        token = self.store.state["sessions"][sid]["token"]
        external_id = "01234567-89ab-cdef-0123-456789abcdef"
        self.client.action("conversation", session=sid, provider="codex", external_id=external_id)
        path = f"/api/conversation?session={sid}"
        self.assertEqual(self.client.exchange(path, token=None)[0], 401)
        self.assertEqual(self.client.exchange(path, token=token)[0], 403)
        self.assertEqual(self.client.exchange(path + "&before=-1")[0], 400)
        with patch("arbiter.server.transcripts.conversation", return_value={"messages": [], "before": None}) as reader:
            self.assertEqual(self.client.request(path + "&before=123"), {"messages": [], "before": None})
        reader.assert_called_once_with("codex", external_id, 123)
    def test_operator_messages_and_direct_replies(self):
        alice, bob = self.session("alice"), self.session("bob")
        token = self.store.state["sessions"][alice]["token"]
        direct = self.client.action("operator_message", to=alice, text="Update?")
        broadcast = self.client.action("operator_message", text="Hello everyone")
        self.assertIsNone(direct["from"])
        self.assertEqual(self.client.inbox(alice)["messages"], [direct, broadcast])
        self.assertEqual(self.client.inbox(bob)["messages"], [broadcast])
        reply = {"action": "message", "data": {"session": alice, "to": "arbiter", "text": "Ready", "reply_to": direct["id"]}}
        code, _, raw = self.client.exchange("/api/action", reply, token=token)
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(raw)["to"], "arbiter")
        self.assertEqual(self.client.inbox(bob)["messages"], [broadcast])
        reply["action"] = "operator_message"
        self.assertEqual(self.client.exchange("/api/action", reply, token=token)[0], 403)
    @unittest.skipIf(os.name == "nt", "Linux PAM authentication")
    def test_linux_browser_sign_in(self):
        self.server.login_lock = threading.Lock()
        self.server.next_login = 0
        headers = {"Origin": "http://127.0.0.1:7483", "Content-Type": "application/x-www-form-urlencoded"}
        body = urlencode({"username": "owner", "password": "example +&"}).encode()
        self.assertIn(b'action="/login"', self.client.exchange('/', token=None)[2])
        with patch("arbiter.server.pam.authenticate", side_effect=[False, True]) as authenticate:
            self.assertEqual(self.client.exchange('/login', body, token=None)[0], 403)
            code, head, _ = self.client.exchange('/login', body, token=None, headers=headers)
            self.assertEqual(code, 401)
            self.assertNotIn(b"Set-Cookie", head)
            self.assertEqual(self.client.exchange('/login', body, token=None, headers=headers)[0], 429)
            authenticate.assert_called_once_with("owner", "example +&")
            self.server.next_login = 0
            code, head, _ = self.client.exchange('/login', body, token=None, headers=headers)
        self.assertEqual(code, 303)
        self.assertIn(b"Location: /", head)
        self.assertIn(b"arbiter=test-token; HttpOnly; SameSite=Strict", head)
        self.assertEqual(self.client.exchange('/', token=None, headers={'Cookie': 'arbiter=test-token'})[0], 200)
    def test_session_token_is_scoped_to_its_own_session(self):
        a,b=self.session('a'),self.session('b')
        ta=self.store.state['sessions'][a]['token']
        act=lambda op,**d:self.client.exchange('/api/action',{'action':op,'data':d},token=ta)[0]
        self.assertEqual(act('report',session=a,status='ok'),200)
        self.assertEqual(act('report',session=b,status='impersonation'),403)
        self.assertEqual(act('control',session=a,action='pause'),403)
        self.assertEqual(act('register',name='clone'),403)
        req=self.client.action('approval_request',session=a,method='x/y',params={})
        self.assertEqual(act('approval_answer',session=a,approval=req['id'],response={'decision':'accept'}),403)
        self.assertEqual(self.client.snapshot()['approvals'][req['id']]['state'],'pending')
        self.assertEqual(self.client.exchange('/api/inbox?session='+a,token=ta)[0],200)
        self.assertEqual(self.client.exchange('/api/inbox?session='+b,token=ta)[0],403)
        self.assertEqual(self.client.exchange('/api/events',token=ta)[0],403)
        self.assertEqual(self.client.exchange('/api/state',token=ta)[0],200)
    def test_finished_session_token_is_rejected(self):
        a=self.session()
        token=self.store.state['sessions'][a]['token']
        self.client.action('finish',session=a,safe=True)
        self.assertEqual(self.client.exchange('/api/state',token=token)[0],401)
    def test_unexpected_errors_return_json_500_not_a_dropped_connection(self):
        a=self.session()
        with patch.object(self.store,'apply',side_effect=RuntimeError('boom')),patch('sys.stderr',io.StringIO()):
            code,_,raw=self.client.exchange('/api/action',{'action':'report','data':{'session':a,'status':'x'}})
        self.assertEqual(code,500)
        self.assertEqual(json.loads(raw),{'error':'internal error'})
    def test_malformed_fields_are_client_errors(self):
        for data in ({'name':'x','cwd':5},{'name':'x','capabilities':'no'},{'name':'x','capabilities':[1]}):
            self.assertEqual(self.client.exchange('/api/action',{'action':'register','data':data})[0],400)
        a=self.session()
        self.assertEqual(self.client.exchange('/api/action',{'action':'approval_request','data':{'session':a,'method':'m','params':'str'}})[0],400)
        self.assertEqual(self.client.exchange('/api/action',{'action':['x'],'data':{}})[0],400)
    def test_state_long_poll_returns_on_new_event(self):
        a=self.session()
        cursor=self.client.snapshot()['cursor']
        timer=threading.Timer(.2,lambda:self.client.action('report',session=a,status='wake'))
        timer.start();start=time.monotonic()
        state=self.client.snapshot(wait=cursor)
        timer.join()
        self.assertGreater(state['cursor'],cursor)
        self.assertLess(time.monotonic()-start,.9)
        start=time.monotonic()
        self.client.snapshot(wait=state['cursor'])
        self.assertGreaterEqual(time.monotonic()-start,.9)
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
