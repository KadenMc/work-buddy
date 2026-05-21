"""Workflow DAG — task dependency graph for workflow execution.

Provides a programmatic structure that agents interact with instead of
trying to remember complex task sequences. The DAG enforces:

- Dependency ordering: a task cannot start until its dependencies complete
- Execution policy: per-task rules about subagent vs main agent execution
- State tracking: persistent status across agent interactions
- Blocking: hard stops when dependencies aren't met

Usage from Claude:
    1. Agent calls create_workflow() or load_workflow() to get a DAG
    2. Agent calls dag.next_available() to find tasks ready to execute
    3. Agent calls dag.start_task(id) — blocked if deps not met
    4. Agent (or subagent) executes the task
    5. Agent calls dag.complete_task(id) or dag.fail_task(id)
    6. Repeat until dag.is_complete()

State is persisted to agents/<session>/workflows/<workflow_name>.json
so it survives across tool calls within a session.
"""

import json
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

import networkx as nx

from work_buddy.agent_session import get_session_dir
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)


class TaskStatus(str, Enum):
    PENDING = "pending"
    BLOCKED = "blocked"     # has unmet dependencies
    AVAILABLE = "available"  # deps met, ready to start
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    RETRY_PENDING = "retry_pending"  # failed transiently, queued for background retry


class ExecutionPolicy(str, Enum):
    """Preferred execution context for a task.

    Combined with allow_override:
      - execution=main, allow_override=false → must run in main agent
      - execution=main, allow_override=true  → prefer main, subagent OK
      - execution=subagent, allow_override=false → must use a subagent
      - execution=subagent, allow_override=true  → prefer subagent, main OK
    """
    SUBAGENT = "subagent"
    MAIN = "main"


