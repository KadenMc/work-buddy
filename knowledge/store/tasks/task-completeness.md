---
name: Task Completeness
kind: workflow
description: Investigate whether a task was already completed (fully/partially/differently), judge the spirit not the letter, then optionally mark it complete with the correct prior date.
summary: 'Investigate-and-retro-complete a task: auto-gather task + session commit/write evidence, judge completeness against a spirit-over-letter rubric (done/done-differently/partial/consciously-descoped/not-done), then on confirmation mark it done with the landing-commit date via task_toggle''s done_date.'
workflow_name: task-completeness
execution: main
allow_override: false
steps:
- id: gather-evidence
  name: Gather completion evidence
  step_type: code
  depends_on: []
  auto_run:
    callable: work_buddy.task_completeness.gather_completeness_evidence
    input_map:
      task_id: __params__.task_id
    timeout: 90
- id: investigate
  name: Investigate completeness
  step_type: reasoning
  depends_on:
  - gather-evidence
  result_schema:
    required_keys:
    - disposition
    - evidence
    - completion_date
    - recommended_action
    - confidence
    key_types:
      disposition: str
      evidence: list
      recommended_action: str
      confidence: str
      divergence: str
    min_items:
      evidence: 1
- id: resolve
  name: Confirm and apply
  step_type: reasoning
  depends_on:
  - investigate
  invokes:
  - task_toggle
  - task_create
params_schema:
  task_id:
    type: str
    description: Task ID to investigate (e.g. t-99b8a4ff)
    required: true
command: wb-task-completeness
tags:
- tasks
- task
- completeness
- stale
- retroactive
- investigate
- done-date
aliases:
- was this done
- already completed
- retroactive complete
- check task completeness
- did we already fix this
parents:
- tasks
---

Investigate whether a task the user is unsure about was ALREADY completed — fully, partially, or differently — and if so, mark it complete with the correct PRIOR date.

Core principle — judge the SPIRIT, not the letter. Tasks are written before the work; designs change, get improved, or a sub-part is deliberately dropped. Decide whether what is PRESENT in the codebase/notes satisfies the task's INTENT.

**Lead with intent.** Before you judge OR present, restate what the task actually asked for — read `task.note_content` for its INTENT and any prescribed how-to (a "Suggested Approach"/design in the note). Judge the implementation against THAT, never against the commit subject: a commit can bundle unrelated work, and its subject can under- or over-state the task's scope. Mistaking the commit subject for the intent is the classic shallow pass.

**Provenance-first.** The gather-evidence bundle carries a `provenance` block (created_by / assigned / developed_by). `developed_by` is the structural answer to "who shipped this" — sessions whose commits reference the task id — each tagged with note-read `awareness` and an informed/convergent `classification`. Start there; it replaces hand git-archaeology. `session_search` into a developer's session recovers the design reasoning before you judge.

Disposition rubric (pick one): done / done-differently / partial / consciously-descoped / not-done.

**done-differently is a QUALITY judgment, not just intent-coverage.** When the task prescribed a how-to (an approach/design in the note) and the implementation diverged, classify the divergence — better / lateral / worse — and justify it from the developer's rationale + the code. "Intent met by a different, better-reasoned design" and "intent met by a worse shortcut" are both done-differently but very different verdicts; say which.

**Informed vs convergent.** Use `awareness`: informed development (read the note / assigned) that diverged is a deliberate design choice; convergent development (no signal) that hits the same intent is independent corroboration, not a footnote. `developed_by` empty + no structural link is the Rung-3 (intent-only) case — judge by reading the code/tests, never by absence of a link.

Verification ladder — cheapest first, stop when confident: (1) the provenance block + per-session commits/writes, (2) session_search into the developing session for rationale, plus `git log --grep`/`-S` and `gh` PR/commit search for anything provenance missed, (3) read the actual code and tests, (4) run the targeted test. Inactivity is NOT completion.

Dating: completion date = the date the LANDING COMMIT merged, not today. Cite the SHA.

**Confidence.** Every verdict states a `confidence` (high/medium/low) justified by signal strength — a structural `developed_by` plus read code is high; an intent-only Rung-3 inference is medium-at-best. Be authoritative where the evidence is, honest where it isn't.

Always confirm before mutating. investigate produces the judgment; resolve acts only on explicit user approval. Backdating uses task_toggle's done_date (ISO YYYY-MM-DD).

## investigate

You are judging whether this task was ALREADY completed — and if so, WHEN, BY WHOM, and HOW WELL.

