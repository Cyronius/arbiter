"""Read visible messages and context measurements from local provider logs."""

import json
import os
import re
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

CONVERSATION_ID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
PAGE_BYTES = 2 * 1024 * 1024
PAGE_MESSAGES = 50


def valid_id(value):
    return isinstance(value, str) and CONVERSATION_ID.fullmatch(value) is not None


def transcript_paths(provider, conversation_id):
    if not valid_id(conversation_id):
        return
    if provider == "claude":
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


def visible_message(row, provider):
    if not isinstance(row, dict):
        return None
    if provider == "codex":
        if row.get("type") != "response_item":
            return None
        message = row.get("payload")
        if not isinstance(message, dict) or message.get("type") != "message":
            return None
    else:
        if row.get("type") not in {"user", "assistant"} or any(
                row.get(key) for key in ("isMeta", "isSidechain", "isCompactSummary", "isVisibleInTranscriptOnly")):
            return None
        message = row.get("message")
        if not isinstance(message, dict):
            return None

    role = message.get("role")
    if role not in {"user", "assistant"}:
        return None
    content = message.get("content")
    if isinstance(content, list):
        block_type = "text" if provider == "claude" else ("input_text" if role == "user" else "output_text")
        content = "\n".join(block["text"] for block in content if isinstance(block, dict)
                            and block.get("type") == block_type and isinstance(block.get("text"), str))
    if isinstance(content, str) and content.strip():
        return {"role": role, "text": content}
    return None


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
            message = visible_message(json.loads(line), provider)
        except (ValueError, TypeError, RecursionError):
            continue
        if message:
            messages.append(message)
            if len(messages) == PAGE_MESSAGES:
                oldest = offset
                break
    return {"messages": list(reversed(messages)), "before": oldest or None}


@contextmanager
def open_transcript(provider, conversation_id):
    for path in transcript_paths(provider, conversation_id):
        try:
            # Open only regular provider logs. Avoid following a symlink or blocking on a FIFO.
            file = os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb")
        except OSError:
            continue
        with file:
            if stat.S_ISREG(os.fstat(file.fileno()).st_mode):
                yield file
                return
    yield None


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


def conversation(provider, conversation_id, before=None):
    try:
        with open_transcript(provider, conversation_id) as file:
            return read_page(file, provider, before) if file is not None else None
    except OSError:
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


def sample_context(provider, conversation_id, previous=None):
    """Read new complete records, bounding work on first observation or after a large append."""
    with open_transcript(provider, conversation_id) as file:
        if file is None:
            return None
        current = os.fstat(file.fileno())
        start, usage = 0, None
        if previous and os.path.samestat(current, previous.stat):
            if current.st_size == previous.stat.st_size and current.st_mtime_ns == previous.stat.st_mtime_ns:
                return previous
            if current.st_size > previous.stat.st_size:
                start, usage = previous.offset, previous.usage
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
        return ContextSample(current, offset, usage)
