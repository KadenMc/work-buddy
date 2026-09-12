"""Profile task browsing against disposable synthetic data only.

Run ``uv run python scripts/profile_task_workspace.py --rows 6000 --repeats 7``.
There is intentionally no database-path argument: this tool cannot profile or
modify the user's task authority. Timings exclude fixture creation.
"""

from __future__ import annotations

import argparse
from contextlib import ExitStack
import json
import logging
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def measure(call, repeats):
    call()  # warm disk/import caches for a repeatable comparison
    values = []
    for _ in range(repeats):
        started = time.perf_counter()
        call()
        values.append(1000 * (time.perf_counter() - started))
    ordered = sorted(values)
    return {"median_ms": round(statistics.median(values), 2),
            "min_ms": round(min(values), 2), "max_ms": round(max(values), 2),
            "p95_ms": round(ordered[min(len(ordered) - 1, int(len(ordered) * .95))], 2)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=6000)
    parser.add_argument("--repeats", type=int, default=7)
    args = parser.parse_args()
    if not 10 <= args.rows <= 100000 or not 1 <= args.repeats <= 100:
        parser.error("rows must be 10..100000 and repeats 1..100")
    with ExitStack() as cleanup:
        directory = cleanup.enter_context(tempfile.TemporaryDirectory(prefix="wb-task-query-profile-"))
        cleanup.callback(logging.shutdown)
        root = Path(directory)
        os.environ["WORK_BUDDY_DATA_DIR"] = str(root / "data")
        os.environ["WORK_BUDDY_CONFIG_DIR"] = str(root / "config")
        os.environ["WORK_BUDDY_SESSION_ID"] = "task-query-profile"
        from work_buddy.tasks.models import TaskQuery
        from work_buddy.tasks.service import TaskApplicationService
        from work_buddy.tasks.store import TaskStore
        from work_buddy.tasks.workspace_query import WorkspaceQuery, read_workspace

        store = TaskStore(root / "tasks.db")
        store.initialize()
        with store.transaction() as conn:
            conn.executemany(
                "INSERT INTO task_metadata(task_id,description,state,created_at,updated_at,"
                "outcome_text,agent_required_contexts,current_action_item_id) VALUES(?,?,?,?,?,?,?,?)",
                [(f"t-{index:06}", f"Review ECG result {index}",
                  ("active", "inbox", "waiting", "snoozed", "done")[index % 5],
                  f"2026-09-{1 + index % 28:02}T12:00:00Z", f"2026-09-{1 + index % 28:02}T13:00:00Z",
                  "Synthetic outcome context. " * 80, '["research"]', 2 * index + 1)
                 for index in range(args.rows)],
            )
            conn.executemany("INSERT INTO task_tags VALUES(?,?,?)", [
                (f"t-{index:06}", value, namespace)
                for index in range(args.rows)
                for value, namespace in ((f"research/ecg/study-{index % 40}", 1),
                                         (f"research/ecg/analysis-{index % 8}", 1), ("label", 0))
            ])
            conn.executemany("INSERT INTO task_projects VALUES(?,?)", [
                (f"t-{index:06}", index % 5 + 1) for index in range(args.rows)
            ])
            conn.executemany(
                "INSERT INTO task_action_items(task_id,sequence,description,created_at,updated_at) "
                "VALUES(?,?,?,?,?)", [
                    (f"t-{index:06}", action, "Review the synthetic action. " * 20,
                     "2026-09-01", "2026-09-02")
                    for index in range(args.rows) for action in range(2)
                ],
            )
        service = TaskApplicationService(store)
        old_query = TaskQuery(include_done=True, include_archived=True, include_deleted=True,
                              include_snoozed=True, limit=5000)

        def baseline_view():
            # Same read shape as the previous endpoint: hydrate 5000, read all
            # their document links, then Python-filter and build options/counts.
            tasks = service.list(old_query)
            conn = store.connect()
            try:
                links = {}
                for offset in range(0, len(tasks), 900):
                    ids = [task.task_id for task in tasks[offset:offset + 900]]
                    links.update({row["task_id"]: dict(row) for row in conn.execute(
                        f"SELECT * FROM task_document_links WHERE task_id IN ({','.join('?' for _ in ids)})", ids
                    )})
            finally:
                conn.close()
            visible = [task for task in tasks if task.state not in {"done", "snoozed"}
                       and task.deleted_at is None and task.archived_at is None]
            summaries = [{"task_id": task.task_id, "title": task.description,
                          "namespaces": list(task.namespace_tags),
                          "current_action": next((a.description for a in task.action_items
                                                  if a.id == task.current_action_item_id), None),
                          "has_document": task.task_id in links} for task in visible]
            return {"tasks": summaries,
                    "states": {state: sum(task.state == state for task in tasks)
                               for state in ("focused", "active", "inbox", "snoozed", "done")},
                    "namespaces": sorted({tag for task in tasks for tag in task.namespace_tags}),
                    "contracts": sorted({task.contract for task in tasks if task.contract}),
                    "contexts": sorted({value for task in tasks
                                        for value in (*task.agent_required_contexts, *task.user_required_contexts)})}

        def connection(readonly=False):
            conn = store.connect_readonly() if readonly else store.connect()
            conn.close()

        report = {
            "environment": "disposable synthetic SQLite database; warm in-process reads; no HTTP/browser",
            "rows": args.rows, "actions_per_task": 2, "tags_per_task": 3, "repeats": args.repeats,
            "connect_with_schema_validation": measure(connection, args.repeats),
            "connect_readonly": measure(lambda: connection(True), args.repeats),
            "service_list_5000_aggregates": measure(lambda: service.list(old_query), args.repeats),
            "previous_view_read_shape": measure(baseline_view, args.repeats),
            "workspace_default_page_50": measure(lambda: read_workspace(store), args.repeats),
            "workspace_filtered_page_50": measure(lambda: read_workspace(
                store, WorkspaceQuery(namespaces=("research/ecg/study-1",), projects=("2",))
            ), args.repeats),
            "workspace_updated_page_50": measure(lambda: read_workspace(
                store, WorkspaceQuery(sort="updated_at")
            ), args.repeats),
        }
        report["default_result"] = {key: read_workspace(store)[key] for key in ("total", "page")}
        report["default_speedup"] = round(report["previous_view_read_shape"]["median_ms"] /
                                           report["workspace_default_page_50"]["median_ms"], 2)
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
