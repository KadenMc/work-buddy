"""Compile constrained JSON query plans into Datacore query strings.

This is the key NL-to-structured-query enabler. Instead of asking agents to
emit raw Datacore syntax, they produce a structured plan that this module
compiles deterministically. This reduces brittleness and keeps agents
focused on intent decomposition rather than syntax.

Plan schema
-----------
A query plan is a dict with these keys (all optional except ``target``):

    target: str
        Object type. One of: "page", "section", "block", "codeblock",
        "list-item", "task". Compiled to @page, @section, etc.

    path: str | list[str]
        Path prefix filter(s). "journal" → path("journal").
        Multiple values are OR-combined.

    tags: str | list[str]
        Tag filter(s). "#project/foo" or "project/foo".
        Multiple values are AND-combined (all tags must match).

    tags_any: str | list[str]
        Like tags but OR-combined (any tag matches).

    exists: str | list[str]
        Field existence filter(s). AND-combined.

    frontmatter: dict[str, str | int | float | bool]
        Frontmatter equality filters. AND-combined.

    text_contains: str
        For tasks/list-items: filter by $text containing string.

    status: str
        For tasks: filter by $status (e.g. " " for open, "x" for done).

    parent: dict
        Nested plan for parentof() — compile the inner plan and wrap.

    child_of: dict
        Nested plan for childof() — compile the inner plan and wrap.

    expressions: str | list[str]
        Raw Datacore expression fragments to AND-combine.
        Escape hatch for queries the plan schema doesn't cover.

    negate: bool
        If true, wraps the entire compiled clause in !(...).

Example
-------
    plan = {
        "target": "task",
        "tags": "#projects/uhn-deploy",
        "status": " ",
    }
    compile_plan(plan)
    # → '@task and #projects/uhn-deploy and $status = " "'

    plan = {
        "target": "section",
        "child_of": {
            "target": "page",
            "path": "journal",
        },
    }
    compile_plan(plan)
    # → '@section and childof(@page and path("journal"))'
"""

from __future__ import annotations

from typing import Any

VALID_TARGETS = {
    "page",
    "section",
    "block",
    "codeblock",
    "list-item",
    "task",
}


class CompileError(ValueError):
    """Raised when a query plan is invalid."""


def compile_plan(plan: dict[str, Any]) -> str:
    """Compile a query plan dict into a Datacore query string.

    Args:
        plan: Structured query plan (see module docstring for schema).

    Returns:
        Compiled Datacore query string.

    Raises:
        CompileError: If the plan is invalid (missing target, unknown target, etc.).
    """
    if not isinstance(plan, dict):
        raise CompileError(f"Plan must be a dict, got {type(plan).__name__}")

    target = plan.get("target")
    if not target:
        raise CompileError("Plan must have a 'target' key")
    if target not in VALID_TARGETS:
        raise CompileError(
            f"Invalid target '{target}'. Must be one of: {', '.join(sorted(VALID_TARGETS))}"
        )

    clauses: list[str] = [f"@{target}"]

    # Path filter(s) — OR-combined
    if "path" in plan:
        paths = _as_list(plan["path"])
        if len(paths) == 1:
            clauses.append(f'path("{_esc(paths[0])}")')
        else:
            or_parts = [f'path("{_esc(p)}")' for p in paths]
            clauses.append(f'({" or ".join(or_parts)})')

    # Tags — AND-combined
    if "tags" in plan:
        for tag in _as_list(plan["tags"]):
            clauses.append(_normalize_tag(tag))

    # Tags any — OR-combined
    if "tags_any" in plan:
        tags = [_normalize_tag(t) for t in _as_list(plan["tags_any"])]
        if len(tags) == 1:
            clauses.append(tags[0])
        else:
            clauses.append(f'({" or ".join(tags)})')

    # Field existence
    if "exists" in plan:
        for field in _as_list(plan["exists"]):
            clauses.append(f"exists({field})")

    # Frontmatter equality
    if "frontmatter" in plan:
        fm = plan["frontmatter"]
        if not isinstance(fm, dict):
            raise CompileError("'frontmatter' must be a dict")
        for key, value in fm.items():
            clauses.append(f"$frontmatter.{key} = {_literal(value)}")

    # Text contains (for tasks/list-items)
    if "text_contains" in plan:
        text = plan["text_contains"]
        clauses.append(f'contains($text, "{_esc(text)}")')

    # Status filter (for tasks)
    if "status" in plan:
        clauses.append(f'$status = "{_esc(plan["status"])}"')

    # Parent relationship
    if "parent" in plan:
        inner = compile_plan(plan["parent"])
        clauses.append(f"parentof({inner})")

    # Child-of relationship
    if "child_of" in plan:
        inner = compile_plan(plan["child_of"])
        clauses.append(f"childof({inner})")

    # Raw expression escape hatch
    if "expressions" in plan:
        for expr in _as_list(plan["expressions"]):
            clauses.append(expr)

    # Combine
    result = " and ".join(clauses)

    # Negation
    if plan.get("negate"):
        result = f"!({result})"

    return result


def validate_plan(plan: dict[str, Any]) -> list[str]:
    """Validate a plan without compiling it. Returns list of warnings/errors."""
    errors: list[str] = []

    if not isinstance(plan, dict):
        return [f"Plan must be a dict, got {type(plan).__name__}"]

    target = plan.get("target")
    if not target:
        errors.append("Missing required 'target' key")
    elif target not in VALID_TARGETS:
        errors.append(f"Invalid target '{target}'")

    known_keys = {
        "target",
        "path",
        "tags",
        "tags_any",
        "exists",
        "frontmatter",
        "text_contains",
        "status",
        "parent",
        "child_of",
        "expressions",
        "negate",
    }
    unknown = set(plan.keys()) - known_keys
    if unknown:
        errors.append(f"Unknown keys: {', '.join(sorted(unknown))}")

    # Validate nested plans
    for nested_key in ("parent", "child_of"):
        if nested_key in plan:
            nested_errors = validate_plan(plan[nested_key])
            errors.extend(f"{nested_key}.{e}" for e in nested_errors)

    return errors


# ── Helpers ─────────────────────────────────────────────────────


def _as_list(value: Any) -> list:
    """Coerce a scalar to a single-element list."""
    if isinstance(value, list):
        return value
    return [value]


def _esc(text: str) -> str:
    """Escape a string for Datacore query literals."""
    return str(text).replace("\\", "\\\\").replace('"', '\\"')


def _normalize_tag(tag: str) -> str:
    """Ensure a tag starts with #."""
    tag = str(tag)
    if not tag.startswith("#"):
        tag = f"#{tag}"
    return tag


def _literal(value: Any) -> str:
    """Convert a Python value to a Datacore literal."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return f'"{_esc(value)}"'
    raise CompileError(f"Cannot convert {type(value).__name__} to Datacore literal")
