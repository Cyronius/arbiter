import unittest
from unittest.mock import patch

from arbiter.adapters import codex_command
from arbiter.client import Client, Unreachable


class ClientTests(unittest.TestCase):
    def client(self, patient):
        return Client(url="http://127.0.0.1:1", token="t", patient=patient)

    def test_retries_only_refused_connections_until_daemon_returns(self):
        c = self.client(patient=30)
        calls = iter([Unreachable("down"), Unreachable("down"), {"ok": True}])
        def once(*a):
            value = next(calls)
            if isinstance(value, Exception):
                raise value
            return value
        with patch.object(c, "request_once", side_effect=once), patch("time.sleep") as sleep:
            self.assertEqual(c.request("/x"), {"ok": True})
        self.assertEqual(sleep.call_count, 2)

    def test_gives_up_after_patience_and_never_retries_other_errors(self):
        with patch.object(self.client(0), "request_once", side_effect=Unreachable("down")):
            with self.assertRaises(Unreachable):
                self.client(0).request("/x")
        c = self.client(30)
        with patch.object(c, "request_once", side_effect=RuntimeError("bad request")) as once, patch("time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "bad request"):
                c.request("/x")
        self.assertEqual(once.call_count, 1)

    def test_real_connection_refused_is_classified_as_unreachable(self):
        with self.assertRaises(Unreachable):
            self.client(0).request("/healthz")

    def test_codex_mcp_tool_timeout_allows_long_waits(self):
        self.assertIn("mcp_servers.arbiter.tool_timeout_sec=86400", codex_command("/x/entry.py"))
