import os
import sqlite3
import tempfile
import unittest
from argparse import Namespace
from contextlib import closing, redirect_stderr, redirect_stdout
from io import StringIO
from unittest.mock import Mock, patch

from arbiter import gaia, transcripts
from arbiter.approvals import confirmation_response
from tests.test_adapters import FakeTransport


class GaiaTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.enterContext(patch.dict(os.environ, {"GAIA_TUI_HOME": tmp.name}))
        self.enterContext(patch("arbiter.gaia.agent_binary", return_value="/bin/gaiapy"))
        self.help = self.enterContext(patch("arbiter.gaia.subprocess.run"))
        self.help.return_value.stdout = "--history-file --session-kind"
        self.enterContext(patch("arbiter.adapters.Transport", FakeTransport))
        self.enterContext(redirect_stdout(StringIO()))
        self.enterContext(redirect_stderr(StringIO()))
        self.args = Namespace(cwd=tmp.name, prompt="first\nquestion", model="local-model", resume=None)

    def agent(self):
        return gaia.Gaia(self.args, {"ARBITER_TOKEN": "private"}, None)

    def test_native_launch_and_approval_wire(self):
        agent = self.agent()
        path = gaia.history_path(agent.thread)
        self.assertEqual(agent.t.argv, ["/bin/gaiapy", "--history-file", str(path),
                                        "--session-kind", "chat", "--model", "local-model"])
        agent.start()
        self.assertIn(self.args.prompt, agent.t.sent[0]["gaia_query"])
        req = {"type": "needs_confirmation", "summary": "Edit report", "confirm_id": "first"}
        approval = Mock()
        agent.handle(req, Mock(), approval)
        approval.assert_called_once_with("first", "gaia/needs_confirmation", req)
        for allow, decision in [(False, "deny"), (True, "allow")]:
            agent.answer("first", confirmation_response({"method": "gaia/needs_confirmation"}, allow))
            self.assertEqual(agent.t.sent[-1], {"gaia_control": "tool_decision", "confirm_id": "first", "decision": decision})
        with self.assertRaises(ValueError):
            agent.answer("first", {"decision": "always"})
        with self.assertRaisesRegex(RuntimeError, "confirm_id"):
            agent.handle({"type": "needs_confirmation"}, Mock(), approval)

    def test_resume_uses_sqlite_and_rejects_missing_or_invalid_ids(self):
        for guid in ["../outside", "", "deadbeef"]:
            self.args.resume = guid
            with self.assertRaises(ValueError):
                self.agent()
        path = gaia.history_path("deadbeef")
        path.parent.mkdir()
        with closing(sqlite3.connect(path)) as db:
            db.execute("create table unrelated(value text)")
            self.assertFalse(transcripts.can_resume("gaia", self.args.resume))
            db.execute("create table turns(id integer primary key, messages text)")
        self.assertTrue(transcripts.can_resume("gaia", self.args.resume))
        agent = self.agent()
        self.assertEqual(agent.thread, self.args.resume)
        self.assertIn(str(path), agent.t.argv)
        self.assertFalse(transcripts.valid_id(agent.thread, "codex"))
        path.unlink()
        path.with_suffix(".jsonl").touch()
        self.assertFalse(transcripts.can_resume("gaia", agent.thread))

    def test_legacy_display_log_queue_and_error(self):
        self.help.return_value.stdout = "--history-file"
        agent = self.agent()
        path = gaia.history_path(agent.thread)
        self.assertEqual(agent.t.argv[:6], ["flock", "--nonblock", "--conflict-exit-code", "73", "--no-fork", str(path.with_suffix(".lock"))])
        agent.message("first question")
        agent.message("second question")
        agent.handle({"type": "final", "answer": "answer one"}, Mock(), Mock())
        self.assertFalse(agent.done)
        with self.assertRaisesRegex(RuntimeError, "server offline"):
            agent.handle({"type": "error", "detail": "server offline"}, Mock(), Mock())
        with patch("arbiter.transcripts.PAGE_MESSAGES", 2):
            last = transcripts.conversation("gaia", agent.thread)
            first = transcripts.conversation("gaia", agent.thread, last["before"])
        self.assertEqual([m["text"] for m in first["messages"] + last["messages"]],
                         ["first question", "answer one", "second question", "server offline"])
        self.assertEqual(path.with_suffix(".jsonl").stat().st_mode & 0o777, 0o600)
