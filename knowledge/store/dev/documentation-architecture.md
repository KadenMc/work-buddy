---
name: Documentation Architecture
kind: directions
description: Where system documentation lives, what is canonical vs legacy, and how to make documentation changes
summary: The knowledge store (knowledge/store/, one Markdown file per unit) is the single source of truth for all agent documentation. CLAUDE.md is the agent orientation file. In-package README.md files are legacy artifacts pending deletion — never edit them. The metacognition/ directory is fully migrated to personal knowledge.
trigger: When the user asks about documentation structure, where docs live, or how to update system documentation
tags:
- dev
- documentation
- knowledge-store
- architecture
- legacy
- README
parents:
- dev
- dev
---

## Documentation Hierarchy

1. **`CLAUDE.md`** — Agent orientation file. High-level structure, capability registry tables, operational rules. Valid to edit directly for top-level changes.
2. **`knowledge/store/**/*.md`** — The canonical, queryable documentation store, one Markdown file per unit. All detailed subsystem docs, behavioral directions, capability declarations, and workflow definitions live here. Edit or create units via the `docs_edit` workflow (or a direct `.md` edit + `agent_docs_rebuild`); remove/relocate via `docs_delete` / `docs_move`.
3. **`CLAUDE.local.md`** — User-specific behavioral instructions. Not checked into git.

## What is DEPRECATED / Legacy

- **`work_buddy/*/README.md` files** — These are legacy documentation from before the knowledge store existed. They are pending extraction and deletion. **NEVER edit these files.** If you find useful content in them that isn't in the knowledge store, extract it into a knowledge unit via the `docs_edit` workflow, then mark the README for removal.
- **`metacognition/` directory** — Fully migrated to the personal knowledge system (`knowledge_personal`). The directory should not be referenced, edited, or maintained. Pattern detection uses `knowledge_personal` with category/severity filters.
- **`workflows/` directory** — Deleted. Workflow DAGs now live as `kind: workflow` units (one Markdown file per workflow) under `knowledge/store/`.
- **`DEV.md`** — Deleted. Architecture patterns, design tenets, and import discipline are now in the knowledge store under `dev/design-tenets`, `architecture/mcp-import-discipline`, and `architecture/workflows`.

## How to Make Documentation Changes

1. **For knowledge store content**: Use the `docs_edit` workflow to edit or create units — it returns the unit's `.md` path, you edit it with your native `Edit` tool, and the commit step validates and reconciles. A direct `.md` edit + `agent_docs_rebuild` works too.
2. **For CLAUDE.md**: Edit the file directly — it's the orientation layer, not a knowledge store unit.
3. **For capability declarations**: a capability is an Op (callable in `work_buddy/mcp_server/ops/`) plus a `kind: capability` declaration unit. Edit the declaration like any other unit (via `docs_edit`); see `architecture/data-first-capabilities`.
4. **For personal knowledge**: Use `knowledge_mint` to create/update vault-backed units.

## Common Mistakes to Avoid

- Editing in-package README.md files instead of knowledge store units
- Referencing `metacognition/` paths instead of using `knowledge_personal`
- Referencing the deleted `workflows/*.md` paths instead of the workflow's `kind: workflow` unit under `knowledge/store/`
- Duplicating knowledge store content into CLAUDE.md (keep CLAUDE.md as a summary/pointer layer)
- Spawning agents to review README files for accuracy (they are legacy and doomed for deletion)
