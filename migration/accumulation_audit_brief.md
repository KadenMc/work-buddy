# Briefing — cross-step accumulation audit

**Read this before doing the work.** This brief is the only context you
need; it covers the bug pattern you're hunting, the patterns to look for,
the one confirmed offender as a reference, and the output shape required.

## What you're doing

Some workflows have reasoning steps whose `result_schema` and
`step_instructions` ask the agent to return upstream-step data echoed back
alongside the step's own new fields. That produces silent response bloat:
the same data ends up in `step_results` two or three times across
consecutive steps. You're auditing every reasoning step in the workflow
store, flagging offenders, and producing structured rewrite proposals that
will feed `workflow_update` calls.

## The rule: step results are deltas, not running accumulations

Each reasoning step's `result` should contain *only the new fields the
step computed*. Upstream-step data is already in `step_results[upstream_id]`
— the agent never has to re-emit it.

## Worked example

A three-step pipeline that processes a list of items:

- **Step 1 — `categorize`**: takes raw items, decides what category each
  belongs to. **Should return `{categories: [...]}`.**
- **Step 2 — `rank`**: looks at the categorized items, ranks them.
  **Should return `{ranks: [...]}`.**
- **Step 3 — `summarize`**: writes a final summary.
  **Should return `{summary: "..."}`.**

After all three run, `step_results` should look like:

```yaml
step_results:
  categorize: { categories: [...] }
  rank:       { ranks: [...] }
  summarize:  { summary: "..." }
```

Three distinct deltas, each step contributing exactly its new piece. Total
size = sum of the three new pieces.

**Bug shape** (what you're flagging): each step takes the previous step's
whole result dict and returns it back **plus** its new field:

```yaml
step_results:
  categorize: { items: [...], categories: [...] }
  rank:       { items: [...], categories: [...], ranks: [...] }
  summarize:  { items: [...], categories: [...], ranks: [...], summary: "..." }
```

Same `items` list appears three times. Same `categories` list appears
twice. Cumulative bloat with no information gain.

## Patterns that indicate an offender

For each reasoning step in `knowledge/store/workflows.json`:

1. Check `step_instructions[<step_id>]`. Does the prose tell the agent to
   "return `{X, Y, my_new_field}`" where X / Y are keys that came from a
   prior step's output? Flag.
2. Check `result_schema.required_keys`. Do any of the listed keys match
   keys produced by an upstream step? Flag.
3. Check `result_schema.key_types` for keys that semantically belong to an
   upstream step (e.g., `result_schema.key_types: {items: list}` on a
   step whose name is `summarize`). Flag.

Note that some keys are coincidentally named the same in multiple steps
without being the same data — judgment call. When in doubt, read the
upstream step's instruction to confirm whether the keys reference the same
underlying values.

## The one confirmed offender (as reference)

The `route-information` workflow (`workflows.json`, `workflow_name:
route-information`) has three reasoning steps: `contextualize`,
`intent-group`, `summarize`. Each returns `{singletons, clusters,
my_new_field}` — the `singletons` and `clusters` lists, ~140KB combined,
get emitted three times in a single response. This is the canonical
offender. If you find similar shapes elsewhere, those are also offenders.

## Output contract

Produce one row per offender. Required fields:

- `workflow_name` — e.g. `route-information`
- `step_id` — e.g. `summarize`
- `evidence_quote` — the offending fragment of `step_instructions` or
  `result_schema`. Quote verbatim.
- `proposed_replacement_instruction` — the rewritten prose. Should explicitly
  say: "Return `{<delta keys only>}`. Upstream data is in
  `step_results.<upstream_id>` — read it from there if you need it; do not
  echo it back."
- `proposed_replacement_schema` — the rewritten `result_schema`. Strip the
  upstream keys from `required_keys` and `key_types`.

A worked example for the `summarize` step (hypothetical exact wording):

```yaml
workflow_name: route-information
step_id: summarize
evidence_quote: |
  Return {summary, singletons, clusters} where singletons and clusters
  come from the prior step.
proposed_replacement_instruction: |
  Reasoning step. Read step_results.intent-group for singletons and
  clusters. Write a final summary. Return {summary: "..."} only — do not
  echo singletons or clusters back; they're already in step_results.
proposed_replacement_schema:
  required_keys: [summary]
  key_types:
    summary: str
```

## Process

1. Walk `knowledge/store/workflows.json` linearly. For each workflow,
   inspect every reasoning step (`step_type: "reasoning"`).
2. Apply the three patterns above to flag offenders.
3. For each offender, draft the rewrite. Do not apply it yet — your output
   feeds a separate apply pass that uses `workflow_update`.
4. If you find zero offenders besides `route-information`, say so explicitly.
   That's a useful answer.

## Output the report

Save to `migration/accumulation_audit_findings.md` (alongside this brief).
Use the structure above. Apply pass will read it.
