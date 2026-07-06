---
name: Sidecar Daemon
kind: service
description: Unified process supervisor, cron scheduler, and message-driven job dispatcher
summary: Single long-lived Python process replacing multiple Windows Task Scheduler entries. Supervises child services (messaging :5123, embedding :5124, telegram :5125, mcp_gateway :5126, dashboard :5127), runs cron-scheduled jobs from sidecar_jobs/, and dispatches message-driven jobs.
entry_points:
- work_buddy.sidecar
tags:
- sidecar
- daemon
- supervisor
- scheduler
- jobs
aliases:
- sidecar daemon
- process supervisor
- job scheduler
- sidecar_jobs
- agent spawning
parents:
- services
- services
dev_notes: |-
  ## Service-log rotation pattern (roll-at-startup + artifact reaper)

  Subprocess stdout/stderr lands in raw OS file handles, not Python's logging framework — `RotatingFileHandler` doesn't apply. The child also holds that handle open for its whole lifetime, so the file can only be safely renamed at child *startup*, before the handle exists (on Windows, renaming a file with an open handle is a sharing violation). The daemon therefore owns exactly **one** job: roll an oversized live log out of the way at startup. **Retention of the rolled-out backups is owned by the artifact reaper, not the daemon** — see the `service-logs` artifact in `architecture/artifact-system`.

  `_roll_oversize_log(log_path)` runs in `_start_child` immediately before the append-mode handle opens:

  - if the file is missing or at/below `_SERVICE_LOG_CAP_BYTES` (16 MiB), no-op (returns `None`);
  - otherwise rename the live `<name>.log` → `<name>.<UTC-timestamp+micros>.log` (with a `-<n>` tiebreaker against same-microsecond collisions) and return the backup path. The fresh `<name>.log` is created by the next `open(..., "a")`.

  Retention of the rolled-out backups is owned by the `service-logs` artifact (`MtimeWindow(7d)` + `Delete`, pinning the live `<name>.log`), not by this roll — mirroring logrotate's `dateext` + `maxage` split. The split is deliberate: a roll that also manages a backup *count* weighs only the live file at startup and never bounds an already-rolled oversized backup, so a low-volume service's backup can persist indefinitely. Keeping the roller purely size-triggered and delegating age/retention to the reaper avoids that.

  The roll still only fires at child start, so a child streaming continuously between restarts can exceed the cap mid-run. Accepted in practice: restart cadence is "once or twice a week", the access-log filter already throttles the dominant write source, and the live log self-heals on the next restart. Stricter mid-run bounding would require piping child stdout through a per-child reader thread — rejected as over-engineering for disposable logs, since it adds failure surface to the startup path and loses the "captured stdout always observable in a raw file" property. A startup self-check (`_oversize_service_logs`) logs a WARNING if any of a service's logs exceed the cap, so a regression is visible immediately.

  Note the daemon now closes its *own* copy of the child log handle after `Popen` (in a `finally`); the child keeps its inherited dup. Leaving the parent copy open leaked a handle per restart and, on Windows, would pin the file against the next startup roll.

  ## Child-process insulation: takeover + interpreter pin

  Two defenses, layered. Both exist because Windows makes the obvious approach impossible.

  **The class of bug**: a child service outlives the daemon that spawned it, keeps holding its port, and serves stale in-memory bytecode indefinitely while the supervisor thinks everything's fine (the orphan answers `/health` 200 OK).

  **Why graceful shutdown can't carry the load**: on Windows `os.kill(pid, signal.SIGTERM)` is `TerminateProcess` — a hard kill, not a catchable signal. Across two unrelated processes there is no IPC channel for a polite shutdown. So the existing daemon's signal handler / `_shutdown` / `_stop_child` chain is unreliable as a children-cleanup mechanism. We don't try to make it work; we route around it.

  ### Defense 1 — takeover reaps children itself

  `work_buddy/sidecar/pid.py:takeover_existing_daemon` enumerates the old daemon's direct children via `compat.find_child_pids(old_pid)` and force-kills each **before** killing the daemon. The new daemon doesn't depend on the old daemon's cleanup at all — it has the OS authority to kill processes itself, so it does.

  **Order is load-bearing**: kill children first, then the daemon. Once the parent dies its children reparent (PPID=1 on Unix, orphaned on Windows) and enumeration via the original PID returns empty — you lose the only handle you had on them. Don't "simplify" by reordering.

  `compat.find_child_pids`: Windows uses WMIC (~100ms, deprecated but ships) with `Get-CimInstance -NoProfile` fallback (~6-15s cold). Unix uses `pgrep -P`. Best-effort — returns `set()` on enumeration failure; the supervisor's per-port clean-up in `_start_child` (`_kill_process_on_port`) is the secondary backstop.

  ### Defense 1b — OS-enforced kill-time reaping (Windows Job Object)

  Defense 1 only runs on the *next* startup, and every signal-handler / watchdog / `_shutdown` layer only runs if the dying daemon's own code runs. A **hard kill** (`taskkill /F`, `kill -9`, crash, power loss) vaporizes the daemon mid-instruction — no code runs, and on Windows children do **not** die with their parent. That leaves a window (hard-kill → next boot) where an orphan keeps holding its port and serving stale bytecode.

  `compat.create_kill_on_close_job()` creates a Windows Job Object with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`; `daemon.py:run()` creates it once (module global `_kill_job`, kept alive for the daemon's whole life) before spawning children, and `_start_child` assigns each child via `compat.assign_process_to_job(_kill_job, pid)` right after `Popen`. When the daemon's process object is destroyed by any means, the **OS** — not our code — kills everything in the job. This is the only mechanism that closes the hard-kill case.

  **Handle lifetime is the rule**: the kill fires when the job's *last* handle closes, so `_kill_job` must be a module global, never a local that can be GC'd. After assigning, the per-child *process* handle is closed (`win32api.CloseHandle`); only the *job* handle stays open.

  **Best-effort assignment**: `AssignProcessToJobObject` can fail when the daemon is itself nested inside another restrictive job (some VS Code terminals / scheduled-task wrappers). A failed assign is logged and non-fatal — Defense 1's startup sweep is the fallback.

  **Windows-only by design, not bias**: there is no cross-platform OS guarantee for kill-time reaping. Linux's `prctl(PR_SET_PDEATHSIG)` needs an unsafe `preexec_fn` in a threaded process and fires on *thread* death; macOS has only a cooperative child-side kqueue self-watch. Both are deliberately not implemented (commented as such next to the Windows code). The cross-platform baseline stays Defense 1. Windows is also the only OS the sidecar actually runs on.

  ### Defense 2 — children spawn under a pinned interpreter

  `work_buddy/compat.py:resolve_child_python(cfg)` is the single chokepoint for which Python children get spawned with: `cfg['sidecar']['python_executable']` if set and existing, else `sys.executable`. Shared by the sidecar daemon and the messaging client's auto-start.

  **Why a config pin and not a startup guard**: `sys.executable` is locked in by the launch context (scheduled task, shell activation, etc.) — the daemon can't change it once it's running. What the daemon *can* control is which interpreter its children inherit. Moving the pin from "how the daemon launched" to "what the daemon spawns" puts it inside our authority. A parent accidentally launched on the wrong interpreter (e.g. a Windows login task started on the base env in a headless context) still spawns children on the right one.

  Mismatch between pin and `sys.executable` logs a WARNING; missing pinned file logs an ERROR and falls back. **Warn-only, not refuse-to-start** — hard-stop would brick boot for users who haven't opted in. Easy to escalate to `raise` if you want fail-closed.

  Do not bypass `resolve_child_python` by reading `sys.executable` directly in `_start_child`. The config pin is the user's only knob for fixing a misconfigured launch context; bypassing it silently invalidates the knob.

  ## Child-process encoding: PYTHONUTF8 chokepoint

  A third spawn-time invariant, sibling to the interpreter pin in Defense 2 above. The class of bug is `UnicodeEncodeError` from `logging.StreamHandler` — children on Windows wrap their `sys.stdout`/`sys.stderr` in a `TextIOWrapper(encoding="cp1252")` at interpreter init, and any non-Latin-1 codepoint reaching a log line raises. Python's logging module catches the exception and falls back to a `--- Logging error ---` stack trace, so the function completes but the log file fills with noise. The bug recurs anywhere a `logger.*` call interpolates non-ASCII data — vault content, task descriptions, log glyphs (`→`, `—`, `×`).

  `work_buddy/compat.py:build_child_env()` is the chokepoint that fixes this by setting `PYTHONUTF8=1` in the child's env before `Popen`, flipping the child interpreter into UTF-8 mode (every stream — std, subprocess pipes, file ops — encodes via UTF-8). Lives adjacent to `resolve_child_python` so the two spawn invariants are obviously paired.

  **`setdefault` semantics.** An explicit user override (`PYTHONUTF8=0` to debug a bytes-vs-str regression) is preserved. The helper adds, it doesn't clobber.

  **Returns a copy, not `os.environ`.** Mutating `os.environ` would leak `PYTHONUTF8` into the parent and into any subprocess spawn that bypasses `build_child_env`. Test `test_build_child_env_does_not_mutate_os_environ` in `tests/unit/test_sidecar_child_env.py` enforces this.

  **Forward-compat with PEP 686.** Python 3.15 makes UTF-8 mode the default on all platforms; once we upgrade, the env injection becomes a no-op (the runtime is already in UTF-8 mode regardless of the env var). Don't rip the helper out at upgrade time — it remains the user-override knob for `PYTHONUTF8=0`.

  **Layer 2 fallback in `work_buddy/logging_config.py:setup_logging()`.** Layer 1 (the env injection) only covers sidecar-spawned children. Standalone launches (`python -m work_buddy.<service>` directly, tests, dev one-offs) bypass the sidecar path. As a fallback, `setup_logging()` calls `sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")` (and same for stderr) at first invocation. `errors="backslashreplace"` is the never-crash policy: any unencodable codepoint becomes `→` text instead of raising. Class-level regression sentinel: `tests/unit/test_logging_config.py::test_log_with_non_ascii_under_cp1252_does_not_crash` installs a cp1252 stream and proves the bug class is dead.

  Do not bypass `build_child_env` by passing `env=os.environ.copy()` directly in the `Popen` call. Same rule as `resolve_child_python`: the chokepoint is the single knob; bypassing it silently invalidates future env additions.

  ## Scheduler jitter: pending-fire layer

  The ``Scheduler`` keeps a ``_pending_fires: dict[(name, scheduled_at_epoch), due_at_epoch]`` between ``cron_matches`` and ``_fire_job``. When a cron match has ``jitter_seconds > 0``, the match becomes a *queue* operation rather than a fire; ``_fire_due_pending`` (called at the top of every tick) pops entries whose due time has passed and runs them through the same ``_fire_job`` path as no-jitter jobs.

  **No-jitter fast path.** When ``jitter_seconds == 0`` the queue is bypassed entirely: the per-minute name dedupe in ``_fired_this_minute: set[str]`` runs and ``_fire_job`` is called inline on cron match.

  **Per-occurrence dedupe key.** The pending-fire key is ``(name, scheduled_at.timestamp())`` where ``scheduled_at = now.replace(second=0, microsecond=0)`` — the cron eligibility minute. Same minute, repeated tick within the same minute: idempotent. Different minute (jitter > schedule period or tick rolls minutes): a separate pending entry is allowed, so rapid-recurrence jobs don't collapse occurrences.

  **Stable hash.** ``_stable_jitter_offset(job)`` uses ``hashlib.sha256(f"{name}\n{schedule}\n{jitter_seconds}".encode())`` — deterministic across processes (Python's built-in ``hash()`` is process-randomized and would silently re-shuffle offsets on every restart). Adding the name+schedule into the seed means renaming a job or changing its schedule reshuffles its offset; that's intentional and documented.

  **One-shot interaction.** ``recurring=false`` jobs with jitter are *not* cleared at first match — only after the deferred ``_fire_job`` actually executes. ``_fire_job`` clears the schedule line on the file as the last step regardless of jitter, so the on-disk frontmatter and the in-memory ``Job.schedule`` agree.

  **Hot-reload prune.** ``_hot_reload`` runs ``valid_names = {j.name for j in self.jobs if j.enabled and j.schedule}`` after loading the new jobs and drops any pending fire whose name isn't valid (deleted, disabled, lost schedule). The prune runs **unconditionally** every reload — outside the fingerprint-changed branch — so a partial-load recovery or a simple disable can't leave stale pending entries. Per-occurrence schedule/jitter invalidation is a stricter refinement that can layer on later if a stale-pending bug surfaces.

  **``_now()`` indirection.** The scheduler's wall clock reads through ``Scheduler._now()`` rather than ``datetime.now(timezone.utc)`` directly so tests can subclass and inject a fake clock. Production behavior is unchanged. Tests that drive ``tick()`` should also stub ``work_buddy.config.load_config`` to avoid clobbering the test's ``_jobs_dirs`` from the real ``config.yaml`` on the first hot-reload.

  **``job_fingerprint`` includes ``enabled`` and ``recurring``.** These flags are load-bearing for hot-reload: a job that flips ``enabled`` or ``recurring`` must re-trigger the loader (and therefore the pending-fire prune). Don't drop them when extending the fingerprint.

  ## Schedule-aware jitter ceiling

  ``compute_max_jitter_seconds(interval_seconds)`` in ``cron.py`` is the single source of truth for the per-schedule cap that bounds ``jitter_seconds``: ``min(interval_seconds // 10, 300)``, rounded down to the nearest 10s. The ``//10`` keeps a jittered fire inside the same logical interval bucket (the spreading effect, not interval drift); the 5-minute hard cap protects daily/weekly schedules from oversized spread windows.

  The interval itself comes from ``cron_interval_seconds(expr)``, which walks 8 consecutive matches from a fixed Monday-midnight reference and returns the **smallest** gap. The min-of-gaps form is needed because irregular schedules like ``0 9,13 * * *`` have unequal gaps (4h then 20h) and jitter must not exceed the smaller one or two firings would interleave. For the typical ``*/N`` / hourly / daily case every gap is identical and the loop just confirms the value.

  The dashboard's ``/api/cron/describe`` endpoint surfaces ``interval_seconds`` and ``max_jitter_seconds`` alongside the human-readable description; the Add-job form uses these to set the Jitter input's ``max`` attribute live as the user types a schedule, and clamps any pre-existing value down when the schedule narrows. The form-bridge ``jitter_seconds`` field handler also clamps agent-pushed values to the same ceiling, so a chat-walkthrough agent setting `jitter_seconds: 600` on a `*/5` schedule (cap 30) silently lands at 30 rather than the form rejecting it.

  **Don't re-implement the cap on the server-side write path.** ``create_user_job_file`` validates only ``jitter_seconds >= 0``; the schedule-aware cap is purely a UI-side recommendation, not a hard backend rule. A user who wants to override the cap by hand-editing a `.md` file can. If you ever want to hard-enforce it, do it once in the underlying create function so all three create paths (direct file, dashboard form, agent) get it for free — don't sprinkle the same check across each path.

  ## Message-to-job dispatch: consent_grant special case

  `work_buddy/sidecar/dispatch/router.py:_handle_message` special-cases `subject == "consent_grant"` ahead of the generic `_classify_and_execute` path. `_handle_consent_grant_message(body)` parses the body for `{operation, mode, ttl_minutes, notification_id}`, calls `work_buddy.consent.resolve_consent_request(notification_id, approved=True, mode=..., ttl_minutes=...)`, and returns the dispatch status. Routing through `resolve_consent_request` (rather than calling the `consent_grant` capability directly via `_execute_capability`) is what makes out-of-band modal approvals land in the originating agent's session DB — the resolver reads `notification.callback_session_id` and threads it through `grant_consent(..., session_id=...)`. See `notifications/consent` for the full cross-session routing + bundle-unbundle story.

  A missing `notification_id` (a `consent_grant` message from an out-of-sync Obsidian plugin that hasn't been rebuilt) falls back to a bare in-process `grant_consent(operation, mode, ttl_minutes)` with a WARN log — the grant lands in the sidecar's own session DB, which doesn't unblock the originating agent. The fallback exists so an old plugin doesn't crash the path; the agent surface is degraded until the plugin is rebuilt + reloaded.

  ## Supervisor/dispatch thread split: ownership discipline

  `daemon.run()` owns the supervisor loop (main thread); `_dispatch_loop` (thread name `dispatch`, daemon=True) runs `_run_dispatch_cycle`: scheduler tick, cron event tick, message poll, retry sweep. Shared state is the single `SidecarState` instance, coordinated by field ownership instead of locks: each field has exactly one writer thread, and container fields are replaced wholesale (fresh list from `set_job_states`, fresh list for `events`), never mutated in place. Supervisor owns `services`, `events`, `last_tick_at`, `dispatch_job`; dispatch owns `jobs`, `exclusion_active`, `last_dispatch_at`, `dispatch_phase`, `dispatch_phase_since`. Scalar cross-thread reads are GIL-atomic. Don't add a field with two writers, and don't mutate a shared container in place.

  `Scheduler.update_state` must stay on the dispatch thread — it iterates `_pending_fires`, which `tick()` mutates, so calling it from the supervisor would race the dict. `Scheduler.current_job` is the one scheduler attribute the supervisor reads (set in `_fire_job` under try/finally); it's a plain string for exactly that reason.

  The supervisor's stall warning (`_dispatch_stall_message`) is pure classification; the emit is deduplicated by remembering `dispatch_phase_since` — one warning per stall, not one per supervisor tick. `TickFailureTracker` has two instances, one per loop, so a sustained dispatch failure and a sustained supervisor failure escalate independently.

  `cleanup_pid_file` only deletes the pid file when it records the calling process's own pid. `write_pid_file` registers it via atexit, and that hook can fire after a takeover replaced the file with the successor's pid — an unguarded delete erases the live daemon's pid file. Stale-foreign-pid removal stays in `check_existing_daemon`.

  Tests must never touch the real runtime files: the autouse `_isolate_sidecar_runtime_files` fixture in `tests/conftest.py` redirects `pid.PID_FILE` and `state.STATE_FILE` to per-test temp paths (opt out with `@pytest.mark.real_sidecar_runtime_files`). Test code must reference the module attributes (`pid_mod.PID_FILE`), not by-value imports, or the redirection is invisible to the assertions.
---

A single long-lived Python process that replaces multiple independent Windows Task Scheduler entries with a unified process supervisor, cron/heartbeat scheduler, and message-driven job dispatcher.

Starting: powershell.exe -Command "cd <repo-root>; conda activate work-buddy; python -m work_buddy.sidecar"

Manages its own lifecycle via PID file (`<data_root>/runtime/sidecar.pid`) and state file (`<data_root>/runtime/sidecar_state.json`).

Two loops split the daemon's work by blocking behavior. The **supervisor loop** (main thread) evaluates cached health probes, restarts failed children, and writes `sidecar_state.json` every tick — everything on it is fast and bounded, so the state file's freshness is a true daemon-liveness signal (`wbuddy status` classifies ~90s of staleness as wedged). The **dispatch loop** (a background thread) runs scheduler cron ticks, message-driven dispatch, and retry sweeps — the phases that execute jobs and replays inline and can legitimately block for minutes (agent spawns, index rebuilds, local-LLM leases). A slow job therefore reads as a busy dispatch phase in the state file, never as a hung daemon, and can never delay child restarts.

Three subsystems:
1. Process Supervisor — starts and monitors child services. A dedicated HealthMonitor thread probes every service's /health endpoint concurrently (via ThreadPoolExecutor) at health_probe_interval (default 5s), so dispatch work or slow capabilities can never delay probes. The supervisor loop consumes cached health state only; a service is restarted after health_failure_threshold (default 2) consecutive failed probes, under exponential backoff, and given up on after max_service_crashes (default 5).
2. Cron/Heartbeat Scheduler — loads job .md files from BOTH `sidecar_jobs/` (system jobs, git-tracked) AND `<data_root>/user_jobs/` (user-authored, gitignored) and fires them on their cron schedule, inline on the dispatch loop. Supports exclusion windows (quiet hours), and optional per-job stable jitter to spread phase-aligned starts. Hot-reload has two triggers: a `JobsWatcher` (kernel filesystem events via `watchdog`; ~50ms latency on file change) AND a 30s polling interval as a safety net. Watcher events set a `threading.Event` (`Scheduler.jobs_reload_pending`) that the daemon's dispatch-loop sleep waits on, so the next cycle reloads immediately. On filename-stem collision the user file wins and a WARN is logged.
3. Message-to-Job Dispatch — polls messaging service for pending messages addressed to work-buddy. Classifies each message and executes automatically, also inline on the dispatch loop.

Shutdown: First Ctrl+C / SIGTERM requests graceful shutdown; a watchdog thread force-exits after 15s if the main thread is stuck in a blocking syscall, so shutdown is always bounded. A second Ctrl+C force-kills children immediately. The `JobsWatcher` observer thread is stopped and joined alongside the HealthMonitor in the cleanup path; the dispatch thread is a daemon thread, briefly joined and abandoned if mid-job. The pid file is removed on shutdown only when it still records the exiting process's own pid — the atexit hook can fire after a takeover has replaced the file, and deleting the successor's pid file would make a healthy daemon read as not running.

Child stdout/stderr: redirected to `<data_root>/runtime/service_logs/<service>.log` so a silent or crashing child is always observable — Popen inheritance with CREATE_NO_WINDOW can otherwise drop output on Windows. At child launch the daemon rolls an oversized live log (>16 MiB) aside to a timestamped backup (`_roll_oversize_log`); the `service-logs` artifact then reaps rolled backups older than 7 days on the twice-daily cleanup tick (pinning the live log). See the rotation dev-note above.

Job file format: .md files in either jobs directory with YAML frontmatter (schedule, recurring, type, capability/params, enabled, spawn_mode, optional jitter_seconds). Each loaded `Job` carries a `source` field (`"system"` or `"user"`) that propagates through `JobState` into `sidecar_state.json` and is used by the dashboard's Jobs tab to group entries.

Job types: capability (calls registered MCP gateway capability directly), workflow (triggers registered workflow), prompt (freeform text — spawns claude -p agent session, consent-gated).

Agent spawn modes for prompt jobs: headless_ephemeral (default, --print --no-session-persistence), headless_persistent (--print only, registered in `<data_root>/runtime/agent_registry.json`), interactive_persistent (deferred).

Session launcher (work_buddy/session_launcher.py): remote_session_begin launches a visible Claude Code terminal session, optionally with --remote-control. Consent-gated. Primary use case: Remote Control from phone.

Cron syntax: standard 5-field cron in the timezone set in config.yaml (default: America/New_York).

Jitter — the **thundering herd** problem and how the scheduler avoids it

Many of work-buddy's jobs run on phase-aligned schedules: `*/3`, `*/5`, `*/10`, `*/30`. These coincide at common minute boundaries (`:00`, `:30`, hourly, etc.) and fire simultaneously — every five minutes a wave of indexers, sync jobs, and health checks all hit at the same second. That's the *thundering herd*: a synchronized burst of work whose contention (CPU, disk, lock acquisition, downstream API rate limits) is much worse than the same total work spread across the interval. Each individual job is fine; the simultaneous-start is the problem.

The scheduler's per-job jitter solves this: set ``jitter_seconds: <N>`` in a job's frontmatter and it fires at ``scheduled_at + offset`` where ``offset`` is in ``[0, N]``, deterministic per job. The same job lands at the same offset every cycle; two jobs sharing a schedule land at *different* offsets and stop colliding.

The dashboard form caps ``jitter_seconds`` per schedule. Worked examples: `*/3`→10s, `*/5`→30s, `*/10`→60s, `*/30`→180s, hourly/daily/weekly→300s (the hard cap is 5 minutes regardless). The cap is a UI recommendation, not a backend rule — hand-editing a .md file can exceed it.

Tick cadence is ``health_check_interval`` (default 30 s), so values < 30 are quantized away in practice — the dashboard form warns when the typed value falls below that floor. ``jitter_seconds: 0`` (the default) bypasses the pending-fire queue entirely and fires inline on cron match.

Observability fields ``next_at`` (raw cron eligibility) and ``effective_at`` (next_at + offset, or queued pending due time) ride alongside each ``JobState`` in ``sidecar_state.json``; the dashboard's Jobs tab renders ``effective_at`` in the "Next Run" column and shows the configured ``jitter_seconds`` in a dedicated "Jitter" column so users can correlate display time with cause.

Config (sidecar: section in config.yaml): health_check_interval (cadence of both loops: supervisor restart-decision evaluation and dispatch cycles), health_probe_interval (HealthMonitor cadence), health_probe_timeout, health_failure_threshold, max_service_crashes, restart_backoff_base, dispatch_stall_warn_seconds (default 600 — warn when the dispatch loop sits in one phase this long), services (with module/port/enabled per service), jobs_dir (system jobs, defaults to `sidecar_jobs`), user_jobs_dir (user jobs override; empty = `<data_root>/user_jobs/`), heartbeat, message_poll_interval.

Observability: The supervisor writes `<data_root>/runtime/sidecar_state.json` every tick regardless of what the dispatch loop is doing. Alongside services/jobs/events, the state carries dispatch-loop fields: `dispatch_phase` (scheduler_tick | message_poll | retry_sweep | idle), `dispatch_phase_since`, `dispatch_job` (the job currently executing inline, if any), and `last_dispatch_at` (end of the most recent dispatch cycle). A phase held past dispatch_stall_warn_seconds emits a `dispatch_stalled` event (once per stall) naming the phase and job; `wbuddy status` prints a busy line for a phase held past ~2 minutes. Query via sidecar_status or sidecar_jobs capabilities. Per-service child logs at `<data_root>/runtime/service_logs/*.log` (rotated as described above). Dashboard subscribes to `cron.hot_reload` events on the bus to refresh its Jobs tab on every actual reload.
