---
name: Context Pipeline
kind: reference
description: Unified two-stage context collection + curation. ContextCollector fetches raw JSON from registered sources (git, tasks, projects, chrome + 9 markdown wrappers), ContextCurator renders into depth-adapted markdown or JSON. Feeds LLM prompts (build_triage_context retrofits onto this) and bundle files (collect.py retrofits onto this).
summary: Two-stage pipeline. ContextCollector fetches raw JSON per source with cache awareness (max_age + source-level is_stale). ContextCurator renders any cached Context into markdown or JSON at the caller's depth. 13 registered sources split into structured (git/tasks/projects/chrome with drill-down) and markdown-wrapper (9 sources delegating to legacy collectors). Exposed over MCP as context_block + context_drill_down.
entry_points:
- work_buddy.context.types
- work_buddy.context.collector
- work_buddy.context.curator
- work_buddy.context.cache
- work_buddy.context.registry
- work_buddy.context.sources
tags:
- context
- context_collector
- context_curator
- context_source
- context_block
- context_drill_down
- cache
- depth
- target_date
- sources
- bundle
aliases:
- context pipeline
- context collector
- context curator
- context sources
- context cache
parents:
- architecture
- architecture
---

Two-stage pipeline. ContextCollector fetches raw JSON per source with cache awareness (max_age + source-level is_stale). ContextCurator renders any cached Context into markdown or JSON at the caller's depth. 13 registered sources split into structured (git/tasks/projects/chrome with drill-down) and markdown-wrapper (9 sources delegating to legacy collectors). Exposed over MCP as context_block + context_drill_down.

GitSource is multi-repo since the Phase-A migration: it walks every `.git` directory at depth 1 under `cfg['repos_root']`, tags commits with a per-repo `project` field, and renders them bucketed under `#### <project>` subheadings. Pass `custom={'git': {'repo_path': ...}}` to force single-repo scope. The legacy `work_buddy/collectors/git_collector.py` is retained for test fixtures and historical callers but is no longer on the bundle path.

Project sync lives at `work_buddy/projects/sync.py` (formerly `work_buddy/collectors/project_collector.py`, which is now a back-compat shim). Entry: `sync_projects(cfg) -> str` (alias: `collect`). This is a synthesis job that writes the SQLite registry, not a context fetcher — `ProjectsSource` reads the already-synced registry.
