"""One-shot script: drop ``v5`` / ``Stage N`` / ``v4`` from durable
knowledge-unit names, descriptions, and tags. Tier-1 user-facing
naming cleanup per the unified-pipeline-rebuild plan.

Doesn't touch ``content.full`` blocks or non-user-facing prose;
those are Tier 2/3 (deferred to a follow-up cleanup PR).
"""

from __future__ import annotations

import json
from pathlib import Path


def _strip_durable_tags(tags):
    if not tags:
        return tags
    drop = {"v5", "v4", "stage5"}
    return [t for t in tags if t not in drop]


def main():
    p = Path("knowledge/store/threads.json")
    with open(p, encoding="utf-8") as f:
        data = json.load(f)

    # threads (top-level)
    if "threads" in data:
        u = data["threads"]
        u["name"] = "Threads — universal-entity primitive"
        u["description"] = (
            "The Thread is the universal entity for 'context that may "
            "need an action'. Replaces the older split between "
            "PoolEntry (now folded into states) and ActionItem (now "
            "folded into sub-Threads). Task survives as a subclass."
        )
        u["tags"] = _strip_durable_tags(u.get("tags"))

    # threads/grouping
    if "threads/grouping" in data:
        u = data["threads/grouping"]
        u["name"] = "Threads — group-relationship pattern"
        u["description"] = (
            "Parent-child relationship pattern alongside 'decompose'. "
            "Group umbrellas hold N group sub-threads; each child "
            "carries its items as ContextItems and supports per-cluster "
            "actions (close-tabs, route-to-tasks, etc.) proposed by the "
            "shared LLM cluster-refinement step. Drag-and-drop moves "
            "items between sibling children."
        )
        u["tags"] = _strip_durable_tags(u.get("tags"))

    # threads/aggregator
    if "threads/aggregator" in data:
        u = data["threads/aggregator"]
        u["name"] = "Aggregator — read-only synthesis of legacy entities as Threads"
        u["description"] = (
            "Lets Thread query paths see legacy task_metadata / "
            "task_action_items / ClarifyPool data without a migration. "
            "Cutover by replacing this with real migrated rows."
        )
        u["tags"] = _strip_durable_tags(u.get("tags"))

    # Strip 'v5'/'stage5'/'v4' from every other unit's tag list too.
    for key, u in data.items():
        if not isinstance(u, dict):
            continue
        if "tags" in u:
            u["tags"] = _strip_durable_tags(u["tags"])

    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"wrote {p}")


if __name__ == "__main__":
    main()
