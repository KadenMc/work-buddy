# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/), and this project adheres to [Semantic Versioning](https://semver.org/). During the `0.x` series, minor versions may contain breaking changes.

## [0.2.0] - 2026-05-20

Second release (pre-release). ~95 merged PRs since 0.1.0. Relicenses work-buddy to GPL-3.0-only, redesigns the task pipeline around *Getting Things Done*, adds local model inference, migrates the knowledge store to a file-per-unit substrate, and reworks the dashboard.

### License

- Relicensed from MIT to the GNU General Public License v3.0 (`GPL-3.0-only`); the 0.1.x line remains MIT. (#123)
- Adopted a Developer Certificate of Origin (not a CLA) for contributions — every commit is signed off, enforced by a required check. (#123, #124)

### Task pipeline — a Getting Things Done redesign

- Rebuilt the task pipeline around the five phases of *Getting Things Done* (GTD): items are captured without an immediate verdict, processed through a structured Clarify decision tree, and routed to typed destinations (task, reference, calendar, trash). (#59, #64)
- Separate first-class `outcome`, `next action`, and `definition-of-done` fields. (#60)
- An automation-tier model (0–4) formalizing where the agent acts autonomously vs. requires consent, grounded in a typed risk model. (#68)
- Action contexts (`@vault`, `@email_send`, …) and `/wb-task-me`, a re-runnable "what can I do now" surface. (#69)
- The Threads rework: one FSM-governed entity per captured item, decomposable into sub-threads, with autonomy composed per-transition. (#74, #98)

### Task infrastructure

- Hierarchical namespace tags with embedding-backed suggestion and a dashboard tree. (#42)
- Task-store hardening: a description column, atomic line mutations, multi-effect post-write-uncertain recovery, cross-session consent inheritance. (#61)
- `task_read` capability; `projects/` made first-class; `task_set_tags` unified to manage `#projects/*`. (#45, #47, #92)

### Local inference & embedding

- Local model inference: `llm_call` / `llm_submit` / `llm_with_tools` running local models through LM Studio (LM Link as the remote transport), with tool access governed by a gateway-enforced capability ACL. (#29)
- Unified LLM runner — one `LLMRunner.call(tier, …)` with semantic model tiers, a normalized response, and an error taxonomy, fed by a 13-source context pipeline. (#40)
- Inference broker with priority-classed slot scheduling; LM Studio added as an embedding provider, offloading document-side encoding to a remote device. (#52)

### Knowledge store

- File-per-unit Markdown substrate — each knowledge unit is now a single Markdown file instead of packed JSON. (#120)
- Op registry + capability declarations — the executable callable separated from the inert capability declaration; all ~199 capabilities migrated. (#119, #120)
- Unit-kind taxonomy evolved from 5 to 9 kinds; 89 units reclassified. (#89)
- `<<wb:path>>` cross-reference placeholders with cycle detection; per-source multi-projection retrieval (BM25 + dense via RRF). (#17, #36)

### Dashboard

- Live event bus — server-pushed SSE mutates only the cards an event touches, replacing the 30-second wholesale refresh. (#66)
- Settings rebuilt on a unified control graph, with a typed fix system and diagnostic help agents; the Status tab was absorbed into Settings and the Overview tab retired; cards became preference-gated. (#44, #110, #112, #113)
- New tabs: Jobs, Chats, Projects, and Memory (the entity registry). (#82, #103, #109, #122)
- Launch or resume Claude Code sessions from the dashboard. (#22, #53)

### Reliability, data safety & consent

- Data-safety model: soft-delete, a versioned migration ladder (`user_version` + audit + AST-stable hash check + downgrade guard), and off-machine backups to GitHub Releases. (#100, #101, #102)
- Artifact lifecycle system (`Storage × Lifecycle × Provenance`) replacing ad-hoc per-module pruners. (#94)
- MarkdownDB — a markdown-canonical two-way-sync abstraction (markdown is the source of truth; SQLite is a queryable projection). (#115)
- Typed Obsidian exception hierarchy and a three-layer closure of the double-write hazard; vault writes that would clobber unsaved editor typing are refused. (#56, #50)
- Consent hardening: call-stack-aware risk reduction, cross-session consent inheritance for sidecar replay, modal-consent routing. (#32, #61, #99)
- Sidecar: instance takeover on restart, bounded shutdown, user-authored cron jobs with parametric workflows, a filesystem watchdog, opt-in cron jitter. (#37, #82, #86)

### Integrations

- Thunderbird email triage — an email-provider abstraction and triage adapter; ships disabled by default. (#63)
- Multi-repo git context — context collection walks every repo under the repos root, attributing commits per project. (#51)
- Vault-recon collector — a daily pass surfacing recurring vault conventions via significance rules. (#84)
- Entity registry — an authored store of people, places, projects, and concepts, with hierarchical tags and a federated `entity_resolve`. (#122)
- Conversation observability — a durable, session-attributed activity DB derived from Claude Code session logs. (#109)
- Tailscale wired across all four health-system layers. (#91)

### Removed

- Earlier task surfaces — Review Queue, Daily Log, Engage, Blocked-by-Context — and the clarify pool, all superseded by Threads. (#74, #95)
- Status and Overview dashboard tabs. (#110)
- `_generated_capabilities.json` and `build.py`; the JSON knowledge-store loading path. (#120)
- The `module` knowledge-unit kind (folded into `reference`). (#89)
- `/wb-dev-commit` renamed `/wb-dev-pr`. (#93)

### Companion repositories

- `obsidian-work-buddy` (the Obsidian plugin, 0.1.0 → 0.1.1) — Obsidian community-plugin-review prep (plugin-ID rename, manifest cleanup, lint); an EditorConflict guard that refuses changes clobbering unsaved editor typing; a modal-consent message fix.
- `thunderbird-work-buddy` (new) — an initial read-only Thunderbird bridge, created to pair with the email triage.

## [0.1.0] - 2026-04-13

Initial open-source release (open beta).

### Core Framework

- **MCP Gateway** with dynamic capability discovery (`wb_search`, `wb_run`, `wb_advance`, `wb_status`)
- **Workflow Conductor** with DAG-based multi-step execution, auto-run steps for deterministic code, and persistent state
- **Knowledge Store** with 207 typed units (capabilities, workflows, directions, system docs) and hybrid BM25 + semantic search
- **Consent system** with session-scoped grants, multi-surface delivery, and workflow-level blanket consent
- **Notification system** with simultaneous delivery to Obsidian, Telegram, and web dashboard
- **Artifact store** with typed storage, TTL-based automatic cleanup, and session provenance
- **Retry queue** for automatic background retry of transient failures

### Integrations

- **Obsidian** bridge with plugin-level access to Tasks, Day Planner, Tag Wrangler, Smart Connections, Datacore, and Google Calendar
- **Persistent memory** via Hindsight integration (semantic search, reflection, pruning)
- **Telegram bot** for mobile consent approval, session control, and workflow triggers
- **Chrome extension** with tab export, semantic clustering, content extraction, and four-tier triage
- **Web dashboard** with session browsing, thread conversations, task board, and notification management

### Productivity

- **Task management** lifecycle: create, triage, assign, track, review, archive (90+ capabilities)
- **Contract system** with claims, evidence plans, stop rules, and WIP limits
- **Context collection** from 16 sources (git, Obsidian, conversations, Chrome, calendar, and more)
- **Journal system** with activity detection, sign-in tracking, day planning, and running notes processing
- **Project registry** with observations, memory, and auto-discovery
- **Metacognition** patterns for blindspot detection and minimal intervention

### Infrastructure

- **Sidecar supervisor** managing 5 long-running services with health checks and auto-restart
- **Inter-agent messaging** for asynchronous coordination between sessions
- **Embedding service** with shared dense vector index
- **Feature toggles** for dependency-aware subsystem management
- **Cross-platform support** for Windows, Linux, and macOS

### Developer Experience

- 34 slash commands loading from the knowledge store
- 15+ structured workflows (morning routine, weekly review, task triage, chrome triage, and more)
- Agent-oriented development loop (`/wb-dev`, `/wb-commit`, `/wb-task-handoff`)
- Knowledge store tooling: validation, programmatic editor, MkDocs documentation site

[0.2.0]: https://github.com/KadenMc/work-buddy/releases/tag/0.2.0
[0.1.0]: https://github.com/KadenMc/work-buddy/releases/tag/0.1.0
