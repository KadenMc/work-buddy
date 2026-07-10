# Harness Projection Progress

This document summarizes the current harness-projection work and the local
findings that should stay visible while the broader setup roadmap continues.
It is intentionally narrower than the native-installer roadmap: it covers the
agent-harness bridge, not the whole setup/diagnostics redesign.

## Implemented

- Added `work_buddy/harness/` as the first harness projection layer.
- Added a rulesync subprocess backend using pinned `rulesync@9.6.0` through a
  configured command, installed `rulesync`, or `npx`.
- Added `wbuddy harness list`, `enable`, `disable`, `primary`, and `sync`.
- Added `wbuddy provision --harness`, `--no-harness`, and
  `--allow-experimental-harness`.
- Wired the Windows installer and `bootstrap.ps1` to pass a selected harness
  through provision.
- Added local harness config defaults under `harness:` in `config.example.yaml`
  and runtime defaults.
- Added generated-output ignores for `.codex/`, `.agents/`, `.claude/rules/`,
  and `AGENTS.md`.
- Added knowledge-store coverage for `harness` and updated `operations/wb-cli`.
- Added focused tests for the backend, CLI routing, provision behavior,
  installer wiring, and knowledge validation.

## Confirmed Locally

- `npx -y rulesync@9.6.0` works in this development environment.
- rulesync can generate Codex-shaped project artifacts from generated input:
  `AGENTS.md`, `.codex/config.toml`, and `.agents/skills/...`.
- rulesync can generate Claude Code command/rule/MCP artifacts from the same
  managed input tree.
- The Codex target did not accept the `commands` feature in the local spike, so
  the current Codex target uses `rules,mcp,skills` with simulated skills.
- Regenerated Claude Code command files are not byte-identical to the tracked
  `.claude/commands` launchers, so the implementation keeps those launchers as
  tracked source material.
- `rulesync import` can ingest `CLAUDE.md` and commands, but it did not carry
  `CLAUDE.local.md`; personal/local files remain outside projection.
- `wbuddy provision --harness claudecode --no-start` succeeded against an
  isolated home/data directory.
- `wbuddy provision --harness codexcli --no-start` rejects without
  `--allow-experimental-harness`.

## Important Gotchas

- Generated artifacts are not runtime parity. A Codex-shaped `AGENTS.md` and
  skill tree do not prove Codex can complete work-buddy setup.
- Codex remains experimental until session identity, environment propagation,
  MCP initialization, and any hook-equivalent path are proven end to end.
- The Windows installer does not install Node/npm. Harness projection currently
  needs `rulesync` or `npx`, so the installer offers Claude Code projection but
  defaults to skipping harness setup until the projection toolchain is bundled
  or otherwise guaranteed.
- `CLAUDE.local.md` should not be projected as a normal shared artifact. Treat
  local/personal files as host-local state unless a later design explicitly
  defines a private projection channel.
- Multi-harness generation can create root-level files for multiple hosts. Keep
  generated harness outputs disposable and gitignored unless a later migration
  promotes a specific file to source material.
- Existing user-authored host files need a preserve/merge policy before broad
  sync is safe. Current generation should be tested in controlled output roots
  or controlled install homes.
- The rulesync input tree under the data root is generated. Work-buddy behavior
  still belongs in the knowledge store, MCP runtime, and canonical launchers.

## Open Follow-Ups Specific to Harness Projection

- Decide how release installers obtain rulesync without assuming a developer
  Node/npm environment.
- Prove a fresh Codex session can load generated project instructions, skills,
  and MCP config, then call the work-buddy gateway after sidecar startup.
- Define the Codex session-init equivalent for `WORK_BUDDY_SESSION_ID` and
  `wb_init` so operation logging, consent, and workflows attach to the right
  session automatically.
- Decide whether Codex setup needs a generated skill, prompt, or another entry
  point for the setup handoff.
- Add a clean-machine installer test that includes harness selection once the
  projection toolchain is self-contained.
- Add a stale-output check path that is useful in CI without requiring rulesync
  network access.
- Define collision handling for users who already have `.codex/config.toml`,
  `.agents/skills`, `.claude/rules`, or generated `AGENTS.md`.

## Covered Elsewhere

The native-installer roadmap already owns the broader setup-experience redesign,
Linux/macOS installers, update lifecycle, knowledge-store overlay design, and
release process. This document should not absorb those tracks; it should only
carry harness-projection findings that would otherwise get lost between those
larger workstreams.
