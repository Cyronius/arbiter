"""Read visible messages and context measurements from local provider logs."""

import json
import os
import re
import sqlite3
import stat
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path

from . import gaia

CONVERSATION_ID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
PAGE_BYTES = 2 * 1024 * 1024
PAGE_MESSAGES = 50


def valid_id(value, provider=None):
    pattern = gaia.SESSION_ID if provider == "gaia" else CONVERSATION_ID
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def transcript_paths(provider, conversation_id):
    if not valid_id(conversation_id, provider):
        return
    if provider == "gaia":
        yield from paths_under(gaia.history_dir(), f"{conversation_id}.jsonl")
    elif provider == "claude":
        root = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects"
        yield from paths_under(root, f"*/{conversation_id}.jsonl")
    elif provider == "codex":
        root = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        yield from paths_under(root / "sessions", f"*/*/*/rollout-*-{conversation_id}.jsonl")
        yield from paths_under(root / "archived_sessions", f"rollout-*-{conversation_id}.jsonl")


def paths_under(root, pattern):
    root = root.resolve()
    for path in root.glob(pattern):
        try:
            if not path.is_symlink() and path.resolve().is_relative_to(root):
                yield path
        except (OSError, RuntimeError):
            continue


# These provider envelopes are injected alongside the user's text.
CONTEXT_BLOCK = re.compile(
    r"<(system-reminder|environment_context|codex_internal_context|local-command-caveat|"
    r"task-notification|local-command-stdout)(?:\s[^>]*)?>.*?</\1>", re.DOTALL)
SETUP = re.compile(r"^# AGENTS\.md instructions[^\n]*\s+<INSTRUCTIONS>.*?</INSTRUCTIONS>", re.DOTALL)


def chat_message(role, value):
    if role not in ("user", "assistant") or not isinstance(value, str):
        return []
    if role == "user":
        value = CONTEXT_BLOCK.sub("", SETUP.sub("", value)).strip()
        if value.startswith(("[Arbiter peer message ", "[Arbiter command ", "[Recorded tool result:", "[check:")):
            return [{"role": "activity", "text": value.splitlines()[0][:200]}]
        if value.startswith("<command-name>"):
            command = re.search(r"<command-name>(.*?)</command-name>", value, re.DOTALL)
            if command:
                return [{"role": "activity", "text": command[1]}]
    if not value.strip():
        return []
    if value.lstrip().startswith("{"):
        try:
            body = json.loads(value)
        except (ValueError, RecursionError):
            body = None
        if isinstance(body, dict):
            if {"risk_level", "outcome", "rationale"} <= body.keys():
                return [{"role": "activity", "text": f"Approval review · {body['outcome']}", "detail": str(body["rationale"])}]
            if {"tool", "sandbox_permissions", "command"} <= body.keys():
                return [{"role": "activity", "text": "Approval requested", "detail": str(body["command"])}]
    return [{"role": role, "text": value}]


def tool_call(name, args):
    if not isinstance(name, str) or not name:
        return []
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (ValueError, RecursionError):
            args = {}
    detail = next((args[key] for key in ("cmd", "command", "file_path", "path", "pattern", "query")
                   if isinstance(args.get(key), str)), "") if isinstance(args, dict) else ""
    labels = {"exec_command": "Run command", "Bash": "Run command", "Read": "Read file", "Write": "Write file",
              "Edit": "Edit file", "Grep": "Search files", "apply_patch": "Apply patch"}
    return [{"role": "activity", "text": labels.get(name.rsplit(".", 1)[-1], name), **({"detail": detail[:4000]} if detail else {})}]


def codex_messages(row):
    item = row.get("payload")
    if row.get("type") != "response_item" or not isinstance(item, dict):
        return []
    if item.get("type") in ("function_call", "custom_tool_call"):
        return tool_call(item.get("name"), item.get("arguments"))
    role = item.get("role")
    if item.get("type") != "message" or role not in ("assistant", "user"):
        return []
    content = item.get("content")
    if isinstance(content, list):
        content = "\n".join(b["text"] for b in content if isinstance(b, dict)
                            and b.get("type") in ("input_text", "output_text") and isinstance(b.get("text"), str))
    return chat_message(role, content)


def claude_messages(row):
    origin = row.get("origin")
    if row.get("type") == "user" and isinstance(origin, dict) and origin.get("kind") == "peer" and not row.get("isSidechain"):
        # A cross-session message (from another session or the Arbiter board) starts a turn of its own.
        body = origin.get("body")
        if isinstance(body, str) and body.strip():
            name = origin.get("name") if isinstance(origin.get("name"), str) else "Another session"
            return [{"role": "peer", "text": body, "from": name[:100]}]
    if row.get("type") not in ("assistant", "user") or any(row.get(k) for k in (
            "isMeta", "isSidechain", "isCompactSummary", "isVisibleInTranscriptOnly")):
        return []
    message = row.get("message")
    if not isinstance(message, dict):
        return []
    role, blocks = message.get("role"), message.get("content")
    if not isinstance(blocks, list):
        return chat_message(role, blocks)
    result, parts = [], []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            parts.append(block["text"])
        elif block.get("type") == "tool_use":
            result.extend(chat_message(role, "\n".join(parts)))
            parts.clear()
            result.extend(tool_call(block.get("name"), block.get("input")))
        elif block.get("type") == "tool_result" and block.get("is_error"):
            result.append({"role": "activity", "text": "Tool returned an error"})
    return result + chat_message(role, "\n".join(parts))


