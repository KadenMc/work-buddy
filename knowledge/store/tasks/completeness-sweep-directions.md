---
name: Task Completeness Sweep Directions
kind: directions
description: How to run a full-list completeness sweep — audit every open task for whether it is already done, warn about cost upfront, fan out the per-task task-completeness investigator over the whole list, defer all mutations to a reviewable AUDIT.md, and apply backdated toggles only on user sign-off.
trigger: user runs /wb-task-completeness-sweep or asks to audit/sweep the whole task list for tasks that are already done
command: wb-task-completeness-sweep
capabilities:
- tasks/task_list
- tasks/task-completeness
- tasks/task_toggle
tags:
- tasks
- completeness
- sweep
- audit
- batch
- retroactive
- directions
aliases:
- audit all tasks
- sweep the task list
- which tasks are already done
- bulk completeness check
- full task audit
parents:
- tasks
---

Audit the WHOLE open-task list for tasks that are secretly already done (fully, differently, or partially), so stale "open" tasks get closed with the correct prior completion date. This is the bulk form of `/wb-task-completeness`: that command investigates one `task_id`; this one enumerates every open task and runs the same per-task investigator over all of them, then synthesizes one reviewable audit.

The whole sweep is READ-ONLY until the user signs off. You produce an `AUDIT.md` of verdicts; you mutate nothing until the user approves specific task ids.

## When this fits

- "Which of my open tasks are already done?"
- "Audit the whole task list, I think a lot of these are stale."
- A periodic backlog hygiene pass.

For a single task the user already suspects is done, use `/wb-task-completeness` instead — do not spin up the full sweep for one id.

## Step 1 — Enumerate

Call `task_list` (no arguments) to get every live, non-archived, OPEN task, bridge-independent:

```
mcp__work-buddy__wb_run("task_list", {})
```

Read `count` and the `tasks` list. Each row is a full `task_metadata` record, so for a large backlog the result can be sizable and your harness may save it to a file rather than inline it. You only need `task_id`, `description`, `state`, and `created_at` per task: extract just those (e.g. with `jq`/a small script over the saved file) rather than carrying every column. If the user passed a scope hint (e.g. "older than 30 days", "just the inbox ones"), apply it by filtering the returned list, or pass `state=`/`limit=` to `task_list`. Optionally split out empty-shell rows (no `description` and no linked note) as non-auditable and report them separately rather than spending an investigator on them.

## Step 2 — Cost gate (the upfront warning)

This is the required confirmation. Before spawning anything, tell the user plainly:

> This audits N open tasks by spawning roughly ceil(N / 4) sub-agents, each reading code and git history. Expect several minutes and non-trivial token cost.

Then wait for an explicit go. Name the cost, do not moralize, let the user decide (the house style for expensive operations). Two extra rules:

- If N is large (more than ~150 tasks), do not just proceed on a bare "go." Recommend narrowing scope (by age or state) or running in confirmed batches, and get a second explicit confirmation for the full run.
- If N is 0, say so and stop. There is nothing to audit.

Do not start any `task-completeness` run before the user says go.

## Step 3 — Partition

Group the tasks into shards of about 4 (`S = 4`). Default to simple sequential chunking in `created_at` order (adjacent tasks tend to share a subsystem, which helps a sub-agent reuse what it reads). For a large list (more than ~40 tasks) you may optionally run a single cheap clustering pass first (one sub-agent that groups task ids by subsystem/theme from their descriptions) so each shard is thematically coherent. Clustering is polish, not correctness; skip it for small runs.

Write each shard's task ids to its own input file and decide a per-shard append-only output file, under a dated audit directory:

```
.data/designs/task-completeness-audit-<YYYY-MM-DD>/
  shards/shard-01.json      (input: the shard's task records)
  shards/shard-01.jsonl     (output: one verdict line per task, append-only)
  ...
  AUDIT.md                  (your final synthesis)
```

Use today's date from your context for `<YYYY-MM-DD>`.

## Step 4 — Fan out

Spawn one sub-agent per shard using your harness sub-agent tool (the built-in Task/Agent tool — this does NOT require any external batch skill). A capable reasoning model is appropriate per sub-agent; this is code-reading judgment, not a lightweight extraction. Hand each sub-agent the brief below, its shard input path, and its append-only output path. Do not let sub-agents spawn further sub-agents.

Each sub-agent works through its shard and appends one verdict line per task. Collect their brief summaries; the real output is the JSONL.

### Embedded sub-agent brief (copy into each sub-agent verbatim, filling the three placeholders)

