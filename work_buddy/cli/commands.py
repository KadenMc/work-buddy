"""Verb handlers for the ``wbuddy`` CLI: rendering and side effects.

Each ``cmd_*`` takes the parsed args, prints human-readable output (or JSON
when ``--json`` is set on verbs that support it), and returns a process exit
code. Lifecycle verbs delegate to ``cli.lifecycle``. ``setup`` / ``doctor``
render existing health output as text and add no new health logic. ``mcp
print`` emits the Claude Code config from the same port source the gateway
binds, so it cannot drift.
"""

from __future__ import annotations

import json
import sys
import time

EXIT_OK = 0
EXIT_FAIL = 1

_GLYPH = {True: "ok  ", False: "FAIL"}

# Only call out a busy dispatch phase once it has run long enough to be
# noteworthy (a cycle legitimately spends seconds in each phase).
_DISPATCH_BUSY_DISPLAY_S = 120.0


def _err(msg: str) -> None:
    print(msg, file=sys.stderr)


def _want_json(args) -> bool:
    return getattr(args, "json", False)


def _fmt_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


# ---------------------------------------------------------------------------
# Lifecycle verbs
# ---------------------------------------------------------------------------

def cmd_start(args) -> int:
    from work_buddy.cli import lifecycle

    if getattr(args, "foreground", False):
        lifecycle.start_sidecar(foreground=True)
        return EXIT_OK

    res = lifecycle.start_sidecar()
    if not res["started"]:
        _err(res["detail"])
        return EXIT_FAIL
    if res["already_running"]:
        print(f"{res['detail']} (pid={res['pid']}).")
    else:
        print(f"Sidecar started (pid={res['pid']}).")
    _print_dashboard_url(prefix="Dashboard: ")
    return EXIT_OK


def cmd_stop(args) -> int:
    from work_buddy.cli import lifecycle

    res = lifecycle.stop_sidecar()
    if not res["was_running"]:
        print(res["detail"])
        return EXIT_OK
    print(res["detail"]) if res["stopped"] else _err(res["detail"])
    return EXIT_OK if res["stopped"] else EXIT_FAIL


def cmd_restart(args) -> int:
    from work_buddy.cli import lifecycle

    stop = lifecycle.stop_sidecar()
    if stop["was_running"] and not stop["stopped"]:
        _err(stop["detail"])
        return EXIT_FAIL
    time.sleep(0.5)
    start = lifecycle.start_sidecar()
    if start["started"]:
        print(f"Sidecar restarted (pid={start['pid']}).")
        return EXIT_OK
    _err(start["detail"])
    return EXIT_FAIL


def cmd_status(args) -> int:
    from dataclasses import asdict

    from work_buddy.cli import lifecycle

    res = lifecycle.sidecar_status()
    st = res["state"]
    pid = res["pid"]
    health = res.get("health", "down")

    if _want_json(args):
        out = {"running": res["running"], "health": health, "pid": pid}
        if st is not None:
            out["state"] = asdict(st)
        print(json.dumps(out, indent=2))
        return EXIT_OK if health in ("up", "booting") else EXIT_FAIL

    if health == "down":
        print("Sidecar not running.")
        return EXIT_FAIL

    # A wedged daemon holds the pid file but has stopped ticking: its children
    # never came up (or died), so it is alive-but-not-serving. Say so plainly
    # rather than reporting an indefinite "starting up", and point at the fix.
    if health == "wedged":
        print(
            f"Sidecar process alive (pid={pid}) but not publishing state "
            f"(looks wedged). Run 'wbuddy restart'."
        )
        return EXIT_FAIL

    # Booting: the daemon has taken over / written its pid file but has not
    # published its first tick yet (services can take ~60s to come up).
    if health == "booting":
        print(f"Sidecar running (pid={pid}); starting up, state not yet published.")
        return EXIT_OK

    # health == "up": the state file names this pid and is fresh.
    uptime = _fmt_duration(time.time() - st.started_at) if st.started_at else "?"
    print(f"Sidecar running (pid={pid}, uptime={uptime})")
    if st.services:
        print("Services:")
        for name, svc in sorted(st.services.items()):
            port = f" :{svc.port}" if svc.port else ""
            crashes = f", {svc.crash_count} crash(es)" if svc.crash_count else ""
            print(f"  {name}{port}: {svc.status}{crashes}")
    if st.last_tick_at:
        print(f"Last tick: {_fmt_duration(time.time() - st.last_tick_at)} ago")
    _print_dispatch_status(st)
    return EXIT_OK


