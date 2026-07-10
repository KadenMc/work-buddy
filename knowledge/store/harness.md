---
name: Harness Projection
kind: concept
description: Agent-host projection for Claude Code, Codex, and future harnesses using generated rulesync input rather than duplicating work-buddy workflow behavior.
summary: Work-buddy owns workflow/capability behavior in the knowledge store and MCP runtime; harness projection generates host-native instruction, MCP, command, and skill surfaces from that source. The first backend is rulesync, reached through `wbuddy harness ...`.
tags:
- harness
- rulesync
- codex
- claude-code
- setup
parents:
- architecture
- operations
---

Harness projection is the boundary between work-buddy's canonical behavior and agent-host-specific files.

The canonical behavior stays in:

- `knowledge/store/` directions, capabilities, and workflows.
- the `wb_*` MCP gateway and runtime services.
- existing thin launchers such as `.claude/commands/wb-dev-pr.md`, which load directions and start workflows instead of embedding behavior.

The generated harness surface is intentionally disposable. `wbuddy harness sync` builds `.rulesync/` input under `<data_root>/harness/rulesync-input` and invokes rulesync to project selected hosts:

- `codexcli`: `AGENTS.md`, `.codex/config.toml`, and `.agents/skills/*/SKILL.md`.
- `claudecode`: rules, MCP config, and `.claude/commands/*`.

First-run install:

- `wbuddy provision --harness <id>` selects one primary harness, persists it in `config.local.yaml`, and runs harness projection into the install home.
- Only setup-ready harnesses should be offered by installer flows. `claudecode` is setup-ready at the harness-behavior level, but rulesync still needs a local executable path (`rulesync` or `npx rulesync@<version>`), so the Windows installer defaults to skipping harness projection until the projection toolchain has a self-contained install story. `codexcli` is currently experimental because the surface generates, but session hook/env propagation has not been proven end-to-end.
- `--allow-experimental-harness` exists for development/testing, not ordinary first-run setup.

Do not edit generated harness outputs as the source of truth. Update the knowledge-store directions, MCP capabilities/workflows, or the canonical launcher source, then rerun `wbuddy harness sync`.

Git hygiene:

- `AGENTS.md`, `.codex/`, `.agents/`, and `.claude/rules/` are generated harness outputs and should stay gitignored.
- `.claude/commands/`, `.claude/hooks/session-init.sh`, `.claude/launch.json`, `.mcp.json`, and `CLAUDE.md` are still tracked inputs/compatibility files today. Do not gitignore or delete them until the source-of-truth migration explicitly moves them behind harness projection.
- `CLAUDE.local.md` remains personal, gitignored, and outside rulesync projection.
