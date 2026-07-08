---
name: Dev Doc Update
kind: workflow
description: Review current-session code changes, cross-check against the knowledge store, update units that have gone stale or need creating, then validate store integrity. Enforces scan → propose → confirm → apply → validate → report so doc drift cannot be silently skipped and broken cross-refs cannot silently ship.
workflow_name: dev-document
execution: main
allow_override: false
steps:
- id: scan
  name: Scan current changes and candidate knowledge units
  step_type: code
  depends_on: []
  auto_run:
    callable: work_buddy.dev.document.scan_changes
    kwargs:
      base_ref: HEAD
    timeout: 90
  invokes: []
- id: propose
  name: Propose knowledge-store edits (updates + new units)
  step_type: reasoning
  depends_on:
  - scan
  result_schema:
    required_keys:
    - proposals
    - units_loaded
    key_types:
      proposals: list
      units_loaded: list
    min_items:
      units_loaded: 3
  invokes:
  - agent_docs
- id: confirm
  name: Confirm proposals with the user
  step_type: reasoning
  depends_on:
  - propose
  result_schema:
    required_keys:
    - confirmed
    key_types:
      confirmed: bool
      final_proposals: list
  invokes: []
- id: apply
  name: Apply accepted edits (native Edit for content, docs_delete for removals, then reconcile)
  step_type: code
  depends_on:
  - confirm
  visibility:
    mode: summary
    include_keys:
    - applied
    - failed
    - skipped
    - message
  invokes:
  - docs_delete
  - agent_docs_rebuild
- id: validate
  name: Validate store integrity after edits
  step_type: code
  depends_on:
  - apply
  auto_run:
    callable: work_buddy.knowledge.validate.docs_validate
    kwargs: {}
    timeout: 30
  visibility:
    mode: summary
    include_keys:
    - passed
    - failed
    - summary
    - total_units
  invokes: []
- id: report
  name: Report what changed and flag follow-ups
  step_type: reasoning
  depends_on:
  - validate
  invokes: []
command: wb-dev-document
tags:
- dev
- document
- docs
- knowledge-store
- hygiene
- drift
- stale
aliases:
- update docs
- doc update
- doc hygiene
- update knowledge
- check doc drift
- dev-document
- dev document
- sync knowledge store
parents:
- dev
- dev
dev_notes: |-
  The `scan` step runs a hybrid matcher: BM25 + dense (embedding) retrieval over the knowledge store fused with Reciprocal Rank Fusion, with the scored substring-grep matcher (`_match_units_via_grep`) as a graceful lexical fallback. The semantic pass embeds every query — one structural query over changed paths/slugs, plus one per changed `.py` file's docstring — in a SINGLE batched call per model via `work_buddy.knowledge.search.search_many`, never one round-trip per query. This is the load-bearing design choice: on weak/contended GPUs the per-round-trip embedding overhead (not the in-process BM25/fusion) is the cost that scales with changeset size, so batching collapses ~2N round-trips to 2. If the embedding service is contended past `_QUERY_EMBED_TIMEOUT_S` (in `work_buddy/dev/document.py`), the dense signal drops and results fall back to BM25 (lexical) without failing the step; only a hard search failure falls all the way back to grep. **Footgun**: when changing the query fan-out, keep it batched — reintroducing a per-file `search()` loop brings back the O(changed_files) round-trip blowup that times the step out on large changesets.

  Index backend (two paths, flag-selected). When `index.enabled` (config), `_search_units_via_consolidated` routes the batched knowledge search to the resident **consolidated** index via `embedding.client.index_search_many` (`POST /index/search_many`, partition `knowledge`, `filters={"scope":"system"}`) — scoring against warm in-service matrices, so the scan skips the in-process `ensure_index` build entirely. It falls back to the in-process `search_many` (then grep) on ANY failure/empty/stale (service down, shape mismatch, cold/missing partition), so the step never breaks; the batching footgun and the `_QUERY_EMBED_TIMEOUT_S` budget apply to both paths.

  The `auto_run` timeout is 90s, not the workflow default. On the in-process (fallback) path the scan subprocess pays ~5-6s fixed startup + the knowledge-index build before the first query runs; the consolidated path avoids that build. 90s is deliberate headroom for a step whose failure aborts the whole doc-sync flow (it is also chained inside `/wb-dev-pr`).

  The `apply` step is a code step with an `invokes` list, not an auto_run — this is the task-new pattern, applied here so the agent mediates each doc mutation (consent flows, error recovery, per-edit rationale all stay in agent hands).
