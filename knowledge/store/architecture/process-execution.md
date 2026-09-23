---
name: Process Execution
kind: system
description: How work-buddy starts operating-system processes. One package, work_buddy.process, owns every launch through four intents (tool run, worker, host, visible terminal), fixes the runtime context of work-buddy's own processes through host roles (console, interpreter image, identity), records that context for status and health, and an architecture test keeps every launch inside the package.
summary: 'Every process launch goes through work_buddy.process. Callers state an intent (run_tool, spawn_worker, start_host, open_visible_terminal) and the policy module decides window, console, stdin, environment, timeout and cleanup. Host roles (SIDECAR, SERVICE, TRAY) give work-buddy''s own processes the same runtime context on every start path: background hosts run on python.exe with a console that has no window, which their console children inherit, and the sidecar establishes that console at its entry point when its launcher could not. The daemon records its context in the state file, wbuddy status shows it, and tests/unit/architecture/test_process_boundary.py fails any launch outside the package.'
entry_points:
- work_buddy.process
- work_buddy.process.launch
- work_buddy.process.policy
- work_buddy.process.host
- work_buddy.process.tree
- work_buddy.process.runner
tags:
- process
- subprocess
- launch
- windows
- console
- create-no-window
- host
- sidecar
- tray
- job-object
- timeout
- testing
- architecture-test
aliases:
- process launch boundary
- launch policy
- run_tool
- spawn_worker
- start_host
- open_visible_terminal
- establish_host_context
- host roles
- console model
- RecordingRunner
- recording_runner
- terminate_tree
- windowless launch
parents:
- architecture
dev_notes: |-
  ## Import hygiene is a requirement, not a preference

  Every module in `work_buddy/process/` imports only the standard library at
  module level and logs through `logging.getLogger`, never
  `logging_config.get_logger`, which initializes session logging on first use.
  `work_buddy.config` and `work_buddy.paths` are imported inside the functions
  that need them. The sidecar's entry point imports the package before its
  session identity exists, and the hook path imports it on every call.
  `tests/unit/process/test_import_hygiene.py` and the boundary test enforce
  this.

  ## The uv launcher is part of the process tree

  In a uv venv on Windows, `Scripts\python.exe` and `Scripts\pythonw.exe` are
  small launchers that start the real interpreter and hold it in a
  kill-on-close job of their own. So one logical Python process appears twice
  in the process table, `Popen.pid` names the launcher, and `sys.executable`
  inside the real interpreter still names the venv path. Everything here is
  built to work through that layer: sibling images are chosen from the venv
  directory, and the relaunch parent's job allows silent breakaway, so the
  real interpreter's own children keep the jobs their parents give them.

  ## The relaunch fallback

  On builds without `AllocConsoleWithOptions`, `establish_host_context`
  relaunches the entry module under `python.exe` with `CREATE_NO_WINDOW` and
  `WORK_BUDDY_HOST_RELAUNCHED=1`, assigns the child to a kill-on-close job with
  `JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK`, waits, and exits with the child's
  status mapped into the signed range `sys.exit` accepts
  (`host.signed_exit_status`). The job handle is never closed, so the job
  lives exactly as long as the waiting parent: ending the parent, as ending a
  scheduled task does, ends the daemon. If the job cannot be assigned (a
  restrictive outer job), the parent logs a warning and keeps waiting: only
  that coupling is lost, and `wbuddy stop` and takeover still stop the
  daemon. The relaunch happens before `daemon.run`, so the waiting parent
  never writes a PID file and never takes the instance lock.

  Tests force this path with `WORK_BUDDY_HOST_FORCE_RELAUNCH=1`, which makes
  the in-process API count as unavailable.

  ## Detecting a console

  `policy.process_has_console` opens `CONOUT$`, which succeeds exactly when a
  console is attached. It is checked at every tool run, because a host can
  gain a console after it starts, and it avoids importing ctypes, which costs
  tens of milliseconds in a short-lived process such as a hook.

  ## Tool-run termination never recurses

  On timeout, or any exception while waiting (including `KeyboardInterrupt`),
  the real runner ends the whole tree. On Windows it runs `taskkill /F /T`
  directly (`runner.taskkill_tree`), not through a runner, so a hung taskkill
  can never start another termination. The public `terminate_tree` runs
  `taskkill` as an ordinary tool run instead, so tests of an owner's
  cancellation path see it in the recording runner.

  ## Windows runtime tests

  `tests/process_windows/` starts throwaway probes under `pythonw.exe` (no
  console, like the logon task) and asks Windows what each process got: the
  console code page and whether the console has a window. They never start
  work-buddy and never register scheduled tasks. The `process-windows` CI job
  runs them, with the process unit tests and the boundary test, on
  `windows-2025`, whose build has the in-process console API. The relaunch
  tests force the fallback there.
---

