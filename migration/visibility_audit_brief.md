# Briefing — visibility-mode audit

**Read this before doing the work.** This brief is the only context you need;
it covers the visibility system, the bug pattern you're fixing, the steps to
audit, and the output shape required.

## What you're doing

Six workflow steps misuse the conductor's `summary` visibility mode by
listing every output key in `include_keys`, defeating the manifest. For each
step, you'll read the auto_run callable's source, identify the actual return
shape, and recommend the right visibility mode. Output is a structured
report ready to feed `workflow_update` calls.

## What the visibility modes do

The conductor's response to the agent includes a `step_results` dict keyed by
step ID. For an auto_run step's entry, the visibility mode (declared in the
step's `visibility` block in `workflows.json`) decides what the agent sees
inline vs. what they have to fetch on demand via
`wb_step_result(run_id, step_id, key)`.

The "keys" in `include_keys` are the literal top-level keys of the dict
returned by the auto_run callable. Example: `dev-document.scan` calls
`scan_changes()` which returns:

```python
{
    "changed_files":    [<list of paths>],          # ~1KB
    "classified":       {<bucket: paths>},          # ~1KB
    "subsystem_slugs":  [<list of strings>],        # ~0.5KB
    "candidate_units":  [<list of unit dicts>],     # ~32KB  ← the bloat
    "warnings":         [<strings>],                # ~0.1KB
    "base_ref":         "HEAD",                     # ~10B
}
```

Four modes:

| Mode | What the agent gets inline | What's omitted |
|---|---|---|
| `full` | **Every key, full value, in the response.** The complete dict. | Nothing. |
| `summary` *with* `include_keys` | A manifest (`{_manifest: true, _keys, _key_sizes, ...}`) **plus** an inlined `_partial` dict containing only the values for keys named in `include_keys`. | Keys *not* in `include_keys` are listed in `_keys`/`_key_sizes` (the agent knows they exist) but their values must be fetched. |
| `none` | Bare manifest only — no `_partial` at all. The agent sees `_keys` and `_key_sizes` but no values. | All values; everything must be fetched. |
| `auto` *(default)* | Falls through to `full` if the entire result serializes to ≤ 10KB; otherwise produces a manifest-only response (equivalent to `none`). | If small: nothing. If large: all values, fetchable. |

## The anti-pattern you're fixing

The six affected steps declare `mode: "summary"` *and* list every (or nearly
every) output key under `include_keys`. That makes `_partial` hold the whole
result — functionally identical to `mode: "full"`, just written via a longer
code path. Worse, when the result is genuinely large (e.g. `dev-document.scan`
at 35KB), the agent eats the whole thing inline despite the elision system
being engaged. The visibility mode and `include_keys` together accomplished
nothing.

## How to pick the right mode

For each step, after reading its callable's return shape:

- **All keys small + always wanted** → `full`. (Result is small, agent
  always wants every field.)
- **Variable / size-dependent** → `auto`. (Lets the conductor pick `full`
  when small, manifest when large.)
- **One huge optional key surrounded by small ones** → `summary` *with that
  one field excluded* from `include_keys`. (e.g., output is `{total: 5,
  count: 3, items: <50KB>}` — pick `summary` with `include_keys: ["total",
  "count"]`. The agent sees the small fields inline and can fetch `items`
  on demand.)

## The six steps and their callables

| Workflow.step | Callable (dotted path) | File |
|---|---|---|
| `dev-document.scan` | `work_buddy.dev.document.scan_changes` | `work_buddy/dev/document.py` |
| `dev-document.validate` | `work_buddy.knowledge.validate.docs_validate` | `work_buddy/knowledge/validate.py` |
| `dev-commit.assess` | `work_buddy.dev.commit.assess_state` | `work_buddy/dev/commit.py` |
| `update-journal.read-journal` | (see workflows.json — find the `auto_run.callable`) | (follow the dotted path) |
| `stress-test.compute-primes` | (see workflows.json) | (follow the dotted path) |
| `task-me.load-context` | (see workflows.json) | (follow the dotted path) |

The visibility blocks live in `knowledge/store/workflows.json` — search for
each step ID to find its current `visibility` config.

## Output contract

Produce one row per affected step. Required fields:

- `workflow_step` — e.g. `dev-document.scan`
- `return_keys_with_sizes` — observed/estimated, e.g.
  `{"changed_files": "~1KB", "candidate_units": "~32KB", ...}`
- `recommended_mode` — `"full"` | `"summary"` | `"none"` | `"auto"`
- `include_keys_if_summary` — list of key names; empty list if not summary
- `justification` — one-to-two sentences; reference specific keys/sizes

If a row's mode is `summary`, `include_keys_if_summary` MUST be a *strict
subset* of the return keys (typically 1–3 keys). Listing every key is the
bug pattern.

A worked example for `dev-document.scan`:

```yaml
workflow_step: dev-document.scan
return_keys_with_sizes:
  changed_files:    "~1KB"
  classified:       "~1KB"
  subsystem_slugs:  "~0.5KB"
  candidate_units:  "~32KB"
  warnings:         "~0.1KB"
  base_ref:         "~10B"
recommended_mode: auto
include_keys_if_summary: []
justification: "candidate_units is the dominant size and varies. Problem H
  will slim it; in the meantime auto picks full when small and manifest
  when large, which is the right behavior across both regimes."
```

## How the report gets used

Each row will be applied as a `workflow_update` call modifying that step's
`visibility` block. The user will read the justification column to spot-check
the recommendation; expect rejection if the justification doesn't reference
specific size evidence.

## Background pointers (only if you need them)

- Visibility implementation lives in
  `work_buddy/mcp_server/conductor.py` lines ~1070–1123 (`_apply_visibility`
  and `_make_manifest`).
- Auto thresholds: `_VISIBILITY_AUTO_THRESHOLD = 10_000` (chars), hard cap
  `_STEP_RESULT_CAP = 50_000`.
- The `architecture/workflows` knowledge unit has more on the visibility
  system if you need it; you shouldn't.
