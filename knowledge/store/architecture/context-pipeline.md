---
name: Context Pipeline
kind: reference
description: Unified two-stage context collection and curation, including structured native task context and separate Obsidian vault context.
summary: >-
  Two-stage pipeline. ContextCollector fetches source sections with cache
  awareness; ContextCurator renders them at the caller's depth. Fifteen
  registered sources split into four structured sources
  (git/tasks/projects/chrome) and eleven wrappers. Obsidian opt-out is checked
  before cache or collector access: legacy-only adapters are skipped, while
  mixed Journal and task adapters may explicitly serve native authority.
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

Two-stage pipeline. ContextCollector fetches source sections with cache awareness
(`max_age` plus source-level `is_stale`). ContextCurator renders cached Context
as Markdown or JSON at the caller's depth. Structured sources include git,
native tasks, projects, and Chrome; wrapper adapters remain separate. Exposed
over MCP as `context_block` plus `context_drill_down`.

When `is_wanted("obsidian") is False`, `ContextCollector` suppresses
`obsidian`, `obsidian_tasks`, `obsidian_wellness`, `day_planner`, and `datacore`
before cache lookup or collector invocation unless the adapter's
`serves_native_without_obsidian(request)` explicitly confirms a native route.
The Journal, day-planner, and wellness adapters can read sealed Journal SQLite;
the task adapter can read `TaskStore`. Filesystem-native `vault` and
provider-neutral `calendar` are deliberately not classified as Obsidian-app
sources.

After native task activation, `TasksSource` and the task section of the Obsidian
collector query `TaskStore`. They never scan the frozen master list or task-note
Markdown as current task truth.

GitSource is multi-repo since the Phase-A migration: it walks every `.git` directory at depth 1 under `cfg['repos_root']`, tags commits with a per-repo `project` field, and renders them bucketed under `#### <project>` subheadings. Pass `custom={'git': {'repo_path': ...}}` to force single-repo scope. The legacy `work_buddy/collectors/git_collector.py` is retained for test fixtures and historical callers but is no longer on the bundle path.

`ProjectsSource` reads authoritative rows directly from the Projects SQLite
store. `work_buddy/projects/sync.py` and the
`work_buddy/collectors/project_collector.py` shim exist only for explicit
pre-seal compatibility reconciliation. After the Projects SQLite seal,
`project_sync` returns `status=disabled` with
`reason=projects_sqlite_authority` and performs no legacy file reads or writes.
