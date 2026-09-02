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

0. **Freeze today's Journal composition.** Call `mcp__work-buddy__wb_run("journal_state", {"target": "today", "create_on_read": true})`. This persists the native logical-day/profile snapshot without creating a file.

1. Get the configured lookback window: `hours = step_results["load-config"].get("morning", {}).get("context_hours", 24)` (default: 24).

2. Run the context bundle collector:
   ```
   mcp__work-buddy__wb_run("context_bundle", {"hours": <hours>})
   ```

3. Read the resulting pack files from the bundle path returned in the result. Priority files are `git_summary.md`, `tasks_summary.md`, `projects_summary.md`, `messages_summary.md`, `agent_session_summary.md` (agent conversations: per-session tldr/topics, commits, PRs), `chat_summary.md` (absent when empty), and `calendar_summary.md`. Obsidian and legacy wellness packs may be absent by design.

4. Condense into a structured activity digest â€” not the raw pack, but a ~20 line summary of: what repos had activity, what journal entries exist, outstanding tasks, recent conversations, messages.

**Result:** Condensed activity digest string.

---

## sign-in

**Purpose:** Brief morning check-in that fills the journal Sign-In fields. Always enabled.

Agentic step. The agent conducts a conversational check-in and writes responses to the journal. Behavioral instructions (conversation tone, what to ask, how to use wellness data) are in the slash command, not here.

**Procedure:**

1. Read the target day's profile-defined fields:
   ```
   mcp__work-buddy__wb_run("journal_sign_in")
   ```
   Returns `sign_in.fields` with generic field IDs, labels, prompts, types, constraints, function/behavior contracts, values, and revisions. No particular marker is guaranteed to exist.

2. **If `sign_in.all_filled`**: Summarize the fields that actually exist; do not mention absent fields or invent a fixed wellness schema.

3. **If NOT all filled**: Conduct a concise check-in using the missing fields' labels, prompts, requiredness, and constraints. Optional fields stay optional.

4. Write responses to the journal:
   ```
   mcp__work-buddy__wb_run("journal_sign_in", {"write_fields": "{\"<fieldId>\": {\"value\": <typed value>, \"expected_revision\": 0}}", "client_mutation_id": "<stable retry key>"})
   ```
   This is consent-gated â€” on `consent_required` response, follow the standard consent flow.

5. Return a field-aware summary plus the generic `wellness`/declared-function payload for downstream steps. Do not run the retired fixed-marker interpreter.

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

2. **Placement:** `journal_write` resolves the active profile's generated-artifact-capable module and stores the briefing as a Source-backed, provenance-only Journal item. It does not assume any profile-specific section or field.
   ```
   mcp__work-buddy__wb_run("journal_write", {"mode": "briefing", "briefing_md": "<briefing markdown>"})
   ```
   This is consent-gated -- on `consent_required` response, follow the standard consent flow.

**Result:** A native Journal item receipt or `{"persisted": false, "reason": "..."}`.

---

## day-planner

**Purpose:** Generate a provider-neutral schedule from calendar events and focused tasks, then optionally store it as a native Journal artifact.

**Phase gate:** Check `step_results["load-config"]["morning"]["day_planner"]["enabled"]`. If `false`, return `{"generated": false, "skipped_reason": "disabled by config"}`.

**Procedure -- follow ALL five sub-steps in order:**

**Sub-step 1 -- Status check:**
```
status = mcp__work-buddy__wb_run("day_planner", {"action": "status"})
```
If not ready, return `{"generated": false, "skipped_reason": "native Journal profile is not ready: <reason>"}`.

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

- **Calendar inclusion:** The native Journal has no remote-calendar projection, so pass the selected Calendar events once. `status.hasRemoteCalendars` is false under native authority.

- **Past-time protection:** `clamp_to_now` is `true` by default — unpinned tasks will not be placed before the current local time. Pinned tasks (`time_start`) bypass this clamp; pin intentionally for retrospective blocks.

**Sub-step 4 -- Generate and write:**
```
mcp__work-buddy__wb_run("day_planner", {"action": "generate_and_write", "calendar_events": "<json>", "focused_tasks": "<json>"})
```

**Sub-step 5 -- Present summary:**
Show the user a brief summary of the generated timed blocks for the day.

**Result:** `{"generated": true, "entries": [...]}` or `{"generated": false, "skipped_reason": "..."}`

---
