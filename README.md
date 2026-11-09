# Arbiter

A local command process for coordinating Codex sessions, Claude Code sessions, and their jobs. Arbiter schedules shared resources, asks workers to reach safe stopping points, resumes waiting work, and records decisions and conversations on a live message board.

Python 3.11+ on Linux. No runtime dependencies. Uses your existing Codex/Claude installations and authentication; it does not manage provider credentials.

## Start

```bash
cd arbiter   # your clone of this repository
./bin/arbiter serve
```

`serve` prints a one-time link to the board. Open it once; your browser keeps an HttpOnly cookie after that. For a fresh link later (for example after restarting the daemon), run `arbiter open`. Plain `http://127.0.0.1:7483` is refused without the link or cookie, so other local users cannot obtain access just by visiting it. In another terminal:

```bash
arbiter demo
arbiter agent codex 'Inspect this project and report what you find' --cwd .
arbiter agent claude 'Inspect this project and report what you find' --cwd .
```

The source entry point is `bin/arbiter`; symlink it somewhere on your PATH (for example `ln -s "$PWD/bin/arbiter" ~/.local/bin/arbiter`) to get the short command. If needed, use the absolute entry point. A standard Python package installation also exposes `arbiter`.

The demo uses no models. It exercises resource contention, an update request, direct messaging, a cooperative pause, automatic wakeup of a waiting worker, and reacquisition on resume. Demo sessions remain in the board's history; enable **Show finished** to see them.

Default admission budgets are 80% of logical CPUs and 70% of host RAM. Override these for your machine and workloads:

```bash
arbiter serve --cpu 8 --memory-mb 16000 --headroom-mb 1024
```

CPU values are scheduling weights. Fractional requests let many light jobs share capacity, and you can configure a budget greater than the core count for deliberate oversubscription. Arbiter does not set OS CPU quotas or memory limits in this version. Measured `MemAvailable` must also cover new memory requests plus the headroom reserve.

## Managed sessions

`arbiter agent` starts a foreground supervisor, configures Arbiter MCP tools for that child only, streams its output, and stays alive after a completed turn to receive messages and requests. Use `--once` to exit after the first turn. Session IDs are printed on startup. Provider conversation IDs are reported on the board; use `--resume <provider-id>` to continue one under a new Arbiter registration.

Managed Codex children preapprove the known Arbiter coordination tools: report, inbox, message acknowledgment, messaging, resource reservation/release, and safe checkpoint. These are per-tool command-line overrides for the child process; global Codex settings and other tools' approval rules are unchanged. New tools and `arbiter_finish` are not automatically trusted. An agent supervisor already running before this change must be stopped and relaunched (optionally with `--resume`) to pick up its launch-time policy.

```bash
arbiter status
arbiter update s_0123456789ab
arbiter pause s_0123456789ab --reason 'Let the tests use the machine'
arbiter resume s_0123456789ab
arbiter message s_sender 'Schema is ready' --to s_recipient --topic database
arbiter events --after 100
```

Codex receives active-turn input through its app-server protocol. Claude receives queued stream-JSON input; a request may wait until the CLI processes input. **Neither delivery nor a natural-language promise counts as a completed pause.** The worker must stop resource-using child work and call `arbiter_checkpoint`. That tool releases its resource bundle, waits without model polling, then returns only after resume and successful reacquisition.

Checkpoints are cooperative, not snapshots of process memory. An agent's MCP call can remain blocked while the coding process stays alive. Declare job memory separately from persistent agent overhead. A pause cannot reclaim RAM from a process that remains resident. Restarting a completed executable from a checkpoint requires workload-specific support; this version resumes agent conversations and queued jobs, not arbitrary native process images.

Provider permission requests appear on the board. Supported command/file requests, Claude tool requests, and MCP confirmation forms have explicit approval buttons. You can also list and answer confirmations from another terminal:

```bash
arbiter approvals
arbiter approve a_0123456789ab
arbiter decline a_0123456789ab
```

These commands approve or decline one call; they do not grant persistent trust. Forms that require input and other request types show their exact payload and can be answered with:

```bash
arbiter answer a_0123456789ab '{"decision":"decline"}'
```

The response must match that provider's request type. Peer messages never approve actions. Arbiter preserves the provider's configured permissions and does not use skip-permission flags.

A reserve tool blocked before its bundle was granted can acknowledge a pause locally: the gated work has not started. It stays blocked until resumed and admitted, avoiding a deadlock waiting for model acknowledgment inside that tool.

## Schedule commands

```bash
arbiter run --name build --cpu 4 --memory-mb 4096 --exclusive repo:my-project -- make -j4
arbiter run --name gpu-check --exclusive device:gpu0 -- python check.py
```

The wrapper waits for the entire bundle before starting, heartbeats automatically, answers status requests, and tracks the launched process group. A pause drains the current command instead of suspending it. Once that command and its children exit, the wrapper finishes and releases its reservation; waiting jobs start automatically. Run separate work units as separate commands if you need stopping points between them. Children that deliberately detach into another process group need their own registration or a future cgroup integration.

## Connect an existing coding session

Use Arbiter as a stdio MCP server. Configure this entry in that session's MCP settings:

```json
{
  "mcpServers": {
    "arbiter": {
      "command": "/absolute/path/to/arbiter/bin/arbiter",
      "args": ["mcp", "--name", "my-session"]
    }
  }
}
```

For Codex the equivalent TOML is:

```toml
[mcp_servers.arbiter]
command = "/absolute/path/to/arbiter/bin/arbiter"
args = ["mcp", "--name", "my-session"]
```

No global agent configuration is changed by installation. The MCP server registers a session and heartbeats while connected. Ask that session to report progress and check `arbiter_inbox` between work units. Externally launched sessions only receive requests when they poll the inbox; Arbiter cannot inject prompts into them. Their registration becomes stale on disconnection and retains reservations. After verifying all work stopped, reconcile it explicitly:

```bash
arbiter finish s_0123456789ab --safe --status 'Confirmed worker and child processes stopped'
```

For manual integration, `register`, `reserve`, `heartbeat`, `report`, `inbox`, `checkpoint`, `release`, and `finish` expose the same protocol through the CLI. `reserve --wait` and `checkpoint --safe --wait` block outside the model until allowed to continue. One atomic bundle per session avoids hold-and-wait deadlocks; use separate job registrations for separate work.

## Scheduling and recovery

- Requests are ordered by priority plus time waiting. Unrelated work can backfill temporarily; after 30 seconds a blocked head request protects its place against continued backfill.
- Resource ownership is exclusive for named resources and additive for CPU/memory. Names are cooperative conventions: use the same canonical name across projects when a resource is shared.
- Every grant/wait decision includes a reason. State and events commit together in SQLite WAL mode.
- Heartbeats expire after 45 seconds (measured on a monotonic clock, so wall-clock changes cannot expire sessions). A stale session's granted reservations remain held. A restarted daemon marks live registrations stale; returning heartbeats reconcile them. Supervisors, wrappers and MCP servers retry for up to 60 seconds while the daemon is down, so a daemon restart does not kill them. No stale owner is silently replaced.
- When measured memory falls below headroom, Arbiter asks one managed, preemptible memory holder to pause. It waits for a safe acknowledgment before releasing anything. After free memory stays above twice headroom for 15 seconds, automatically paused sessions are resumed and queued for reacquisition. Manual pauses require manual resume. Ordinary commands and external sessions are not automatically preempted.
- Requests have deadlines. An overdue request becomes visibly unanswered; it does not imply success or release resources. Late acknowledgments remain possible.
- This is cooperative admission, not a security boundary against other processes running as your OS user. It does not fence uncooperative filesystem/device access or force-stop workers. Sessions that finished more than 14 days ago are pruned along with their reservations, commands, approvals, messages and events (`--retention-days N`, `0` keeps everything).

## Local API and storage

The daemon listens only on `127.0.0.1`. Browser writes require the same origin; API requests require a bearer token or the board's HttpOnly, SameSite cookie. The cookie is issued only in exchange for a single-use launch secret (`arbiter serve` prints one; `arbiter open` mints another). Host checks reject DNS rebinding hosts.

There are two kinds of credential. The **operator token** (in `connection.json`) can do everything. A **session token**, returned when a session registers, can only act on that one session: report, reserve, release, checkpoint, message, acknowledge, finish and raise approval requests. It cannot answer approvals, pause or resume sessions, register new sessions or read the event stream. Managed agents (`arbiter agent`) receive only their session token, so a model cannot approve its own permission prompts through the API. This is not a sandbox: a process running as your OS user can still read `connection.json`. For real isolation, run agents as a different OS user or inside a sandbox. The local OS user is otherwise the trust boundary.

State is under `~/.local/state/arbiter` (directory mode 0700). `connection.json` (0600, created owner-only and removed on clean shutdown) contains the local address and operator token. Heartbeats are kept in memory and not written to disk; other state changes commit to SQLite in WAL mode with `synchronous=NORMAL`. The state blob carries a `schema_version`, and a daemon refuses to open state written by a newer version. Override the directory with `ARBITER_STATE_DIR`, or use `ARBITER_URL` and `ARBITER_TOKEN` for clients. Do not commit connection files. A daemon lock prevents two schedulers from owning the same state directory.

- `GET /healthz`: liveness.
- `GET /api/state`: sessions, reservations, commands, messages, approvals, capacity, telemetry, event cursor. Add `?wait=<cursor>&timeout=5` to block until a newer event exists (long poll; timeout is capped at 10 seconds).
- `POST /api/launch`: operator only; returns a single-use board link.
- `GET /api/events?after=N`: next 200 durable events.
- `GET /events?after=N`: server-sent events; reconnect supports `Last-Event-ID`.
- `GET /api/inbox?session=ID`: unacknowledged requests and messages.
- `POST /api/action`: `{"action":"report","data":{"session":"s_…","status":"…"}}`.

`arbiter serve --unix-socket /absolute/path/arbiter.sock` optionally serves HTTP over a mode-0600 Unix socket for CLI/MCP clients. Browsers need the normal TCP mode.

## Verification

```bash
python3 -m unittest discover -s tests -v
node --check arbiter/static/app.js
```

The tests cover contention, sharing, preemption handshakes, wakeups, starvation protection, stale/restarted owners, memory pressure, persistence, real subprocess pipes and process groups, HTTP authentication/origin checks, SSE replay, MCP requests, and adapter protocol handling. HTTP tests exercise the actual request parser and handlers through byte streams without opening sockets.

During initial development, the execution sandbox prohibited socket binding, so a listening daemon, browser layout, and real model turns could not be verified there. Installed Codex 0.167.0 and Claude Code 2.1.319 both completed real initialization handshakes over process pipes with fresh temporary state and no model prompt. See [protocol notes](docs/protocols.md) for exact integration boundaries.
