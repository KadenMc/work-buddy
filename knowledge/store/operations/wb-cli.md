---
name: wbuddy CLI
kind: directions
description: 'The wbuddy shell CLI: bootstrap, provisioning, app launch, harness surface sync, sidecar lifecycle, and the deliberately scoped local Truth consumer surface. The wb_* gateway remains the agent operations surface.'
summary: '`wbuddy launch` makes the local runtime ready and opens the React dashboard; installer shortcuts reuse that operation through a dedicated console-less launcher. `wbuddy start` runs the sidecar detached, `wbuddy status` and `doctor` report health, and `wbuddy setup` plus `wbuddy mcp print` do pre-MCP bootstrap. `wbuddy harness ...` manages generated agent-host surfaces through rulesync. `wbuddy truth {capture,propose,query,confirm,migrate}` is the deliberate local-store exception. The wb_* MCP gateway remains the agent operations surface.'
trigger: user or agent needs to manage the local runtime from the shell, or a user needs to capture, review, query, confirm, or migrate a scoped Truth Store locally
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
- wbuddy harness
- wbuddy dashboard
- wbuddy launch
- wbuddy provision
- wbuddy uninstall
- wbuddy autostart
- wbuddy tray
- wbuddy truth
- wbuddy truth confirm
- start the sidecar
- wbuddy command
parents:
- operations
dev_notes: |
  Package `work_buddy/cli/`: `dispatch.py` (argparse subcommand tree plus `main(argv)`, mirroring `work_buddy/statusctl/cli.py`), `commands.py` (verb handlers plus text rendering), `lifecycle.py` (start/stop/status as pure functions returning dicts), and `truth.py` (the exact local Truth consumer surface). Harness projection lives under `work_buddy/harness/` with rulesync as the first backend; it builds generated `.rulesync/` input under the data root from current `.claude/commands` launchers and MCP config, then asks rulesync to project host-native outputs. Entry point `wbuddy = work_buddy.cli:main` in pyproject, also runnable as `python -m work_buddy.cli` via `__main__`. The command is `wbuddy` (not `wb`) because `wb` is the Weights & Biases console script, so a bare `wb` would shadow or be shadowed by wandb on any shared environment.

  Reuses existing plumbing rather than reimplementing it. Sidecar lifecycle goes through `sidecar.pid.check_existing_daemon` / `takeover_existing_daemon` and `sidecar.state.load_state`. The windowless detached launch uses `compat.detached_process_kwargs` (a hidden console its subprocesses inherit). The wizard output comes from `health.wizard.SetupWizard` and `health.requirements.RequirementChecker.check_bootstrap`. The MCP config uses `mcp_server.server._get_port`, so `wbuddy mcp print` cannot drift from the bound port. `start` classifies the existing daemon (down/booting/wedged/up) and takes over a wedged one rather than refusing; the daemon also takes over on boot. The `lifecycle` helpers never print, which keeps them unit-testable, and all rendering lives in `commands.py`. `provision` (verb -> `work_buddy/provision.py`) orchestrates the existing fixers, `check_bootstrap`, and `start_sidecar` into the installer's one-shot setup; `autostart` (verb -> `work_buddy/autostart/`) registers the login launcher per OS. Both reuse rather than reimplement.

  Platform packaging scripts wrap `provision` and `autostart`; they do not reimplement service lifecycle. Their uninstallers call `wbuddy uninstall` for integration teardown before applying platform-specific application-file and optional data removal.

  `wbuddy truth` is the one deliberate state-operation exception to the gateway rule. It discovers the nearest `.wb-truth` scope, or accepts `--store`, then opens that local Truth Store through the engine library. The frozen verbs are exactly `capture`, `propose`, `query`, `confirm`, and `migrate`. Agent writes prefer non-placeholder model and harness identity from the matching session manifest, reject conflicting environment claims, and durably distinguish a manifest-backed model from the caller-asserted environment fallback. Human confirmation may mint its single-use gesture only in an interactive local TTY outside a detected agent context. Agent sessions must use MCP per-invocation consent or supply a human-minted `--gesture`. Clearing local environment signals is outside this local-machine trust boundary.
---

`wbuddy` is work-buddy's shell command-line interface: the bootstrap and sidecar-lifecycle ramp. It is for the user (and for setup), not the agent's general operations surface. State operations go through the `wb_*` MCP gateway (see operations/mcp-gateway), except for the deliberately narrow `wbuddy truth` local-store consumer surface.

Installed as a console script (`wbuddy`) via pyproject, also runnable as `python -m work_buddy.cli`.

## Verbs

