---
name: wbuddy CLI
kind: directions
description: 'The wbuddy shell CLI: bootstrap, provisioning, and sidecar-lifecycle ramp (start/stop/status/doctor/setup/provision/uninstall/autostart/mcp print). Not the agent operations surface, the wb_* gateway stays that.'
summary: '`wbuddy start` runs the sidecar detached, `wbuddy status` and `doctor` report health, and `wbuddy setup` plus `wbuddy mcp print` do pre-MCP bootstrap. A console script (via pyproject) and also `python -m work_buddy.cli`. The wb_* MCP gateway remains the agent operations surface.'
trigger: user or agent needs to start/stop/check the sidecar from the shell, run bootstrap setup, or print the MCP config
tags:
- cli
- shell
- sidecar
- setup
- lifecycle
- operations
aliases:
- wbuddy start
- wbuddy stop
- wbuddy restart
- wbuddy status
- wbuddy doctor
- wbuddy setup
- wbuddy mcp print
- wbuddy dashboard
- wbuddy provision
- wbuddy uninstall
- wbuddy autostart
- start the sidecar
- wbuddy command
parents:
- operations
dev_notes: |
  Package `work_buddy/cli/`: `dispatch.py` (argparse subcommand tree plus `main(argv)`, mirroring `work_buddy/statusctl/cli.py`), `commands.py` (verb handlers plus text rendering), and `lifecycle.py` (start/stop/status as pure functions returning dicts). Entry point `wbuddy = work_buddy.cli:main` in pyproject, also runnable as `python -m work_buddy.cli` via `__main__`. The command is `wbuddy` (not `wb`) because `wb` is the Weights & Biases console script, so a bare `wb` would shadow or be shadowed by wandb on any shared environment.

  Reuses existing plumbing rather than reimplementing it. Sidecar lifecycle goes through `sidecar.pid.check_existing_daemon` / `takeover_existing_daemon` and `sidecar.state.load_state`. The windowless detached launch uses `compat.detached_process_kwargs` (a hidden console its subprocesses inherit). The wizard output comes from `health.wizard.SetupWizard` and `health.requirements.RequirementChecker.check_bootstrap`. The MCP config uses `mcp_server.server._get_port`, so `wbuddy mcp print` cannot drift from the bound port. `start` classifies the existing daemon (down/booting/wedged/up) and takes over a wedged one rather than refusing; the daemon also takes over on boot. The `lifecycle` helpers never print, which keeps them unit-testable, and all rendering lives in `commands.py`. `provision` (verb -> `work_buddy/provision.py`) orchestrates the existing fixers, `check_bootstrap`, and `start_sidecar` into the installer's one-shot setup; `autostart` (verb -> `work_buddy/autostart/`) registers the login launcher per OS. Both reuse rather than reimplement.
---

`wbuddy` is work-buddy's shell command-line interface: the bootstrap and sidecar-lifecycle ramp. It is for the user (and for setup), NOT the agent's operations surface. Anything that acts on work-buddy state goes through the `wb_*` MCP gateway (see operations/mcp-gateway), and `wbuddy` deliberately does not duplicate it.

Installed as a console script (`wbuddy`) via pyproject, also runnable as `python -m work_buddy.cli`.

## Verbs

- `wbuddy start [--foreground]` -- start the sidecar. Detached by default (no console window), `--foreground` runs it in the current terminal. Idempotent for a healthy sidecar: an already-running (or still-booting) sidecar is reported, not duplicated, while a wedged one is taken over.
- `wbuddy stop` -- stop the running sidecar and its child services.
- `wbuddy restart` -- stop then start.
- `wbuddy status [--json]` -- sidecar liveness, uptime, and per-service health, read from the sidecar state file. Distinguishes booting from wedged; exits non-zero when not running or wedged.
- `wbuddy doctor [<component>] [--json]` -- render the setup wizard's status, or one component's diagnosis: bootstrap, requirements, health.
- `wbuddy setup` -- run the bootstrap checks, print the Claude Code MCP config, and point to `/wb-setup guided` for the interactive feature selection.
- `wbuddy mcp print` -- emit the Claude Code MCP config (HTTP, the gateway port) to stdout.
- `wbuddy dashboard [--open]` -- print (or open) the dashboard URL.
- `wbuddy provision [--home ...] [--data-dir ...] [--vault-root ...] [--repos-root ...] [--timezone ...] [--anthropic-key ...] [--no-start]` -- the native installer's one-shot entry point. `--home` targets a specific install dir (redirects `config_dir`, the one safe way since `config_dir()` is env-var-only), defaulting to the running package's repo root. Seeds `config.yaml` from the template, relocates the mutable-state tree to a per-user data dir (absolute `paths.data_root`), pins `sidecar.python_executable` to the running interpreter, writes secrets to `.env`, refreshes `.mcp.json`, publishes `wbuddy` on the user's PATH (a one-command shim, best-effort: `<home>\bin\wbuddy.cmd` plus a per-user PATH entry on Windows, `~/.local/bin/wbuddy` on POSIX; the venv's own `Scripts`/`bin` never lands on PATH), runs the bootstrap checks, and starts the sidecar. Idempotent. Logic in `work_buddy/provision.py` and `work_buddy/userpath.py`.
- `wbuddy uninstall` -- tear down machine integration: stop the sidecar, remove the login auto-start task, and remove the PATH shim. User data is preserved; removing the install directory itself is the OS uninstaller's (or the user's) job. The Windows uninstaller invokes this before deleting files.
- `wbuddy autostart {enable,disable,status}` -- manage login auto-start of the detached sidecar (Windows Task Scheduler `WB-Sidecar`, Linux systemd `--user` unit, macOS launchd agent), via `work_buddy/autostart/`.

## When to use

- First-run bootstrap before MCP is wired: `wbuddy setup`, `wbuddy mcp print`.
- Sidecar lifecycle from the shell instead of `python -m work_buddy.sidecar`: `wbuddy start` / `stop` / `restart` / `status`.
- The interactive, domain-by-domain feature selection stays in `/wb-setup guided` inside Claude Code, because that walk needs an agent. `wbuddy setup` is its pre-MCP, shell-side complement.
