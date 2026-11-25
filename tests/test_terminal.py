import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from arbiter.core import Store
from arbiter.server import permitted
from tests.test_transcripts import CONVERSATION


class TerminalTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Store(Path(tmp.name) / "state.db", cpu=2, memory_mb=1024, headroom_mb=100)
        self.addCleanup(self.store.db.close)
        self.args = dict(name="worker", kind="codex", provider="codex", cwd=tmp.name,
                         external_id=CONVERSATION, pid=os.getpid())
        self.sid = self.store.apply("register", self.args)["id"]

    def test_attaches_only_after_supervisor_reports_ready(self):
        self.assertIsNone(self.store.terminal_action(self.store.session(self.sid)))
        self.store.apply("conversation", dict(session=self.sid, provider="codex", external_id=CONVERSATION,
                                               terminal_ready=True))
        self.assertEqual(self.store.terminal_action(self.store.session(self.sid)), "attach-terminal")
        self.store.apply("terminal_input", dict(session=self.sid, text="hello"))
        self.assertTrue(self.store.inbox(self.sid)["messages"][0]["terminal_input"])
        for action in ("terminal_input", "terminal_claim", "open_terminal"):
            self.assertTrue(permitted(self.sid, action, {"session": self.sid}, self.store))

    def test_terminal_resume_claim_excludes_concurrent_continuation(self):
        self.store.apply("finish", dict(session=self.sid, safe=True))
        claim = dict(session=self.sid, pid=os.getpid())
        with self.assertRaisesRegex(ValueError, "transcript"):
            self.store.apply("terminal_claim", claim)
        with patch("arbiter.transcripts.can_resume", return_value=True):
            child = self.store.apply("terminal_claim", claim)
            self.assertEqual(child["external_id"], CONVERSATION)
            self.assertEqual(child["predecessor"], self.sid)
            with self.assertRaisesRegex(ValueError, "unavailable"):
                self.store.apply("terminal_claim", claim)
