---
name: Route Information
kind: workflow
description: Given a batch of discrete information items (each with an ID, raw text, and optional agent-proposed metadata), present routing recommendations to the user in clusters, get confirmation or correction, and execute the approved routings.
summary: Given a batch of discrete information items (each with an ID, raw text, and optional agent-proposed metadata), present routing recommendations to the user in clusters, get confirmation or correction, and execute the approved routings.
workflow_name: route-information
execution: main
steps:
- id: cluster-items
  name: Cluster items for review by type/theme
  step_type: reasoning
  depends_on: []
  invokes: []
- id: present-clusters
  name: Present each cluster to the user for confirmation
  step_type: reasoning
  depends_on:
  - cluster-items
  invokes: []
- id: record-decisions
  name: Record user decisions for each item
  step_type: reasoning
  depends_on:
  - present-clusters
  visibility:
    mode: full
  invokes: []
- id: execute-routing
  name: Execute approved routings (consent-gated writes)
  step_type: code
  depends_on:
  - record-decisions
  visibility:
    mode: summary
    include_keys:
    - executed
    - failed
    - skipped
  invokes:
  - task_create
- id: return-results
  name: Return complete routing record
  step_type: code
  depends_on:
  - execute-routing
  visibility:
    mode: full
  invokes: []
tags:
- routing
- route
- information
parents:
- routing
- routing
---

## What NOT to do

- Don't modify the raw text of any item â€” present it verbatim
- Don't auto-route without user confirmation, even at high confidence
- Don't create elaborate categorization hierarchies â€” use existing destinations
- Don't invent new filing locations the user hasn't established
- Don't pressure the user to decide on every item â€” "skip" is always valid
- Don't treat "unknown" as a problem â€” some items need the user's context, that's expected
- Don't batch-delete without showing each item â€” the user might spot something worth keeping


## Related blindspots

- **Insight hoarding**: if the user parks everything and deletes nothing, name the pattern. But don't force deletion â€” that triggers avoidance
- **Infrastructure displacement**: if routing itself is taking longer than the items are worth, flag it. "We've spent 20 minutes on 3 items â€” should we batch the rest?"
- **Organization skill gap**: if the user can't decide where things go, the destination system might need simplification, not more process

## cluster-items

Group items by `proposed_type` (or by theme if types are mixed). The goal is to minimize the number of review interactions while keeping each interaction coherent.

Suggested clustering:
- **Admin/personal** items together (usually quick decisions)
- **Research/technical** items together (need domain context)
- **Unknown/low-confidence** items together (need the most user input)
- **Proposed deletions** together (batch confirm)

## present-clusters

For each cluster, present:
1. The cluster theme/type
2. For each item in the cluster:
   - The **raw text** (verbatim, never modified)
   - The agent's **one-line interpretation**
   - The **proposed action** (route to X, delete, park with review date)
   - **Confidence level** and any staleness notes
3. Ask the user to confirm, correct, or skip each item

**Example interaction:**

```
## Admin items (3 items)

**[t_d44f12]** "Admin - VPN/taxes/new laptop stuff"
â†’ Proposed: task (inbox) â€” "Set up VPN, handle tax prep, research laptops"
â†’ Confidence: low â€” this bundles 3 things, and taxes may be done
What is this? Confirm / correct / skip?

**[t_c44d1a]** "Taxes - Varsha"
â†’ Proposed: task (inbox) â€” "Tax-related task involving Varsha"
â†’ Confidence: low â€” don't know who Varsha is or what the task is
What is this? Confirm / correct / skip?

**[t_7b2e09]** "Varsha - T4 / T4A"
â†’ Proposed: linked to t_c44d1a? â€” "Tax form from Varsha"
â†’ Confidence: low
What is this? Confirm / correct / skip?
```

The user might reply: "Varsha is the lab admin. I already got the T4A. Delete both of those. The VPN/taxes/laptop thing â€” VPN is done, taxes are done, laptop is still a live task."

## record-decisions

Update each item with the user's confirmed routing:

```json
{
  "id": "t_c44d1a",
  "action": "delete",
  "user_note": "Already completed â€” got T4A from Varsha"
}
```

```json
{
  "id": "t_d44f12",
  "action": "split",
  "splits": [
    {"text": "VPN setup", "action": "delete", "reason": "done"},
    {"text": "Taxes", "action": "delete", "reason": "done"},
    {"text": "New laptop research", "action": "route", "destination": "tasks/master-task-list.md", "task_text": "Research and purchase new laptop"}
  ]
}
```

## execute-routing

For each confirmed item, execute the routing:

- **Task creation**: Use `mcp__work-buddy__wb_run("task_create", {"task_text": "...", "project": "...", "due_date": "..."})` which produces clean lines and creates a SQLite metadata record:
  ```
  - [ ] #todo <task text> #projects/<project> ðŸ†” t-<hex> ðŸ“… <date if applicable>
  ```
  State and urgency are stored in `tasks/task_metadata.db`, not inline tags.
- **Consideration creation**: Create a new file in `work/considerations/<project>/` using the consideration template frontmatter
- **Project note append**: Append to the relevant project file
- **Park with review date**: Create a consideration with `status: parked` and a `decision_date` set to the review date
- **Delete**: Mark as deleted in the routing record (the caller handles actual text removal)

**All write operations are consent-gated.** The first write triggers `@requires_consent`. Grant with a reasonable TTL (e.g., 30 minutes) to cover the batch.

## return-results

Return the complete routing record as JSON â€” the caller uses this to know what was processed and what action was taken.

```json
{
  "routed": [{"id": "t_7b2e09", "action": "route", "destination": "tasks/master-task-list.md"}],
  "deleted": [{"id": "t_c44d1a", "reason": "Already completed"}],
  "split": [{"id": "t_d44f12", "splits": [...]}],
  "skipped": [{"id": "t_x9y8z7", "reason": "User wants to think about it"}]
}
```
