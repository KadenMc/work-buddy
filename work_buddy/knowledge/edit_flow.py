"""Auto_run callables backing the ``docs_edit`` workflow.

``docs_edit`` is a three-step workflow that realizes the file-per-unit store's
intent — a unit's content is edited *as its own ``.md`` file*, with validation
and index propagation bracketing the agent's native ``Edit``:

1. ``resolve`` (auto_run → :func:`resolve_for_edit`) — validate the request,
   scaffold the file if creating, and return the canonical ``.md`` path.
2. ``edit`` (reasoning) — the agent edits the file with its native ``Edit`` tool.
3. ``commit`` (auto_run → :func:`commit_edit`) — re-read the file, validate
   (kind-aware, via ``docs_validate``), and signal the conductor to reconcile
   the store cache + search index.

Both callables run in the auto_run **subprocess**, so they are read-mostly:
``commit_edit`` validates the freshly-written bytes (a fresh process has no
stale cache) and returns the ``__reconcile_store__`` sentinel; the conductor —
which runs in the main MCP server process — performs the in-process cache
invalidation that actually makes the edit visible to live queries. (A
subprocess cannot reach the server's in-memory ``_STORE`` / ``_INDEX``.)
"""

from __future__ import annotations

from typing import Any

from work_buddy.knowledge import file_store
from work_buddy.knowledge.model import _KIND_MAP
from work_buddy.knowledge.store import _STORE_DIR, load_store
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Scaffolding for the create path
# ---------------------------------------------------------------------------

def _scaffold(path: str, kind: str, params: dict[str, Any]) -> dict[str, Any]:
    """Build a minimal valid unit dict for a brand-new unit of *kind*.

    Authoring placeholders (``TODO: ...``) mark what the agent fills in during
    the edit step. Kind-specific required fields are seeded so the unit loads
    and validates structurally before the agent's content lands.
    """
    leaf = path.rsplit("/", 1)[-1]
    name = params.get("name") or leaf.replace("-", " ").replace("_", " ").title()
    description = params.get("description") or "TODO: one-line description"

    unit: dict[str, Any] = {"kind": kind, "name": name, "description": description}

    if kind == "directions":
        unit["trigger"] = params.get("trigger") or "TODO: when to use this"
        unit["content"] = {"full": "TODO: author this unit's body."}
    elif kind == "workflow":
        unit["workflow_name"] = params.get("workflow_name") or leaf
        unit["execution"] = "main"
        unit["steps"] = []
        unit["content"] = {"full": "TODO: author the workflow narrative."}
    elif kind == "capability":
        unit["capability_name"] = params.get("capability_name") or leaf
    else:
        unit["content"] = {"full": "TODO: author this unit's body."}

    parents = params.get("parents")
    if parents:
        unit["parents"] = parents if isinstance(parents, list) else [parents]
    return unit


# ---------------------------------------------------------------------------
# Step 1 — resolve
# ---------------------------------------------------------------------------

