---
name: Morning Routine
kind: workflow
description: Configurable morning routine that coordinates journal, tasks, contracts, calendar, and metacognition into a single briefing-first flow. Collect everything, then synthesize and act.
workflow_name: morning-routine
execution: main
steps:
- id: load-config
  name: Load morning config
  step_type: code
  depends_on: []
  invokes: []
  auto_run:
    callable: work_buddy.morning.get_morning_config
- id: resolve-phases
  name: Resolve enabled phases
  step_type: code
  depends_on:
  - load-config
  invokes: []
  auto_run:
    callable: work_buddy.morning.resolve_phases
    input_map:
      cfg: load-config
- id: context-snapshot
  name: Collect fresh context snapshot
  step_type: code
  depends_on:
  - resolve-phases
  invokes:
  - context_bundle
  - journal_state
  execution: subagent
  visibility:
    mode: summary
- id: sign-in
  name: Morning sign-in conversation
  step_type: reasoning
  depends_on:
  - context-snapshot
  invokes:
  - journal_sign_in
- id: yesterday-close
  name: Close out yesterday's journal
  step_type: reasoning
  depends_on:
  - context-snapshot
  optional: true
  invokes:
  - journal_state
  - update-journal
- id: calendar-today
  name: Fetch today's calendar schedule
  step_type: code
  depends_on:
  - context-snapshot
  optional: true
  invokes:
  - context_calendar
- id: task-briefing
  name: Get task status summary
  step_type: code
  depends_on:
  - context-snapshot
  optional: true
  invokes:
  - task_briefing
  visibility:
    mode: summary
    include_keys:
    - focused
    - mit
    - overdue
    - inbox_count
    - stale
- id: contract-check
  name: Check contract health and constraints
  step_type: code
  depends_on:
  - context-snapshot
  optional: true
  invokes:
  - contract_constraints
  - contract_health
  visibility:
    mode: full
- id: blindspot-scan
  name: Scan yesterday's work for blindspot patterns
  step_type: reasoning
  depends_on:
  - context-snapshot
  optional: true
  invokes: []
- id: synthesize
  name: Synthesize morning briefing
  step_type: reasoning
  depends_on:
  - sign-in
  - yesterday-close
  - calendar-today
  - task-briefing
  - contract-check
  - blindspot-scan
  invokes: []
  result_schema:
    required_keys:
    - briefing_md
    key_types:
      briefing_md: str
- id: propose-mits
  name: Propose MITs, review with user, and create tasks
  step_type: reasoning
  depends_on:
  - synthesize
  invokes:
  - task_change_state
  - task_create
  - task_toggle
- id: persist-briefing
  name: Write morning briefing to journal
  step_type: reasoning
  depends_on:
  - propose-mits
  invokes:
  - journal_write
- id: day-planner
  name: Generate Day Planner schedule
  step_type: reasoning
  depends_on:
  - propose-mits
  - calendar-today
  invokes:
  - day_planner
tags:
- morning
- routine
parents:
- morning
---

## context-snapshot

**Purpose:** Gather fresh context from all tracked systems. This is always the first step â€” everything downstream depends on it.

**Phase gate:** Always enabled (core). No skip check needed.

**Procedure:**

0. **Ensure today's journal exists.** Call `mcp__work-buddy__wb_run("journal_state", {"target": "today"})` â€” if the result shows `exists: false`, the journal needs creating via Obsidian.

1. Get the configured lookback window: `hours = step_results["load-config"].get("morning", {}).get("context_hours", 24)` (default: 24).

2. Run the context bundle collector:
   ```
   mcp__work-buddy__wb_run("context_bundle", {"hours": <hours>})
   ```

3. Read the resulting pack files from the bundle path returned in the result. Priority files: `git_summary.md`, `tasks_summary.md`, `projects_summary.md`, `obsidian_summary.md`, `messages_summary.md`, `chat_summary.md`, `calendar_summary.md`, `wellness_summary.md`.

