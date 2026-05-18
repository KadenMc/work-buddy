---
name: Session Retrospective
kind: directions
description: Switch to developmental mode and critique/debug this session's execution — then fix what you find
summary: Switches the agent from operational to developmental mode to critique and debug the current session's execution. Covers capability bugs, prompt and context issues, agent reasoning failures, and output quality — then triages fixes with the user and executes them.
trigger: When the user invokes /wb-dev-retro or asks for a session retrospective, post-session critique, or to debug what went wrong during an operational session
command: wb-dev-retro
tags:
- dev
- developmental
- retrospective
- debugging
- quality
- self-critique
aliases:
- session retro
- session retrospective
- debug this session
- critique execution
- fix friction
- post-session review
- what went wrong
parents:
- dev
- dev
---

<<wb:dev/dev-mode --recursive>>

Switch from operational to developmental mode. Critique and debug this session's execution, then fix what you find.

The design rationale behind operational vs developmental agents: operational agents get things done (workflows, tasks, journals). Developmental agents improve the system itself (code, prompts, capabilities). The session retrospective bridges the two — the agent that *experienced* the friction diagnoses and fixes it, preserving first-person context that would be lost in a handoff to a separate evaluator.

**Prerequisite:** You must have done operational work in this session (morning routine, task triage, journal update, context collection, etc.). If you haven't, tell the user there's nothing to retrospect on.

## Step 1: Inspect prior runs (mandatory)

Before you critique anything, **look at the actual outputs** — not just the tool-call metadata. Top-level tool responses summarize what was called with what args, but the crucial diagnostic signal lives inside the per-call artifacts. Skipping this is how you end up confidently wrong about why a run failed.

For every `llm_with_tools` run referenced in this session:
1. `artifact_get(<reasoning_artifact_id>)` if one was persisted — this is the model's actual chain-of-thought, and it almost always reveals the root cause (wrong input schema, misread flag, bad inference) that the args list alone cannot show.
2. `artifact_get(<output_artifact_id>)` for every tool call whose `output_size_chars` is either large OR surprisingly small. An `output_size_chars: 2` means `[]` — something the model saw as a null result that looked identical to a valid zero-match query. The distinction usually lives in the raw response, not the count.
3. `artifact_list(session_id=<lms-XXXX>)` when a run timed out or errored: if no artifacts exist at all, the model never produced anything (cold-load or transport failure); if artifacts exist, they'll tell you where it stopped.

For `llm_call` runs: inspect `cached`, `error`, and if the run returned empty `content`, cross-check `output_tokens` against the profile's `max_output_tokens` — a thinking model that ate its whole budget in reasoning produces zero visible content.

For workflow runs (`update-journal`, `task-triage`, etc.): `wb_step_result(run_id, step_id)` to retrieve full step data that was elided by the visibility system. A step showing `_manifest: true` has real data you haven't seen yet.

**Do not conclude anything about a run's behavior based on tool-call args alone.** That was the most expensive failure mode in the session this directions unit exists to prevent.

## Step 2: Critique

Review everything that happened in this session. Be specific, be harsh, and cite evidence (quote the call, the response, the output). You are debugging the system — not just the code, but the behavior, the prompts, the context engineering, the workflow design, and your own decisions.

Look for:

**Capability bugs**
- Calls that returned wrong, irrelevant, or excessively broad results
- Missing capabilities (you had to write raw Python because nothing existed in the gateway)
- Silent failures or unhelpful error messages
- Redundant calls (same data fetched multiple ways)

**Prompt and context issues**
- Slash commands that didn't give you enough context, or gave you the wrong context
- Workflow steps that were awkward, out of order, or unnecessary
- Violations of just-in-time retrieval: context loaded too early (bloat) or too late (missed)
- Unclear data contracts between workflow steps

**Agent behavior issues (debug your own reasoning)**
- Wrong approach — a better-contextualized agent would have avoided this path
- Repeated mistakes within the session
- Missed signals that were obvious in retrospect
- Over-engineering or under-engineering

**Output quality**
- Journal entries, tasks, briefings, or user-facing summaries that were poorly structured, too verbose, or missed the point

Format as:

```markdown
## Session Critique

### Critical (broke something or produced wrong output)
1. **[Category] Short title**: What happened. What should have happened. Evidence.

### Friction (slowed things down or degraded quality)
1. **[Category] Short title**: What happened. What should have happened.

### Minor (polish)
1. **[Category] Short title**: What happened. What should have happened.
```

Present this to the user. Ask if they experienced friction you missed or if any of your critiques are off-base.

## Step 3: Triage (with the user)

Present the full critique and let the user decide what to act on. For each issue, offer these options:

- **Fix now** — code change, prompt edit, config tweak. Do it in this session.
- **Debug first** — root cause is unclear. Reproduce the issue, investigate, then fix.
- **Create a task** — needs deeper work or a fresh session. Use `/wb-task-handoff` to capture the issue with full context so a future agent can pick it up.
- **Skip** — not worth fixing, or not actually a problem.

The user drives this. Don't prescribe how many to fix.

## Step 4: Do the work

For each issue the user selected:

**Fix now:** Read the relevant files, make the change, test if possible (re-run the call that failed), briefly note what you changed.

**Debug first:** Reproduce the problem — re-run the call or workflow step. Narrow the root cause: is it the capability code, the prompt, the workflow, the data? Then fix and test. If you learn something non-obvious, document it in the relevant knowledge store unit.

**Create a task:** Use `/wb-task-handoff`. The handoff should include the specific failure (with evidence from this session), your root cause hypothesis, the files involved, and what you already tried.

## Step 5: Close out

If you made code changes:
1. Review them holistically — do they introduce new issues?
2. Summarize: what was fixed, what was handed off, what was skipped
3. Call out systemic patterns if you see them (e.g., "three issues were all about context bloat — the JIT pattern needs enforcement across workflows")

## What NOT to do

- **Boil the ocean** — don't try to fix everything
- **Stay abstract** — "search should be better" is not actionable; "context_search returned 47 results including binary files, needs a file-type filter" is
- **Bikeshed** — don't spend 30 min on naming when a capability is broken
- **Scope-creep into features** — this is about fixing friction, not adding new features
- **Self-congratulate** — don't list things that went well; the point is improvement
- **Critique runs without inspecting their artifacts first** — this is the Step 1 mandate. Violations produce confident-but-wrong analyses that cost real local-compute minutes to re-run.
- **Exonerate a recent PR from a friction connection without inspecting its diff file-by-file.** "I checked the obvious file and it didn't change" produces confidently-wrong analyses — the same epistemic shortcut as the artifact-inspection rule above. Run `gh pr view <N> --json files` to enumerate the changed files, then trace each one against your friction list. Especially watch for: workflow renames (slugs, slash commands, aliases — agents with stale mental models hit discovery friction), validation/schema changes (stricter `result_schema`, new required fields), RAG/search changes (different units surface for the same query), and error-message format changes. The diff check costs minutes; a wrong attribution costs the same friction recurring.