- `wbuddy start [--foreground]` -- start the sidecar. Detached by default (no console window), `--foreground` runs it in the current terminal. Idempotent for a healthy sidecar: an already-running (or still-booting) sidecar is reported, not duplicated, while a wedged one is taken over.
- `wbuddy stop` -- stop the running sidecar and its child services.
- `wbuddy restart` -- stop then start.
- `wbuddy status [--json]` -- sidecar liveness, uptime, and per-service health, read from the sidecar state file. Distinguishes booting from wedged; exits non-zero when not running or wedged. Also reports the daemon's dispatch loop: a phase busy past ~2 minutes prints as busy with the running job's name (scheduled work is queued behind it, supervision unaffected), otherwise the time since the last completed dispatch cycle.
- `wbuddy doctor [<component>] [--json]` -- render the setup wizard's status, or one component's diagnosis: bootstrap, requirements, health.
- `wbuddy setup` -- run bootstrap checks, print the gateway MCP config, and point to the selected harness's generated `wb-setup` command or skill for interactive feature selection.
- `wbuddy mcp print` -- emit the gateway MCP config (HTTP, the gateway port) to stdout.
- `wbuddy harness list [--json]` -- list supported agent-host harnesses (`claudecode`, `codexcli`), their rulesync target ids, feature projection, and selection state.
- `wbuddy harness enable <id>` / `disable <id>` / `primary <id>` -- update the local `harness:` selection in `config.local.yaml`. The harness selection is local-machine state, not a workflow operation.
- `wbuddy harness sync [--target <id> ...] [--dry-run] [--check] [--json] [--output-root <path>] [--no-install-toolchain]` -- generate or check agent-host artifacts through pinned rulesync. Ordinary sync and provision install the checksum-verified standalone binary when needed; the opt-out is for controlled development environments. Sync previews paths, backs up existing generated files, rolls back on failure, and projects owned local Codex overrides without clobbering unrelated files. Codex receives `rules,mcp,skills,hooks`; Claude Code receives `rules,mcp,commands,skills,hooks`.
- `wbuddy harness doctor [--json]` -- report configured, PATH, managed, or pinned-npx rulesync availability and exact-version agreement.
- `wbuddy hook {session-start,user-prompt-submit,post-tool-use,stop} --harness <id>` -- internal JSON stdin/stdout lifecycle bridge used by generated native hook files. Users normally do not invoke it directly.
- `wbuddy dashboard [--open]` -- print (or open) the dashboard URL.
- `wbuddy launch` -- the terminal/admin form of the shared app-launch operation. It idempotently starts or recovers the sidecar, best-effort ensures the tray when enabled, waits until the React dashboard at `/app/` returns successfully, then focuses an existing matching browser tab or opens one. It fails instead of opening a dead page when the app does not become ready. Installed Windows shortcuts, Linux `.desktop` entries, and `Work Buddy.app` on macOS reuse this operation through the console-less `work_buddy.desktop_launcher`, which records `<data_root>/logs/desktop_launcher.log` and presents a native error surface when available.
- `wbuddy provision [--home ...] [--data-dir ...] [--vault-root ...] [--repos-root ...] [--timezone ...] [--anthropic-key ...] [--harness <id>] [--no-harness] [--allow-experimental-harness] [--no-start]` -- the native installer's one-shot entry point. `--home` targets a specific install dir. It seeds config, relocates mutable state, pins the interpreter, writes secrets and MCP wiring, optionally selects one setup-ready primary harness, installs pinned rulesync, projects the native harness surface, publishes the CLI shim, runs bootstrap checks, and starts the sidecar. Harness projection failure fails provision. Idempotent.
- `wbuddy uninstall` -- tear down machine integration: stop the sidecar, remove the login auto-start task, and remove the PATH shim. User data is preserved. The Windows uninstaller and the Linux/macOS artifact uninstall helpers invoke this before removing application files; their explicit remove-data modes are separate from this command.
- `wbuddy autostart {enable,disable,status}` -- manage login auto-start of the detached sidecar (Windows Task Scheduler `WB-Sidecar`, Linux systemd `--user` unit, macOS launchd agent), via `work_buddy/autostart/`.
- `wbuddy tray {enable,disable,status,run}` -- manage the system-tray icon (needs the `tray` extra). `enable` sets `tray.enabled`, registers the `WB-Tray` login item, and starts the tray; `disable` reverses all three; `status` reports enabled/registered/running; `run` is the foreground login-item entry point. The tray is a separate process and login item, NOT a sidecar-supervised service -- see services/tray.
- `wbuddy truth {capture,propose,query,confirm,migrate} [--store ...] [--json]` -- discover or select a scoped local Truth Store and use the frozen consumer surface directly. `capture` records immutable evidence and an optional quote span, `propose` writes a profile-valid claim, `query` exposes current/as-of/review/conflict views, `confirm` requires a local-human interactive TTY or an existing human gesture, and `migrate` opens one store or every registered store. Detected agent sessions cannot mint an interactive human gesture.

## When to use

- First-run bootstrap before MCP is wired: `wbuddy setup`, `wbuddy mcp print`.
- Sidecar lifecycle from the shell instead of `python -m work_buddy.sidecar`: `wbuddy start` / `stop` / `restart` / `status`.
- Terminal launch of the complete local app: `wbuddy launch`. Installed Windows Start/Desktop shortcuts, Linux application entries, and the macOS app bundle use the console-less wrapper around the same operation.
- Interactive, domain-by-domain feature selection stays in the generated `wb-setup` command/skill inside the selected harness because that walk needs an agent. `wbuddy setup` is its pre-MCP shell-side complement.
- Local Truth capture, proposal, query, human review, and schema migration: `wbuddy truth ...`. Agents should use the corresponding `truth_*` MCP capabilities for lifecycle operations and per-invocation confirmation authority.