def gaia_messages(row):
    return [message for key, role in (("user", "user"), ("error", "assistant"), ("answer", "assistant"))
            for message in chat_message(role, row.get(key))]


READERS = {"codex": codex_messages, "claude": claude_messages, "gaia": gaia_messages}


def read_page(file, provider, before):
    size = os.fstat(file.fileno()).st_size
    end = size if before is None else min(before, size)
    start = max(0, end - PAGE_BYTES)
    # Check if the window already starts on a record boundary.
    file.seek(max(0, start - 1))
    aligned = not start or file.read(1) == b"\n"
    data = file.read(end - start)
    lines = data.split(b"\n")
    # Ignore the last fragment of a log entry that might still be getting written.
    offset = start + len(data) - len(lines[-1])
    messages = []
    oldest = start
    if not aligned and len(lines) > 1:
        boundary = start + len(lines[0]) + 1
        if boundary < end:
            oldest = boundary
    for index in range(len(lines) - 2, -1, -1):
        line = lines[index]
        offset -= len(line) + 1
        if index == 0 and not aligned:
            break
        try:
            row = json.loads(line)
            batch = READERS[provider](row) if isinstance(row, dict) else []
        except (ValueError, TypeError, RecursionError):
            continue
        if batch:
            messages.extend(reversed(batch))
            if len(messages) >= PAGE_MESSAGES:
                oldest = offset
                break
    return {"messages": list(reversed(messages)), "before": oldest or None}


@contextmanager
def open_transcript(provider, conversation_id):
    for path in transcript_paths(provider, conversation_id):
        try:
            # Open only regular provider logs. Avoid following a symlink or blocking on a FIFO.
            if os.name == "nt":
                from .win32 import open_shared_file
                file = open_shared_file(path)
            else:
                file = os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb")
        except OSError:
            continue
        with file:
            if stat.S_ISREG(os.fstat(file.fileno()).st_mode):
                yield file
                return
    yield None


def can_resume(provider, guid):
    if provider == "gaia":
        return valid_id(guid, provider) and gaia.has_history(guid)
    with open_transcript(provider, guid) as file:
        return file is not None


def initial_cwd(provider, guid):
    """Recover an old relative --cwd from the matching provider's session metadata."""
    with open_transcript(provider, guid) as file:
        if file is None:
            return None
        for line in file.read(PAGE_BYTES).splitlines():
            try:
                row = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(row, dict):
                continue
            if provider == "codex" and row.get("type") == "session_meta":
                meta = row.get("payload")
                cwd = meta.get("cwd") if isinstance(meta, dict) and meta.get("id") == guid else None
            elif provider == "claude" and row.get("sessionId") == guid and not row.get("isSidechain"):
                cwd = row.get("cwd")
            else:
                continue
            if isinstance(cwd, str) and os.path.isabs(cwd):
                return cwd
    return None


def gaia_history_page(guid, before):
    if not valid_id(guid, "gaia"):
        return None
    path = gaia.history_path(guid)
    if path.is_symlink() or not path.is_file():
        return None
    result = []
    limit = before if before is not None else 2**63 - 1
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
        rows = db.execute("SELECT id, CASE WHEN length(CAST(messages AS blob)) <= ? THEN messages END "
                          "FROM turns WHERE id < ? ORDER BY id DESC LIMIT 51", (PAGE_BYTES, limit))
        oldest = None
        for index, (row_id, raw) in enumerate(rows):
            if oldest is not None and (len(result) >= PAGE_MESSAGES or index == 50):
                return {"messages": list(reversed(result)), "before": oldest}
            oldest = row_id
            if raw is None:
                result.append({"role": "activity", "text": "Turn exceeds the conversation preview limit"})
                continue
            try:
                turn = json.loads(raw)
            except (TypeError, ValueError, RecursionError):
                continue
            if not isinstance(turn, list):
                continue
            entries = []
            for message in turn:
                if not isinstance(message, dict):
                    continue
                entries.extend(chat_message(message.get("role"), message.get("content")))
                calls = message.get("tool_calls")
                for call in calls if isinstance(calls, list) else []:
                    fn = call.get("function") if isinstance(call, dict) else None
                    if isinstance(fn, dict):
                        entries.extend(tool_call(fn.get("name"), fn.get("arguments")))
            result.extend(reversed(entries))
    return {"messages": list(reversed(result)), "before": None}


