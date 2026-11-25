# Arbiter

A local scheduler and message board for the Claude Code sessions, Codex sessions and jobs sharing one machine. Sessions take turns with shared resources (GPU, NPU, network, ports, test databases, disk, the whole machine for benchmarks), get asked to reach safe stopping points, resume when it is their turn, and talk to each other on a live board.

Python 3.11+ on Windows and Linux. No runtime dependencies. Uses your existing Codex/Claude installations and authentication.

## Install

### Windows

Run these commands in PowerShell from your clone.

```powershell
python -m pip install .
arbiter install
arbiter open
```

If PowerShell can't find `arbiter`, add your Python installation's `Scripts` directory to your user `PATH`. You can also run the CLI with `python -m arbiter`.

`arbiter install` creates a Task Scheduler task for your Windows account that starts the daemon and its tray icon at sign-in. It adds the Claude Code and Codex hooks and skills for that account. Approve the Codex hooks once with `/hooks`. The default config is `%LOCALAPPDATA%\Arbiter\config.toml`, and the daemon keeps its state in `%LOCALAPPDATA%\Arbiter\state`.

Click the Arbiter icon in the Windows notification area to open the board with a one-time sign-in link. Its right-click menu also opens **Settings...** or closes the icon with **Exit tray** while the daemon keeps running. If Windows puts the icon in the overflow area, look under the arrow beside the clock. Run `arbiter tray` to show the icon for a daemon that is already running, or `arbiter open` to open the board from PowerShell.

Use `arbiter install --no-service` and then `arbiter serve --tray` to run the daemon in PowerShell. `arbiter uninstall` removes the scheduled task, hooks, and skills, and keeps your state and config.

### Linux

```bash
cd arbiter   # your clone of this repository
./bin/arbiter install
```

This:

- links `~/.local/bin/arbiter` to `bin/arbiter`;
- writes a starter config at `~/.config/arbiter/config.toml` if there is none;
- installs and starts the `arbiter.service` systemd user service (the daemon);
- adds hooks to `~/.claude/settings.json` and `~/.codex/hooks.json`, so **every existing Claude Code and Codex session** attaches to Arbiter (see below). Existing hook entries are kept, and a timestamped backup is written first;
- installs an `arbiter` skill for Claude Code (`~/.claude/skills`) and Codex (`~/.codex/skills`).

Codex skips hooks it has not been told to trust: approve them once with `/hooks` in Codex. Sessions pick up new hooks when they start or resume. `arbiter install --no-service`, `--no-hooks` and `--no-skills` skip parts; `arbiter uninstall` removes all of it again (state and config stay).