4. Condense into a structured activity digest â€” not the raw pack, but a ~20 line summary of: what repos had activity, what journal entries exist, outstanding tasks, recent conversations, messages.

**Result:** Condensed activity digest string.

---

## sign-in

**Purpose:** Brief morning check-in that fills the journal Sign-In fields. Always enabled.

Agentic step. The agent conducts a conversational check-in and writes responses to the journal. Behavioral instructions (conversation tone, what to ask, how to use wellness data) are in the slash command, not here.

**Procedure:**

1. Read current sign-in state and wellness trends:
   ```
   mcp__work-buddy__wb_run("journal_sign_in")
   ```
   Returns `{sign_in: {sleep, energy, mood, check_in, motto, all_filled}, wellness: "..."}`.

2. **If `sign_in.all_filled`**: Return `{"summary": "Sign-in already complete. Sleep: X, Energy: X, Mood: X", "wellness_context": wellness}`.

3. **If NOT all filled**: Conduct check-in conversation for missing fields.

4. Write responses to the journal:
   ```
   mcp__work-buddy__wb_run("journal_sign_in", {"write_fields": "{\"sleep\": 7, \"energy\": 8, \"mood\": 7, \"check_in\": \"...\", \"motto\": \"...\"}"})
   ```
   This is consent-gated â€” on `consent_required` response, follow the standard consent flow.

5. Return `{"summary": "...", "wellness_context": wellness}` for downstream steps.

**Result:** Sign-in summary with wellness context.

---

## yesterday-close

**Purpose:** Close out yesterday's journal by filling Log gaps.

Agentic step. The agent checks yesterday's journal and auto-fills gaps. Behavioral instructions (interaction rules) are in the slash command, not here.

**Phase gate:** Check `step_results["resolve-phases"]["yesterday-close"]`. If `false`, skip.

**Procedure:**

1. Read yesterday's journal state:
   ```
   mcp__work-buddy__wb_run("journal_state", {"target": "yesterday"})
   ```

2. **If `error`**: Log the error and skip: `{"skipped": true, "reason": error}`.

3. **If `ambiguous`**: Log a warning and skip.

4. **If NOT `exists`**: Return `{"skipped": true, "reason": "no journal for {date}"}`.

5. **If `exists`**: Check the journal content. If the Log section has 3+ entries, return `{"summary": "Yesterday ({date}) has N log entries. Last: HH:MM AM/PM."}`.

6. **If the Log is sparse (< 3 entries)**: Delegate to `update-journal` workflow: `mcp__work-buddy__wb_run("update-journal", {"target": "yesterday"})` and advance to completion. Return the summary.

**Result:** Brief summary string describing yesterday's state.

---

## calendar-today

**Purpose:** Fetch today's Google Calendar schedule.

**Phase gate:** Check `step_results["resolve-phases"]["calendar-today"]`. If `false`, skip.

**Procedure:**

1. Fetch today's calendar (includes readiness check):
   ```
   mcp__work-buddy__wb_run("context_calendar")
   ```

2. If the result indicates unavailability, return `{"available": false, "reason": "Calendar not available"}`. Do NOT fail â€” graceful degradation.

3. Otherwise return the calendar data.

**Result:** Calendar data or unavailability notice.

---

## task-briefing

**Purpose:** Get current task status.

**Phase gate:** Check `step_results["resolve-phases"]["task-briefing"]`. If `false`, skip.

**Procedure:**
```
mcp__work-buddy__wb_run("task_briefing")
```
Return the result directly.

**Result:** Task briefing data dict.

---

## contract-check

**Purpose:** Check contract health, active constraints, and deadlines.

**Phase gate:** Check `step_results["resolve-phases"]["contract-check"]`. If `false`, skip.

**Procedure:**

1. `mcp__work-buddy__wb_run("contract_constraints")` â€” active contracts with bottleneck constraints.
2. `mcp__work-buddy__wb_run("contract_health")` â€” health check report.
3. Combine into a single result dict with `active_count`, `constraints`, `health`, `top_constraint`, `has_paper_contract`.
4. If no active contracts exist, note it explicitly.