work-buddy starts operating-system processes in one place: the
`work_buddy.process` package. Callers say what they are launching, and the
package applies the operating-system policy for that intent, so no call site
decides window, console, stdin, environment, timeout or cleanup behaviour on
its own. The package also owns the runtime context of work-buddy's own
long-running processes, which on Windows decides whether the console
programs they start stay invisible.

## The four intents

| Intent | Function | Used for | Windows | POSIX | Timeout |
|---|---|---|---|---|---|
| Tool run | `run_tool(argv, *, timeout, ...)` | Captured, awaited programs: git, gh, PowerShell, netstat, tailscale, lms, pandoc, `python -m` runners | This process's console, or `CREATE_NO_WINDOW` when it has none | Own session, except in a terminal's foreground job | Required, clamped to the caller's resilience deadline |
| Worker | `spawn_worker(argv, ...)` | Children the caller owns and does not await: agent workers, the document kernel, the dashboard build, OS openers | `CREATE_NO_WINDOW` | Own session on request (`new_session=True`) | None: the owner ends it with `terminate_tree` |
| Host | `start_host(role, module, ...)` | work-buddy's own Python processes: the sidecar, its services, the messaging auto-start, the tray | By role (below) | Own session for detached starts | None |
| Visible terminal | `open_visible_terminal(argv, ...)` | Remote-session terminals, and nothing else | `CREATE_NEW_CONSOLE` | The platform terminal emulator | None |

A tool run shares this process's console when it has one: that console has
no window (every background host establishes one) or is a terminal the user
already has open, so sharing it opens nothing, and it saves starting a console
host for every call. Only a process with no console, such as the tray or
anything under `pythonw.exe`, gives the tool run `CREATE_NO_WINDOW`. Workers
and hosts always get a console of their own, so they never depend on a
terminal staying open.

Tool runs never go through a shell and never wait on a prompt. stdin is closed
unless `input=` is given, and the environment gets a non-interactive overlay:
`GIT_TERMINAL_PROMPT=0`, `GCM_INTERACTIVE=never`, `GIT_OPTIONAL_LOCKS=0` and
`GH_PROMPT_DISABLED=1`. `env=`, when given, is the child's complete
environment, as with `subprocess.run`. Output decodes exactly as
`subprocess.run(..., text=True)` does unless `text=False`, `encoding=` or
`errors=` say otherwise. `powershell_argv(script)` builds a PowerShell argv
that always carries `-NoProfile -NonInteractive`, and `encode=True` passes the
script as `-EncodedCommand`.

A tool run without a finite, positive timeout is rejected. When the caller
holds a `work_buddy.resilience` deadline, the shorter of the two applies, and a
deadline that has already passed raises before anything starts. On timeout the
whole process tree is ended, not only the direct child.

Results and errors stay compatible with existing handlers:

- `ProcessResult` is a `subprocess.CompletedProcess`, and `check=True` raises
  `subprocess.CalledProcessError`.
- `ExecutableNotFound` is a `FileNotFoundError`.
- `ProcessTimeout` is both a `subprocess.TimeoutExpired` and a `TimeoutError`,
  so the resilience framework classifies it as a timeout.

`work_buddy.process.tree` holds the process-tree primitives:
`terminate_tree(handle_or_pid)` (`taskkill /F /T` on Windows, the process group
on POSIX), `kill_process_on_port` (frees a port and reports truthfully whether
it is free, refusing rather than guessing when the owner lookup fails),
`find_child_pids`, and the kill-on-close Job Object helpers
`create_kill_on_close_job` and `assign_process_to_job`.

## Host roles and the console model

A process with no console that starts a console program without
`CREATE_NO_WINDOW` makes Windows create a new console, with a visible window,
for that program. Library code does exactly that. So work-buddy's background
hosts run with a console of their own that has no window: their console
children inherit it and stay invisible, whoever launches them. GUI hosts have
no console and launch every child through the policy instead.

| Role | Interpreter | Windows image | Windows console | Identity |
|---|---|---|---|---|
| `SIDECAR` | The launcher's own (`sys.executable`) | `python.exe` | Its own, with no window | A fresh `sidecar-` session id, never inherited |
| `SERVICE` | The pinned child interpreter | `python.exe` | Hidden, from `CREATE_NO_WINDOW` | The sidecar's, inherited |
| `TRAY` | The pinned child interpreter | `pythonw.exe` | None, by design | The CLI's (`wbuddy-cli`), never inherited |

`start_host` applies the role: it selects the role's image from the same
directory as the interpreter (`host.sibling_image`, which never changes which
environment runs), builds the role's environment, and starts the host with
`CREATE_NO_WINDOW`. A service's stdout and stderr go to its log file after a
start banner, and every other host's stdio is discarded.

`establish_host_context(role)` is the first statement of
`work_buddy/sidecar/__main__.py`, so it runs under every launcher, including a
logon task or shortcut that starts `pythonw.exe`, which has no console. On
Windows, for a background role:

1. A console is already attached: nothing to do.
2. Otherwise, on Windows 11 24H2 and Windows Server 2025 and later, the process
   calls `AllocConsoleWithOptions` with `ALLOC_CONSOLE_MODE_NO_WINDOW`.
