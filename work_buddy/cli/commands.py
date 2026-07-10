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
from pathlib import Path

EXIT_OK = 0
EXIT_FAIL = 1

_GLYPH = {True: "ok  ", False: "FAIL"}


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
    _ensure_tray()
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
        _ensure_tray()
        return EXIT_OK
    _err(start["detail"])
    return EXIT_FAIL


def _ensure_tray() -> None:
    """Best-effort tray resurrection on any deliberate start (never fails it).

    Quiet unless it actually did something or genuinely failed: printing
    "tray disabled" on every start would be noise.
    """
    try:
        from work_buddy import tray

        res = tray.ensure_running()
        if res.get("spawned") or not res.get("ok"):
            print(f"Tray: {res.get('detail')}")
    except Exception:
        pass


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
    ticking (the freshness ``Last tick`` reports). The busy classification
    (threshold and shape) lives in ``lifecycle.dispatch_busy``, shared with
    the tray so the two surfaces can never disagree. State files written by
    a daemon without dispatch fields print nothing.
    """
    from work_buddy.cli import lifecycle

    busy = lifecycle.dispatch_busy(st)
    if busy:
        job = f" (job '{busy['job']}')" if busy["job"] else ""
        print(
            f"Dispatch: busy in {busy['phase']}{job} for "
            f"{_fmt_duration(busy['busy_for_s'])}, scheduled work is queued behind it"
        )
    elif st.last_dispatch_at:
        print(
            f"Last dispatch cycle: "
            f"{_fmt_duration(time.time() - st.last_dispatch_at)} ago"
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
        harness=getattr(args, "harness", None),
        no_harness=getattr(args, "no_harness", False),
        allow_experimental_harness=getattr(args, "allow_experimental_harness", False),
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
    hs = res.get("harness")
    if hs:
        print(
            f"Harness: {hs.get('id')} "
            f"({'ok' if hs.get('ok') else 'failed'})"
        )
        if hs.get("setup_note"):
            print(f"  note: {hs['setup_note']}")
    print()
    if hs and hs.get("id") == "claudecode":
        print("Next: open Claude Code in this directory and run /wb-setup guided for")
        print("feature selection and the interactive integrations.")
    elif hs and hs.get("id") == "codexcli":
        print("Next: open Codex in this directory and invoke the generated wb-setup")
        print("skill in guided mode for feature selection and integrations.")
    elif hs:
        print("Next: open your selected harness in this directory and invoke its")
        print("generated wb-setup command or skill in guided mode.")
    else:
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


def cmd_harness(args) -> int:
    from work_buddy.harness.config import load_harness_config, save_harness_selection
    from work_buddy.harness.registry import get_harness, list_harnesses
    from work_buddy.harness.sync import sync_harnesses

    action = getattr(args, "harness_command", None)
    cfg = load_harness_config()

    if action == "list":
        rows = [
            {
                "id": h.id,
                "label": h.label,
                "enabled": h.id in cfg.enabled,
                "primary": h.id == cfg.primary,
                "rulesync_target": h.rulesync_target,
                "features": list(h.features),
                "description": h.description,
                "support_tier": h.support_tier,
                "setup_ready": h.setup_ready,
                "session_env": h.session_env,
                "transcript_provider": h.transcript_provider,
                "lifecycle_events": list(h.lifecycle_events),
            }
            for h in list_harnesses()
        ]
        if _want_json(args):
            print(json.dumps({"harnesses": rows}, indent=2))
            return EXIT_OK
        for row in rows:
            marks = []
            if row["enabled"]:
                marks.append("enabled")
            if row["primary"]:
                marks.append("primary")
            suffix = f" ({', '.join(marks)})" if marks else ""
            print(f"{row['id']}: {row['label']}{suffix}")
            print(f"  {row['description']}")
            print(f"  support: {row['support_tier']}; session: {row['session_env']}")
        return EXIT_OK

    if action == "doctor":
        from work_buddy.harness.toolchain import rulesync_status

        status = rulesync_status(cfg)
        payload = {
            "ok": bool(status["available"] and status["version_ok"]),
            "rulesync": status,
            "enabled": list(cfg.enabled),
            "primary": cfg.primary,
        }
        if _want_json(args):
            print(json.dumps(payload, indent=2))
        else:
            state = "ok" if payload["ok"] else "unavailable"
            print(f"rulesync: {state}")
            print(f"  command: {status['command']}")
            print(
                f"  version: {status['version'] or '(unknown)'} "
                f"(expected {status['expected_version']})"
            )
            print(f"enabled: {', '.join(cfg.enabled) or '(none)'}")
            print(f"primary: {cfg.primary or '(none)'}")
        return EXIT_OK if payload["ok"] else EXIT_FAIL

    if action == "enable":
        hid = getattr(args, "harness_id")
        get_harness(hid)
        enabled = tuple(dict.fromkeys((*cfg.enabled, hid)))
        next_cfg = save_harness_selection(enabled=enabled)
        print(f"enabled harnesses: {', '.join(next_cfg.enabled) or '(none)'}")
        return EXIT_OK

    if action == "disable":
        hid = getattr(args, "harness_id")
        get_harness(hid)
        enabled = tuple(x for x in cfg.enabled if x != hid)
        primary = "" if cfg.primary == hid else cfg.primary
        next_cfg = save_harness_selection(enabled=enabled, primary=primary)
        print(f"enabled harnesses: {', '.join(next_cfg.enabled) or '(none)'}")
        return EXIT_OK

    if action == "primary":
        hid = getattr(args, "harness_id")
        get_harness(hid)
        enabled = cfg.enabled if hid in cfg.enabled else tuple((*cfg.enabled, hid))
        save_harness_selection(enabled=enabled, primary=hid)
        print(f"primary harness: {hid}")
        return EXIT_OK

    if action == "sync":
        try:
            res = sync_harnesses(
                getattr(args, "target", None),
                output_root=(
                    Path(args.output_root)
                    if getattr(args, "output_root", None)
                    else None
                ),
                dry_run=getattr(args, "dry_run", False),
                check=getattr(args, "check", False),
                install_toolchain=not getattr(args, "no_install_toolchain", False),
            )
        except ValueError as exc:
            _err(str(exc))
            return EXIT_FAIL
        if _want_json(args):
            print(json.dumps(
                {
                    "ok": res.ok,
                    "returncode": res.returncode,
                    "targets": list(res.targets),
                    "input_root": str(res.input_root),
                    "output_root": str(res.output_root),
                    "generated_paths": res.generated_paths,
                    "has_diff": res.has_diff,
                    "total_files": res.total_files,
                    "error": res.error,
                    "stderr": res.stderr,
                    "warnings": res.warnings,
                    "backup_dir": str(res.backup_dir) if res.backup_dir else None,
                },
                indent=2,
            ))
            return EXIT_OK if res.ok else EXIT_FAIL
        print(f"targets: {', '.join(res.targets)}")
        print(f"input: {res.input_root}")
        print(f"output: {res.output_root}")
        if res.generated_paths:
            print("paths:")
            for path in res.generated_paths:
                print(f"  - {path}")
        if res.has_diff is not None:
            print(f"has diff: {str(res.has_diff).lower()}")
        if res.error:
            _err(res.error)
        for warning in res.warnings:
            _err(f"warning: {warning}")
        if res.backup_dir:
            print(f"backup: {res.backup_dir}")
        if res.stderr:
            _err(res.stderr.strip())
        return EXIT_OK if res.ok else EXIT_FAIL

    _err(f"unknown harness action: {action}")
    return EXIT_FAIL


def cmd_hook(args) -> int:
    """Bridge native harness hook JSON from stdin into work-buddy."""
    from work_buddy.harness.hooks import handle_hook, parse_hook_payload

    try:
        payload = parse_hook_payload(sys.stdin.read())
        result = handle_hook(
            getattr(args, "event"),
            harness_id=getattr(args, "harness"),
            payload=payload,
        )
    except ValueError as exc:
        _err(str(exc))
        return EXIT_FAIL
    if result is not None:
        print(json.dumps(result, ensure_ascii=False))
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


def cmd_tray(args) -> int:
    """``wbuddy tray {enable,disable,status,run}``: the system-tray icon.

    ``enable``/``disable`` keep three things in lockstep: the ``tray.enabled``
    config flag (which gates the ``wbuddy start`` resurrection hook), the
    WB-Tray login item, and the running process.
    """
    from work_buddy import autostart, paths, tray

    action = getattr(args, "tray_command", None)

    if action == "status":
        from work_buddy.config import load_config

        enabled = bool((load_config().get("tray") or {}).get("enabled"))
        registered = autostart.tray_is_registered()
        pid = tray.running_pid()
        if _want_json(args):
            print(json.dumps(
                {"enabled": enabled, "registered": registered,
                 "running": pid is not None, "pid": pid},
                indent=2,
            ))
            return EXIT_OK
        run_state = f"running (pid={pid})" if pid else "not running"
        print(
            f"tray: {'enabled' if enabled else 'disabled'}, login item "
            f"{'registered' if registered else 'not registered'}, {run_state}"
        )
        return EXIT_OK

    if action == "enable":
        from work_buddy.health import fixers

        ok, detail, _ = fixers._set_config_value("tray.enabled", True)
        if not ok:
            _err(f"could not set tray.enabled: {detail}")
            return EXIT_FAIL
        reg = autostart.register_tray(
            python_exe=sys.executable,
            home_dir=paths.config_dir(),
            data_dir=paths._data_base(),
        )
        print(reg.get("detail"))
        if not reg.get("ok"):
            return EXIT_FAIL
        res = tray.ensure_running()
        print(res.get("detail"))
        return EXIT_OK if res.get("ok") else EXIT_FAIL

    if action == "disable":
        from work_buddy.health import fixers

        stop = tray.stop_running()
        print(stop.get("detail"))
        reg = autostart.unregister_tray()
        print(reg.get("detail"))
        ok, detail, _ = fixers._set_config_value("tray.enabled", False)
        if not ok:
            _err(f"could not set tray.enabled: {detail}")
            return EXIT_FAIL
        return EXIT_OK if reg.get("ok") else EXIT_FAIL

    if action == "run":
        from work_buddy.tray.__main__ import main as tray_main

        return tray_main()

    _err(f"unknown tray action: {action}")
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


def dashboard_url() -> str:
    """Resolve the dashboard URL (``dashboard.external_url``, else localhost).

    Public: the tray's "Open dashboard" action resolves through this too, so
    every surface honors the same external-URL override.
    """
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


_dashboard_url = dashboard_url  # back-compat for existing internal callers


def dashboard_local_url() -> str:
    """The machine-local dashboard URL, ignoring ``external_url``.

    The tray is inherently local, so it targets localhost for tab focus/create
    (matching the ``/api/open-dashboard`` precedent); ``external_url`` is for
    reaching the dashboard from OTHER devices, not from the local tray.
    """
    from work_buddy.config import load_config

    port = (
        load_config()
        .get("sidecar", {})
        .get("services", {})
        .get("dashboard", {})
        .get("port", 5127)
    )
    return f"http://127.0.0.1:{port}"


def _print_dashboard_url(prefix: str = "") -> None:
    print(f"{prefix}{_dashboard_url()}")


def cmd_dashboard(args) -> int:
    url = _dashboard_url()
    print(url)
    if getattr(args, "open", False):
        import webbrowser

        webbrowser.open(url)
    return EXIT_OK
