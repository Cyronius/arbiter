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

The [CLI reference](https://code.claude.com/docs/en/cli-reference) and [streaming input documentation](https://code.claude.com/docs/en/agent-sdk/streaming-vs-single-mode) expose persistent structured input/output. Arbiter uses `claude -p --input-format stream-json --output-format stream-json --verbose --permission-prompts host` with a child-specific MCP configuration file (0600; token excluded from argv; removed on supervisor shutdown).

The installed executable answered a `control_request` with subtype `initialize` successfully, using a fresh temporary `CLAUDE_CONFIG_DIR` and no model prompt. Arbiter performs that handshake before submitting its initial user message. Input messages use type `user`; output includes system initialization, assistant content, result messages, and host control requests. Host requests are persisted on the board for explicit response. These CLI control-envelope details are version-sensitive; tests check adapter envelopes, while a full live tool/approval round trip still needs verification outside the socket-restricted development sandbox.

Incoming managed-session prompts are queued stream input. Arbiter does not claim that this interrupts an active tool or that delivery is immediate. A safe pause is confirmed only through the Arbiter MCP checkpoint tool.

Claude also has documented [cross-session messaging](https://code.claude.com/docs/en/cross-session-messaging), `ListAgents`/`SendMessage`, and a per-session local inbox socket exposed through `CLAUDE_CODE_MESSAGING_SOCKET`. The docs describe optional/required authentication using `CLAUDE_CODE_MESSAGING_TOKEN`, per-session inbound controls, and messages reaching agents between tool calls or waking idle sessions. We did not send messages to existing sessions or guess the socket's message envelope. A future native attach adapter should probe compatibility in a throwaway session, preserve inbound controls, and verify acknowledgments before using this path.

## Shared Arbiter protocol

Both providers receive the same stdio MCP tools. Arbiter's own transport is local authenticated HTTP (TCP or Unix socket); SQLite state transitions and events are atomic. Application-level command states distinguish pending, delivered-to-adapter/input, acknowledged, unanswered, cancelled, and closed. A transport write is not proof that a model read or honored it. Peer messages carry explicit sender attribution, and agents acknowledge them using the message ID.

The common lifecycle is register → reserve → grant/wait → report/message → pause request → drain → safe checkpoint → release/suspend bundle → resume request → reacquire → run → finish. MCP waiting takes place in the local tool process without repeated model turns.

Remaining integration work: validate real Codex and Claude work/tool/approval runs, verify browser rendering, investigate native attach for existing sessions, and optionally add Linux cgroups for hard limits and detached descendants. Resume is conversation/job admission, not transparent checkpoint/restore of arbitrary executable memory.
