# Accumulation audit findings

Generated: 2026-05-08
Source: `knowledge/store/workflows.json`
Method: walked every `step_type: "reasoning"` step; applied all three patterns from brief.

---

## Preamble — route-information status

The brief identifies `contextualize → intent-group → summarize` as the confirmed offender chain
in `route-information`. **This chain no longer exists in the file.** The workflow now has
entirely different steps (`cluster-items`, `present-clusters`, `record-decisions`,
`execute-routing`, `return-results`) with no accumulation pattern in their instructions or
schemas. The prior fix has already landed; no action needed for `route-information`.

---

## Offenders found: 1

---

```yaml
workflow_name: morning-routine
step_id: synthesize
shape: instruction-mandated (no result_schema on this step)
evidence_quote: |
  Synthesize a briefing from the step results. The briefing covers: yesterday summary,
  calendar, tasks, contracts, projects, blindspots. Tone and format rules are in the
  slash command.

  Return {"briefing_md": briefing_md, "results": results} for downstream steps.
```

**Analysis.** The `results` key is an explicit bundle of all prior step data gathered in this
step: `sign-in`, `yesterday-close`, `calendar-today`, `task-briefing`, `contract-check`,
`blindspot-scan`. Each of those is already in `step_results.<step_id>`. Bundling them again
under `synthesize.results` means the same data occupies `step_results.sign-in`,
`step_results.task-briefing`, etc. AND `step_results.synthesize.results.*` simultaneously.

Two downstream steps reference `synthesize` output:
- `persist-briefing` reads `step_results["synthesize"]["briefing_md"]` only — does NOT need
  `results`.
- `propose-mits` reads `step_results["synthesize"]` for "underlying data" (vague), but the
  only concrete field it reads is `briefing_md` and config from `load-config`. It can read
  individual upstream steps directly for any additional data.

Neither downstream step needs `results` to be re-bundled here.

```yaml
proposed_replacement_instruction: |
  Reasoning step. Gather the prior step data you need directly from step_results:
  step_results["sign-in"], step_results["yesterday-close"], step_results["calendar-today"],
  step_results["task-briefing"], step_results["contract-check"], step_results["blindspot-scan"].
  For skipped or failed steps the value will be None or {skipped: true} — handle gracefully.

  Synthesize a briefing from those inputs. Tone, format rules, and section coverage are in
  the slash command.

  Present the briefing and offer follow-ups.

  Return {briefing_md: "..."} only. Do not re-bundle the upstream step results under a
  "results" key — they are already in step_results and downstream steps can read them from
  there directly.

proposed_replacement_schema:
  required_keys: [briefing_md]
  key_types:
    briefing_md: str
```

**Note for apply pass.** The `propose-mits` step instruction says "Using the synthesized
briefing and underlying data from `step_results["synthesize"]`" — this prose should also be
tightened to remove the ambiguous "underlying data" reference and replace it with explicit
reads from the named upstream steps. The relevant line in `propose-mits` to update:

> "Using the synthesized briefing and underlying data from `step_results["synthesize"]`"

Replacement:

> "Read the synthesized briefing from `step_results["synthesize"]["briefing_md"]`. For task
> context, read `step_results["task-briefing"]` directly. For contract context, read
> `step_results["contract-check"]` directly. Do not expect a bundled `results` key on the
> synthesize step output."

---

## Workflows with no offenders

All reasoning steps in the following workflows were checked and found clean — no upstream
keys in `result_schema.required_keys`, no instructions saying "return X+Y+my_new_field"
where X/Y came from a prior step:

- `contracts/analyze-contracts` (health-check, check-alignment, surface-actions,
  handle-no-contracts)
- `context/collect-and-orient` (synthesize, connect-contracts, suggest-action)
- `contracts/create-contract` (identify-deliverable, draft-contract, check-scope,
  review-existing, confirm-save)
- `tasks/inline-todos` (triage, execute)
- `context/review-latest-bundle` (read-and-synthesize)
- `routing/route-information` (cluster-items, present-clusters, record-decisions) — see
  preamble
- `daily-journal/segment-notes` (segment-and-tag)
- `tasks/task-new` (plan, confirm, report) — confirm's `final_plan` is a confirmed/modified
  copy of the plan, not a direct echo; it's the only version the apply step should read
  from, and may differ from the original plan step output
- `tasks/task-triage` (present, summary)
- `tasks/weekly-review` (draft, validate, record)
- `dev/dev-orient` (orient)
- `dev/stress-test` (verify-result)
- `dev/dev-document` (propose, confirm, report) — confirm's `final_proposals` is
  deliberately optional and only emitted when the list was modified; the apply step reads
  from `propose.proposals` by default
- `dev/dev-commit` (branch_guard, test, document, cleanup, commit, push)
- `tasks/task-me` (engage, write-back)
- `morning/morning-routine` (sign-in, yesterday-close, blindspot-scan, propose-mits,
  persist-briefing, day-planner) — only `synthesize` is flagged above
