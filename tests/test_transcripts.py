import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from arbiter.transcripts import codex_title, conversation, sample_context

CONVERSATION = "01234567-89ab-cdef-0123-456789abcdef"


def codex_row(role, text):
    content = [{"type": "input_text" if role == "user" else "output_text", "text": text}]
    return {"type": "response_item", "payload": {"type": "message", "role": role, "content": content}}


class TranscriptTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        env = patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": temp.name, "CODEX_HOME": temp.name})
        env.start()
        self.addCleanup(env.stop)

    def write(self, location, rows, tail=""):
        path = self.root / location
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(row) + "\n" for row in rows) + tail)
        return path

    def test_claude_ignores_tool_blocks_and_metadata(self):
        rows = [
            {"type": "user", "message": {"role": "user", "content": "Question"}},
            {"type": "user", "isMeta": True, "message": {"role": "user", "content": "Hook metadata"}},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "hidden"}, {"type": "text", "text": "Answer"},
                {"type": "tool_use", "input": {"command": "hidden"}}]}},
            {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "content": "hidden"}]}},
        ]
        self.write(f"projects/project/{CONVERSATION}.jsonl", rows, tail='{"incomplete":')
        page = conversation("claude", CONVERSATION)
        self.assertEqual(page, {"messages": [{"role": "user", "text": "Question"},
                                             {"role": "assistant", "text": "Answer"}], "before": None})

    def test_codex_pagination_does_not_drop_a_row_crossing_the_window(self):
        rows = [codex_row("user" if i % 2 else "assistant", f"Message {i}") for i in range(12)]
        self.write(f"sessions/2026/11/19/rollout-example-{CONVERSATION}.jsonl",
                   rows + [codex_row("developer", "hidden")])
        loaded = []
        with patch("arbiter.transcripts.PAGE_BYTES", 420), patch("arbiter.transcripts.PAGE_MESSAGES", 2):
            before = None
            for _ in range(15):
                page = conversation("codex", CONVERSATION, before)
                loaded[0:0] = page["messages"]
                before = page["before"]
                if before is None:
                    break
        self.assertIsNone(before)
        self.assertEqual([message["text"] for message in loaded], [f"Message {i}" for i in range(12)])

    def test_navigation_skips_machine_context_but_keeps_real_input(self):
        rows = [codex_row("user", "<environment_context>setup</environment_context>"),
                codex_row("user", '{"tool":"shell","sandbox_permissions":"default","command":"pwd"}'),
                codex_row("user", "Show me the result"),
                {"type": "response_item", "payload": {"type": "function_call", "name": "exec_command",
                                                       "arguments": '{"cmd":"pwd"}'}},
                codex_row("assistant", "Here it is")]
        self.write(f"sessions/2026/11/19/rollout-example-{CONVERSATION}.jsonl", rows)
        messages = conversation("codex", CONVERSATION)["messages"]
        self.assertEqual([m["role"] for m in messages], ["activity", "user", "activity", "assistant"])
        self.assertEqual(messages[2], {"role": "activity", "text": "Run command", "detail": "pwd"})

    def test_claude_mixed_content_preserves_tool_order_and_input(self):
        rows = [{"type": "user", "message": {"role": "user", "content": "<system-reminder>setup</system-reminder>Hi"}},
                {"type": "assistant", "message": {"role": "assistant", "content": [
                    {"type": "text", "text": "Looking"},
                    {"type": "tool_use", "name": "Read", "input": {"file_path": "README.md"}},
                    {"type": "text", "text": "Done"}]}}]
        self.write(f"projects/project/{CONVERSATION}.jsonl", rows)
        messages = conversation("claude", CONVERSATION)["messages"]
        self.assertEqual([m["text"] for m in messages], ["Hi", "Looking", "Read file", "Done"])

    def test_gaia_native_history_paginates_complete_turns(self):
        import sqlite3
        from contextlib import closing
        history = self.root / "history"
        history.mkdir()
        with closing(sqlite3.connect(history / "deadbeef.sqlite3")) as db, db:
            db.execute("CREATE TABLE turns (id INTEGER PRIMARY KEY, messages TEXT)")
            for i in range(3):
                turn = [{"role": "user", "content": f"question {i}"},
                        {"role": "assistant", "content": f"answer {i}"}]
                db.execute("INSERT INTO turns(messages) VALUES (?)", (json.dumps(turn),))
        with patch.dict(os.environ, {"GAIA_TUI_HOME": str(self.root)}), patch("arbiter.transcripts.PAGE_MESSAGES", 2):
            page = conversation("gaia", "deadbeef")
            self.assertEqual([m["text"] for m in page["messages"]], ["question 2", "answer 2"])
            older = conversation("gaia", "deadbeef", page["before"])
            self.assertEqual(older["messages"][0]["text"], "question 1")

    def test_path_traversal_and_symlink_are_rejected(self):
        original = self.write("outside.jsonl", [codex_row("assistant", "hidden")])
        project = self.root / "projects" / "project"
        project.mkdir(parents=True)
        self.assertIsNone(conversation("claude", "../../outside"))
        try:
            (project / f"{CONVERSATION}.jsonl").symlink_to(original)
        except OSError as exc:
            if getattr(exc, "winerror", None) != 1314:
                raise
            self.skipTest("Windows symlink creation requires Developer Mode or permission")
        self.assertIsNone(conversation("claude", CONVERSATION))
        self.assertIsNone(sample_context("claude", CONVERSATION))

    def test_codex_context_uses_latest_request_and_accepts_smaller_counts(self):
        def event(used, window):
            info = {"last_token_usage": {"input_tokens": used, "cached_input_tokens": used, "output_tokens": 30},
                    "total_token_usage": {"input_tokens": 1000000}, "model_context_window": window}
            return {"type": "event_msg", "payload": {"type": "token_count", "info": info}}

        new = json.dumps(event(25, None))
        path = self.write(f"sessions/2026/11/19/rollout-example-{CONVERSATION}.jsonl",
                          [event(800, 1000)], tail=new[:20])
        old = sample_context("codex", CONVERSATION)
        self.assertEqual(old.usage, {"input_tokens": 800, "window_tokens": 1000})
        self.assertIs(sample_context("codex", CONVERSATION, old), old)
        with path.open("a") as file:
            file.write(new[20:] + "\n")
        smaller = sample_context("codex", CONVERSATION, old)
        self.assertEqual(smaller.usage, {"input_tokens": 25})
        path.write_text(json.dumps(codex_row("user", "new conversation")) + "\n")
        self.assertIsNone(sample_context("codex", CONVERSATION, smaller).usage)

    def test_titles_prefer_a_custom_claude_title_and_follow_codex_thread_names(self):
        rows = [{"type": "ai-title", "aiTitle": "Auto  title"}, {"type": "user", "message": {"role": "user", "content": "Hi"}}]
        path = self.write(f"projects/project/{CONVERSATION}.jsonl", rows)
        first = sample_context("claude", CONVERSATION)
        self.assertEqual(first.title, "Auto title")
        with path.open("a") as file:
            file.write(json.dumps({"type": "custom-title", "customTitle": "Named by me"}) + "\n")
            file.write(json.dumps({"type": "ai-title", "aiTitle": "Later auto title"}) + "\n")
        self.assertEqual(sample_context("claude", CONVERSATION, first).title, "Named by me")
        self.write("session_index.jsonl", [{"id": CONVERSATION, "thread_name": "Old"}, {"id": CONVERSATION, "thread_name": "New name"}])
        self.assertEqual(codex_title(CONVERSATION), "New name")

    def test_claude_context_includes_cached_input_and_ignores_synthetic_records(self):
        def assistant(**usage):
            return {"type": "assistant", "message": {"role": "assistant", "usage": usage}}

        synthetic = assistant(input_tokens=0)
        synthetic["message"]["model"] = "<synthetic>"
        self.write(f"projects/project/{CONVERSATION}.jsonl", [
            assistant(input_tokens=3, cache_creation_input_tokens=10, cache_read_input_tokens=4, output_tokens=100),
            {**assistant(input_tokens=500), "isSidechain": True}, synthetic, assistant(input_tokens=True)])
        self.assertEqual(sample_context("claude", CONVERSATION).usage, {"input_tokens": 17})
