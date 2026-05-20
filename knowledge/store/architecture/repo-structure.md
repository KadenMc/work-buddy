---
name: Repository Structure
kind: concept
description: Directory layout — what lives where, with subsystem README pointers
summary: work_buddy/ = Python package. knowledge/store/ = workflow definitions + agent docs. agents/ = per-session (gitignored). .claude/commands/ = slash commands.
tags:
- repo
- structure
- directories
- layout
aliases:
- directory layout
- where is
- file location
- project structure
parents:
- architecture
- architecture
dev_notes: 'The entities/ package mirrors projects/ structurally (store.py CRUD + migrations.py versioned ladder via storage.MigrationRunner) but is deliberately leaner: no revision-history tables, no soft-delete, no markdown-canonical sync. entities.db is durable user data and SHOULD eventually join the architecture/backups vital set -- that wiring was scoped out of entity-registry v1 and is a known follow-up.'
---

work_buddy/ = Python package. Key top-level modules: agent_session.py (identity), artifacts.py (centralized store), paths.py (path resolution), tools.py (feature toggles), config.py (config.yaml + config.local.yaml overlay), consent.py (SQLite-backed), workflow.py (DAG engine).

Key packages: knowledge/ (agent docs: query, index, store, editor, vault adapter), dashboard/ (Flask + frontend/ package), health/ (diagnostics engine, checks, components), collectors/ (16 sources), mcp_server/ (gateway + activity_ledger.py), sessions/ (conversation inspector), notifications/ (surfaces: obsidian, telegram, dashboard), messaging/ (inter-agent), memory/ (Hindsight), telegram/ (bot), threads/ (chat system), projects/ (registry + Hindsight observations), entities/ (entity registry: SQLite store + migrations -- authored names, tags, aliases, append-only reference index), triage/ (Chrome tab pipeline), llm/ (API wrappers), chrome_native_host/ (extension host), obsidian/ (bridge, vault_writer, tasks, tags, smart, datacore, ktr, day_planner, vault_events, commands), calendar/, journal_backlog/, web/ (shared HTTP/Flask helpers), backups/ (vital-DB hot-backup, manifest, gh-CLI remote push, restore pipeline -- see architecture/backups), storage/ (cross-package storage infrastructure: MigrationRunner schema-version ladder -- see architecture/migrations), control/ (control-graph aggregator).

knowledge/store/ = workflow definitions + agent docs (JSON). contracts/ = live data. sidecar_jobs/ = system scheduled jobs (git-tracked, ship with work-buddy).

<data_root>/ = all generated data (default `.data/`, gitignored; configured via `paths.data_root` in `config.yaml`): agents/ (per-session: consent.db, manifests, logs, ledgers), context/ (bundles), runtime/ (PID, state, tool status, per-service rotated logs), cache/ (LLM, chrome tabs, Claude Code usage DB), chrome/ (ledger), db/ (SQLite: messages, tasks, projects, threads -- the four vital DBs backed up by architecture/backups; plus entities -- the entity-registry DB, not yet registered with the backup set), logs/ (debug logs), commit/ (90d TTL), export/, report/, scratch/ (artifact types with TTL lifecycle), user_jobs/ (user-authored scheduled jobs -- personal cron tasks not shared across installs), backups/ (local snapshot tarballs + last_run.json health-check sentinel).

.claude/commands/ = slash commands (thin launchers loading from knowledge store).
