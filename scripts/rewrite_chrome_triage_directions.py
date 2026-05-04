"""One-shot rewriter: update the ``browser/chrome-triage-directions``
knowledge unit to describe the new unified-pipeline flow (post Chrome
modal retirement).
"""

from __future__ import annotations

import json
from pathlib import Path

PATH = Path("knowledge/store/browser.json")

NEW_SUMMARY = (
    "Start chrome-triage workflow. The unified source pipeline "
    "runs automatically (collect → annotate → cluster → Sonnet "
    "refine → spawn group threads). Your job is to confirm "
    "completion; the user reviews + approves on the dashboard's "
    "Threads tab via the column grid + per-column action chip."
)

NEW_FULL = """\
Start via ``mcp__work-buddy__wb_run("chrome-triage")``.

The system runs the unified Chrome-triage source pipeline
end-to-end:

1. **Collect** — currently-open tabs from the ledger + engagement
   scores; cached Haiku summaries (from earlier runs) are
   attached automatically.
2. **Annotate** — synthesises tag signals from each tab's domain
   + Chrome group title (no new LLM call).
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
   its tabs as ``context_items``. Each child's proposed action is
   recorded as a synthetic ``action_inferred`` event.

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

Be efficient — the pipeline produces a complete dashboard surface
on its own; don't explain the stages or post-process the result.
"""


def main():
    with open(PATH, encoding="utf-8") as f:
        data = json.load(f)
    unit = data["browser/chrome-triage-directions"]
    unit["description"] = (
        "How to run Chrome tab triage — pipeline overview + the "
        "agent's role (confirm completion; user reviews on the "
        "dashboard column grid)."
    )
    if "content" not in unit:
        unit["content"] = {}
    unit["content"]["summary"] = NEW_SUMMARY
    unit["content"]["full"] = NEW_FULL
    with open(PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"wrote {PATH}")


if __name__ == "__main__":
    main()
