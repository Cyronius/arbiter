# Arbiter job recovery implementation plan

Give the operator two actions on the board and CLI: **Resume conversation** for a saved agent session and **Rerun command** for a recorded shell job. Both go through Arbiter’s scheduler and work with the operator model off. Ship manual recovery first and bounded automatic retry second.

## 1. Record how to recover each kind of work

- **Claude and Codex.** Use the provider, persistent conversation GUID (Arbiter’s `external_id`), and original working directory. Reuse the existing Claude `--resume` and Codex `thread/resume` adapters. Read existing records on upgrade. A missing transcript, GUID, or directory should produce a specific explanation. Give the new turn a brief account of the interruption and any surviving work.
- **Pi.** For a persistent Pi session, the direct lookup is `pi --session <full-id>`. The current managed-agent CLI supports Claude and Codex. Expose Pi recovery when a managed adapter can record and reopen a persistent Pi conversation. Keep ephemeral Pi operator calls out of recovery.
- **Ordinary shell commands.** Add an opt-in launch record to `arbiter run`. Store the exact argument vector, working directory, resource bundle, and execution options needed for replay. Protect arguments that might contain secrets in owner-only storage. Keep them out of session snapshots, events, and logs. Avoid capturing the whole environment. Support a named script that obtains credentials when it runs. Older jobs with only a display label have insufficient information for replay.
- Link each attempt to its original job and its predecessor. Preserve the outcome, exit code or signal when observed, cancellation intent, and any reported application checkpoint. An interrupted wrapper may leave the outcome unknown.

## 2. Reconcile the owner and worker after a crash

Start from the PID and process-group checks in `arbiter/core.py` and the launch sequence in `arbiter/workers.py`. Inspect both the wrapper and the work it started. A live worker retains its reservation. Surface that state and suppress duplicate execution. Distinguish a deliberate stop, expected yield, observed failure, and lost owner.

Close the gap between spawning a worker and registering its identity. One option is a launcher that waits for a durable tracking acknowledgement before executing the workload and exits if its supervisor disappears before acknowledgement. Handle PID reuse and host reboot when validating identities. If the daemon cannot establish whether a command already produced side effects, show the uncertainty and require an explicit manual decision before replay.

## 3. Ship manual recovery on the board and CLI

Add an operator-authorized recovery request through the existing API. Give it durable idempotency and an atomic claim so concurrent clicks, CLI retries, and a daemon restart during launch converge on one new attempt. Revalidate eligibility when claiming it. Keep the original attempt and audit trail visible. Restrict recovery to the stored launch record or provider conversation and issue fresh session credentials for the new attempt.

Submit the resource request through normal admission. Respect queue order, machine-interactivity settings, and active grants. Show the new attempt, queue reason, and outcome from the original session. Application code owns checkpoint files. A shell rerun starts the program in its working directory, where it can discover the last complete checkpoint.

## 4. Add bounded automatic retries

Default to manual. Let the operator opt a repeatable job into two automatic retries, after 30 seconds and two minutes. Record the policy, consumed attempts, and next eligible time durably. Backoff expiring makes the attempt eligible for the queue. Provide a way to cancel a pending retry.

Keep cancellations stopped. Route confirmed out-of-memory failures and uncertain outcomes to manual review. An unexplained SIGKILL alone does not establish an OOM. Preserve the existing cooperative exit-75 handoff and its separate requeue accounting. Automatic recovery should use the same claim and launch path as the manual action.

## Acceptance checks

1. Open a pre-upgrade database. Resume eligible managed Claude and Codex conversations using their recorded GUIDs. Check a missing transcript and an older shell job without a launch record.
2. Use a temporary daemon, database, and a sharded shell fixture that marks a shard complete only after publishing its output. Kill the wrapper while its worker is writing. Restart the daemon. Verify that the worker retains the grant and no competing copy starts.
3. After the worker exits, issue simultaneous recovery requests from the board and CLI. Kill and restart the daemon around the claim and launch boundaries. Verify one queued attempt, one worker, and a link to the failed attempt. A waiting peer should get its normal turn.
4. Confirm that the sharded job skips completed shards and resumes from its last valid checkpoint. Exercise retry exhaustion, backoff across restart, cancellation during backoff, confirmed OOM, and a cooperative yield.
5. Check access with a session-scoped token. Verify that private launch data stays out of the board state, public session data, and event feed. Exercise the board with the model off. Run the Python suite and frontend typecheck/build, and document the CLI with a runnable shell example.

## Starting points

- `arbiter/adapters.py` records provider GUIDs and opens resumed conversations.
- `arbiter/workers.py` owns command launch, process-group tracking, shutdown, and exit-75 handling.
- `arbiter/core.py` owns SQLite state, migration, reconciliation, reservations, and transactional actions.
- `arbiter/server.py`, `arbiter/cli.py`, and `frontend/src/` expose recovery to the operator.