class WorkflowDAG:
    """A directed acyclic graph of workflow tasks with dependency tracking.

    Each node (task) has:
    - id: unique identifier
    - name: human-readable name
    - workflow_file: path to the workflow .md file to execute (optional)
    - execution_policy: how this task should be executed
    - allow_override: whether the execution policy can be overridden
    - status: current execution state
    - result: output from execution (optional)
    - started_at / completed_at: timestamps
    """

    _REPO_ROOT = Path(__file__).parent.parent

    def __init__(self, name: str, description: str = ""):
        self.name = name
        self.description = description
        self._graph = nx.DiGraph()
        self._created_at = datetime.now(timezone.utc).isoformat()

        # Run-level lifecycle state. ``agent_session_id`` pins the run to the
        # agent session that started it (assigned by the conductor); it is
        # persisted so a run recovered after an MCP-server restart can still
        # resolve consent against the right session.  The cancellation fields
        # record an explicit cancel — manual or by the idle-timeout sweep —
        # which is a run-level property, distinct from per-task status.
        self.agent_session_id: str | None = None
        self.cancelled: bool = False
        self.cancelled_reason: str | None = None
        self.cancelled_at: str | None = None

        # When this DAG was loaded from disk, the file it came from. A
        # subsequent _save() writes back there rather than recomputing a
        # path under the *current* process's session directory — see
        # _get_save_path.
        self._loaded_from: Path | None = None

    @staticmethod
    def _read_workflow_policy(workflow_file: str) -> tuple[str, bool]:
        """Read execution policy from a workflow file's YAML frontmatter.

        Returns (default_execution, allow_override).
        """
        from work_buddy.frontmatter import parse_frontmatter

        path = WorkflowDAG._REPO_ROOT / workflow_file
        if not path.exists():
            return "main", True

        fm, _ = parse_frontmatter(path)
        default_exec = fm.get("execution", "main")
        allow_override = fm.get("allow_override", True)
        return default_exec, allow_override

    def add_task(
        self,
        task_id: str,
        name: str,
        workflow_file: str | None = None,
        execution: str | None = None,
        depends_on: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Add a task to the DAG.

        Args:
            task_id: Unique identifier for this task.
            name: Human-readable name.
            workflow_file: Path to the workflow .md file (relative to repo root).
                If provided, the file's frontmatter defines the default execution
                policy and whether overrides are allowed.
            execution: Caller's requested execution mode ("main" or "subagent").
                If None, uses the workflow file's default. If provided, must be
                compatible with the workflow's allow_override setting.
            depends_on: List of task_ids that must complete before this task can start.
            metadata: Arbitrary metadata dict stored with the task.
        """
        # Resolve execution policy from workflow file + caller request
        wf_default = "main"
        wf_allow_override = True

        if workflow_file:
            wf_default, wf_allow_override = self._read_workflow_policy(workflow_file)

        if execution is not None:
            resolved_policy = ExecutionPolicy(execution)
            if resolved_policy.value != wf_default and not wf_allow_override:
                raise ValueError(
                    f"Workflow '{workflow_file}' requires execution={wf_default} "
                    f"and does not allow override. Caller requested '{execution}'."
                )
        else:
            resolved_policy = ExecutionPolicy(wf_default)

        self._graph.add_node(task_id, **{
            "name": name,
            "workflow_file": workflow_file,
            "execution": resolved_policy.value,
            "execution_actual": None,
            "workflow_default": wf_default,
            "allow_override": wf_allow_override,
            "status": TaskStatus.PENDING.value,
            "result": None,
            "started_at": None,
            "completed_at": None,
            "metadata": metadata or {},
        })

        if depends_on:
            for dep_id in depends_on:
                if dep_id not in self._graph:
                    raise ValueError(
                        f"Dependency '{dep_id}' not found. Add it before "
                        f"adding '{task_id}' that depends on it."
                    )
                self._graph.add_edge(dep_id, task_id)

        # Validate no cycles
        if not nx.is_directed_acyclic_graph(self._graph):
            self._graph.remove_node(task_id)
            raise ValueError(
                f"Adding task '{task_id}' would create a cycle in the DAG."
            )

        self._update_availability()
        logger.info(f"Task added: {task_id} ({name})")

    def _update_availability(self) -> None:
        """Recalculate which tasks are available vs blocked."""
        for node_id in self._graph.nodes:
            data = self._graph.nodes[node_id]
            if data["status"] not in (TaskStatus.PENDING.value, TaskStatus.BLOCKED.value):
                continue

            deps = list(self._graph.predecessors(node_id))
            all_deps_met = all(
                self._graph.nodes[d]["status"] in (
                    TaskStatus.COMPLETED.value,
                    TaskStatus.SKIPPED.value,
                )
                for d in deps
            )

            if all_deps_met:
                data["status"] = TaskStatus.AVAILABLE.value
            else:
                data["status"] = TaskStatus.BLOCKED.value

    def start_task(self, task_id: str, execution_actual: str | None = None) -> dict[str, Any]:
        """Mark a task as running. Raises if dependencies aren't met.

        Args:
            task_id: The task to start.
            execution_actual: How the task is actually being executed ("main" or
                "subagent"). If None, assumed to match the planned execution.
                If provided and differs from plan, must be allowed by allow_override.

        Returns the task data dict.
        """
        if task_id not in self._graph:
            raise KeyError(f"Task '{task_id}' not found in DAG.")

        data = self._graph.nodes[task_id]

        if data["status"] == TaskStatus.BLOCKED.value:
            unmet = [
                d for d in self._graph.predecessors(task_id)
                if self._graph.nodes[d]["status"] != TaskStatus.COMPLETED.value
            ]
            raise RuntimeError(
                f"Cannot start '{task_id}': blocked by unmet dependencies: "
                f"{', '.join(unmet)}"
            )

        if data["status"] not in (TaskStatus.AVAILABLE.value, TaskStatus.PENDING.value):
            raise RuntimeError(
                f"Cannot start '{task_id}': current status is {data['status']}"
            )

        # Resolve actual execution mode
        if execution_actual is not None:
            actual = ExecutionPolicy(execution_actual)
            if actual.value != data["execution"] and not data["allow_override"]:
                raise RuntimeError(
                    f"Cannot start '{task_id}' as {actual.value}: "
                    f"workflow requires {data['execution']} and does not allow override."
                )
            data["execution_actual"] = actual.value
        else:
            data["execution_actual"] = data["execution"]

        data["status"] = TaskStatus.RUNNING.value
        data["started_at"] = datetime.now(timezone.utc).isoformat()
        logger.info(
            f"Task started: {task_id} "
            f"(planned={data['execution']}, actual={data['execution_actual']})"
        )
        self._save()
        return dict(data)

    def complete_task(self, task_id: str, result: Any = None) -> None:
        """Mark a task as completed and unblock dependents."""
        if task_id not in self._graph:
            raise KeyError(f"Task '{task_id}' not found in DAG.")

        data = self._graph.nodes[task_id]
        if data["status"] != TaskStatus.RUNNING.value:
            raise RuntimeError(
                f"Cannot complete '{task_id}': current status is {data['status']} "
                f"(must be running)"
            )

        data["status"] = TaskStatus.COMPLETED.value
        data["completed_at"] = datetime.now(timezone.utc).isoformat()
        data["result"] = result
        self._update_availability()
        logger.info(f"Task completed: {task_id}")
        self._save()

    def mark_cancelled(self, reason: str) -> None:
        """Mark the whole run as cancelled and persist.

        Cancellation is a run-level property, not a per-task status: a
        cancelled run may still have pending or running tasks. The conductor
        removes the run from its active-runs map after calling this; the
        persisted flag keeps restart-recovery from re-importing the run.
        """
        self.cancelled = True
        self.cancelled_reason = reason
        self.cancelled_at = datetime.now(timezone.utc).isoformat()
        logger.info(f"Workflow cancelled: {self.name} — {reason}")
        self._save()

    def fail_task(self, task_id: str, error: str = "") -> None:
        """Mark a task as failed."""
        if task_id not in self._graph:
            raise KeyError(f"Task '{task_id}' not found in DAG.")

        data = self._graph.nodes[task_id]
        data["status"] = TaskStatus.FAILED.value
        data["completed_at"] = datetime.now(timezone.utc).isoformat()
        data["result"] = f"FAILED: {error}"
        logger.error(f"Task failed: {task_id} — {error}")
        self._save()

    def skip_task(self, task_id: str, reason: str = "") -> None:
        """Mark a task as skipped (e.g., user decided to skip optional step)."""
        if task_id not in self._graph:
            raise KeyError(f"Task '{task_id}' not found in DAG.")

        data = self._graph.nodes[task_id]
        data["status"] = TaskStatus.SKIPPED.value
        data["completed_at"] = datetime.now(timezone.utc).isoformat()
        data["result"] = f"SKIPPED: {reason}"
        # Treat skipped like completed for dependency purposes
        self._update_availability()
        logger.info(f"Task skipped: {task_id} — {reason}")
        self._save()

    def next_available(self) -> list[dict[str, Any]]:
        """Return all tasks that are available to start (deps met, not yet running)."""
        available = []
        for node_id in self._graph.nodes:
            data = self._graph.nodes[node_id]
            if data["status"] == TaskStatus.AVAILABLE.value:
                available.append({"task_id": node_id, **data})
        return available

    def get_task(self, task_id: str) -> dict[str, Any]:
        """Get a task's current state."""
        if task_id not in self._graph:
            raise KeyError(f"Task '{task_id}' not found in DAG.")
        return {"task_id": task_id, **dict(self._graph.nodes[task_id])}

    def get_all_results(self) -> dict[str, Any]:
        """Return ``{step_id: result}`` for all completed/skipped steps."""
        results: dict[str, Any] = {}
        for node_id in self._graph.nodes:
            data = self._graph.nodes[node_id]
            if data["status"] in (
                TaskStatus.COMPLETED.value,
                TaskStatus.SKIPPED.value,
            ) and data.get("result") is not None:
                results[node_id] = data["result"]
        return results

    def is_complete(self) -> bool:
        """Check if all tasks are completed, skipped, or failed."""
        return all(
            self._graph.nodes[n]["status"] in (
                TaskStatus.COMPLETED.value,
                TaskStatus.SKIPPED.value,
                TaskStatus.FAILED.value,
            )
            for n in self._graph.nodes
        )

    def summary(self) -> str:
        """Return a markdown summary of the DAG state."""
        lines = [f"## Workflow: {self.name}", ""]
        if self.description:
            lines.append(self.description)
            lines.append("")

        # Topological order for readable output
        try:
            order = list(nx.topological_sort(self._graph))
        except nx.NetworkXUnfeasible:
            order = list(self._graph.nodes)

        status_icons = {
            TaskStatus.PENDING.value: "  ",
            TaskStatus.BLOCKED.value: "!!",
            TaskStatus.AVAILABLE.value: ">>",
            TaskStatus.RUNNING.value: "**",
            TaskStatus.COMPLETED.value: "OK",
            TaskStatus.FAILED.value: "XX",
            TaskStatus.SKIPPED.value: "--",
            TaskStatus.RETRY_PENDING.value: "RQ",
        }

        for node_id in order:
            data = self._graph.nodes[node_id]
            icon = status_icons.get(data["status"], "??")
            deps = list(self._graph.predecessors(node_id))
            dep_str = f" (after: {', '.join(deps)})" if deps else ""
            actual = data.get("execution_actual")
            if actual and actual != data["execution"]:
                exec_str = f" [{data['execution']}→{actual}]"
            elif actual:
                exec_str = f" [{actual}]"
            else:
                exec_str = f" [{data['execution']}?]"
            lines.append(f"[{icon}] {data['name']}{dep_str}{exec_str}")

        completed = sum(
            1 for n in self._graph.nodes
            if self._graph.nodes[n]["status"] == TaskStatus.COMPLETED.value
        )
        total = len(self._graph.nodes)
        lines.append("")
        lines.append(f"Progress: {completed}/{total} tasks completed")

        return "\n".join(lines)

    # --- Persistence ---

    @staticmethod
    def _safe_name(name: str) -> str:
        """Turn a DAG name into a filesystem-safe stem.

        A run's name is ``"<workflow>:<run_id>"``. The ``:`` is **not**
        merely cosmetic to strip — on Windows/NTFS a ``:`` in a path opens
        an alternate data stream, so ``write_text`` on ``wf:run.json`` would
        silently divert the content into a stream of a 0-byte base file and
        ``glob("*.json")`` would never see it. Replacing it is what makes
        on-disk workflow state real on Windows.
        """
        return (
            name.replace(" ", "_")
            .replace("/", "_")
            .replace(":", "_")
            .lower()
        )

    def _get_save_path(self) -> Path:
        """Get the save path for this workflow's state.

        A DAG loaded from disk re-saves to the file it came from
        (``_loaded_from``). Otherwise — for a run recovered after a restart,
        or a disk-only run being cancelled — ``_save()`` would write a
        duplicate under the *current* process's session directory instead
        of updating the original file.
        """
        if self._loaded_from is not None:
            self._loaded_from.parent.mkdir(parents=True, exist_ok=True)
            return self._loaded_from
        wf_dir = get_session_dir() / "workflows"
        wf_dir.mkdir(exist_ok=True)
        return wf_dir / f"{self._safe_name(self.name)}.json"

    def _save(self) -> None:
        """Persist the DAG state to disk.

        The write is atomic (write to a temp file, then ``replace``) so a
        crash mid-write can never leave a half-written DAG file — important
        now that restart-recovery reads every DAG file on boot.
        """
        data = {
            "name": self.name,
            "description": self.description,
            "created_at": self._created_at,
            "saved_at": datetime.now(timezone.utc).isoformat(),
            # Agent session that owns this run — needed to resolve consent
            # for a run recovered after an MCP-server restart.
            "agent_session_id": getattr(self, "agent_session_id", None),
            # Run-level cancellation record (manual or idle-timeout sweep).
            "cancelled": getattr(self, "cancelled", False),
            "cancelled_reason": getattr(self, "cancelled_reason", None),
            "cancelled_at": getattr(self, "cancelled_at", None),
            "nodes": {},
            "edges": [],
            # Caller-provided initial params (set by start_workflow when the
            # workflow declares a params_schema). Persisted so a workflow
            # that resumes after MCP restart still has them.
            "initial_params": getattr(self, "initial_params", None),
        }
        for node_id in self._graph.nodes:
            data["nodes"][node_id] = dict(self._graph.nodes[node_id])
        for u, v in self._graph.edges:
            data["edges"].append([u, v])

        path = self._get_save_path()
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        tmp.replace(path)

    def save(self) -> Path:
        """Explicitly save and return the save path."""
        self._save()
        return self._get_save_path()

    @classmethod
    def load(cls, path: Path) -> "WorkflowDAG":
        """Load a DAG from a saved JSON file."""
        raw = json.loads(path.read_text(encoding="utf-8"))
        dag = cls(name=raw["name"], description=raw.get("description", ""))
        dag._created_at = raw.get("created_at", "")
        dag._loaded_from = Path(path)
        # Restore initial_params if persisted (None for older save files).
        dag.initial_params = raw.get("initial_params")  # type: ignore[attr-defined]
        # Restore run-level lifecycle state (safe defaults for older files).
        dag.agent_session_id = raw.get("agent_session_id")
        dag.cancelled = raw.get("cancelled", False)
        dag.cancelled_reason = raw.get("cancelled_reason")
        dag.cancelled_at = raw.get("cancelled_at")

        # Rebuild graph
        for node_id, node_data in raw["nodes"].items():
            dag._graph.add_node(node_id, **node_data)
        for u, v in raw["edges"]:
            dag._graph.add_edge(u, v)

        return dag

    @classmethod
    def load_by_name(cls, name: str) -> "WorkflowDAG":
        """Load a DAG by workflow name from the current session."""
        wf_dir = get_session_dir() / "workflows"
        path = wf_dir / f"{cls._safe_name(name)}.json"
        if not path.exists():
            raise FileNotFoundError(f"No saved workflow '{name}' in current session.")
        return cls.load(path)


def list_active_workflows() -> list[dict[str, Any]]:
    """List all workflow DAGs in the current session with their status."""
    wf_dir = get_session_dir() / "workflows"
    if not wf_dir.exists():
        return []

    workflows = []
    for path in sorted(wf_dir.glob("*.json")):
        try:
            dag = WorkflowDAG.load(path)
            completed = sum(
                1 for n in dag._graph.nodes
                if dag._graph.nodes[n]["status"] == TaskStatus.COMPLETED.value
            )
            total = len(dag._graph.nodes)
            workflows.append({
                "name": dag.name,
                "path": path.as_posix(),
                "complete": dag.is_complete(),
                "progress": f"{completed}/{total}",
            })
        except (json.JSONDecodeError, KeyError):
            continue

    return workflows
