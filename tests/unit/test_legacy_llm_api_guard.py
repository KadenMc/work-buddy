"""CI guard: block NEW imports of the deprecated LLM entry points.

Phases 1-7 of the LLM + Context refactor migrated every internal
Python caller from ``run_task`` / ``llm_with_tools`` / ``llm/call.py``
onto :class:`work_buddy.llm.LLMRunner`. The legacy modules remain for
a bounded period so the MCP-exposed ``llm_call`` / ``llm_with_tools``
capabilities keep working for external agents.

This test enforces that no **new** Python caller inside
``work_buddy/`` starts using the legacy APIs during that transition.
If you see this test fail, migrate your caller to
:class:`LLMRunner` instead of adding it to the exceptions list.

Exceptions (expected callers that haven't been removed yet):
    - work_buddy/llm/runner.py itself
    - work_buddy/llm/call.py (the compat shim)
    - work_buddy/llm/with_tools.py itself
    - work_buddy/llm/runner_v2.py (internal delegation — removed when
      LLMRunner grows its own Anthropic + local backends)
    - work_buddy/llm/__init__.py (re-exports for back-compat)
    - work_buddy/mcp_server/registry.py (the MCP-exposed capabilities
      registration)
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_PACKAGE_ROOT = _REPO_ROOT / "work_buddy"


# Patterns that indicate a caller of the legacy entry points. We look
# for ``import`` statements rather than arbitrary mentions — comments
# and string literals don't trip the guard.
_LEGACY_IMPORT_PATTERNS = [
    # from work_buddy.llm.with_tools import llm_with_tools
    re.compile(r"^\s*from\s+work_buddy\.llm\.with_tools\s+import", re.MULTILINE),
    # from work_buddy.llm.runner import run_task   (with_tools re-exports aren't caught by this)
    re.compile(r"^\s*from\s+work_buddy\.llm\.runner\s+import\s+.*\brun_task\b", re.MULTILINE),
    # from work_buddy.llm.call import llm_call
    re.compile(r"^\s*from\s+work_buddy\.llm\.call\s+import\s+llm_call\b", re.MULTILINE),
]


# Files allowed to import the legacy APIs during the transition. Keep
# this list tight — every entry is technical debt.
_ALLOWED_EXCEPTIONS = {
    # The legacy modules themselves.
    "work_buddy/llm/runner.py",
    "work_buddy/llm/with_tools.py",
    "work_buddy/llm/call.py",
    # The unified runner internally delegates to run_task. Removed
    # when LLMRunner grows native Anthropic + local backend adapters.
    "work_buddy/llm/runner_v2.py",
    # Package-level re-exports for back-compat with external callers.
    "work_buddy/llm/__init__.py",
    # MCP registry still exposes legacy capabilities for external agents.
    "work_buddy/mcp_server/registry.py",
    # The llm_call / llm_with_tools MCP capabilities are ops: their data
    # declarations live in the knowledge store, their callables in this op
    # module, which legitimately imports the legacy llm entry points.
    "work_buddy/mcp_server/ops/llm_ops.py",
}


def _normalize(path: Path) -> str:
    return path.relative_to(_REPO_ROOT).as_posix()


def _walk_python_files() -> list[Path]:
    return sorted(p for p in _PACKAGE_ROOT.rglob("*.py") if "__pycache__" not in p.parts)


def test_no_new_callers_of_legacy_llm_apis():
    """Audit every .py under ``work_buddy/`` for legacy imports."""
    violations: list[tuple[str, str]] = []
    for path in _walk_python_files():
        rel = _normalize(path)
        if rel in _ALLOWED_EXCEPTIONS:
            continue
        try:
            src = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for pattern in _LEGACY_IMPORT_PATTERNS:
            m = pattern.search(src)
            if m:
                violations.append((rel, m.group(0).strip()))
                break

    if violations:
        lines = ["New callers of deprecated LLM entry points detected:"]
        for path, snippet in violations:
            lines.append(f"  {path}: {snippet}")
        lines.append("")
        lines.append(
            "Migrate to LLMRunner.call() from work_buddy.llm instead of "
            "adding to the exceptions list. If the legacy path is "
            "genuinely needed (e.g. a new MCP capability), update "
            "_ALLOWED_EXCEPTIONS with a rationale in the commit."
        )
        pytest.fail("\n".join(lines))