3. Otherwise, it relaunches under `python.exe` with `CREATE_NO_WINDOW`, holds
   the relaunched host in a kill-on-close job, waits, and exits with its exit
   status.
4. If both fail, it logs an error and carries on without a console, and status
   reports it. A host never refuses to start over its console.

Then it assigns the role's identity. `wbuddy start --foreground` calls it with
`foreground=True`, which keeps the terminal's console but still gives the
daemon its own sidecar identity, so a foreground daemon uses the sidecar's
consent database like every other start path. The tray's entry point calls it
for the identity alone.

The interpreter pin (`sidecar.python_executable`) keeps its semantics:
`resolve_child_python` returns the pin when it names an existing file, else
`sys.executable`, and warns when they differ. A pin that names `pythonw.exe`
still yields `python.exe` for services, from the same directory.
`build_child_env` gives Python children `PYTHONUTF8=1` and
`OPENBLAS_NUM_THREADS=1`, each with `setdefault`.

## The runtime record

At boot the daemon stores `describe_host_context()` in the `host` field of the
sidecar state file (`sidecar.state.HostRecord`): its executable and image, its
console (`allocated`, `hidden`, `attached`, `none` or `not_applicable`) and the
mechanism behind it (`inherited`, `allocate_api`, `relaunch`, `foreground` or
`none`), the interpreter and image its services run on, the pin, and whether
the pin points outside the project. `wbuddy status` prints it as one line, for
example:

```text
Runtime: python.exe, console allocated without window | services: C:\work-buddy\.venv\Scripts\python.exe (sidecar interpreter)
```

It warns when the daemon has no console on Windows, when services would run on
`pythonw.exe`, and when the pin points outside the project. The sidecar health
component's "runtime context" step reads the same record and fails on the two
kinds of drift that let console programs open windows.

## Testing

`ProcessRunner` is the one test seam. `RecordingRunner` records every
`ResolvedLaunch` (argv, cwd, environment, stdio, creation flags, session,
timeout, input) and answers from scripted results: an argv-prefix rule, a
predicate, or a responder callable, with the most recently added match
winning. Spawned workers come back as `FakeProcess` handles. The
`recording_runner` fixture in `tests/conftest.py` installs one for a test, and
`use_runner` installs one process-wide for a block, including launches from
threads the code under test starts. Code that takes an injected runner, such
as the agent-execution providers, passes it on as `runner=`.

Patching `subprocess.run` or `subprocess.Popen` intercepts nothing: the
package's own runner starts processes. Retarget such a test to the recording
runner.

## The boundary rule

`tests/unit/architecture/test_process_boundary.py` walks `work_buddy/` and
fails on any launch outside `work_buddy/process/`: a call to, or a value
reference of, `subprocess.run`, `Popen`, `call`, `check_call`, `check_output`,
`getoutput` or `getstatusoutput`, `os.system`, `os.popen`, `os.startfile`,
`os.spawn*`, `os.exec*` or `os.posix_spawn*`, or
`asyncio.create_subprocess_exec` or `create_subprocess_shell`. It also fails on
a `creationflags=` argument or a reference to `CREATE_NO_WINDOW`,
`CREATE_NEW_CONSOLE` or `DETACHED_PROCESS`. The `subprocess` exception types,
`CompletedProcess`, `DEVNULL`, `PIPE`, `STDOUT`, `list2cmdline`, and any of
these names inside a type annotation stay allowed everywhere. The test also
checks that the package imports only the standard library at module level.

An exemption goes in the test's `EXEMPT` dictionary with a written reason,
and only for a launch the package cannot express. Stale exemptions fail the
test. PTY sessions (through winpty or ptyprocess, windowless by construction)
and `webbrowser.open` are outside the rule's reach and need no exemption.

## Windows sources

- [Process Creation Flags](https://learn.microsoft.com/en-us/windows/win32/procthread/process-creation-flags): `CREATE_NO_WINDOW` runs a console application without a console window, and is ignored for a GUI application or together with `CREATE_NEW_CONSOLE` or `DETACHED_PROCESS`.
- [Creation of a Console](https://learn.microsoft.com/en-us/windows/console/creation-of-a-console): a console process inherits its parent's console by default, and a process with no console gets a new one.
- [AllocConsoleWithOptions](https://learn.microsoft.com/en-us/windows/console/allocconsolewithoptions): Windows 11 24H2 (build 26100) and Windows Server 2025 and later.
- [JOBOBJECT_BASIC_LIMIT_INFORMATION](https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_basic_limit_information): `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` and `JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK`.

## See also

- `services/sidecar`: the daemon, its services, takeover, and the Job Object that reaps services on a hard kill.
- `services/tray`: the tray, a GUI host.
- `operations/wb-cli`: `wbuddy start` and the status `Runtime:` line.
- `architecture/resilience`: the deadlines tool runs honour.
- `architecture/hot-path-discipline`: why external calls need a deadline.