**Result:** Combined contract data dict.

---

## blindspot-scan

**Purpose:** Check yesterday's work against metacognition patterns.

**Phase gate:** Check `step_results["resolve-phases"]["blindspot-scan"]`. If `false`, skip.

**Procedure depends on `step_results["load-config"]["morning"]["blindspot_depth"]`:**

#### Light mode (default)

Agentic step. The agent scans for HIGH-severity patterns using context-snapshot and yesterday-close data. Behavioral instructions (pattern list, what to look for) are in the slash command, not here.

Return a brief summary: either "None detected" or a list of pattern names with one-line evidence.

#### Full mode

Run the complete blindspot scan rather than the HIGH-severity light pass: load `metacognition/blindspot-directions` (`mcp__work-buddy__wb_run("agent_docs", {"path": "metacognition/blindspot-directions", "depth": "full"})`) and follow it end-to-end — all severities, cascade checking, the intervention ladder. Return the full pattern report.

**Result:** Pattern summary string or full blindspot report.

---

## synthesize

**Purpose:** Combine all collected data into a concise morning briefing.

Agentic step. The agent builds a briefing from all prior step results and presents it. Behavioral instructions (tone, synthesis rules, follow-up offers, presentation style) are in the slash command, not here.

**Procedure:**

1. Read each prior step from `step_results.<step_id>`: `sign-in`, `yesterday-close`, `calendar-today`, `task-briefing`, `contract-check`, `blindspot-scan`. For skipped/failed steps the entry will be `None` or `{"skipped": true}` — handle gracefully.

2. Synthesize a briefing from those step results. The briefing covers: yesterday summary, calendar, tasks, contracts, projects, blindspots. Tone and format rules are in the slash command.

3. Present the briefing and offer follow-ups.

4. Return `{"briefing_md": briefing_md}` only. Downstream steps read the underlying data directly from `step_results.<upstream_id>` — do not echo the upstream results back under a `results` bundle, that just round-trips data already in the response.

**Result:** Briefing markdown.

## propose-mits

**Purpose:** Propose today's Most Important Tasks, present for user review, create tasks, and set them to focused state.

**Phase gate:** None -- always runs.

**Procedure:**

1. Using `step_results["synthesize"]["briefing_md"]` for the briefing narrative and reading underlying data directly from each upstream step's `step_results` entry (`step_results["task-briefing"]`, `step_results["contract-check"]`, `step_results["blindspot-scan"]`, `step_results["yesterday-close"]`), propose up to `step_results["load-config"]["morning"]["max_mits"]` MITs. Behavioral instructions (MIT quality rules, user review, presentation) are in the slash command, not here.

2. Present the proposed MITs to the user for review. Wait for confirmation or adjustments.

3. **Complete any old MITs.** If the task briefing shows previously-focused MITs that are now done, mark them complete:
   ```
   mcp__work-buddy__wb_run("task_toggle", {"task_id": "<id>", "done": true})
   ```

4. **Create MIT tasks** in the master task list. Include `#tasker/state/focused` in the task_text (interim workaround for Dataview visibility -- see t-abe6ea4b):
   ```
   mcp__work-buddy__wb_run("task_create", {"task_text": "MIT: <task text> #tasker/state/focused", "urgency": "high", "project": "...", "due_date": "...", "contract": "..."})
   ```
   Then also set each created task to focused via the state API:
   ```
   mcp__work-buddy__wb_run("task_change_state", {"task_id": "<id>", "state": "focused"})
   ```

**Result:** `{"mits_created": [{"id": "t-...", "text": "...", "due": "..."}], "old_mits_completed": ["t-..."]}`

---

## persist-briefing

**Purpose:** Write the morning briefing to the journal. Consent-gated.

**Phase gate:** Check `step_results["load-config"]["morning"]["persist_briefing"]`. If `false`, return `{"persisted": false, "reason": "disabled by config"}`.