def conversation(provider, conversation_id, before=None):
    try:
        with open_transcript(provider, conversation_id) as file:
            if file is not None:
                return read_page(file, provider, before)
        if provider == "gaia":
            return gaia_history_page(conversation_id, before)
    except (OSError, sqlite3.Error):
        return None


def token_count(value):
    return type(value) is int and 0 <= value <= 2**53 - 1


def context_usage(row, provider):
    if not isinstance(row, dict):
        return None
    if provider == "codex":
        payload = row.get("payload")
        if row.get("type") != "event_msg" or not isinstance(payload, dict) or payload.get("type") != "token_count":
            return None
        info = payload.get("info")
        if not isinstance(info, dict) or not isinstance(info.get("last_token_usage"), dict):
            return None
        used = info["last_token_usage"].get("input_tokens")
        if not token_count(used):
            return None
        usage = {"input_tokens": used}
        window = info.get("model_context_window")
        if token_count(window) and window:
            usage["window_tokens"] = window
        return usage
    if provider == "claude" and row.get("type") == "assistant" and not row.get("isSidechain"):
        message = row.get("message")
        if not isinstance(message, dict) or message.get("model") == "<synthetic>":
            return None
        usage = message.get("usage")
        if not isinstance(usage, dict):
            return None
        counts = (usage.get("input_tokens"), usage.get("cache_read_input_tokens", 0),
                  usage.get("cache_creation_input_tokens", 0))
        if all(token_count(n) for n in counts) and token_count(sum(counts)):
            return {"input_tokens": sum(counts)}
    return None


@dataclass
class ContextSample:
    stat: os.stat_result
    offset: int
    usage: dict | None
    titles: dict | None = None  # Claude Code: {"custom": ..., "ai": ...}

    @property
    def title(self):
        return (self.titles or {}).get("custom") or (self.titles or {}).get("ai")


# Claude Code appends these whenever a conversation is titled (/rename) or auto-titled.
TITLE_FIELDS = ((b'"customTitle"', "custom-title", "customTitle", "custom"), (b'"aiTitle"', "ai-title", "aiTitle", "ai"))


def scan_titles(lines, titles):
    """Update titles from complete transcript lines, keeping the latest of each kind."""
    for line in lines:
        for marker, kind, field, key in TITLE_FIELDS:
            if marker not in line:
                continue
            try:
                row = json.loads(line)
            except (ValueError, RecursionError):
                continue
            value = row.get(field) if isinstance(row, dict) and row.get("type") == kind else None
            if isinstance(value, str) and value.strip():
                titles[key] = " ".join(value.split())[:120]
    return titles


_codex_index = (None, {})


def codex_title(conversation_id):
    """Codex records thread names in session_index.jsonl (the last entry for an id wins)."""
    global _codex_index
    path = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "session_index.jsonl"
    try:
        st = path.stat()
        key = (st.st_ino, st.st_size, st.st_mtime_ns)
        if key != _codex_index[0]:
            names = {}
            with open(path, "rb") as file:
                for line in file:
                    try:
                        row = json.loads(line)
                    except (ValueError, RecursionError):
                        continue
                    if isinstance(row, dict) and isinstance(row.get("thread_name"), str) and row["thread_name"].strip():
                        names[row.get("id")] = " ".join(row["thread_name"].split())[:120]
            _codex_index = (key, names)
    except OSError:
        return None
    return _codex_index[1].get(conversation_id)


def sample_context(provider, conversation_id, previous=None):
    """Read new complete records, bounding work on first observation or after a large append."""
    with open_transcript(provider, conversation_id) as file:
        if file is None:
            return None
        current = os.fstat(file.fileno())
        start, usage, titles = 0, None, None
        if previous and os.path.samestat(current, previous.stat):
            if current.st_size == previous.stat.st_size and current.st_mtime_ns == previous.stat.st_mtime_ns:
                return previous
            if current.st_size > previous.stat.st_size:
                start, usage, titles = previous.offset, previous.usage, previous.titles
        start = max(start, current.st_size - PAGE_BYTES)
        file.seek(max(0, start - 1))
        aligned = start == 0 or file.read(1) == b"\n"
        lines = file.read(current.st_size - start).split(b"\n")
        offset = current.st_size - len(lines.pop())
        if not aligned and lines:
            lines.pop(0)
        marker = b'"token_count"' if provider == "codex" else b'"usage"'
        for line in reversed(lines):
            if marker not in line:
                continue
            try:
                reading = context_usage(json.loads(line), provider)
            except (ValueError, RecursionError):
                continue
            if reading is not None:
                usage = reading
                break
        if provider == "claude":
            fresh = titles is None
            titles = scan_titles(lines, dict(titles or {}))
            if fresh and not titles and start > 0:
                # First look at a long conversation: its title may predate the tail just read.
                file.seek(0)
                titles = scan_titles(iter(file.readline, b""), titles)
        return ContextSample(current, offset, usage, titles)
