"""One-shot rewriter: collapse the 11-step browser/chrome-triage
workflow into the same single-step ``run-source-pipeline`` shape
that ``daily-journal/process-backlog`` uses.

Where the old steps land in the new flow:

  - collect / extract-and-cluster   → ChromeTriagePipeline.collect
                                        + ChromeTriagePipeline.precluster
  - summarize (Haiku per-tab)        → ChromeTriagePipeline.annotate_items
                                        (carries cached summaries through)
  - intent-group / contextualize     → refine_clusters (Sonnet)
  - build-presentation               → no longer needed — the dashboard
                                        column grid IS the presentation
  - resolve-and-clarify              → no longer needed — the LLM cluster
                                        refinement step picks per-cluster
                                        actions; ambiguities surface as
                                        per-cluster proposals the user
                                        can override via the chip
                                        dropdown
  - dispatch-clarify                 → no longer needed (the dashboard
                                        column UI is the review surface)
  - build-recommendations            → folded into refine_clusters'
                                        per-cluster proposed_action output
  - dispatch-review                  → no longer needed (column grid +
                                        action chip + Approve all)
  - execute                          → per-group thread actions, dispatched
                                        when the user clicks Approve all
                                        (or accepts an individual child)

The legacy ``dispatch_clarify`` / ``dispatch_review`` /
``execute_triage_decisions`` callables stay in tree because they're
also used by the v4 review-pool capability; only the chrome workflow
graph stops referencing them.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

WORKFLOWS_PATH = Path("knowledge/store/workflows.json")


NEW_WORKFLOW = {
    "kind": "workflow",
    "name": "Chrome Triage",
    "description": (
        "Triage currently-open Chrome tabs through the unified source "
        "pipeline: collect tabs, attach cached Haiku summaries + tag "
        "signals, embedding-fused cluster (Louvain over "
        "embedding+tag+window-gated proximity), Sonnet-refine cluster "
        "boundaries + propose a per-cluster action (close all tabs / "
        "group in Chrome / route to tasks / etc.), and spawn a group "
        "umbrella thread + group sub-threads with the tabs as "
        "ContextItems. The user reviews and approves via the dashboard "
        "column grid + per-column action chip."
    ),
    "workflow_name": "chrome-triage",
    "execution": "main",
    "steps": [
        {
            "id": "run-pipeline",
            "name": "Run Chrome triage source pipeline",
            "step_type": "code",
            "depends_on": [],
            "execution": "main",
            "auto_run": {
                "callable": "work_buddy.pipelines.run_source_pipeline",
                "kwargs": {
                    "source": "chrome_triage",
                    "engagement_window": "24h",
                    "include_summaries": True,
                },
            },
            "visibility": {"mode": "summary"},
            "invokes": [],
        },
    ],
    "tags": [
        "browser",
        "chrome",
        "triage",
    ],
    "parents": [
        "browser",
    ],
    "allow_override": False,
    "step_instructions": {
        "run-pipeline": (
            "Auto-run. Call "
            "``work_buddy.pipelines.run_source_pipeline(source='chrome_triage', "
            "engagement_window='24h', include_summaries=True)``. The "
            "pipeline runs collect → annotate → precluster → refine → "
            "spawn end-to-end and returns the umbrella thread id + "
            "child group thread ids + per-cluster action proposals. "
            "The umbrella becomes visible on the dashboard's Threads "
            "tab; the user reviews + drag-drops tabs between groups + "
            "picks per-group actions via the column-header action "
            "chip; approving the umbrella runs each child's chosen "
            "action through the standard FSM dispatch path (calls "
            "``chrome_tab_close`` / ``chrome_tab_group`` / "
            "``chrome_route_to_tasks`` / etc.)."
        ),
    },
}


def main() -> None:
    if not WORKFLOWS_PATH.exists():
        print(f"workflows.json not found at {WORKFLOWS_PATH}", file=sys.stderr)
        sys.exit(2)

    with open(WORKFLOWS_PATH, encoding="utf-8") as f:
        workflows = json.load(f)

    key = "browser/chrome-triage"
    old = workflows.get(key)
    if old is None:
        print(f"workflow {key!r} not found in workflows.json", file=sys.stderr)
        sys.exit(3)

    print(f"BEFORE: {len(old.get('steps', []))} steps")
    workflows[key] = NEW_WORKFLOW
    print(f"AFTER:  {len(NEW_WORKFLOW['steps'])} step")

    with open(WORKFLOWS_PATH, "w", encoding="utf-8") as f:
        json.dump(workflows, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"wrote {WORKFLOWS_PATH}")


if __name__ == "__main__":
    main()
