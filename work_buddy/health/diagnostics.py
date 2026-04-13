"""DiagnosticRunner — walks dependency chains and runs check sequences.

Given a component ID:
1. Walks the dependency chain depth-first (check parents before children)
2. For each component: runs check_sequence steps in order
3. Stops at first failure — returns root cause and fix suggestion

Check functions are imported dynamically via dotted paths from CheckStep.check_fn.
"""

from __future__ import annotations

import importlib
import logging
from dataclasses import asdict, dataclass, field
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class StepResult:
    """Result of a single diagnostic check step."""

    description: str
    ok: bool
    detail: str
    component_id: str


@dataclass
class DiagnosticResult:
    """Result of running diagnostics on a component (and its dependencies)."""

    component_id: str
    status: str  # "passed", "failed", "error"
    steps_run: list[StepResult] = field(default_factory=list)
    root_cause: str | None = None
    fix_suggestion: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _import_check_fn(dotted_path: str) -> Any:
    """Dynamically import a check function from a dotted path.

    E.g. "work_buddy.health.checks.check_postgresql" →
         work_buddy.health.checks.check_postgresql
    """
    module_path, _, fn_name = dotted_path.rpartition(".")
    if not module_path:
        raise ImportError(f"Invalid check function path: {dotted_path}")
    mod = importlib.import_module(module_path)
    fn = getattr(mod, fn_name, None)
    if fn is None:
        raise ImportError(f"Function '{fn_name}' not found in {module_path}")
    return fn


class DiagnosticRunner:
    """Runs diagnostic check sequences for components."""

    def diagnose(self, component_id: str) -> DiagnosticResult:
        """Run diagnostics on a component and its dependency chain.

        Walks dependencies depth-first: if component C depends on B
        which depends on A, checks run in order A → B → C. Stops at
        first failure.
        """
        from work_buddy.health.components import COMPONENT_CATALOG

        comp = COMPONENT_CATALOG.get(component_id)
        if comp is None:
            return DiagnosticResult(
                component_id=component_id,
                status="error",
                root_cause=f"Unknown component: '{component_id}'",
            )

        # Build dependency chain (depth-first)
        chain = self._resolve_chain(component_id)

        steps_run: list[StepResult] = []
        for chain_comp_id in chain:
            chain_comp = COMPONENT_CATALOG.get(chain_comp_id)
            if chain_comp is None:
                continue

            for check_step in chain_comp.check_sequence:
                try:
                    fn = _import_check_fn(check_step.check_fn)
                    result = fn()
                    ok = result.get("ok", False)
                    detail = result.get("detail", "")
                except Exception as exc:
                    ok = False
                    detail = f"Check function error: {exc}"

                step_result = StepResult(
                    description=check_step.description,
                    ok=ok,
                    detail=detail,
                    component_id=chain_comp_id,
                )
                steps_run.append(step_result)

                if not ok:
                    return DiagnosticResult(
                        component_id=component_id,
                        status="failed",
                        steps_run=steps_run,
                        root_cause=f"[{chain_comp.display_name}] {check_step.description}: {detail}",
                        fix_suggestion=check_step.on_fail,
                    )

        return DiagnosticResult(
            component_id=component_id,
            status="passed",
            steps_run=steps_run,
        )

    def diagnose_all(self) -> list[DiagnosticResult]:
        """Run diagnostics on all registered components."""
        from work_buddy.health.components import COMPONENT_CATALOG
        return [self.diagnose(comp_id) for comp_id in COMPONENT_CATALOG]

    def _resolve_chain(self, component_id: str) -> list[str]:
        """Resolve the full dependency chain for a component (depth-first).

        Returns a list of component IDs in dependency order (parents first).
        """
        from work_buddy.health.components import COMPONENT_CATALOG

        visited: set[str] = set()
        chain: list[str] = []

        def _visit(cid: str) -> None:
            if cid in visited:
                return
            visited.add(cid)
            comp = COMPONENT_CATALOG.get(cid)
            if comp is None:
                return
            for dep in comp.depends_on:
                _visit(dep)
            chain.append(cid)

        _visit(component_id)
        return chain