def _print_dispatch_status(st) -> None:
    """One line on the daemon's dispatch loop, when informative.

    Jobs, message dispatch, and retry sweeps execute inline in that loop
    and may legitimately block for minutes while the supervisor keeps
    ticking (the freshness ``Last tick`` reports). A long-running phase
    is shown as busy, with the job name when the scheduler is executing
    one, so a quiet cron backlog is attributable from the CLI. State
    files written by a daemon without dispatch fields print nothing.
    """
    now = time.time()
    busy_for = (now - st.dispatch_phase_since) if st.dispatch_phase_since else 0.0
    if (
        st.dispatch_phase
        and st.dispatch_phase != "idle"
        and busy_for >= _DISPATCH_BUSY_DISPLAY_S
    ):
        job = f" (job '{st.dispatch_job}')" if st.dispatch_job else ""
        print(
            f"Dispatch: busy in {st.dispatch_phase}{job} for "
            f"{_fmt_duration(busy_for)}, scheduled work is queued behind it"
        )
    elif st.last_dispatch_at:
        print(
            f"Last dispatch cycle: "
            f"{_fmt_duration(now - st.last_dispatch_at)} ago"
        )


# ---------------------------------------------------------------------------
# Setup / doctor
# ---------------------------------------------------------------------------

def _render_requirements(results: list[dict], *, title: str) -> None:
    print(title)
    for r in results:
        glyph = _GLYPH[bool(r.get("ok"))]
        line = f"  [{glyph}] {r.get('id', '?')}"
        if not r.get("ok") and r.get("severity"):
            line += f"  ({r['severity']})"
        print(line)
        if r.get("detail"):
            print(f"        {r['detail']}")
        if not r.get("ok") and r.get("fix_hint"):
            print(f"        fix: {r['fix_hint']}")


def cmd_doctor(args) -> int:
    from work_buddy.health.wizard import SetupWizard

    wizard = SetupWizard()
    component = getattr(args, "component", None)
    data = wizard.diagnose(component) if component else wizard.status()

    if _want_json(args):
        print(json.dumps(data, indent=2, default=str))
        return EXIT_OK

    if data.get("mode") == "diagnose":
        print(f"Component: {data.get('display_name') or data.get('component')}")
        diag = data.get("diagnostics") or {}
        print(f"  status: {diag.get('status', '?')}")
        if diag.get("root_cause"):
            print(f"  root cause: {diag['root_cause']}")
        if diag.get("fix_suggestion"):
            print(f"  fix: {diag['fix_suggestion']}")
        reqs = (data.get("requirements") or {}).get("results", [])
        if reqs:
            _render_requirements(reqs, title="Requirements:")
        return EXIT_OK

    _render_requirements(
        (data.get("bootstrap") or {}).get("results", []), title="Bootstrap:"
    )
    reqs = (data.get("requirements") or {}).get("results", [])
    if reqs:
        _render_requirements(reqs, title="Requirements (wanted components):")
    boot = (data.get("bootstrap") or {}).get("summary") or {}
    return EXIT_OK if boot.get("all_required_pass", True) else EXIT_FAIL


def cmd_setup(args) -> int:
    from work_buddy.health.requirements import RequirementChecker

    rc = RequirementChecker()
    results = rc.check_bootstrap()
    summary = rc.summarize(results)

    _render_requirements([r.to_dict() for r in results], title="Bootstrap checks:")
    print()
    print(
        f"{summary['passed']}/{summary['total']} passed, "
        f"{summary['failed_required']} required failing."
    )
    print()
    print("Claude Code MCP config (or run: wbuddy mcp print):")
    _print_mcp_config()
    print()
    print("Start the sidecar with:  wbuddy start")
    print(
        "For the interactive feature selection, run /wb-setup guided inside "
        "Claude Code (that walk needs an agent)."
    )
    return EXIT_OK if summary["all_required_pass"] else EXIT_FAIL