def resolve_for_edit(*, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """``docs_edit`` step 1: validate the request and return the file to edit.

    Args (via the workflow's ``__params__``):
        params: ``{"path": str, "create"?: bool, "kind"?: str, ...}``. Extra
            keys (name, description, parents, …) seed a scaffold on create.

    Returns a dict with ``ok``; on success: ``path``, ``file`` (absolute ``.md``
    path the agent edits), ``created`` (bool), ``kind``, and a ``next`` hint.
    On failure: ``error``.
    """
    params = params or {}
    path = str(params.get("path") or "").strip()
    create = bool(params.get("create", False))
    kind = str(params.get("kind") or "").strip()

    if not path:
        return {"ok": False, "error": "A 'path' is required (e.g. 'tasks/my-directions')."}

    store = load_store()
    exists = path in store
    file_path = file_store.path_to_file(_STORE_DIR, path)

    if create:
        if exists:
            return {
                "ok": False,
                "error": f"Path {path!r} already exists — omit 'create' to edit it.",
            }
        if kind not in _KIND_MAP:
            return {
                "ok": False,
                "error": (
                    f"Creating a unit requires a valid 'kind'; got {kind!r}. "
                    f"Valid kinds: {', '.join(sorted(_KIND_MAP))}."
                ),
            }
        file_store.write_unit(_STORE_DIR, path, _scaffold(path, kind, params))
        logger.info("docs_edit.resolve: scaffolded new %s unit at %s", kind, path)
        return {
            "ok": True,
            "path": path,
            "file": str(file_path),
            "created": True,
            "kind": kind,
            "next": (
                f"A scaffold was written to {file_path}. Edit it with your native "
                "Edit tool to author the unit (replace the TODO placeholders), then "
                "advance with {'edited': true}."
            ),
        }

    if not exists:
        return {
            "ok": False,
            "error": (
                f"Path {path!r} not found in the store. To create it, pass "
                "create=true and a valid kind."
            ),
        }

    unit = store[path]
    return {
        "ok": True,
        "path": path,
        "file": str(file_path),
        "created": False,
        "kind": unit.kind,
        "next": (
            f"Edit the unit file at {file_path} with your native Edit tool, then "
            "advance with {'edited': true}. (The frontmatter holds structured "
            "fields; the body is content.full. For workflow units, `steps` lives "
            "in frontmatter and per-step prose under `## <step-id>` sections.)"
        ),
    }


# ---------------------------------------------------------------------------
# Step 3 — commit
# ---------------------------------------------------------------------------

def commit_edit(
    *,
    resolve: dict[str, Any] | None = None,
    path: str | None = None,
) -> dict[str, Any]:
    """``docs_edit`` step 3: validate the edited file and request propagation.

    Reads the path from the ``resolve`` step's result (or an explicit ``path``).
    Runs the kind-aware validation suite over the freshly-written bytes plus the
    raw-file workflow heading check, then returns a status report carrying the
    ``__reconcile_store__`` sentinel so the conductor reloads the store cache +
    search index in-process.

    The unit is considered cleanly committed when there are no validation
    findings scoped to *this* unit, no global DAG-integrity errors, and no
    workflow heading issues. Pre-existing corpus errors in *other* units are
    reported but do not block this commit.
    """
    p = (path or (resolve or {}).get("path") or "").strip()
    if not p:
        return {"ok": False, "error": "No path to commit (resolve step result missing)."}

    file_path = file_store.path_to_file(_STORE_DIR, p)
    if not file_path.is_file():
        return {
            "ok": False,
            "error": f"No file for {p!r} at {file_path} — was it deleted or moved?",
            "__reconcile_store__": True,  # reconcile so a deletion is reflected
        }

    # Raw-file check the loader would hide (workflow heading ↔ step-id drift).
    raw = file_path.read_text(encoding="utf-8")
    heading_issues = file_store.workflow_body_heading_issues(raw)

    # Validate over fresh bytes. A fresh subprocess has no stale store cache,
    # so load_store() inside validate sees exactly what the agent just wrote.
    # Scope to unit-shape checks — the slash-command-file hygiene checks
    # (command_mapping, thinned_commands, store_path_validity) are about the
    # .claude/commands surface, not a single content edit.
    from work_buddy.knowledge.validate import validate_store

    report = validate_store(checks=[
        "dag_integrity",
        "required_fields",
        "directions_fields",
        "kind_specific_fields",
        "placeholder_duplicate",
        "parent_child_symmetry",
        "capability_op_resolution",
        "workflow_step_dag",
        "workflow_step_consistency",
    ])
    reloaded = load_store()

    # A corrupt edit (broken YAML frontmatter) makes the loader skip the file —
    # the unit vanishes from the store. Surface that as the headline failure.
    parse_failed = p not in reloaded
    if parse_failed:
        return {
            "ok": False,
            "status": "error",
            "path": p,
            "error": (
                f"Unit {p!r} failed to load after editing — likely malformed YAML "
                "frontmatter or an unterminated '---' block. Fix the file and "
                "re-commit."
            ),
            "heading_issues": heading_issues,
            "__reconcile_store__": True,
        }

    issues = report.get("issues", [])
    own = [
        e for e in issues
        if e.get("path") == p
        or (e.get("check") == "dag_integrity" and (p in e.get("message", "")))
    ]
    own_errors = [e for e in own if e.get("severity", "error") != "warning"]
    own_warnings = [e for e in own if e.get("severity", "error") == "warning"]
    other_errors = [
        e for e in report.get("errors", []) if e not in own_errors
    ]

    clean = not own_errors and not heading_issues
    return {
        "ok": clean,
        "status": "ok" if clean else "error",
        "path": p,
        "kind": reloaded[p].kind,
        "unit_errors": own_errors,
        "unit_warnings": own_warnings,
        "heading_issues": heading_issues,
        "other_corpus_errors": other_errors,
        "total_units": report.get("total_units"),
        "next": (
            "Committed and propagating. The store cache and search index are "
            "being reconciled."
            if clean else
            "Validation found issues with this unit — fix them in the file and "
            "re-run docs_edit to re-commit."
        ),
        "__reconcile_store__": True,
    }