---

Reviews the agent's current-session code changes and updates the knowledge store to match. The workflow gives the agent a deterministic starting set (changed files, classified subsystems, and knowledge units that textually reference them) via the auto_run `scan` step, then asks the agent to supplement that with semantic searches and user-knowledge about what was changed. After the agent applies edits, an auto_run `validate` step checks the store's structural integrity so broken refs cannot silently ship.

## Philosophy

Stale documentation is worse than missing documentation — it actively misleads. Agents in dev mode have a recurring failure mode: making behavioral changes and forgetting to update the units that describe that behavior. This workflow makes doc hygiene a step with a DAG gate, not a prose item in a checklist that gets skipped under time pressure.

Broken docs are a parallel failure mode: the agent updates a unit's `parents` or `workflow` field and introduces a dangling reference, or mints a new slash command without a matching directions unit. These are easy to create and hard to notice until someone else hits them. The `validate` step runs `docs_validate` right after `apply` so the blast radius stays confined to the same edit pass.

## What the agent must bring

The workflow's intelligence is in the agent, not the scan. Specifically:
- *Which* of the candidate units are actually semantically stale (the scan ranks by lexical+semantic match, but it can't tell whether a unit *describes* a behavior you just changed).
- *What* new units (if any) should be created for subsystems the scan cannot know are new.
- *Which body field* each new fact belongs in — `content_full` (every agent) vs `dev_notes` (dev-mode-only). Conscious routing between the two is the structural mechanism for the operational/developmental separation; the `propose` step's instruction enforces it inline, and `dev/dev-document-directions` carries the full criteria.
- *Whether* CLAUDE.md or similar top-level instruction surfaces need manual updates — those live outside the knowledge store.
- *How* to resolve any validation failures surfaced by the `validate` step — typically a follow-up edit to the referenced unit's `.md` (via `docs_edit`, or native `Edit` + `agent_docs_rebuild`).

## What this workflow is NOT

- Not a commit gate by itself — `/wb-dev-pr` wraps it for that.
- Not a substitute for the agent's own judgment about whether an edit is worth making; empty `proposals: []` is fine when nothing is stale.
- Not a lint pass — `validate` catches structural breakage (missing required fields, DAG violations, dangling refs), not prose quality.

## scan

Auto-run. The conductor calls `work_buddy.dev.document.scan_changes(base_ref="HEAD")` and wires the result into the next step. You don't invoke this — it returns:
- `changed_files`: repo-relative paths of uncommitted + untracked files
- `classified`: files grouped by bucket (module / knowledge / slash / tests / config / other)
- `subsystem_slugs`: module keys derived from the changed paths (e.g. `obsidian/tasks`, `namespace_suggest`)
- `candidate_units`: knowledge units whose text references a changed file or subsystem, ranked by match strength (hybrid BM25 + semantic). **First-pass net only** — you must still do your own semantic searches in the next step.
- `warnings`: non-fatal issues (empty diff, direct JSON edits).
- `_source`: which matcher produced the candidates — `"rag"` (hybrid; degrades to lexical BM25 if the embedding service is busy) or `"grep_fallback"` (substring matcher, used only if the search could not run at all).

## propose

Reasoning step. You're looking at the `scan` output and producing a list of concrete knowledge-store edits. You know the code you just wrote; the scan shows you which existing units textually reference it. Cross-check both, then fill the gap the scan cannot fill: **semantic drift** (the unit's prose still looks fine in isolation but describes a behavior you just changed).

## Required actions

1. **Read each candidate_unit at `depth="full"`** via `agent_docs(path=..., depth="full")` that you haven't already loaded. Skim, decide: is this unit's content still accurate? Does it need new content added?

