---
name: Harness Projection
kind: concept
description: Agent-host projection for Claude Code, Codex, and future harnesses using generated rulesync input rather than duplicating work-buddy workflow behavior.
summary: Work-buddy owns workflow/capability behavior in the knowledge store and MCP runtime; rulesync projects host-native files, lifecycle hooks normalize session behavior, and transcript providers normalize conversation history.
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

- `codexcli`: `AGENTS.md`, `.codex/config.toml`, `.codex/hooks.json`, and `.agents/skills/*/SKILL.md`.
- `claudecode`: rules, MCP config, lifecycle hooks, and `.claude/commands/*`.

Rulesync is pinned by version. Installer provisioning downloads the matching standalone release binary into `<data_root>/tools/rulesync/<version>/`, verifies it against the release `SHA256SUMS`, and executes it directly. An exact-version PATH binary is accepted; pinned `npx` remains a development fallback.

Lifecycle and session identity:

- Both first-class harnesses project `SessionStart`, `UserPromptSubmit`, `PostToolUse`, and `Stop` through `wbuddy hook`.
- Native session identity is preserved: Claude supplies its session id and Codex supplies `CODEX_THREAD_ID` / hook `session_id`. Agents initialize the gateway with `wb_init(session_id=<native-id>, harness_id=<id>)`.
- Hook delivery records harness, native id, transcript path, cwd, and model in the session manifest. Stop blocks only when pending work-buddy messages need review.

Conversation ingestion is a separate provider boundary under `work_buddy/transcripts/`. Built-in `claudecode` and `codexcli` providers map native JSONL into canonical sessions, turns, and tool calls. Third-party harnesses can register providers through the `work_buddy.transcript_providers` Python entry-point group. Canonical data feeds context collection, session inspection, conversation observability, IR search, and Dashboard Chats without those consumers parsing one harness format directly.

First-run install:

- `wbuddy provision --harness <id>` selects one primary harness, persists it in `config.local.yaml`, and runs harness projection into the install home.
- The Windows installer offers Claude Code and Codex as setup-ready primary harnesses, with Claude Code as the default checked choice. Skipping harness setup remains available.
- A successful provision installs rulesync, generates the selected native surface, and fails the install result if projection fails.
- `--allow-experimental-harness` exists for development/testing, not ordinary first-run setup.

Do not edit generated harness outputs as the source of truth. Update the knowledge-store directions, MCP capabilities/workflows, or the canonical launcher source, then rerun `wbuddy harness sync`.

Git hygiene:

- `AGENTS.md`, `.codex/`, `.agents/`, and `.claude/rules/` are generated harness outputs and should stay gitignored.
- `.claude/commands/`, `.claude/hooks/session-init.sh`, `.claude/launch.json`, `.mcp.json`, and `CLAUDE.md` are still tracked inputs/compatibility files today. Do not gitignore or delete them until the source-of-truth migration explicitly moves them behind harness projection.
- `CLAUDE.local.md` remains personal and gitignored. When Codex is selected, sync privately projects it to gitignored `AGENTS.override.md` with an ownership marker. Work-buddy updates only its own marked file and preserves any unrelated existing override with a warning.
- Sync previews generated paths, backs up existing targets under `<data_root>/harness/backups/`, and restores them if generation fails. Generated files remain disposable; user-authored unowned local files are not.
