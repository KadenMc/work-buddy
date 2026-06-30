---
name: wb CLI
kind: directions
description: 'The wb shell CLI: bootstrap and sidecar-lifecycle ramp (start/stop/status/doctor/setup/mcp print). Not the agent operations surface, the wb_* gateway stays that.'
summary: '`wb start` runs the sidecar detached, `wb status` and `doctor` report health, and `wb setup` plus `wb mcp print` do pre-MCP bootstrap. A console script (via pyproject) and also `python -m work_buddy.cli`. The wb_* MCP gateway remains the agent operations surface.'
trigger: user or agent needs to start/stop/check the sidecar from the shell, run bootstrap setup, or print the MCP config
tags:
- cli
- shell
- sidecar
- setup
- lifecycle
- operations
aliases:
- wb start
- wb stop
- wb restart
- wb status
- wb doctor
- wb setup
- wb mcp print
- wb dashboard
- start the sidecar
- wb command
parents:
- operations
dev_notes: |
  Package `work_buddy/cli/`: `dispatch.py` (argparse subcommand tree plus `main(argv)`, mirroring `work_buddy/statusctl/cli.py`), `commands.py` (verb handlers plus text rendering), and `lifecycle.py` (start/stop/status as pure functions returning dicts). Entry point `wb = work_buddy.cli:main` in pyproject, also runnable as `python -m work_buddy.cli` via `__main__`.

  Reuses existing plumbing rather than reimplementing it. Sidecar lifecycle goes through `sidecar.pid.check_existing_daemon` / `takeover_existing_daemon` and `sidecar.state.load_state`. The no-console detached launch uses `compat.detached_process_kwargs`. The wizard output comes from `health.wizard.SetupWizard` and `health.requirements.RequirementChecker.check_bootstrap`. The MCP config uses `mcp_server.server._get_port`, so `wb mcp print` cannot drift from the bound port. `start` is idempotent because the daemon also takes over on boot. The `lifecycle` helpers never print, which keeps them unit-testable, and all rendering lives in `commands.py`.
---

`wb` is work-buddy's shell command-line interface: the bootstrap and sidecar-lifecycle ramp. It is for the user (and for setup), NOT the agent's operations surface. Anything that acts on work-buddy state goes through the `wb_*` MCP gateway (see operations/mcp-gateway), and `wb` deliberately does not duplicate it.

Installed as a console script (`wb`) via pyproject, also runnable as `python -m work_buddy.cli`.

## Verbs

- `wb start [--foreground]` -- start the sidecar. Detached by default (no console window), `--foreground` runs it in the current terminal. Idempotent: an already-running sidecar is reported, not duplicated.
- `wb stop` -- stop the running sidecar and its child services.
- `wb restart` -- stop then start.
- `wb status [--json]` -- sidecar liveness, uptime, and per-service health, read from the sidecar state file. Exits non-zero when not running.
- `wb doctor [<component>] [--json]` -- render the setup wizard's status, or one component's diagnosis: bootstrap, requirements, health.
- `wb setup` -- run the bootstrap checks, print the Claude Code MCP config, and point to `/wb-setup guided` for the interactive feature selection.
- `wb mcp print` -- emit the Claude Code MCP config (HTTP, the gateway port) to stdout.
- `wb dashboard [--open]` -- print (or open) the dashboard URL.

## When to use

- First-run bootstrap before MCP is wired: `wb setup`, `wb mcp print`.
- Sidecar lifecycle from the shell instead of `python -m work_buddy.sidecar`: `wb start` / `stop` / `restart` / `status`.
- The interactive, domain-by-domain feature selection stays in `/wb-setup guided` inside Claude Code, because that walk needs an agent. `wb setup` is its pre-MCP, shell-side complement.