2. **Do 2-4 semantic searches** against `agent_docs(query=...)` for concepts your change touches that the scan may have missed. Examples: if you added a workflow, query `"workflow authoring"`, `"how to add workflow"`, and the specific capability names. The scan is good at surfacing units that mention the changed file or its docstring vocabulary; ad-hoc semantic searches catch units that describe the same concept from a different angle (e.g. a directions unit that names the workflow rather than the implementation file).

3. **Check CLAUDE.md** (the top-level instruction surface) for stale references: grep for keywords from your change. Stale entries in CLAUDE.md mislead every future agent.

4. **Consider new units**: if you added a new subsystem, capability cluster, or workflow, there may be nothing in the store that describes it yet. Propose `action: "create"` with an appropriate path, kind, and content.

## Field placement — `content_full` vs `dev_notes`

Every proposal must consciously route content between **two** body fields:

- **`content_full`** — read by every agent (operational + dev) on `agent_docs(depth="full")`. Surfaces, semantic contracts, user-visible behavior.
- **`dev_notes`** — surfaced only when dev mode is on (`mode_toggle`, auto-enabled by `/wb-dev`). Implementation patterns, snapshot/cache invariants, refactor footguns, decision rationale.

The default failure mode is dumping everything into `content_full`. **Resist it.** Operational agents reading this unit shouldn't have their context window filled with implementation detail they cannot act on. The decision test: "if an operational agent was calling this subsystem from a capability, would they want this in their context window?" Yes → `content_full`. No, only useful while editing the code → `dev_notes`.

When updating an existing unit, route **new** facts by their nature, not by which field you happen to be editing. Adding both a public surface and an internal pattern to the same unit is **two** `fields` entries on the same proposal: one for `content_full`, one for `dev_notes`.

See `dev/dev-document-directions` for the full criteria, anti-patterns, and worked examples.



## Required output: `units_loaded`

Declare which candidate units you actually read at `depth="full"` during this step:

```json
"units_loaded": ["path/to/unit1", "path/to/unit2", ...]
```

This isn't ceremony — it's the same orientation discipline `/wb-dev` enforces via its `units_read` field. The result_schema requires at least 3 entries, but the real intent is *honesty*: declare what you actually loaded so the next agent (or you, in the report step) can tell whether the proposal list is grounded in reading or guessed at from the candidate metadata. Empty or trivial lists are how skim drift gets in.

## Advance with

```json
{
  "proposals": [
    {
      "action": "update" | "create" | "delete" | "no_op",
      "path": "<unit path>",
      "kind": "directions" | "system" | "workflow" (required for create),
      "rationale": "<one-sentence why>",
      "fields": {
        /* for update: only the fields changing */
        /* for create: all fields needed to author the unit (frontmatter + content_full) */
        "content_full": "...",
        "dev_notes": "...",
        "description": "...",
        /* for workflow kind, include steps (as a list) and step_instructions (as a dict) */
      }
    }
  ]
}
```

An empty `proposals: []` is a valid honest answer — if nothing is stale, don't invent work. But "I can't find anything" and "I didn't look" are different; you must have loaded at least the top candidate_units and done your semantic searches first.

---
**Advance via** `wb_advance(workflow_run_id=..., step_result={...})`. The parameter is `step_result` (not `result`) — FastMCP silently drops unknown kwargs.

## confirm