# ---------------------------------------------------------------------------
# provision / autostart
# ---------------------------------------------------------------------------

def cmd_provision(args) -> int:
    from work_buddy import provision as _prov

    res = _prov.provision(
        home=getattr(args, "home", None),
        data_dir=getattr(args, "data_dir", None),
        vault_root=getattr(args, "vault_root", None),
        repos_root=getattr(args, "repos_root", None),
        timezone=getattr(args, "timezone", None),
        anthropic_key=getattr(args, "anthropic_key", None),
        start=not getattr(args, "no_start", False),
    )
    if _want_json(args):
        print(json.dumps(res, indent=2, default=str))
        return EXIT_OK if res["ok"] else EXIT_FAIL
    print(f"Provisioned work-buddy home: {res['home']}")
    print(f"Data dir: {res['data_dir']}")
    for step in res["steps"]:
        print(f"  - {step}")
    print()
    _render_requirements(res["bootstrap"]["results"], title="Bootstrap:")
    sc = res.get("sidecar")
    if sc:
        pid = f" (pid={sc['pid']})" if sc.get("pid") else ""
        print(f"Sidecar: {sc.get('detail')}{pid}")
    print()
    print("Next: open Claude Code in this directory and run /wb-setup guided for")
    print("feature selection and the interactive integrations.")
    return EXIT_OK if res["ok"] else EXIT_FAIL


def cmd_uninstall(args) -> int:
    """Tear down machine integration; the OS uninstaller (or the user) removes files."""
    from work_buddy import provision as _prov

    res = _prov.uninstall()
    for step in res["steps"]:
        print(f"  - {step}")
    return EXIT_OK


def cmd_autostart(args) -> int:
    from work_buddy import autostart, paths

    action = getattr(args, "autostart_command", None)
    if action == "status":
        st = autostart.status()
        state = "registered" if st["registered"] else "not registered"
        print(f"autostart ({st['os']}): {state}")
        return EXIT_OK
    if action == "enable":
        res = autostart.register(
            python_exe=sys.executable,
            home_dir=paths.config_dir(),
            data_dir=paths._data_base(),
        )
        print(res.get("detail"))
        return EXIT_OK if res.get("ok") else EXIT_FAIL
    if action == "disable":
        res = autostart.unregister()
        print(res.get("detail"))
        return EXIT_OK if res.get("ok") else EXIT_FAIL
    _err(f"unknown autostart action: {action}")
    return EXIT_FAIL


# ---------------------------------------------------------------------------
# MCP config / dashboard
# ---------------------------------------------------------------------------

def _mcp_config() -> dict:
    from work_buddy.mcp_server.server import _get_port

    port = _get_port()
    return {
        "mcpServers": {
            "work-buddy": {"type": "http", "url": f"http://localhost:{port}/mcp"}
        }
    }


def _print_mcp_config() -> None:
    print(json.dumps(_mcp_config(), indent=2))


def cmd_mcp_print(args) -> int:
    _print_mcp_config()
    return EXIT_OK


def _dashboard_url() -> str:
    from work_buddy.config import load_config

    cfg = load_config()
    dash = cfg.get("dashboard", {}) or {}
    if dash.get("external_url"):
        return dash["external_url"]
    port = (
        cfg.get("sidecar", {})
        .get("services", {})
        .get("dashboard", {})
        .get("port", 5127)
    )
    return f"http://localhost:{port}"


def _print_dashboard_url(prefix: str = "") -> None:
    print(f"{prefix}{_dashboard_url()}")


def cmd_dashboard(args) -> int:
    url = _dashboard_url()
    print(url)
    if getattr(args, "open", False):
        import webbrowser

        webbrowser.open(url)
    return EXIT_OK
