# Protocol investigation

Inspected locally on 2026-11-08: Codex CLI 0.167.0 and Claude Code 2.1.319. No existing conversations, inbox sockets, credentials, or global agent settings were modified. Dibs was not found in the inspected workspace or command path.

## Codex

The [official app-server protocol](https://learn.chatgpt.com/docs/app-server) covers JSON-RPC-like JSONL over stdio, notifications, client requests, and server-initiated permission/input requests. It also documents Unix socket transport; that does not establish that an arbitrary terminal or app session exposes an attachable endpoint.

Arbiter owns the launched `codex app-server --listen stdio://` process. It initializes the connection, starts/resumes a thread, starts turns, steers active turns, records agent messages and completion, and surfaces server-initiated requests for explicit responses. It does not invoke `turn/interrupt` for cooperative pauses because interrupting a tool is not proof of a safe stopping point.

The installed executable generated its JSON schemas with:

```bash
codex app-server generate-json-schema --out /tmp/arbiter-codex-schema
```

Routine coordination uses the documented per-tool `mcp_servers.arbiter.tools.<tool>.approval_mode="approve"` command-line overrides, scoped to the owned child and a fixed allowlist. See [MCP configuration](https://learn.chatgpt.com/docs/extend/mcp?surface=cli). No global config or blanket approval policy is changed. Pending non-coordination requests still go through the explicit board/CLI response flow.

Checked `ThreadStartParams`, `ThreadResumeParams`, `TurnStartParams`, and `TurnSteerParams` against adapter payloads. In particular, steering requires `expectedTurnId` and thread resume requires `threadId`. The actual executable answered `initialize` successfully over stdio using a fresh temporary `CODEX_HOME`, then exited cleanly with no thread/model turn requested. The normal protected `.codex` directory could not be initialized from this development sandbox.

## Claude Code

The [CLI reference](https://code.claude.com/docs/en/cli-reference) and [streaming input documentation](https://code.claude.com/docs/en/agent-sdk/streaming-vs-single-mode) expose persistent structured input/output. Arbiter uses `claude -p --input-format stream-json --output-format stream-json --verbose --permission-prompts host --permission-prompt-tool stdio --allowedTools <routine arbiter tools>` with a child-specific MCP configuration file (0600; token excluded from argv; removed on supervisor shutdown). Without `--permission-prompt-tool stdio`, `host` prompts were denied silently and never reached the board (found in a live run on 2026-11-17 with Claude Code 2.1.327).

The installed executable answered a `control_request` with subtype `initialize` successfully, using a fresh temporary `CLAUDE_CONFIG_DIR` and no model prompt. Arbiter performs that handshake before submitting its initial user message. Input messages use type `user`; output includes system initialization, assistant content, result messages, and host control requests. Host requests are persisted on the board for explicit response. These CLI control-envelope details are version-sensitive; tests check adapter envelopes. Live on 2026-11-18 (Claude Code 2.1.327, Codex 0.170.0): a managed Claude child and a managed Codex child each called `arbiter_report` without an approval prompt; a non-routine tool's `can_use_tool` request reached the board and was approved with `arbiter approve`.

Incoming managed-session prompts are queued stream input. Arbiter does not claim that this interrupts an active tool or that delivery is immediate. A safe pause is confirmed only through the Arbiter MCP checkpoint tool.

Claude also has documented [cross-session messaging](https://code.claude.com/docs/en/cross-session-messaging) through a per-session inbox socket. Arbiter does not use it; existing sessions are reached through lifecycle hooks instead (below).

## Existing sessions: lifecycle hooks

`arbiter install` adds one command, `arbiter hook claude|codex`, for `SessionStart`, `UserPromptSubmit`, `PreToolUse` (matcher `Bash`, timeout 300 s), `PostToolUse` (all tools), `Stop` and `SessionEnd`, to `~/.claude/settings.json` and `~/.codex/hooks.json`. Both providers use the same JSON shapes ([Claude hooks](https://code.claude.com/docs/en/hooks), [Codex hooks](https://learn.chatgpt.com/docs/hooks)):

- Identity: stdin `session_id` (equal to `CLAUDE_CODE_SESSION_ID` / `CODEX_THREAD_ID` in the agent's shell commands, which the CLI uses to act as the same session). The owner PID is `CLAUDE_PID`, or the nearest `claude`/`codex` ancestor process.
- Deny with a reason the model reads: `{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"…"}}`. Allowing prints nothing (Codex rejects a bare `allow`); context is added with `hookSpecificOutput.additionalContext` (PreToolUse, PostToolUse, UserPromptSubmit, SessionStart).
- `Stop` returns `{"decision":"block","reason":"…"}` only when there is something new to deliver (or once, for an unacknowledged pause), so it cannot loop.
- Codex caps `SessionEnd` hooks at 3 s and skips user hooks until they are trusted (`/hooks`); trust is stored per handler hash, so changing a hook's command or timeout needs re-approval.
- One process can host several conversations (Codex app-server threads; Claude `/clear`), so a new conversation never replaces another with the same PID.

Live on 2026-11-18: a headless Claude Code 2.1.327 session with these hooks attached on `SessionStart`, was refused `llama-server --version` with the `WAIT, not a refusal` message while another session held `gpu`, and ended its registration on `SessionEnd`. The hook takes about 65 ms per call and allows the call if the daemon is down.

## Shared Arbiter protocol

Both providers receive the same stdio MCP tools. Arbiter's own transport is local authenticated HTTP (TCP or Unix socket); SQLite state transitions and events are atomic. Application-level command states distinguish pending, delivered-to-adapter/input, acknowledged, unanswered, cancelled, and closed. A transport write is not proof that a model read or honored it. Peer messages carry explicit sender attribution, and agents acknowledge them using the message ID.

The common lifecycle is register → reserve → grant/wait → report/message → pause request → drain → safe checkpoint → release/suspend bundle → resume request → reacquire → run → finish. MCP waiting takes place in the local tool process without repeated model turns.

The board was rendered in headless Chromium against a live daemon (sessions, holds with ETAs, waiting reasons, SSE event stream). Remaining: a live Codex session through the installed hooks (needs `/hooks` trust first), and optionally Linux cgroups for hard limits and detached descendants. Resume is conversation/job admission, not transparent checkpoint/restore of arbitrary executable memory.

## Recovery requests and supervision

The operator posts `{"action":"recover","data":{"session":"s_predecessor"}}` to `/api/action`. For an unknown outcome, include `"confirm_unknown":true` after inspecting the surviving work. The response contains the claimed attempt’s `id`. The predecessor is the idempotency key. The scheduler commits the successor link, fresh registration, reservation and claim event together. Each attempt exposes its `original`, `predecessor`, outcome, observed exit code or signal, and reported checkpoint where available. A pending retry exposes `retry_at` in Unix seconds. The operator can post `cancel_retry` with the same session ID during backoff.

A recorded job’s private launch row is keyed by its original session ID. Manual and automatic claims load this record inside the daemon. A recovery supervisor reads its private launch ticket from a pipe after the daemon commits its PID, boot ID and process start time. A second pipe gates workload execution on the commit of its process-group identity. A crash before workload tracking can reuse the pending claim. After tracking, loss of the supervisor retains the grant until the worker group exits and leaves the outcome unknown. The service unit lets recovery supervisors survive a daemon restart. The full lineage and private record expire together under the existing retention policy.

Run `python -m unittest discover -q` for the state, admission, identity, privacy and protocol checks. On Linux with Chromium and ChromeDriver installed, `python -m tests.recovery_acceptance` exercises an isolated loopback daemon, browser, migrated conversation fixtures and temporary jobs with the operator model off. Allow about three minutes for the real retry delays. The check uses fixture provider executables and a temporary browser profile.