Start from `step_results['gather-evidence']`:
- `task` — text + linked `note_content` = the original intent (and any prescribed how-to).
- `provenance` — created_by / assigned / developed_by / intent_attribution. `developed_by` names the sessions that structurally shipped work (a commit referencing the task id), each with `rung`, note-read `awareness`, and informed/convergent `classification`. This is your starting point — no git archaeology from scratch.
- `session_evidence` — per session (across created/assigned/developed/**note_reader** roles): commits, writes, summary. Heed each `note` and the top-level `cache_note`. A `note_reader` role means the session demonstrably READ the task note (native `Read` of the note file, or a `task_read`/`task_assign` call) — the Rung-3 surface for "read it, did the work, never referenced the task id in a commit." Pure-triage reads (read, but no commits/writes) are pruned; the ones that survive carry work.

**Step 0 — restate the INTENT (do this FIRST).** From `task.note_content`, name (a) what the task actually asked for and (b) any prescribed how-to (a "Suggested Approach"/design in the note, possibly with a v1-vs-follow-up split). Judge the implementation against THIS intent — NOT the commit subject, which may bundle unrelated work or mis-state scope. The prescribed how-to is what makes the divergence judgment possible: did the impl follow it, improve on it, or cut a corner? Skipping Step 0 is how a shallow "looks done" pass slips through.

Then investigate ADAPTIVELY, cheapest first, stopping when confident:
1. The provenance block + session_evidence.
1.5. Inspect `session_evidence` for `note_reader`-role entries that ALSO carry commits or writes — the strongest *Rung-3 developer candidates* (read the note + made changes but never referenced the task id in a commit, i.e. the classic "forgot to toggle"). When `developed_by` is empty, these are your best leads; `session_search` into them for rationale before judging.
2. `session_search` into a developer's session id to recover the design reasoning (WHY they built it that way). Plus `git log --grep`/`-S` and `gh` PR/commit search for anything provenance missed.
3. Read the ACTUAL code/tests to confirm the behavior is present now and matches (or diverges from) the prescribed how-to.
4. Run the targeted test(s) only if presence is still ambiguous.

Judge the SPIRIT, not the letter. Pick exactly one disposition: done | done-differently | partial | consciously-descoped | not-done. If the note scoped a v1 and blessed a follow-up, shipping the v1 is `done` — but surface the unbuilt follow-up explicitly (it is a scope boundary, not a gap).

If done-differently AND the note prescribed a how-to: set `divergence` to better | lateral | worse and justify it — does the shipped design beat what was proposed, match it, or cut a corner? Use the developer's rationale (from session_search) + the code. Frame with `awareness`: informed divergence = deliberate; convergent match = independent corroboration. When `developed_by` is empty, that's the Rung-3 intent-only case — judge by reading code/tests, not by the missing link.

Return ONLY your delta:
- disposition (one of the five)
- evidence (non-empty list of concrete citations: commit SHAs, PR URLs, path:line, test names, session-id+turn for rationale, note excerpts — each tied to what it proves)
- completion_date (ISO YYYY-MM-DD of the LANDING COMMIT, or null; cite its SHA in evidence)
- divergence (better | lateral | worse | na — `na` unless done-differently with a prescribed how-to)
- confidence (high | medium | low, with a one-clause justification keyed to signal strength)
- recommended_action (one sentence: mark done backdated / finish remainder / spin off residual / leave open)

## resolve

**Open with a plain-language recap BEFORE any verdict fields** — enough that the user can judge without reading the evidence list:
1. **What the task asked for** — its intent + any prescribed how-to (from the note), including any v1-vs-follow-up scoping.
2. **The situation found** — who developed it (informed vs convergent), when, and how the implementation compares to what was prescribed (followed it / improved on it / diverged / partial), plus any deliberately-deferred follow-up.

THEN give the verdict fields: disposition + confidence + divergence + completion_date + a one-line provenance summary. Act ONLY on explicit user confirmation — never mutate first.

Recommend by disposition:
- done / done-differently: offer to mark complete BACKDATED via wb_run('task_toggle', {'task_id': '<id>', 'done': true, 'done_date': '<completion_date>'}). For done-differently, state the divergence verdict (better/lateral/worse) plainly so the user signs off on 'done a different way', not 'done as written'. If a follow-up was deferred, offer to spin it off via task_create.
- consciously-descoped: same backdated toggle; state plainly the dropped part was a deliberate descope.
- partial: use `developed_by` to delineate what SHIPPED vs the remainder. Offer BOTH (a) finishing the remainder now and/or (b) spinning the remainder off as a handoff via wb_run('task_create', {...}); optionally backdate-complete the shipped portion if separable.
- not-done: recommend leaving it open; optionally offer to narrow the task text to the true remainder.

If the task is ALREADY `state=done`, say so up front — no toggle needed; the value of the run is the contextualized confirmation (and surfacing any deferred follow-up), not a mutation.
