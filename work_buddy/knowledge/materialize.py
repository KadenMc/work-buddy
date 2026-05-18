"""Materialization workflow for editing knowledge-store units.

``docs_create`` / ``docs_update`` are full-content-replacement APIs — editing one
line of a large unit means resending the whole unit through the MCP transport,
with JSON-escape hazards. This module adds a two-step alternative:

  - ``docs_checkout(path)`` serializes a unit to an editable YAML-frontmatter +
    markdown buffer, stores it as an artifact (1-day TTL), and returns a
    ``checkout_id`` plus the buffer file path. ``docs_checkout(path, kind=...,
    template=True)`` instead materializes a blank typed template for a new unit.
  - ``docs_commit(checkout_id)`` parses the buffer back, validates it, persists
    it to the store, deletes the buffer, and kicks a non-blocking re-index.

The buffer survives a failed commit, so the agent fixes it in place and retries
rather than re-sending the whole payload. See
``.data/designs/docs-materialization/DESIGN.md``.

Workflow units are out of scope (their ``steps`` / ``step_instructions`` need a
different structured format); ``docs_checkout`` refuses them. Generated units
(no hand-authored source file — e.g. un-migrated capabilities) are also refused:
their source of truth is Python, which a knowledge-unit editor cannot touch.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any

import yaml

from work_buddy.knowledge.editor import (
    _add_child_to_parents,
    _best_file_for_new_path,
    _duplicate_error_response,
    _find_file_for_path,
    _invalidate_and_validate,
    _read_json_file,
    _remove_child_from_parents,
    _scan_placeholder_hints,
    _write_json_file,
    check_duplicate_placeholders,
)
from work_buddy.knowledge.store import load_store

logger = logging.getLogger(__name__)

# Artifact type for editing buffers — 1-day TTL, registered in
# ``work_buddy/artifacts/backends/filesystem.py``. The artifact system's
# cleanup tick sweeps expired buffers automatically.
_BUFFER_ARTIFACT_TYPE = "docs_buffer"

# PromptUnit kinds that can be materialized. Workflow units are excluded
# (different structured format); personal/vault units live in a separate
# store with their own markdown-backed editing path.
_SUPPORTED_KINDS = frozenset({
    "directions", "system", "service", "integration", "reference",
    "concept", "capability",
})

# ---------------------------------------------------------------------------
# YAML serialization — readable block style for multi-line strings
# ---------------------------------------------------------------------------

class _BufferDumper(yaml.SafeDumper):
    """SafeDumper that renders multi-line strings as literal ``|`` blocks."""


def _str_representer(dumper: yaml.Dumper, data: str):  # type: ignore[override]
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_BufferDumper.add_representer(str, _str_representer)


def _dump_frontmatter(fields: dict[str, Any]) -> str:
    """Serialize a frontmatter dict to YAML.

    Key order is preserved as given — for an existing unit that is the stored
    JSON key order, so a checkout→commit with no edits is a no-op diff rather
    than a whole-unit key reshuffle.
    """
    return yaml.dump(
        fields,
        Dumper=_BufferDumper,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
        width=4096,
    )


# ---------------------------------------------------------------------------
# Serialize / parse
# ---------------------------------------------------------------------------

def unit_dict_to_markdown(path: str, unit_dict: dict[str, Any]) -> str:
    """Serialize a raw store unit dict to a YAML-frontmatter + markdown buffer.

    Works on the exact persisted dict (not the typed model), so the round trip
    is faithful. ``content.full`` becomes the markdown body; ``content.summary``
    rides in frontmatter. ``path`` is added to frontmatter for display only.
    """
    content = unit_dict.get("content", {}) or {}
    body = content.get("full", "") if isinstance(content, dict) else ""
    summary = content.get("summary") if isinstance(content, dict) else None

    fields: dict[str, Any] = {"path": path}
    for key, value in unit_dict.items():
        if key == "content":
            continue
        fields[key] = value
    if summary is not None:
        fields["summary"] = summary

    frontmatter = _dump_frontmatter(fields)
    return f"---\n{frontmatter}---\n\n{body}"


def split_buffer(text: str) -> tuple[dict[str, Any], str]:
    """Split a buffer into (frontmatter dict, body).

    Strict — raises ``ValueError`` on a missing or malformed frontmatter block,
    so ``docs_commit`` can reject and keep the buffer for fix-and-retry.
    """
    if not text.startswith("---"):
        raise ValueError(
            "buffer has no YAML frontmatter (expected a '---' block at the top)"
        )
    end_idx = text.find("\n---", 3)
    if end_idx == -1:
        raise ValueError("frontmatter is not closed (no terminating '---' line)")

    yaml_block = text[3:end_idx].strip()
    body = text[end_idx + 4:].lstrip("\n")

    if not yaml_block:
        return {}, body
    try:
        fm = yaml.safe_load(yaml_block)
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid YAML frontmatter: {exc}") from exc
    if not isinstance(fm, dict):
        raise ValueError("YAML frontmatter must be a mapping of fields")
    return fm, body


def markdown_to_unit_dict(text: str) -> dict[str, Any]:
    """Parse a buffer back into a raw store unit dict.

    Inverse of :func:`unit_dict_to_markdown`. The frontmatter ``path`` key is
    dropped (informational only); ``summary`` is folded back into ``content``.
    """
    fm, body = split_buffer(text)
    fm.pop("path", None)
    summary = fm.pop("summary", None)

    unit_dict: dict[str, Any] = dict(fm)
    # ``summary`` before ``full`` — matches the store's usual content shape.
    content: dict[str, Any] = {}
    if summary is not None:
        content["summary"] = summary
    if body:
        content["full"] = body
    if content:
        unit_dict["content"] = content
    return unit_dict


# ---------------------------------------------------------------------------
# Templates for new units
# ---------------------------------------------------------------------------

_KIND_TEMPLATE_FIELDS: dict[str, dict[str, Any]] = {
    "directions": {"trigger": "TODO: when this unit is loaded", "command": ""},
    "system": {"entry_points": []},
    "service": {"ports": [], "health_url": "", "entry_points": []},
    "integration": {
        "external_system": "TODO: name of the external system",
        "bridge_module": "", "ports": [], "entry_points": [],
    },
    "reference": {"entry_points": []},
    "concept": {},
    "capability": {
        "capability_name": "TODO: MCP dispatch name",
        "category": "TODO: registry category",
        "op": "op.wb.TODO",
        "schema_version": "wb-capability/v1",
        "parameters": {},
        "mutates_state": False,
        "retry_policy": "manual",
    },
}


def build_template(path: str, kind: str) -> str:
    """Build a blank typed template buffer for a new unit of ``kind``.

    Fields are ordered name → kind → description → kind-specific → shared, so
    the scaffolded buffer reads top-down.
    """
    unit_dict: dict[str, Any] = {
        "name": "TODO: human-readable name",
        "kind": kind,
        "description": "TODO: one-line summary",
    }
    unit_dict.update(_KIND_TEMPLATE_FIELDS.get(kind, {}))
    unit_dict["tags"] = []
    unit_dict["aliases"] = []
    unit_dict["parents"] = []
    unit_dict["content"] = {"full": "TODO: full content (this markdown body)"}
    return unit_dict_to_markdown(path, unit_dict)


# ---------------------------------------------------------------------------
# Buffer artifact metadata
# ---------------------------------------------------------------------------

def _slugify_path(path: str) -> str:
    return path.replace("/", "-")


def _buffer_meta(target_path: str, mode: str, kind: str) -> str:
    """JSON metadata stored in the artifact ``description`` field."""
    return json.dumps({
        "wb_docs_buffer": True,
        "target_path": target_path,
        "mode": mode,
        "kind": kind,
    })


def _reindex_async() -> None:
    """Rebuild the knowledge search index off the request path.

    ``force=False`` reuses cached dense vectors, so only the just-committed
    unit re-embeds. Runs on a daemon thread — ``docs_commit`` does not wait.
    """
    def _run() -> None:
        try:
            from work_buddy.knowledge.index import rebuild_index
            rebuild_index(knowledge_scope="all", force=False)
        except Exception:
            logger.exception("background re-index after docs_commit failed")

    threading.Thread(
        target=_run, daemon=True, name="docs-commit-reindex",
    ).start()


# ---------------------------------------------------------------------------
# docs_checkout
# ---------------------------------------------------------------------------

def docs_checkout(
    *,
    path: str,
    kind: str = "",
    template: bool = False,
    session_id: str = "",
) -> dict[str, Any]:
    """Materialize a knowledge unit (or a blank template) as an editable buffer.

    Args:
        path: Unit path to check out, or the path for a new unit (template mode).
        kind: Required in template mode — the kind of unit to scaffold.
        template: When True, build a blank typed template for a NEW unit at
            ``path`` instead of reading an existing one.
        session_id: Optional — tags the buffer artifact with the caller's session.

    Returns ``{checkout_id, buffer_path, target_path, mode, kind, expires_at}``.
    Edit the file at ``buffer_path`` with normal file tools, then call
    ``docs_commit(checkout_id)``.
    """
    from work_buddy.artifacts import save

    store = load_store()

    if template:
        if not kind:
            return {"error": "template checkout requires 'kind'."}
        if kind not in _SUPPORTED_KINDS:
            return {
                "error": f"kind {kind!r} is not materializable. "
                         f"Supported: {', '.join(sorted(_SUPPORTED_KINDS))}.",
            }
        if path in store:
            return {
                "error": f"Path {path!r} already exists. Check it out without "
                         "template=True to edit it.",
            }
        buffer_text = build_template(path, kind)
        mode = "create"
    else:
        if path not in store:
            return {"error": f"Path {path!r} not found in the knowledge store."}
        unit = store[path]
        unit_kind = getattr(unit, "kind", "")
        if unit_kind == "workflow":
            return {
                "error": f"{path!r} is a workflow unit — not supported by "
                         "materialization. Use workflow_update for workflows.",
            }
        if unit_kind == "personal":
            return {
                "error": f"{path!r} is a personal/vault unit — edit it via the "
                         "vault, not the knowledge-store editor.",
            }
        target_file = _find_file_for_path(path)
        if target_file is None:
            return {
                "error": f"{path!r} is a generated unit — it has no hand-authored "
                         "source file, so it cannot be edited here. (Generated "
                         "capabilities are defined in Python and compiled into "
                         "_generated_*.json; they become editable once migrated "
                         "to a declaration.)",
            }
        unit_dict = _read_json_file(target_file).get(path)
        if unit_dict is None:
            return {"error": f"{path!r} not found in {target_file.name}."}
        buffer_text = unit_dict_to_markdown(path, unit_dict)
        mode = "edit"
        kind = unit_dict.get("kind", unit_kind)

    record = save(
        buffer_text,
        type=_BUFFER_ARTIFACT_TYPE,
        slug=f"docs-{_slugify_path(path)}",
        ext="md",
        description=_buffer_meta(path, mode, kind),
        session_id=session_id or None,
        ttl_days=1,
    )

    return {
        "status": "checked_out",
        "checkout_id": record.id,
        "buffer_path": record.path.as_posix(),
        "target_path": path,
        "mode": mode,
        "kind": kind,
        "expires_at": record.expires_at.isoformat(),
        "instructions": (
            "Edit the buffer file with Read/Edit/Write, then call "
            "docs_commit(checkout_id). The 'path' field in the buffer is "
            "informational — changing it does NOT move the unit (use docs_move). "
            "If commit fails validation, the buffer is preserved: fix it and "
            "call docs_commit again."
        ),
    }


# ---------------------------------------------------------------------------
# docs_commit
# ---------------------------------------------------------------------------

_REQUIRED_FIELDS = ("name", "description", "kind")


def docs_commit(*, checkout_id: str) -> dict[str, Any]:
    """Parse a checked-out buffer, validate it, and persist it to the store.

    On a hard validation failure (malformed frontmatter, missing required
    fields, duplicate placeholders) nothing is written and the buffer artifact
    is preserved — fix the buffer file and call ``docs_commit`` again. On
    success the unit is persisted, the buffer artifact is deleted, and the
    knowledge index is rebuilt on a background thread.
    """
    from work_buddy.artifacts import delete as _artifact_delete
    from work_buddy.artifacts import get as _artifact_get

    try:
        record = _artifact_get(checkout_id)
    except FileNotFoundError:
        return {
            "error": "checkout_not_found",
            "message": f"No buffer for checkout_id {checkout_id!r} — it may have "
                       "expired (buffers live 1 day) or already been committed.",
        }

    try:
        meta = json.loads(record.description)
    except (ValueError, TypeError):
        return {
            "error": "corrupt_checkout",
            "message": f"Checkout {checkout_id!r} has unreadable metadata.",
        }
    target_path = meta.get("target_path")
    mode = meta.get("mode")
    if not target_path or mode not in ("edit", "create"):
        return {
            "error": "corrupt_checkout",
            "message": f"Checkout {checkout_id!r} metadata is incomplete.",
        }

    buffer_text = record.path.read_text(encoding="utf-8")

    # --- Parse (hard failure → buffer preserved) ---
    try:
        unit_dict = markdown_to_unit_dict(buffer_text)
    except ValueError as exc:
        return {
            "error": "buffer_parse_failed",
            "message": str(exc),
            "checkout_id": checkout_id,
            "buffer_path": record.path.as_posix(),
            "hint": "Fix the buffer file and call docs_commit again.",
        }

    # --- Required fields (hard failure → buffer preserved) ---
    missing = [f for f in _REQUIRED_FIELDS if not unit_dict.get(f)]
    if missing:
        return {
            "error": "missing_required_fields",
            "message": f"Buffer is missing required field(s): {', '.join(missing)}.",
            "checkout_id": checkout_id,
            "buffer_path": record.path.as_posix(),
            "hint": "Fill the field(s) in the buffer and call docs_commit again.",
        }

    kind = unit_dict.get("kind")
    if kind == "workflow":
        return {
            "error": "unsupported_kind",
            "message": "Workflow units cannot be committed via materialization.",
            "checkout_id": checkout_id,
            "buffer_path": record.path.as_posix(),
        }

    # --- Duplicate placeholders (hard failure → buffer preserved) ---
    content_full = ""
    content = unit_dict.get("content")
    if isinstance(content, dict):
        content_full = content.get("full", "") or ""
    duplicates = check_duplicate_placeholders(content_full)
    if duplicates:
        resp = _duplicate_error_response(target_path, duplicates)
        resp["checkout_id"] = checkout_id
        resp["buffer_path"] = record.path.as_posix()
        resp["hint"] = "Remove the duplicate placeholder(s) and call docs_commit again."
        return resp

    store = load_store()

    # --- Persist (full replace) ---
    if mode == "create":
        if target_path in store:
            return {
                "error": "already_exists",
                "message": f"{target_path!r} was created since checkout. "
                           "Nothing was written; the buffer is preserved.",
                "checkout_id": checkout_id,
                "buffer_path": record.path.as_posix(),
            }
        target_file = _best_file_for_new_path(target_path, kind=kind)
        file_data = _read_json_file(target_file)
        file_data[target_path] = unit_dict
        _write_json_file(target_file, file_data)
        _add_child_to_parents(target_path, unit_dict.get("parents", []) or [])
    else:  # edit
        target_file = _find_file_for_path(target_path)
        if target_file is None:
            return {
                "error": "not_editable",
                "message": f"{target_path!r} is not in a hand-authored file "
                           "(it may have been deleted or is generated). "
                           "Nothing was written; the buffer is preserved.",
                "checkout_id": checkout_id,
                "buffer_path": record.path.as_posix(),
            }
        old_unit = store.get(target_path)
        old_parents = list(getattr(old_unit, "parents", []) or [])
        new_parents = list(unit_dict.get("parents", []) or [])

        file_data = _read_json_file(target_file)
        file_data[target_path] = unit_dict
        _write_json_file(target_file, file_data)

        _add_child_to_parents(
            target_path, [p for p in new_parents if p not in old_parents],
        )
        _remove_child_from_parents(
            target_path, [p for p in old_parents if p not in new_parents],
        )

    dag_errors = _invalidate_and_validate()
    hints = _scan_placeholder_hints(target_path, load_store())

    # Buffer is consumed only on success.
    _artifact_delete(checkout_id)

    # Keep search fresh without blocking the caller.
    _reindex_async()

    return {
        "status": "committed",
        "path": target_path,
        "file": target_file.name,
        "mode": mode,
        "dag_errors": dag_errors,
        "hints": hints,
    }
