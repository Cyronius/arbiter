import json
import shutil
import subprocess
import unittest
from pathlib import Path

from arbiter.approvals import confirmation_response


def report_request():
    return {"method": "mcpServer/elicitation/request", "params": {
        "serverName": "arbiter", "mode": "form",
        "message": 'Allow the arbiter MCP server to run tool "arbiter_report"?',
        "_meta": {"codex_approval_kind": "mcp_tool_call", "tool_name": "arbiter_report", "persist": ["session", "always"]},
        "requestedSchema": {"type": "object", "properties": {}}}}


class ApprovalTests(unittest.TestCase):
    def test_observed_report_confirmation_uses_elicitation_response(self):
        self.assertEqual(confirmation_response(report_request(), True), {"action": "accept", "content": {}})
        self.assertEqual(confirmation_response(report_request(), False), {"action": "decline", "content": None})
        self.assertNotIn("_meta", confirmation_response(report_request(), True))

    def test_form_with_input_is_not_silently_approved(self):
        request = report_request()
        request["params"]["requestedSchema"] = {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}
        with self.assertRaisesRegex(ValueError, "needs input"):
            confirmation_response(request, True)
        self.assertEqual(confirmation_response(request, False)["action"], "decline")

    def test_unknown_request_does_not_get_guessed_response(self):
        with self.assertRaises(ValueError):
            confirmation_response({"method": "unknown"}, True)

    @unittest.skipUnless(shutil.which("node"), "Node required for browser response check")
    def test_browser_and_cli_construct_the_same_responses(self):
        source = str(Path(__file__).resolve().parents[1] / "arbiter/static/app.js")
        fixtures = [report_request(), {"method": "item/fileChange/requestApproval"},
                    {"method": "claude/can_use_tool", "params": {"input": {"command": "pwd"}}}]
        code = """const fs=require('node:fs'),vm=require('node:vm');
const context={};vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[1],'utf8').split('let state =')[0],context);
const fixtures=JSON.parse(fs.readFileSync(0,'utf8'));
process.stdout.write(JSON.stringify(fixtures.map(a=>[true,false].map(allow=>context.ArbiterApprovals.response(a,allow)))));"""
        p = subprocess.run(["node", "-e", code, source], input=json.dumps(fixtures), capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(p.stdout), [[confirmation_response(a, allow) for allow in (True, False)] for a in fixtures])
