"""work-buddy's process launch boundary: the only way to start a process.

Callers state an intent, and the package applies the operating-system policy
for it, so no call site decides window, console, stdin, timeout or cleanup
behaviour on its own:

- :func:`run_tool`: run a program to completion and capture its output.
- :func:`spawn_worker`: start a child the caller owns and does not await.
- :func:`start_host`: start one of work-buddy's own long-running processes.
- :func:`open_visible_terminal`: open a terminal the user is meant to see.

Hosts call :func:`establish_host_context` first thing, so they have their
role's runtime context however they were started. Owners end processes with
:func:`terminate_tree`.

Tests install a :class:`RecordingRunner` with :func:`use_runner` and assert
the resolved launches, without starting anything.

An architecture test keeps launches inside this package: see
``architecture/process-execution`` in the knowledge store.
"""

from work_buddy.process.host import (
    HostContext,
    HostRole,
    build_child_env,
    describe_host_context,
    establish_host_context,
    resolve_child_python,
    start_host,
)
from work_buddy.process.launch import open_visible_terminal, run_tool, spawn_worker
from work_buddy.process.policy import Intent, ResolvedLaunch, powershell_argv
from work_buddy.process.runner import (
    ExecutableNotFound,
    FakeProcess,
    LaunchFailed,
    ProcessHandle,
    ProcessResult,
    ProcessRunner,
    ProcessTimeout,
    RecordingRunner,
    Scripted,
    SubprocessRunner,
    current_runner,
    use_runner,
)
from work_buddy.process.tree import (
    assign_process_to_job,
    create_kill_on_close_job,
    find_child_pids,
    kill_process_on_port,
    terminate_tree,
)

__all__ = [
    # intents
    "run_tool",
    "spawn_worker",
    "start_host",
    "open_visible_terminal",
    "powershell_argv",
    # hosts
    "HostRole",
    "HostContext",
    "establish_host_context",
    "describe_host_context",
    "resolve_child_python",
    "build_child_env",
    # results and errors
    "ProcessResult",
    "ProcessHandle",
    "ExecutableNotFound",
    "ProcessTimeout",
    "LaunchFailed",
    # process trees
    "terminate_tree",
    "kill_process_on_port",
    "find_child_pids",
    "create_kill_on_close_job",
    "assign_process_to_job",
    # the runner seam
    "Intent",
    "ResolvedLaunch",
    "ProcessRunner",
    "SubprocessRunner",
    "RecordingRunner",
    "FakeProcess",
    "Scripted",
    "current_runner",
    "use_runner",
]
