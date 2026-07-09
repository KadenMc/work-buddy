"""``wbuddy`` CLI: the bootstrap and sidecar-lifecycle ramp for work-buddy.

``wbuddy`` is the shell entrypoint that takes a fresh user from "installed" to
"the sidecar is running and Claude Code is wired", and manages the sidecar
lifecycle afterward. It is deliberately NOT an operations surface: anything
that acts on work-buddy state goes through the ``wb_*`` MCP gateway. ``wbuddy``
owns only setup, sidecar lifecycle, diagnostics, and emitting the MCP config.

    wbuddy start [--foreground]  wbuddy stop    wbuddy restart
    wbuddy status [--json]       wbuddy doctor [<component>] [--json]
    wbuddy setup                 wbuddy mcp print   wbuddy dashboard [--open]
    wbuddy provision [...]       wbuddy autostart {enable,disable,status}
    wbuddy uninstall             wbuddy tray {enable,disable,status,run}

``provision`` is the native installer's one-shot entry point. The interactive,
domain-by-domain feature selection lives in ``/wb-setup guided`` inside Claude
Code, because that walk needs an agent.
"""

from __future__ import annotations

import argparse
import sys

from work_buddy.cli import commands


def _build_parser() -> argparse.ArgumentParser:
    # --json on a shared parent so verbs that support it accept the flag
    # before or after the subcommand. SUPPRESS keeps it absent unless given.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--json", action="store_true", default=argparse.SUPPRESS,
        help="emit machine-readable JSON on stdout",
    )

    parser = argparse.ArgumentParser(
        prog="wbuddy",
        description="work-buddy setup and sidecar lifecycle.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_start = sub.add_parser("start", help="start the sidecar (detached)")
    p_start.add_argument(
        "--foreground", "-f", action="store_true",
        help="run the sidecar in this terminal instead of detached",
    )

    sub.add_parser("stop", help="stop the running sidecar")
    sub.add_parser("restart", help="stop then start the sidecar")
    sub.add_parser("status", parents=[common], help="show sidecar + service status")

    p_doctor = sub.add_parser(
        "doctor", parents=[common], help="diagnose setup or a single component"
    )
    p_doctor.add_argument(
        "component", nargs="?", default=None,
        help="component id to diagnose (default: full status)",
    )

    sub.add_parser("setup", help="run bootstrap checks and print MCP wiring")

    p_dash = sub.add_parser("dashboard", help="print the dashboard URL")
    p_dash.add_argument("--open", action="store_true", help="open it in a browser")

    p_mcp = sub.add_parser("mcp", help="MCP config helpers")
    mcp_sub = p_mcp.add_subparsers(dest="mcp_command", required=True)
    mcp_sub.add_parser("print", help="print the Claude Code MCP config (HTTP)")

    p_prov = sub.add_parser(
        "provision", parents=[common],
        help="one-shot install provisioning (the native installer's entry point)",
    )
    p_prov.add_argument(
        "--home", default=None,
        help="install HOME / config dir (default: the running package's repo root)",
    )
    p_prov.add_argument(
        "--data-dir", default=None,
        help="per-user data dir (default: the OS per-user location)",
    )
    p_prov.add_argument("--vault-root", default=None, help="Obsidian vault path")
    p_prov.add_argument("--repos-root", default=None, help="git repos directory")
    p_prov.add_argument("--timezone", default=None, help="IANA timezone")
    p_prov.add_argument("--anthropic-key", default=None, help="Anthropic API key (sk-...)")
    p_prov.add_argument(
        "--no-start", action="store_true", help="do not start the sidecar afterward",
    )

    sub.add_parser(
        "uninstall",
        help="remove machine integration (stop sidecar, login task, PATH shim); user data is preserved",
    )

    p_auto = sub.add_parser("autostart", help="manage login auto-start of the sidecar")
    auto_sub = p_auto.add_subparsers(dest="autostart_command", required=True)
    auto_sub.add_parser("enable", help="register login auto-start")
    auto_sub.add_parser("disable", help="remove login auto-start")
    auto_sub.add_parser("status", help="show auto-start registration status")

    p_tray = sub.add_parser(
        "tray", help="manage the system-tray icon (needs the `tray` extra)"
    )
    tray_sub = p_tray.add_subparsers(dest="tray_command", required=True)
    tray_sub.add_parser(
        "enable", help="set tray.enabled, register the login item, start the tray"
    )
    tray_sub.add_parser("disable", help="stop the tray and remove its login item")
    tray_sub.add_parser("status", parents=[common], help="show tray state")
    tray_sub.add_parser(
        "run", help="run the tray in the foreground (login-item entry point)"
    )

    return parser


_HANDLERS = {
    "start": commands.cmd_start,
    "stop": commands.cmd_stop,
    "restart": commands.cmd_restart,
    "status": commands.cmd_status,
    "doctor": commands.cmd_doctor,
    "setup": commands.cmd_setup,
    "dashboard": commands.cmd_dashboard,
    "provision": commands.cmd_provision,
    "uninstall": commands.cmd_uninstall,
}


def main(argv: list[str] | None = None) -> int:
    raw = sys.argv[1:] if argv is None else argv
    parser = _build_parser()
    try:
        args = parser.parse_args(raw)
    except SystemExit as exc:  # argparse exits 2 on usage error
        return int(exc.code) if exc.code is not None else 1

    try:
        if args.command == "mcp":
            if args.mcp_command == "print":
                return commands.cmd_mcp_print(args)
            parser.error(f"unknown mcp subcommand: {args.mcp_command}")
            return 1
        if args.command == "autostart":
            return commands.cmd_autostart(args)
        if args.command == "tray":
            return commands.cmd_tray(args)
        handler = _HANDLERS.get(args.command)
        if handler is None:
            parser.error(f"unknown command: {args.command}")
            return 1
        return handler(args)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except Exception as exc:  # pragma: no cover - top-level guard
        print(f"error: {exc}", file=sys.stderr)
        return 1
