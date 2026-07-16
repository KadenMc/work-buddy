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
dev_notes: 'The entities/ package mirrors projects/ structurally (store.py CRUD + migrations.py versioned ladder via storage.MigrationRunner) but is deliberately leaner: no revision-history tables, no soft-delete, no markdown-canonical sync. entities.db is in the architecture/backups VITAL_DBS set. Dependency install (uv): dependencies are managed by uv. `uv sync` builds the project `.venv` from `uv.lock` (add `--all-extras` for the optional groups), and `uv add` / `uv remove` update `pyproject.toml` and the lockfile together. There is a single environment, so a locked dep is present in the runtime as soon as `uv sync` has run. CI uses `uv sync` to materialize the same `.venv`. Path roots (paths.py): four roots back the layout, each defaulting to repo_root() so a clone is one undivided directory. repo_root() is the code and install anchor. install_root() is the work_buddy package dir. config_dir() holds config.yaml, config.local.yaml, and .env (override WORK_BUDDY_CONFIG_DIR). asset_root() holds the shipped trees knowledge/store, prompts, sidecar_jobs, .claude/commands, and docs (override WORK_BUDDY_ASSET_ROOT or a paths.asset_root config value). The data root drives data_dir() and resolve() (override WORK_BUDDY_DATA_DIR or an absolute paths.data_root). Consumers go through these accessors instead of re-deriving __file__, which is what lets work-buddy run when it is not a clone.'
---

work_buddy/ = Python package. Key top-level modules: agent_session.py (identity), artifacts.py (centralized store), paths.py (path resolution), tools.py (feature toggles), config.py (config.yaml + config.local.yaml overlay), consent.py (SQLite-backed), workflow.py (DAG engine), journal_day.py (timezone-aware logical Journal-day policy).

Key packages: knowledge/ (agent docs: query, index, store, editor, vault adapter), dashboard/ (Flask host + root frontend), settings/ (registry, broker, persistence, migrations), health/ (diagnostics engine, checks, components), collectors/ (16 sources), mcp_server/ (gateway + activity_ledger.py), sessions/ (conversation inspector), notifications/ (surfaces: obsidian, telegram, dashboard), messaging/ (inter-agent), memory/ (Hindsight), telegram/ (bot), threads/ (chat system; also the WorkItem base + Thread/Task sibling subtypes), work_item/ (WorkItem-family home: the Task write port — routes Task mutations to obsidian/tasks/mutations.py), projects/ (registry + Hindsight observations), entities/ (entity registry: SQLite store + migrations -- authored names, tags, aliases, append-only reference index), truth/ (scoped provenance-aware claim ledger and invariant engine -- see architecture/truth), triage/ (Chrome tab pipeline), llm/ (API wrappers), chrome_native_host/ (extension host), obsidian/ (bridge, vault_writer, tasks, tags, datacore, ktr, day_planner, vault_events, commands), calendar/, journal_backlog/, web/ (shared HTTP/Flask helpers), backups/ (vital-DB hot-backup, manifest, gh-CLI remote push, restore pipeline -- see architecture/backups), storage/ (cross-package storage infrastructure: MigrationRunner schema-version ladder -- see architecture/migrations), control/ (control-graph aggregator), vault_index/ (native chunk-level semantic index over configured Markdown roots: chunker, SQLite chunk+vector store, FTS5, hybrid search -- see architecture/vault-index), index/ (consolidated hybrid-search substrate: one SQLite DB across all partitions, FTS5+dense RRF, writer-gated incremental builds -- see architecture/consolidated-index), indexing/ (index-agnostic status seam over the IR/vault/knowledge/consolidated indexes), cli/ (the wbuddy shell CLI for bootstrap and sidecar lifecycle -- start/stop/status/setup/doctor/mcp print -- see operations/wb-cli).

dashboard-react/ = React dashboard source, UI foundations, widget library, Settings surface, development harnesses, and browser tests. Its production build is served by the Flask dashboard at `/app`.

knowledge/store/ = agent docs, one Markdown file per unit (directions, system, capability declarations, workflows). contracts/ = live data. sidecar_jobs/ = system scheduled jobs (git-tracked, ship with work-buddy).

<data_root>/ = all generated data (default `.data/`, gitignored; configured via `paths.data_root` in `config.yaml`): agents/ (per-session: consent.db, manifests, logs, ledgers), context/ (bundles), runtime/ (PID, state, tool status, per-service rotated logs), cache/ (LLM, chrome tabs, Claude Code usage DB), chrome/ (ledger), db/ (SQLite: messages, tasks, projects, threads, entities, and settings -- the vital DBs backed up by architecture/backups; plus the non-vital work_item_events.db WorkItem audit log, and the derived/rebuildable vault-index.db semantic chunk+vector store), logs/ (debug logs), commit/ (90d TTL), export/, report/, scratch/ (artifact types with TTL lifecycle), user_jobs/ (user-authored scheduled jobs -- personal cron tasks not shared across installs), backups/ (local snapshot tarballs + last_run.json health-check sentinel).

Truth state is intentionally scoped differently: each participating project or purpose root owns a `.wb-truth/` sidecar containing its profile, permanent identity, SQLite ledger, optional blobs, and deterministic recovery export. These stores live beside the material they describe rather than under the shared `<data_root>`; see `architecture/truth`.

.claude/commands/ = slash commands (thin launchers loading from knowledge store).