Reasoning step. Present the proposed edits to the user in a compact, readable form. For each proposal show: action, path, one-sentence rationale, and for content updates a brief diff hint (what's being added / changed / removed — not the full prose).

Ask the user to accept, reject, or modify each. Batch the ask when the proposals are obviously independent; itemize when any single one might be contentious.

## Advance with

**Common case** — user accepted all proposals as-is, OR declined entirely:

```json
{"confirmed": true}
```

or, if declined:

```json
{"confirmed": false}
```

**Modified case** — you (or the user) trimmed or edited the proposal list. Pass the modified list under `final_proposals`:

```json
{"confirmed": true, "final_proposals": [/* the modified subset */]}
```

Do NOT pass `final_proposals` when it's identical to `propose.proposals` — that just round-trips a potentially large list through the response. The apply step reads from `step_results.propose.proposals` by default; only override when you actually changed the list.

---
**Advance via** `wb_advance(workflow_run_id=..., step_result={...})`. The parameter is `step_result` (not `result`) — FastMCP silently drops unknown kwargs.

## apply

Code step. The proposals to apply come from one of two places:

- If `step_results.confirm.final_proposals` is present (agent modified the list), use it.
- Otherwise, read `step_results.propose.proposals` (the unmodified list from the propose step).

If `step_results.confirm.confirmed` is `false`, skip everything and return `{applied: [], failed: [], skipped: [<all paths from propose.proposals>], message: "User declined; no edits applied."}`.

Otherwise, iterate the proposals. Each unit is one Markdown file at `knowledge/store/<path>.md` — applying an edit is editing that file. For each:

- `action: "update"` (any kind) → open `knowledge/store/<path>.md` and apply the proposal's `fields` with your native `Edit` tool. YAML frontmatter carries the structured fields (`description`, `dev_notes`, `parents`, and for workflow units the `steps` DAG); the Markdown body is `content_full`. For a workflow unit, keep the frontmatter `steps` ids and the `## <step-id>` body sections in sync.
- `action: "create"` (any kind) → write a new `knowledge/store/<path>.md` with native `Write`: YAML frontmatter (`name`, `kind`, `description`, kind-specific fields such as `trigger` / `workflow_name` / `capability_name`, `parents`, optional `dev_notes`) followed by the `content_full` body. Copy the shape from a sibling unit of the same kind.
- `action: "delete"` → `docs_delete(path=...)`.
- `action: "no_op"` → skip.

After all file edits, call `agent_docs_rebuild()` **once** to reconcile the store cache + search index so the `validate` step (and later queries) see your changes. For a single unit you can instead drive the whole open → edit → commit through the `docs_edit` workflow (`wb_run("docs-edit", {path, ...})`), which validates and reconciles per unit.

**Fault tolerance**: one failure does not abort the loop. Collect results as `{applied: [path, ...], failed: [{path, error}, ...], skipped: [path, ...]}` and return them.

---
**Advance via** `wb_advance(workflow_run_id=..., step_result={...})`. The parameter is `step_result` (not `result`) — FastMCP silently drops unknown kwargs.

## validate

Auto-run. After edits land, the conductor calls `work_buddy.knowledge.validate.docs_validate()` with no args, running every registered check (the canonical list lives in the `context/docs_validate` capability unit's `checks` parameter). Returns:
- `passed` (bool): true iff every check found zero blocking errors.
- `failed` (int): blocking error count across all checks.
- `warnings` (int): advisory finding count (e.g. the durable_surfaces content audit); never blocks.
- `summary` ({check_name: count}): findings per check type, errors and warnings together.
- `errors` (list[{check, path, message}]): the blocking problems, if any.
- `issues` (list): every finding, blocking and advisory together (advisory entries carry `severity: "warning"`).
- `total_units`, `checks_run`: run metadata.

You don't invoke this — it runs automatically. The next (report) step is where you surface any failures to the user.

Why this gate exists: doc edits are easy to get structurally wrong (orphaned parents, typo'd path references, directions unit missing a trigger, slash command pointing at a non-existent unit). Catching these right after `apply` — not on the next unrelated commit — keeps drift localized.

## report

Reasoning step. Short summary of what changed:
- Count of applied / failed / skipped edits (from `apply`).
- **Validation outcome** (from `validate`): if `passed: false`, enumerate the errors by check type and path. These are structural problems you introduced and need to resolve before committing. Advisory warnings (the `warnings` count, e.g. durable_surfaces findings) are different: report the count and any findings your edits introduced, but they never block a commit — the open warning list is the documented cleanup backlog.
- For any CLAUDE.md or similar non-store files the user still needs to edit manually, flag them explicitly (the store capabilities don't touch CLAUDE.md).

If validation failed, recommend concrete next actions to the user: typically a follow-up edit to the referenced unit's `.md` (via `docs_edit`, or native `Edit` + `agent_docs_rebuild`) to fix the path/field. Do NOT treat validation failures as cosmetic; they indicate the store is in a broken state.

Do NOT re-summarize the code change itself — the user already knows what they did. Focus on doc hygiene + validation outcomes.

---
**Advance via** `wb_advance(workflow_run_id=..., step_result={...})`. The parameter is `step_result` (not `result`) — FastMCP silently drops unknown kwargs.
