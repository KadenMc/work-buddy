"""A throwaway host for the Windows console tests. It never starts work-buddy.

Run as ``pythonw.exe -m probe_host <result.json> <mode> [hold-seconds]`` from
this directory. ``pythonw.exe`` has no console, exactly like the sidecar
under its logon task. Modes:

- ``tool-run``: call ``run_tool`` on a console probe, as a GUI host does.
- ``powershell``: call ``run_tool`` on ``powershell_argv(...)``.
- ``host``: establish the sidecar host context, then start a console probe
  WITHOUT the no-window flag, as third-party code in the daemon might.
- ``host-hold``: establish the host context, then hold a long-running console
  probe in a kill-on-close job of its own, as the daemon holds its services,
  and wait for it.

The result file is written by whichever process carries on past
``establish_host_context``: after a relaunch, that is the relaunched copy.
"""

from work_buddy.process import HostRole, establish_host_context

import json
import os
import subprocess
import sys

MODE = sys.argv[2]
if MODE in ("host", "host-hold"):
    CONTEXT = establish_host_context(HostRole.SIDECAR, module="probe_host")
else:
    CONTEXT = None

import ctypes  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
PROBE = os.path.join(HERE, "console_probe.py")
CONSOLE_PYTHON = os.path.join(os.path.dirname(sys.executable), "python.exe")
kernel32 = ctypes.WinDLL("kernel32")
kernel32.GetConsoleWindow.restype = ctypes.c_void_p


def write(record: dict) -> None:
    with open(sys.argv[1], "w", encoding="utf-8") as handle:
        json.dump(record, handle)


def own_console() -> dict:
    return {
        "pid": os.getpid(),
        "console_cp": kernel32.GetConsoleCP(),
        "console_window": kernel32.GetConsoleWindow() or 0,
    }


record: dict = {"mode": MODE, "host": own_console()}
if CONTEXT is not None:
    record["context"] = CONTEXT.to_dict()

if MODE == "tool-run":
    from work_buddy.process import run_tool

    result = run_tool([CONSOLE_PYTHON, PROBE], timeout=60)
    record["child"] = json.loads(result.stdout.strip().splitlines()[-1])
elif MODE == "powershell":
    from work_buddy.process import powershell_argv, run_tool

    script = "\n".join([
        "Add-Type -Namespace Probe -Name Console -MemberDefinition "
        "'[DllImport(\"kernel32.dll\")] public static extern System.IntPtr GetConsoleWindow();'",
        "[Probe.Console]::GetConsoleWindow().ToInt64()",
    ])
    argv = powershell_argv(script, encode=True)
    result = run_tool(argv, timeout=120)
    record["argv"] = argv
    record["returncode"] = result.returncode
    record["child_window"] = int(result.stdout.strip().splitlines()[-1])
elif MODE == "host":
    # Deliberately unflagged: this is how a library inside the daemon would
    # start a console program. With the host context in place it inherits a
    # console with no window.
    completed = subprocess.run(
        [CONSOLE_PYTHON, PROBE], capture_output=True, text=True, timeout=60,
    )
    record["child"] = json.loads(completed.stdout.strip().splitlines()[-1])
    # And a tool run, which shares the host's console rather than creating one.
    from work_buddy.process import run_tool

    result = run_tool([CONSOLE_PYTHON, PROBE], timeout=60)
    record["tool_child"] = json.loads(result.stdout.strip().splitlines()[-1])
elif MODE == "host-hold":
    from work_buddy.process import assign_process_to_job, create_kill_on_close_job

    import time

    job = create_kill_on_close_job()
    hold = sys.argv[3] if len(sys.argv) > 3 else "60"
    report = sys.argv[1] + ".probe.json"
    child = subprocess.Popen([CONSOLE_PYTHON, PROBE, hold, report], stdout=subprocess.DEVNULL)
    record["child_in_job"] = assign_process_to_job(job, child.pid)
    while not os.path.exists(report):
        time.sleep(0.05)
    time.sleep(0.2)
    with open(report, encoding="utf-8") as handle:
        record["probe"] = json.load(handle)
    write(record)
    child.wait()
    raise SystemExit(0)

write(record)