**Procedure:**

1. Compose the briefing markdown from `step_results["synthesize"]["briefing_md"]`, incorporating the MIT list from `step_results["propose-mits"]["mits_created"]`.

2. **Placement:** The briefing callout goes in the **Sign-In section**, after the Motto field and before `# **Tasks & Objectives**`. The `journal_write` capability handles placement automatically. The callout header includes the generation timestamp.
   ```
   mcp__work-buddy__wb_run("journal_write", {"mode": "briefing", "briefing_md": "<briefing markdown>"})
   ```
   This is consent-gated -- on `consent_required` response, follow the standard consent flow.

**Result:** `{"persisted": true, "path": "..."}` or `{"persisted": false, "reason": "..."}`

---

## day-planner

**Purpose:** Check Day Planner plugin readiness, generate a schedule from calendar events and focused tasks, and write to journal.

**Phase gate:** Check `step_results["load-config"]["morning"]["day_planner"]["enabled"]`. If `false`, return `{"generated": false, "skipped_reason": "disabled by config"}`.

**Procedure -- follow ALL five sub-steps in order:**

**Sub-step 1 -- Status check:**
```
status = mcp__work-buddy__wb_run("day_planner", {"action": "status"})
```
If not ready, return `{"generated": false, "skipped_reason": "plugin not ready: <reason>"}`.

**Sub-step 2 -- Read existing plan:**
```
existing = mcp__work-buddy__wb_run("day_planner", {"action": "read"})
```
If `entry_count > 0`, return `{"generated": false, "skipped_reason": "user already has a plan with N entries"}`. Do not clobber user edits.

**Sub-step 3 -- Gather inputs:**

- **Calendar events:** Build from `step_results["calendar-today"]`. The raw `context_calendar` result is a pre-formatted markdown string, not a structured list — you must extract events from it and pass them in one of the accepted shapes below. If calendar was skipped/unavailable, use `[]`.

  Accepted shapes (see `day_planner` capability docstring for full spec):
  - Flat (easier when constructing by hand):
    ```
    [{"start": "13:00", "end": "13:30", "summary": "Team sync"}]
    ```
    `start`/`end` accept `"HH:MM"` or ISO datetime. Label key may be `summary`, `description`, or `text`. Set `past: true` to exclude.
  - Google Calendar API (raw from Calendar API):
    ```
    [{"start": {"dateTime": "2026-04-16T13:00:00-04:00"}, "end": {"dateTime": "2026-04-16T13:30:00-04:00"}, "summary": "...", "timeStatus": "future"}]
    ```

- **Focused tasks:** MITs from `step_results["propose-mits"]["mits_created"]` plus any pre-existing focused tasks from `step_results["task-briefing"]`. Each task accepts:
  - `description` or `text` (required): task label
  - `duration` (optional, int minutes): overrides config default
  - `time_start` (optional, `"HH:MM"`): pin task to a specific start time; conflicts fall to unscheduled

  Example:
  ```
  [{"description": "MIT 1: Ship LLM-judge eval", "duration": 90},
   {"description": "MIT 2: Draft paper contract", "duration": 30, "time_start": "14:00"}]
  ```

- **Calendar duplication guard:** If `hasRemoteCalendars` is `true` in the status from sub-step 1, set `include_calendar_events: false` in config overrides to avoid duplication.

- **Past-time protection:** `clamp_to_now` is `true` by default — unpinned tasks will not be placed before the current local time. Pinned tasks (`time_start`) bypass this clamp; pin intentionally for retrospective blocks.

**Sub-step 4 -- Generate and write:**
```
mcp__work-buddy__wb_run("day_planner", {"action": "generate_and_write", "calendar_events": "<json>", "focused_tasks": "<json>"})
```

**Sub-step 5 -- Present summary:**
Show the user a brief summary of the generated timed blocks for the day.

**Result:** `{"generated": true, "entries": [...]}` or `{"generated": false, "skipped_reason": "..."}`

---
