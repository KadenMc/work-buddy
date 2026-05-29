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
    key_types:
      disposition: str
      evidence: list
      recommended_action: str
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

Core principle — judge the SPIRIT, not the letter. Tasks are written before the work; designs change, get improved, or a sub-part is deliberately dropped. Something can look undone because it was done a better way, or not done on purpose. Your job is to decide whether what is PRESENT in the codebase/notes satisfies the task's INTENT.

Disposition rubric (pick one): done / done-differently / partial / consciously-descoped / not-done.

Verification ladder — cheapest signal first, stop when confident: (1) the auto-gathered evidence bundle (task note + per-session commits/writes), (2) git log --grep/-S and gh PR/commit search, (3) reading the actual code and tests, (4) running the targeted test. Inactivity is NOT completion — a quiet task is not a done task.

Dating: the completion date is the date the LANDING COMMIT merged, not today. Always cite the commit SHA as evidence.

Always confirm before mutating. The investigate step produces the judgment; the resolve step acts only on explicit user approval. Backdating uses task_toggle's done_date param (ISO YYYY-MM-DD).

## investigate

You are judging whether this task was ALREADY completed — and if so, WHEN.

Read the evidence bundle in step_results['gather-evidence']: the task text + linked note (the original intent), its current state, and per assigned session the attributed commits/writes. Heed `cache_note` and any per-session `note` — when a session has no transcript (sidecar/pruned) there is no commit linkage, so you must attribute the work yourself.

Then investigate ADAPTIVELY, cheapest signal first, stopping as soon as you are confident:
1. The evidence bundle (commits/writes already attributed).
2. `git log --grep`/`git log -S` for the task id, symptom, or key symbols; `gh pr list/view` and `gh search prs/commits` for related PRs.
3. Read the ACTUAL code/tests in the repo to confirm the behavior exists now.
4. Run the targeted test(s) only if code presence is still ambiguous.

Judge the SPIRIT, not the letter — a design may have been improved, replaced, or a sub-part deliberately dropped. Decide whether what is PRESENT satisfies the task's INTENT. Pick exactly one disposition: done | done-differently | partial | consciously-descoped | not-done. Inactivity is NOT completion.

Return ONLY your new findings (a delta) with keys: disposition (one of the five); evidence (a non-empty list of concrete citations — commit SHAs, PR URLs, path:line, test names, note excerpts — each tied to what it proves); completion_date (ISO YYYY-MM-DD of the LANDING COMMIT that satisfied the intent, or null; cite its SHA in evidence); recommended_action (one sentence: mark done backdated / finish remainder / spin off residual / leave open).

## resolve

Present the disposition from `investigate` with its evidence and completion date, then act ONLY on explicit user confirmation — never mutate first.

Recommend by disposition:
- done / done-differently: offer to mark complete BACKDATED via wb_run('task_toggle', {'task_id': '<id>', 'done': true, 'done_date': '<completion_date>'}). done_date is the landing-commit date from investigate.
- consciously-descoped: same backdated toggle, and state plainly that the missing part was a deliberate descope.
- partial: offer BOTH (a) finishing the remainder now and/or (b) spinning the remainder off as a handoff task via wb_run('task_create', {...}); optionally backdate-complete the shipped portion if separable.
- not-done: recommend leaving it open; optionally offer to narrow the task text to the true remainder.

After the user picks, apply exactly what was approved and report what changed (the new ✅ date, any new task id). If they decline, leave the task untouched and say so.
