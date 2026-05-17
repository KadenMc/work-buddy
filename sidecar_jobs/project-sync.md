---
schedule: "*/30 * * * *"  # every 30 minutes
jitter_seconds: 240        # spread fire time up to 4 min so it doesn't
                           # collide with task-sync on shared :00/:30 ticks
recurring: true
type: capability
capability: project_sync
params: {}
---
Reconcile the project markdown notes against the projects SQLite registry.

Project notes are markdown-canonical: each project has one
`work-buddy/projects/<slug>.md` note, and the SQLite registry is a
queryable projection of it (see `architecture/markdown-db`). This job
catches edits made out-of-band — a description hand-edited in Obsidian —
and propagates them into the registry. Edits made through the dashboard
already write both surfaces (the endpoint routes through
`ProjectMarkdownDB.apply_mutation`), so this job is the safety net for
the Obsidian-edit path.

Markdown-canonical, and `delete_orphans_in_store` is off: this job never
deletes a project. A registry project whose note is missing or fails to
parse is left intact and surfaced as a warning, not removed.

Runs every 30 minutes (project descriptions drift far less often than
the task list — task-sync runs every 10). Safe to run before the
project-notes directory exists: it simply finds nothing and no-ops.