```
You are ONE worker in a batch completeness audit. Investigate a SHARD of work-buddy tasks and decide, for each, whether it is ALREADY done. You DO NOT mutate anything; the orchestrator synthesizes all shards and the user signs off before any task is toggled.

Inputs (given to you):
- SHARD_FILE: <path to your shard-NN.json — your assigned task ids + descriptions>
- OUT_JSONL:  <path to your shard-NN.jsonl — append ONE JSON line per task as you finish it; never overwrite>
- SESSION_ID: <the orchestrator's WORK_BUDDY_SESSION_ID>

Setup (once): register the SHARED session FIRST:
  mcp__work-buddy__wb_run("wb_init", {"session_id": "<SESSION_ID>"})
Consent is pre-authorized for this session (a workflow_class:task-completeness grant is live), so the workflow runs without prompting you. If a call ever blocks on consent or times out, do NOT hang: record that task with disposition "error" and move on.

Per task (repeat for each task id in your shard):
1. Start the investigator:  mcp__work-buddy__wb_run("task-completeness", {"task_id": "<TID>"})  (param key is "capability"/positional as your gateway expects; it is a workflow). Capture workflow_run_id. The gather-evidence step auto-runs as an ELIDED manifest.
2. Pull evidence (cheapest first, do NOT pull everything):
     mcp__work-buddy__wb_step_result(workflow_run_id="<wf>", step_id="gather-evidence", key="task")        # intent: task text + note_content
     mcp__work-buddy__wb_step_result(workflow_run_id="<wf>", step_id="gather-evidence", key="provenance")  # created_by / assigned / developed_by (small)
   Pull session_evidence (large) only if provenance is empty AND you need a developer session's rationale; prefer session_search into a named developer session over pulling the whole blob.
3. Investigate, judging the SPIRIT not the letter. STEP 0 FIRST: restate the INTENT from note_content (what was asked + any prescribed how-to), then, cheapest-first: provenance -> session_search into a developer session / git log --grep=<TID> / gh PR search -> READ the actual code/tests to confirm behavior is present now -> run a targeted test only if still ambiguous. Inactivity is NOT completion.
4. Advance the investigate step (param name is step_result) with exactly:
     {"disposition": "<done|done-differently|partial|consciously-descoped|not-done>",
      "evidence": ["<concrete citation: SHA / PR / path:line / test / note excerpt>", ...],   # non-empty
      "completion_date": "YYYY-MM-DD or null",   # date of the LANDING COMMIT, cite its SHA in evidence
      "divergence": "<better|lateral|worse|na>", # na unless done-differently with a prescribed how-to
      "confidence": "<high|medium|low> — <one clause keyed to signal strength>",
      "recommended_action": "<one sentence>"}
5. Advance the resolve step DEFERRED (never toggle):
     step_result = {"action": "deferred", "note": "batch sweep — mutations deferred to user review"}
6. Append one JSON line to OUT_JSONL (append-only; survives interruption) with:
     task_id, shard_id, disposition, confidence, divergence, completion_date, recommended_action,
     intent_summary (1-2 sentences: what was asked + prescribed how-to),
     situation_summary (1-3 sentences: who/when developed it, how the impl compares to intent, any deferred follow-up),
     evidence (list), deferred_followup (text or empty), workflow_run_id

Disposition rubric: done (intent met as written, or a blessed v1 shipped — surface any deferred follow-up) / done-differently (intent met by a different design; set divergence better|lateral|worse and justify from rationale + code) / partial (some shipped, some remains — delineate which) / consciously-descoped (a sub-part deliberately dropped) / not-done (no satisfying implementation; inactivity lands here).
Confidence: high = structural developed_by link AND you read the code; medium = Rung-3 intent-only inference backed by code reading; low = weak/ambiguous. Be honest.

HARD RULES: read-only except appending to YOUR OUT_JSONL. Never task_toggle/task_create/edit repo files/edit other shards. If you query the store raw, liveness = deleted_at IS NULL (prefer capabilities). NO fan-out — do not spawn sub-agents. Return ONLY a brief recap (one line per task: <TID> <disposition>/<confidence> — <=10-word gist; plus uncertainties and blockers). Evidence lives in the JSONL.
```

Note for non-fan-out harnesses: if you have no sub-agent tool, skip the fan-out and run the per-task loop above yourself, sequentially, appending to a single JSONL. Slower and more context, same correctness.

## Step 5 — Synthesize

Aggregate every shard JSONL into `AUDIT.md`. Validate coverage first (line count equals the audited count, no duplicate task ids). Then write:

- Headline counts by disposition + confidence.
- Section A: mark-done backdated candidates (done / done-differently), each with the backdate date, divergence verdict, intent, what was found, and evidence.
- Section B: partials, each split into shipped-vs-remainder with a next move.
- Section C: not-done (a compact table).
- Section D: flags for human review — medium-confidence verdicts to check before backdating, done-differently divergence sign-offs, deadline-bearing not-done tasks, and likely duplicates.
- An action queue: backdate wins / spin-off remainders / leave-open.

## Step 6 — Sign-off and apply

Present the backdate candidates and the headline counts. Act ONLY on explicit user approval, never mutate first. On approval, apply each confirmed completion with the LANDING-COMMIT date:

```
mcp__work-buddy__wb_run("task_toggle", {"task_id": "<id>", "done": true, "done_date": "<YYYY-MM-DD>"})
```

For partials, offer to finish the remainder, spin it off as a fresh task, or backdate only the shipped portion if separable. For deadline-bearing not-done tasks, surface the deadline regardless of audit bookkeeping.

## Consent

Starting `task-completeness` raises a one-time `workflow:task-completeness` consent prompt. Have the user grant "Allow always (this session, 24h)" on it BEFORE the fan-out, so the session's `workflow_class:task-completeness` grant lets all sub-agents (which share your session id via `wb_init`) run unprompted. Because every `resolve` is deferred, no high-weight toggle consent is hit during the sweep itself; the only mutating consent is the user-approved backdated toggles in Step 6.

## Do NOT

- Do not mutate anything before the user signs off in Step 6.
- Do not spawn the full sweep for a single task — use `/wb-task-completeness`.
- Do not let a consent stall hang a sub-agent — record it as `error` and move on.
- Do not skip the cost gate, and do not proceed on a bare "go" for a very large list.
- Do not have sub-agents toggle, create, or edit anything except their own append-only JSONL.
