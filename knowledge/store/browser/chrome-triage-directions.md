---
name: Chrome Triage Directions
kind: directions
description: How to run Chrome tab triage — pipeline overview + the agent's role (confirm completion; user reviews on the dashboard column grid).
summary: Start chrome-triage workflow. The unified source pipeline runs automatically (collect → annotate → cluster → Sonnet refine → spawn group threads). Your job is to confirm completion; the user reviews + approves on the dashboard's Threads tab via the column grid + per-column action chip.
trigger: user wants to triage, organize, or close Chrome tabs
command: wb-chrome-triage
workflow: browser/chrome-triage
capabilities:
- context/chrome_activity
- context/chrome_content
- context/chrome_infer
- context/triage_item_detail
tags:
- browser
- chrome
- tabs
- triage
- directions
aliases:
- triage tabs
- chrome triage
- tab cleanup
- browser triage
- close tabs
parents:
- browser
- browser
---

Start via ``mcp__work-buddy__wb_run("chrome-triage")``.

The system runs the unified Chrome-triage source pipeline
end-to-end:

1. **Collect** — currently-open tabs from the ledger + engagement
   scores; cached Haiku summaries (from earlier runs) are
   attached automatically.
2. **Annotate** — synthesises tag signals from each tab's domain
   + Chrome group title, then Haiku-summarises any tab that has
   no cached summary (page content extracted via the Chrome
   extension; capped at 30 tabs per run, content cap 3000 chars).
   Routed through ``LLMRunner`` at
   ``ModelTier.FRONTIER_FAST`` (claude-haiku-4-5, hosted Anthropic,
   no escalation). Tabs without extractable content fall through
   with tags-only annotation.
3. **Precluster** — embedding-fused Louvain clustering over
   embedding + tag + window-gated proximity (weights 0.80 / 0.10
   / 0.10).
4. **Refine** — Sonnet reviews the algorithmic clusters, picks
   final cluster boundaries + labels, and proposes a per-cluster
   action from the Chrome action library (close all tabs, group
   in Chrome, move to focus window, create one task per tab,
   create umbrella task) or returns null when no listed action
   fits.
5. **Spawn** — one umbrella thread (``parent_relationship='group'``)
   + one group sub-thread per final cluster; each child carries
   its tabs as ``context_items``. Each child lands in
   ``AWAITING_CONFIRMATION`` with the proposed action recorded
   as a synthetic ``action_inferred`` event (carrying the LLM's
   ``model_used`` + ``tier_used`` for audit). Children whose
   cluster has no proposed action land in
   ``AWAITING_ACTION_CLARIFICATION``.

Your job: confirm completion. The user reviews the resulting
column grid on the Threads tab — drag-drops items between groups
(``move_item``), picks per-group actions via the column-header
action chip dropdown, and clicks Approve all to dispatch every
non-terminal child's chosen action through the standard FSM.

Capabilities the per-group action chip can dispatch:

- ``chrome_tab_close`` — close every tab in the group
- ``chrome_tab_group`` — create a Chrome tab group named after
  the cluster
- ``chrome_tab_move`` — move tabs to a focus window
- ``chrome_route_to_tasks`` — create one task per tab
- ``chrome_route_to_umbrella_task`` — create one umbrella task
  for the whole group
- Universal: ``thread_dismiss`` / ``thread_defer`` / ``thread_rename``
  (labelled "Dismiss thread" / "Defer thread" / "Rename thread"
  in the chip dropdown to disambiguate from popup-dismissal).

Chrome write-side mutations (``chrome_tab_close`` / ``chrome_tab_group``
/ ``chrome_tab_move``) auto-bind ``tab_ids`` from the child's
``context_items`` at dispatch time; the ``thread_*`` capabilities
auto-bind ``thread_id``. The user does not need to fill in
parameters for any of these.

Be efficient — the pipeline produces a complete dashboard surface
on its own; don't explain the stages or post-process the result.
