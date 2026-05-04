"""One-shot rewriter: collapse the 7-step daily-journal/process-backlog
workflow into the new 1-step ``run-source-pipeline`` shape.

The old workflow's stages (extract / segment / manifest / cluster /
review / route / rewrite) are now all handled by
``run_source_pipeline(source="journal_backlog", ...)``:

  - extract / segment       → ``JournalBacklogPipeline.collect``
  - manifest                → ``JournalBacklogPipeline.annotate_items``
  - cluster                 → ``JournalBacklogPipeline.precluster``
  - review (the LLM half)   → ``refine_clusters``
  - review (the human half) → dashboard column-grid review +
                              action-chip dropdown
  - route                   → per-group action proposals (run on
                              umbrella's "Approve all" or per-child
                              individual approval)
  - rewrite                 → umbrella-level
                              ``journal_rewrite_running_notes`` action

Run with:
  /c/Users/Owner/miniforge3/envs/work-buddy/python.exe scripts/rewrite_process_backlog_workflow.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

WORKFLOWS_PATH = Path("knowledge/store/workflows.json")


NEW_WORKFLOW = {
    "kind": "workflow",
    "name": "Process Backlog",
    "description": (
        "Process today's Running Notes backlog through the unified "
        "source pipeline: collect line-range segments, annotate with "
        "Haiku-generated tags + summaries, embedding-fused cluster, "
        "Sonnet-refine cluster boundaries + propose a per-group "
        "action, and spawn a group umbrella thread + group "
        "sub-threads with the segments as ContextItems. The user "
        "reviews and refines via the dashboard column grid."
    ),
    "workflow_name": "process-backlog",
    "execution": "main",
    "steps": [
        {
            "id": "run-pipeline",
            "name": "Run journal backlog source pipeline",
            "step_type": "code",
            "depends_on": [],
            "execution": "main",
            "auto_run": {
                "callable": "work_buddy.pipelines.run_source_pipeline",
                "kwargs": {"source": "journal_backlog"},
            },
            "visibility": {"mode": "summary"},
            "invokes": [],
        },
    ],
    "tags": [
        "daily-journal",
        "process",
        "backlog",
    ],
    "parents": [
        "daily-journal"
    ],
    "allow_override": False,
    "step_instructions": {
        "run-pipeline": (
            "Auto-run. Call "
            "``work_buddy.pipelines.run_source_pipeline(source='journal_backlog', "
            "journal_date=...)``. The pipeline runs collect → annotate → "
            "precluster → refine → spawn end-to-end and returns the "
            "umbrella thread id + child group thread ids + per-cluster "
            "action proposals. The umbrella becomes visible on the "
            "dashboard's Threads tab; the user reviews + drag-drops "
            "items between groups + picks per-group actions via the "
            "column header action chip; approving the umbrella runs "
            "each child's chosen action through the standard FSM "
            "dispatch path."
        ),
    },
}


def main() -> None:
    if not WORKFLOWS_PATH.exists():
        print(f"workflows.json not found at {WORKFLOWS_PATH}", file=sys.stderr)
        sys.exit(2)

    with open(WORKFLOWS_PATH, encoding="utf-8") as f:
        workflows = json.load(f)

    key = "daily-journal/process-backlog"
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
