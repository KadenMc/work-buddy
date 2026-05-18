---
name: Vault Recon Directions
kind: directions
description: How to read vault_recon output and identify recurring conventions worth surfacing.
trigger: user runs /wb-vault-recon, or an agent needs to reason over vault_recon cross-tabs to identify patterns
command: wb-vault-recon
tags:
- vault
- recon
- directions
- discovery
- frontmatter
- tag-tree
- state-machine
aliases:
- vault recon
- vault reconnaissance
- recon directions
- identify vault patterns
- discover vault conventions
parents:
- vault
- vault
---

Read `vault_recon` output to identify recurring conventions in the user's vault. Returns cross-tabs an agent can pivot to spot state machines, tag families, hot regions of work.

## When to use

- The user runs `/wb-vault-recon` and wants a diagnostic peek at vault structure.
- An investigation agent is reasoning over a fresh recon snapshot to draft a proposal.
- A future vault-health check needs structural ground truth.

Do NOT use as a substitute for `datacore_schema` for cheap "is anything queryable" probes. `vault_recon` is heavier (full page walk + list-item walk, ~2–3s on a 6k-page / 200k-list-item vault, capped at 90s by bridge timeout). Direct invocation via `wb_run("vault_recon")` may exceed the MCP tool result token limit — prefer reading `.data/vault_recon/latest.json` written by the collector.

## Invocation

```
wb_run("vault_recon")                                       # full vault
wb_run("vault_recon", {"path_prefix": "repos/electricrag/"}) # region focus
wb_run("vault_recon", {"activity_days": 14})                 # tighter activity window
```

## Output structure

Key fields and what they answer:

| Field | Question it answers |
|---|---|
| `object_types` | How many pages/sections/tasks/etc. exist (vault-wide; ignores path_prefix). |
| `pages_total`, `pages_walked` | Total in vault vs. walked given filter. |
| `top_tags` | Top-30 normalized top-level tags (page-level union). |
| `frontmatter_keys` | Top-30 keys with usage counts. The keys are the alphabet of structure. |
| `frontmatter_values[key]` | For each key, top 20 values + `distinct_count` + `truncated` flag. **THIS IS WHERE STATE MACHINES SHOW UP.** Look for keys like `status`, `state`, `phase` with discrete value sets that look like an enum. |
| `high_cardinality_keys` | Keys skipped because their value cardinality > 100 (UUIDs, timestamps). |
| `tag_tree` | Hierarchical tag tree to depth 3 with counts (page-level). |
| `type_by_status` | Cross-tab `{type: {status: count}}`. The Kanban view of any state machine. |
| `path_by_type` | `{path: {type: count}}` filtered to paths with ≥2 typed pages. |
| `recent_activity_by_path` | Depth-2 prefix → count of pages with mtime within `activity_days`. |
| `list_item_top_tags` | Top-30 inline-tag families on list-items (excluding `#todo*`). The user's inline-concept stream. |
| `list_item_tag_tree` | Same as `tag_tree` but for list-items. Concept-stream substrate for Tier-2 surfacing. |
| `list_item_tagged_total` | Count of list-items with at least one non-`#todo` tag. |
| `task_statuses`, `tasks_total` | Task statuses (note: redundant with `task_metadata.db`; included for completeness). |

## Pattern-recognition heuristics

### Spot a frontmatter state machine

Examine `frontmatter_values`. A state machine looks like:
- A key (commonly `status`, `state`, `phase`) with `truncated: false` and 3–8 distinct values.
- Values that look like a workflow: `PROPOSED`, `DESIGNED`, `COMPLETED` or `draft`, `published`, `archived`.
- Often paired with a `type` key and `type_by_status` cross-tab showing per-type counts in each status.

If you see one: name it. "You have a `hypothesis | experiment | thread` state machine with `PROPOSED → DESIGNED → COMPLETED` transitions, mostly under `repos/electricrag/`."

### Spot a tag family

Walk `tag_tree` (page-level) or `list_item_tag_tree` (inline). A family looks like a node with multiple children, where the children are themselves substructured. E.g. `#mide` with children `workflow`, `system`, `context`, `meta`.

### Spot a concept-stream pattern (Tier-2)

`list_item_top_tags` and `list_item_tag_tree` capture inline-tagged list-items — the user's running thinking log. Patterns to spot:
- A tag prefix appearing 50+ times across list-items = an active concept the user references repeatedly.
- A previously-active tag with low recent count = drift candidate.
- Co-occurrence within the same line = related concepts.

These are NOT in `task_metadata.db` (only `#todo*` tasks are) — list-item tags are concept references, not actions.

### Spot a path convention

Look at `path_by_type`. If a single path holds multiple pages of one type (e.g. `repos/electricrag/kb/research/threads` holds 8 pages of type=thread), that's a directory naming convention.

### Spot a hot region

`recent_activity_by_path` ranks regions by recent mtime activity. Top 1–2 entries are where the user is working now.

## What to do with findings

This directions unit is for the *reader* of recon output, not for the user-facing slash command response.

- If invoked from `/wb-vault-recon`: present the most striking 3–5 findings in plain English, no auto-action.
- If invoked from the investigation agent (after a delta has triggered escalation): cross-reference with `vault/investigation-directions` for the proposal protocol.

Do NOT cement anything from a single recon run. Cementing is the user's call (or a follow-up workflow's). This unit only teaches an agent how to *see*.
