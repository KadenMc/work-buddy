# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/), and this project adheres to [Semantic Versioning](https://semver.org/). During the `0.x` series, minor versions may contain breaking changes.

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

[0.1.0]: https://github.com/KadenMc/work-buddy/releases/tag/v0.1.0
