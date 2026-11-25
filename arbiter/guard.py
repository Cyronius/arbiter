"""Classify shell commands for the session guard. Pure functions; no I/O."""
from __future__ import annotations

import re
import shlex
from .system import WINDOWS

# First words of commands that only read or inspect, and never need a turn.
READ_ONLY = {
    "ls", "ll", "cat", "head", "tail", "less", "more", "wc", "file", "stat", "du", "df", "pwd", "echo", "printf",
    "which", "type", "whereis", "command", "env", "printenv", "id", "whoami", "date", "uname", "hostname", "uptime",
    "free", "ps", "pgrep", "top", "htop", "nvidia-smi", "rocm-smi", "grep", "egrep", "fgrep", "rg", "ag", "find", "fd",
    "tree", "realpath", "readlink", "basename", "dirname", "diff", "cmp", "sort", "uniq", "cut", "tr", "jq", "yq",
    "sha256sum", "md5sum", "true", "false", "test", "[", "sleep", "cd", "export", "set", "unset", "source", ".",
    "mkdir", "touch", "cp", "mv", "rm", "ln", "chmod", "tee", "sed", "awk", "xxd", "hexdump", "strings", "nl", "column",
    "codegraph", "arbiter", "dibs",
}
READ_ONLY_GIT = {"status", "log", "diff", "show", "branch", "remote", "rev-parse", "describe", "blame", "ls-files",
                 "grep", "tag", "stash", "config", "add", "restore", "checkout", "switch", "commit", "fetch", "worktree"}
WRAPPERS = {"sudo", "nice", "ionice", "time", "nohup", "env", "timeout", "stdbuf", "exec", "command"}
SEPARATORS = re.compile(r"\|\||&&|[;|&\n]")
if WINDOWS:
    READ_ONLY.update({"get-childitem", "get-content", "get-location", "get-command", "get-item", "get-itemproperty",
                      "get-process", "select-object", "where-object", "select-string", "test-path", "resolve-path",
                      "write-output", "write-host", "set-location", "format-table", "format-list", "dir", "gci",
                      "gc", "gi", "gl", "gcm", "sls", "where", "findstr", "tasklist"})


def segments(command: str) -> list[list[str]]:
    """Split a shell command into simple commands, each as a word list (best effort)."""
    out = []
    for part in SEPARATORS.split(command):
        part = part.strip().lstrip("({").strip()
        if not part:
            continue
        try:
            if WINDOWS:
                lexer = shlex.shlex(part, posix=True)
                lexer.whitespace_split = True
                lexer.escape = ""
                words = list(lexer)
            else:
                words = shlex.split(part, comments=True)
        except ValueError:
            words = part.split()
        # Drop leading VAR=value assignments and transparent wrappers with their options.
        while words and (re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0]) or words[0] in WRAPPERS):
            head = words.pop(0)
            if head == "timeout" and words and re.match(r"^[\d.]+[smhd]?$", words[0]):
                words.pop(0)
            while words and words[0].startswith("-") and head in WRAPPERS:
                words.pop(0)
        if words:
            out.append(words)
    return out


def command_name(word):
    if WINDOWS:
        word = word.replace("\\", "/").lower()
        if word.endswith((".exe", ".cmd", ".bat", ".ps1")):
            word = word.rsplit(".", 1)[0]
    return word.rsplit("/", 1)[-1]


def self_gated(words: list[str]) -> bool:
    """`arbiter run/hold/wait ...` waits for its own turn; the guard must not hold it too."""
    return bool(words) and command_name(words[0]) in {"arbiter", "dibs"}


def read_only(command: str, extra: frozenset | set = frozenset()) -> bool:
    segs = segments(command)
    if not segs:
        return True
    for words in segs:
        name = command_name(words[0])
        if name in READ_ONLY or name in extra:
            continue
        if name == "git" and len(words) > 1 and next((w for w in words[1:] if not w.startswith("-")), "") in READ_ONLY_GIT:
            continue
        return False
    return True


def wrapped(command: str) -> bool:
    """True when every resource-using part of the command goes through arbiter itself."""
    segs = segments(command)
    return bool(segs) and all(self_gated(w) or command_name(w[0]) in READ_ONLY for w in segs)


def matched(command: str, patterns: dict) -> list[str]:
    """Configured resources whose `match` pattern this command fits."""
    return sorted(name for name, pattern in patterns.items() if pattern.search(command))