Open [the board](http://127.0.0.1:7483/) and sign in with the Linux account that runs Arbiter. On Omarchy, Arbiter verifies the password through Linux PAM. The browser keeps an HttpOnly session cookie. `arbiter open` also opens the board using a one-time link. To run the daemon in a terminal instead of the service: `arbiter serve`.

Use **Grouping** in Sessions to switch between the flat list, Roster by provider, and Flow by state. Keyword search and quick filters work with every grouping. **Resource wait** selects sessions with queued reservations. **Needs approval** selects sessions with pending approvals, and hooked sessions waiting on a permission prompt or input dialog in their own terminal.

Run `arbiter` to see current status and the board URL. Running `arbiter serve` again prints the existing daemon's address.

The Sessions tab and `arbiter status` show the last measured input size for active Codex and Claude sessions with local usage records. Arbiter samples the logs every 15 seconds. The count includes cached input and can drop after compaction. When the provider reports the context window size, Arbiter also shows the percentage used. Scripts can read `sessions[id].context_usage` from `arbiter status --json`.

In **Reservations**, select **Run next** on a waiting request to move it to the front. From the terminal, use `arbiter promote <reservation-id>` with the ID shown in `arbiter status`. The most recent selection goes first as soon as its resources are available. Jobs using independent resources can still run.

Sessions, Reservations, Approvals, Resources, Messages, and Operator decisions each have their own tab. On small screens, use the **Board area** selector. Groupings and tabs can be bookmarked, for example `?group=flow` or `?tab=reservations`.

The board is built with React and TypeScript. After changing its source, rebuild the bundled JavaScript served by the daemon:

```sh
npm ci --prefix frontend
npm run build --prefix frontend
```

The Python package includes the bundle, so running Arbiter does not require Node.js.

## Existing sessions

With the hooks installed, each Claude Code or Codex conversation is an Arbiter session, named after its directory (`arbiter whoami` prints it). Nothing has to be launched through Arbiter.

- **Liveness.** The session lives exactly as long as its Claude Code/Codex process, and ends on `SessionEnd`. No heartbeats are needed.
- **The guard.** Before each shell command, the `PreToolUse` hook checks the command: is another session measuring? does another session hold a resource whose `match` pattern this command fits? is the machine under memory or CPU pressure (unless this session already holds resources)? If so, the command is held inside the hook for up to `wait_inline_s` (240 s); if it is still blocked the agent gets `WAIT, not a refusal: gpu is held by … until ~14:20` and an `arbiter wait gpu` command to run in the background before retrying. Light commands (`ls`, `git status`, `grep`, …) and anything already wrapped in `arbiter` pass straight through. An unwrapped command that uses a configured resource gets a one-line nudge to wrap it next time.
- **Delivery.** Pause and status requests and messages from other sessions are injected into the conversation after tool calls (`PostToolUse`), on the next prompt, at session start, and at `Stop` (which keeps the agent going once to handle them). Messages are acknowledged when delivered.
- **Pauses.** While a pause is requested, new non-trivial commands are refused with instructions to stop resource-using work and run `arbiter checkpoint --safe '…'`; after the checkpoint they are held until the session is resumed.
- **Needs you.** The `PermissionRequest` hook (and Claude's `Notification` hook for MCP input dialogs) marks the session on the board and in `arbiter status` while a dialog waits in its terminal. Arbiter only observes: the hook prints nothing and the provider shows its dialog unchanged. The mark clears when that tool call finishes, at the next prompt or `Stop`, when the session ends, or after an hour, since an interrupted dialog fires no hook.
- **Fail-open.** If the daemon is stopped or unreachable, the hook prints a note on stderr and allows the call.

Inside a session, CLI commands act as that session automatically: `arbiter report`, `arbiter message`, `arbiter checkpoint`, `arbiter inbox`. The `arbiter mcp` server, when configured in a Claude Code/Codex session, also attaches to the same session.

## Taking turns

```bash
arbiter run gpu --label 'one training epoch' --eta 8m -- python train.py --epochs 1
arbiter run gpu npu -- python pipeline.py
arbiter run port-3000 -- npm run dev
arbiter run --cpu 4 --memory-mb 8000 build -- make -j4
```

Check `arbiter status` for the preferred reservation length (ten minutes by default). Aim for one checkpoint per reservation, release, and queue the next phase separately. Set a different ETA when the work needs it.

`run` waits for the whole bundle, runs the command in a Windows Job Object or Linux process group, heartbeats, answers status requests, and releases when the command and all its children exit. If the wrapper is killed, Arbiter watches the workload and releases its reservation once it exits. A pause request lets the current command finish.

On Windows, Ctrl+C in the wrapper and `--yield-signal` send a Ctrl+Break event to console jobs. Jobs running without a console can be terminated as a group. Use the yield file for portable cooperative checkpoints. Run PowerShell scripts with `-- powershell.exe -File .\job.ps1` or `-- pwsh.exe -File .\job.ps1`. The CLI can run npm shims such as `npm` and `codex` directly through Node.js. For other batch files, use an explicit `cmd.exe` command. On Linux, the wrapper forwards termination signals to the command's process group.

| You want to | Run |
|---|---|
| Keep resources across several commands | `arbiter hold gpu --eta 10m --label 'what'`, then `arbiter release gpu` |
| Keep them while a server runs | `arbiter hold gpu --eta 2h --pid $SERVER_PID` |
| Push back your ETA | `arbiter extend gpu --eta 20m` |
| Get a benchmark number you can trust | `arbiter run --measure -- python bench.py` (prints CLEAN or NOISY) |
| Keep the whole machine for an A/B series | `arbiter hold --measure --eta 10m` … `arbiter release --measure` |
| Wait until something is free | `arbiter wait gpu` (or `--measure`, `--pressure`, `--disk 40`, `--resumed`) |
| Reserve disk for a big download | `arbiter disk claim 60 --eta 2h --path ~/models/qwen3`, then `arbiter disk release` |
| Check disk first | `arbiter disk check 60 --path ~/models` |
| Run a long job that steps aside | `arbiter run gpu --preemptible --requeue -- python train.py` |
| See who holds what | `arbiter status` (`--json` for everything) |
| Free someone else's stuck hold | `arbiter release gpu --force` |

A resource is any name. Holds and runs started inside a Claude Code/Codex session belong to that session: a hold covers the session's own commands (its `arbiter run gpu` calls pass straight through), while two runs of the same session still take turns. A hold without `--pid` lives by its ETA: it is reported stale after the ETA plus a grace period (half the duration, 10–60 minutes) and released then, unless extended. It also ends when its conversation ends.

`--measure` takes the whole machine: it waits until no other reservation is held, keeps everything else waiting (and the guard holds other sessions' commands), waits up to two minutes for the machine to be quiet, runs the command, and prints `CLEAN` or `NOISY: <pid> <name> (up to N% of a core)` — a process outside the run using more than `noise_core_pct` of a core makes it noisy.

`--preemptible`: when another session waits for the job's resources, Arbiter asks the job to yield, and the wrapper creates the file named by the `ARBITER_YIELD_FILE` environment variable. `--yield-signal` also signals the command to stop. With `--requeue`, a command that then exits 75 hands its resources over and is rerun when it is its turn again. Without `--preemptible`, nothing is ever preempted for exclusive resources.

Disk claims count against the filesystem's free space until written: a claim of 60 GB of which 45 GB has landed in its path still reserves 15 GB.

## Configuration

The config file is optional and reloads when it changes. The board reports invalid edits. Set `ARBITER_CONFIG` to override the platform's default config path.

```toml
[resource.gpu]
match = '\b(?:ollama|llama-server|llama-bench|vllm)\b'   # the guard catches unwrapped use

[resource.network]
capacity = 2                                             # two holders at once

[guard]
wait_inline_s = 240     # hold a blocked command this long in the hook before saying WAIT
pressure = true         # hold new commands under memory pressure or when CPU is this busy:
cpu_busy_pct = 95
read_only = []          # extra command names the guard never holds

[measure]
noise_core_pct = 50
quiet_pct = 15          # whole-machine CPU % that counts as quiet
quiet_timeout_s = 120
```

`arbiter serve --cpu 8 --memory-mb 16000 --headroom-mb 1024` sets admission budgets (defaults: 80% of logical CPUs, 70% of RAM, 512 MiB headroom). CPU values are scheduling weights; Arbiter does not set OS quotas.

## Managed sessions

`arbiter agent` starts a foreground supervisor that owns a Codex, Claude or GAIA child, forwards its output, and stays alive after a completed turn to relay messages and requests. Use `--once` to exit after the first turn, `--resume <provider-id>` to continue a conversation.

```bash
arbiter agent codex 'Inspect this project and report what you find' --cwd .
arbiter agent claude 'Inspect this project and report what you find' --cwd .
arbiter agent gaia 'Inspect this project and report what you find' --cwd . --once
```

GAIA uses its Python flagship agent (`gaiapy` or `gaia-agent`) and the Arbiter CLI for coordination. Tool confirmations appear on the board. Peer messages and pause requests arrive as follow-up turns. The supervisor prints the GAIA conversation ID. Pass the full ID to continue in the same SQLite history:

```bash
arbiter agent gaia 'Continue from the last completed step' --resume <gaia-conversation-id>
```

GAIA stores these conversations under `~/.gaia/tui/history/`. Builds with `--session-kind` also list them in the GAIA terminal's `/sessions` picker. Close an open conversation before resuming it elsewhere. If you override `GAIA_TUI_HOME`, use the same directory for the supervisor and daemon so the board can find its transcripts and recover them.

For Codex and Claude, the routine coordination tools (report, inbox, acknowledge, message, reserve, release, checkpoint) are preapproved for the child only: per-tool overrides for Codex, `--allowedTools` for Claude. `arbiter_finish` and every other tool still ask. Claude's permission prompts reach the board through `--permission-prompt-tool stdio`; answer them there or with:

```bash
arbiter approvals
arbiter approve a_0123456789ab
arbiter decline a_0123456789ab
arbiter answer a_0123456789ab '{"decision":"decline"}'
```

These approve or decline one call; they never grant persistent trust. A request closes without an answer when nothing can deliver one: **cancelled** when the provider withdraws it (Codex `serverRequest/resolved`, Claude `control_cancel_request`) or the session ends, **expired** when the supervisor exits while its commands still run. Answering a closed request fails with the reason. Peer messages never approve actions. A managed child gets a token scoped to its own session (and to job/hold sessions it starts), so a model cannot approve its own prompts through the API.

Checkpoints are cooperative, not snapshots of process memory: the worker must stop resource-using child work and call `arbiter_checkpoint`, which releases its bundle, waits without model polling, and returns after resume and reacquisition.

## Recover a job or conversation

On the Sessions board, choose **Rerun command** for a recorded shell job or **Resume conversation** for a managed Claude, Codex or GAIA session. Both actions work with the operator model off. Use `arbiter rerun-command SESSION_ID` or `arbiter resume-conversation SESSION_ID` from the CLI. The shorthand `arbiter recover SESSION_ID` selects the action from the stored session kind. Each returns the new attempt ID. Repeating the request against the same predecessor returns the same ID, including after a daemon restart. To recover again, select the latest attempt. The original board row links to it and shows its queue reason or outcome.

Use `arbiter run --record` to save an exact argument vector, working directory, resource request and execution options in Arbiter’s owner-only SQLite database. The board and event feed show the public name and label. Choose labels suitable for sharing. A replay runs unattended in the original directory with the daemon’s environment and fresh session credentials. Put credential lookup and environment setup in a named script. Write output and checkpoints to files that the next invocation can read.

This example deliberately exits after publishing its first shard. The rerun skips that shard and completes the second one.

```bash
workdir=$(mktemp -d)
cd "$workdir"
cat > shards.sh <<'SH'
#!/bin/sh
set -eu
for shard in 1 2; do
    [ -f "$shard.done" ] && continue
    printf 'output for shard %s\n' "$shard" > "$shard.tmp"
    mv "$shard.tmp" "$shard.out"
    touch "$shard.done"
    arbiter report --session "$ARBITER_JOB" --checkpoint "$shard.done" "Published shard $shard"
    if [ "$shard" = 1 ]; then exit 1; fi
done
SH
arbiter run --record --label 'Publish shards' scratch -- sh ./shards.sh 2>first-run.log
job=$(awk '$1 == "arbiter:" && $2 == "session" { print $3; exit }' first-run.log)
arbiter rerun-command "$job"
```

After a wrapper dies, Arbiter keeps its reservation while tracked work survives. Once the worker exits, inspect the output and checkpoint files. For an unknown outcome the board asks for confirmation, or use `arbiter recover SESSION_ID --confirm-unknown`. An older shell job needs a recorded launch to rerun. A managed conversation needs its provider GUID, readable local transcript and original directory. Recovery opens one new turn in that conversation. Pi operator calls are ephemeral. A persistent Pi session can be opened separately with `pi --session FULL_ID`.

For repeatable commands, the operator can use `arbiter run --auto-retry` at launch. This records the command and allows two automatic retries across its attempt history. After an observed failure, the delays are 30 seconds and then two minutes. When a delay expires, normal scheduler admission applies. Use **Cancel retry** or `arbiter cancel-retry SESSION_ID` during backoff. Press Ctrl+C in the wrapper to stop a running job deliberately. Deliberate stops, unknown outcomes and confirmed OOM kills require a manual decision. On Linux, the wrapper classifies SIGKILL by checking the current boot’s kernel journal for an OOM victim record matching the command PID and invocation time. Cooperative exit-75 handoffs with `--preemptible --requeue` keep their own count on the same attempt.

When a queued reservation is granted, Arbiter sends an inbox message to its session, or to the parent conversation for a job or hold. The waiting command proceeds automatically. Hook-connected conversations receive the message on their next hook.

### Using a session from a terminal

The board labels managed agents **Headless**. After the supervisor connects to the conversation, **Attach terminal** opens an Arbiter console that shows new reports and sends each line you enter to that agent. Ctrl-D detaches. Approval requests still use the board or `arbiter answer`. You can also run `arbiter attach-terminal SESSION` from a local terminal.

For a finished Codex or Claude session, **Resume in terminal** opens the provider’s interactive UI in the original project directory. The CLI equivalent is `arbiter resume-terminal SESSION`. Arbiter checks local history, surviving work, and active copies of the conversation before claiming a new attempt. Hook-discovered sessions display **Terminal unknown** when their launch mode is unavailable. Board buttons use the machine’s desktop terminal through `xdg-terminal-exec`.

In **View conversation**, use the previous and next arrows to jump between user inputs. Previous can fetch earlier history. Tool activity appears alongside the conversation, with command or path details you can expand.

## Operator commands

```bash
arbiter status
arbiter update s_0123456789ab
arbiter pause s_0123456789ab --reason 'Let the tests use the machine'
arbiter resume s_0123456789ab
arbiter message 'Schema is ready' --session s_sender --to s_recipient --topic database
arbiter finish s_0123456789ab --safe --status 'Confirmed its processes stopped'
arbiter events --after 100
arbiter demo     # model-free walkthrough of contention, messaging, pause and resume
```

For manual integrations, `register [--pid N]`, `reserve`, `heartbeat`, `report`, `inbox`, `checkpoint`, `release --reservation` and `finish` expose the protocol; `reserve --wait` and `checkpoint --safe --wait` block outside the model.

## Scheduling and recovery

- Requests are ordered by priority plus time waiting. After 30 seconds, an older request protects the resources it needs. Work using unrelated resources can still start.
- While a whole-machine measurement waits for a reservation already running when it queued, other jobs can use idle capacity for phases estimated at ten minutes or less. If the original holders have future ETAs, the phase must fit before the latest one. Once those holders finish, the scheduler lets admitted phases finish and gives the measurement its turn. The measurement runs exclusively.
- When a reservation has run for the preferred interval and blocks another session, Arbiter asks its owning agent to release at the next safe checkpoint. A yielding job that waited for a grant gets up to one short phase before the scheduler asks it to yield again.
- Resource ownership is exclusive (or up to `capacity`) for named resources and additive for CPU/memory. Names are cooperative conventions.
- Every grant/wait decision includes a reason. State and events commit together in SQLite WAL mode.
- **Owner liveness.** Sessions with an owner process (runs, holds while waiting or with `--pid`, managed agents, attached Claude Code/Codex sessions, MCP servers) live as long as that process, checked every 3 seconds by PID and creation time. If tracked work outlives the owner, Arbiter marks the session stale and waits for the workload to exit before releasing the reservation.
- Sessions without an owner process (old-style external registrations) go stale after 45 s without heartbeats and keep their reservations until `arbiter finish --safe`.
- When a stale reservation blocks another session, Arbiter queues one status request for the holder or its live parent conversation. After one minute without an answer, the board and `arbiter status` flag the blocker for review. An undelivered request stays queued for the next hook. Delivered requests can be retried after five minutes if the blocker persists. An unanswered check never releases a reservation.
- **Restarts.** The operator token survives daemon restarts and `connection.json` stays in place, so wrappers and supervisors reconnect (they retry for 60 s). Sessions whose owners still run carry on; others go stale as above.
- When measured memory falls below headroom, Arbiter asks one managed, preemptible memory holder to pause, and resumes automatically paused sessions after memory has stayed above twice headroom for 15 s.
- This is cooperative admission, not a security boundary against other processes of your OS user. Sessions that finished more than 14 days ago are pruned (`--retention-days N`, `0` keeps everything).

## Local API and storage

The daemon listens only on `127.0.0.1` (port 7483). Browser writes require the same origin. API requests require a bearer token or the board's HttpOnly, SameSite cookie. Sign in via a one-time link from `arbiter open` on either platform, or with Linux PAM on Linux. Host checks reject DNS rebinding.

The **operator token** (in `connection.json`) can do everything; hooks and CLI commands run as your user and use it. A **session token** can only act on its own session and the job/hold sessions it registered: report, reserve, release, checkpoint, message, acknowledge, finish, track, extend, requeue, raise approval requests. It cannot answer approvals, pause or resume sessions, or read the event stream. This is not a sandbox: a process running as your user can read `connection.json`.

On Linux, state is under `~/.local/state/arbiter` with owner-only permissions. On Windows, the state directory grants access to its owner and SYSTEM. Both store `connection.json` and a SQLite database in WAL mode. The daemon checks the database schema version when it starts. Use `ARBITER_STATE_DIR` to point both CLI and daemon at a dedicated state directory, or `ARBITER_URL` and `ARBITER_TOKEN` to configure clients directly.

- `GET /healthz`: liveness.
- `GET /api/state`: sessions, reservations, commands, messages, approvals, capacity, pressure, resources, telemetry, event cursor. `?wait=<cursor>&timeout=5` long-polls (capped at 10 s).
- `GET /api/disk?path=/abs&gb=N`: free space after outstanding claims.
- `POST /api/launch`: operator only; returns a single-use board link.
- `GET /api/events?after=N`, `GET /events?after=N` (server-sent events, `Last-Event-ID` supported): operator only.
- `GET /api/inbox?session=ID`: unacknowledged requests and messages.
- `POST /api/action`: `{"action":"report","data":{"session":"s_…","status":"…"}}`. The hooks use the `hook` action: attach, guard and deliver in one round trip.

On Linux, `arbiter serve --unix-socket /absolute/path/arbiter.sock` serves HTTP over a mode-0600 Unix socket for CLI and MCP clients. The board uses TCP.

## Verification

```bash
python3 -m unittest discover -s . -p 'test_*.py' -t .
node --check arbiter/static/app.js
```

The tests cover contention, sharing and capacity, holds and ETAs, conversation families, measurement, yielding, disk claims, owner liveness and reaping, restarts, memory pressure, attaching sessions, the guard and hook protocol, token scoping, HTTP authentication/origin checks, SSE replay, MCP requests, adapter protocol handling and the installer's hook merging. See [protocol notes](docs/protocols.md) for integration details and what was verified live.

## Built-in operator

**Scheduler rules · No model required** controls the preferred reservation length, automatic approval of new routine Arbiter inbox reads, and **Preserve machine interactivity**. The latter delays new reservations when CPU usage exceeds the configured guard threshold or available RAM falls below the daemon's headroom. GPU and whole-machine jobs also wait when Linux DRM reports over 90% GPU utilization or VRAM use. Driver support varies. These are admission checks. A running job can still saturate the machine.

**Operator · Requires a model** lets you choose a provider and independently enable queue assistance, inbox review, replies to session messages, and status reviews. The existing inbox review setting keeps its model-dependent behavior. Automatic inbox approval is a separate opt-in rule. Custom instructions guide the operator's choices for eligible events.

**Saved operator presets** store a provider, model ID, enabled actions, and instructions by name on this machine. Add or update a preset, then choose **Save settings**. Loading a preset fills the operator fields for review. Scheduler rules are independent. Restoring configuration file defaults keeps the preset library.

Saved board settings apply to this machine and survive daemon restarts. **Use
configuration file** restores the settings from `~/.config/arbiter/config.toml`
(or the file named by `ARBITER_CONFIG`). To configure the operator there:

```toml
[scheduler]
preferred_phase_minutes = 10
auto_approve_inbox = false
preserve_interactivity = false

[operator]
instructions = "Ask for a status update when a short interactive job is waiting."
provider = "pi"                 # "codex", "claude", "pi", or "command"
model = "provider/model-id"     # Use the model ID understood by the selected CLI
approve_inbox = true            # Single-call confirmations for Arbiter inbox reads
manage_resources = true         # Request updates and make bounded queue priority adjustments
reply_messages = false          # Review direct-to-Arbiter and broadcast session messages
review_status = false           # Flag important reports and session state changes
```

The built-in providers use the local CLI's login. If you omit `model`, the CLI uses
its default. For another configured model runner, set `provider = "command"` and
`command = ["/absolute/path/to/runner", "model-id"]`. Arbiter sends the prompt on
stdin and expects a JSON object with `choice` (a candidate ID or null) and `reason`
(a short string) on stdout. Use a trusted runner that only returns model text.
Set `provider = ""` to turn off the operator. The daemon reloads these settings as
part of its regular config refresh.

The model sees candidate IDs, resource names, priorities, and wait times. Enabling message or status review also supplies excerpts from the corresponding events. Arbiter
rechecks every choice against current state before acting. Command execution, file
changes, and other provider approvals remain manual. A priority boost applies
once per waiting reservation, and the scheduler still checks admission limits
and lock ownership. Applied decisions are recorded in **Operator decisions** on
the board and as `operator.decided` events.
