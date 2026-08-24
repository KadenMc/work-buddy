"""Task file ↔ store synchronization.

The master task list is the **source of truth**. The store follows the file.

Compares the master task list (markdown file) against the SQLite metadata
store and auto-resolves discrepancies:

1. **Orphan in file**: Task has a 🆔 in the file but no store record.
   → Auto-creates a store record (state=inbox or done, urgency=medium).

2. **Orphan in store**: Store record exists but no matching task line in
   the file (manually deleted or moved).
   → Tombstone-deleted from the store.

3. **Checkbox mismatch**: File says done (``- [x]``) but store says
   non-done, or vice versa.
   → Store state updated to match the file.

Designed to run as a sidecar scheduled job (every 30 minutes). Uses the
Obsidian bridge when available, falls back to direct filesystem reads.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from work_buddy.config import load_config
from work_buddy.logging_config import get_logger
from work_buddy.obsidian.tasks import store
from work_buddy.obsidian.tasks.authority import frozen_task_mutation_boundary
from work_buddy.obsidian.tasks.mutations import (
    DONE_DATE_RE,
    DUE_DATE_RE,
    MASTER_TASK_FILE,
    TASK_ID_RE,
    URGENCY_EMOJI_RE,
    extract_description_from_line,
)


# Plugin-emoji → SQLite-column mapping.
#
# The Obsidian Tasks plugin owns the markdown emoji syntax; the
# task_metadata schema has the matching columns but the parser used to
# ignore them, leaving the bridge half-built. Mapping each glyph to a
# canonical urgency level lets task_sync drift-reconcile in both
# directions (📅 file → deadline_date, ⏫🔼🔽 file → urgency, ✅ file →
# completed_at). Adding a new priority emoji means updating this map +
# the URGENCY_EMOJI_RE in mutations.py.
_URGENCY_EMOJI_TO_LEVEL = {
    "⏫": "high",
    "🔼": "medium",
    "🔽": "low",
}

# Matches the task-note wikilink embedded in a task line, e.g. [[<uuid>|📓]].
# The 📓 alias keeps this distinct from ordinary wikilinks on the same line.
NOTE_WIKILINK_RE = re.compile(
    r"\[\[([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\|📓\]\]"
)

# Matches inline tags like `#paper/ecg-classifier` or `#health/sleep`.
# The lookbehind avoids matching `#` that sits inside a word (e.g., an ID or
# URL fragment). Nested paths (a/b/c) are allowed.
TAG_RE = re.compile(r"(?<![\w/])#([a-z0-9][a-z0-9_/-]*)", re.IGNORECASE)

# Tag prefixes that are never treated as user-defined namespaces.
#
# - `tasker/...`: legacy work-buddy metadata (being stripped elsewhere)
#
# Note: `projects/` is NOT reserved — `#projects/<slug>` is both the registry
# link AND a first-class organizational axis in the namespace tree. Keeping
# it out of the tree previously forced users to invent parallel taxonomies
# (e.g., `#research/<slug>`) just to surface tasks in the dashboard.
#
# Note: `wb/` is NOT reserved — it's the canonical work-buddy-dev namespace.
# Only the specific inline-todo markers `wb/todo` and `wb/done` are excluded
# (see RESERVED_TAG_EXACT).
RESERVED_TAG_PREFIXES: tuple[str, ...] = (
    "tasker/",
)

# Specific tag values that are reserved regardless of prefix. These are
# system markers (plugin-owned or inline-todo workflow) that would otherwise
# be mis-classified as namespaces.
RESERVED_TAG_EXACT: frozenset[str] = frozenset({
    "todo",
    "wb/todo",
    "wb/done",
})

# Tags starting with these prefixes are *always* namespacey, regardless of
# discovery frequency. They give power users an explicit opt-in.
NAMESPACE_OPT_IN_PREFIXES: tuple[str, ...] = ("ns/", "task/")

logger = get_logger(__name__)


def _is_reserved(tag: str) -> bool:
    """True if ``tag`` matches a reserved prefix or exact-value (never a
    user namespace)."""
    tag_lower = tag.lower()
    if tag_lower in RESERVED_TAG_EXACT:
        return True
    for prefix in RESERVED_TAG_PREFIXES:
        if tag_lower.startswith(prefix):
            return True
    return False


def _is_opt_in(tag: str) -> bool:
    """True if ``tag`` uses an always-namespacey opt-in prefix."""
    tag_lower = tag.lower()
    return any(tag_lower.startswith(p) for p in NAMESPACE_OPT_IN_PREFIXES)


def _namespace_threshold() -> int:
    """Minimum open-task count for a tag to be classified as a namespace."""
    cfg = load_config()
    val = cfg.get("tasks", {}).get("namespace_threshold", 2)
    try:
        return max(1, int(val))
    except (TypeError, ValueError):
        return 2


def extract_tags_from_line(line: str) -> list[str]:
    """Pull all `#tag` tokens out of a task line, normalized (no leading '#').

    Preserves first-seen order; de-duplicates case-insensitively.
    """
    seen: set[str] = set()
    out: list[str] = []
    for m in TAG_RE.finditer(line):
        tag = m.group(1)
        key = tag.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(tag)
    return out


def _tag_prefixes(tag: str) -> list[str]:
    """Return every ancestor prefix of a slash-separated tag, shallowest first.

    ``research/electricrag/writing-prep`` → ``["research", "research/electricrag",
    "research/electricrag/writing-prep"]``.
    """
    parts = [p for p in tag.lower().split("/") if p]
    return ["/".join(parts[: i + 1]) for i in range(len(parts))]


def classify_tags(
    prefix_counts: dict[str, int],
    tag_list_for_this_task: list[str],
    *,
    threshold: int = 2,
) -> list[tuple[str, bool]]:
    """Classify each tag on a single task as namespacey or not.

    A tag is namespacey iff:
      - it is NOT in the reserved-prefix blocklist, AND
      - it uses an opt-in prefix (ns/, task/), OR *any* of its ancestor
        prefixes (including itself) has >= ``threshold`` tasks carrying it
        or a descendant (per ``prefix_counts``).

    The ancestor walk is what rescues rare leaves: a one-off like
    ``research/electricrag/writing-prep`` inherits namespacey-ness from
    a popular parent like ``research/electricrag`` or ``research``, so a
    unique sub-bucket is not silently dropped from the tree.

    Reserved tags are still returned (so the cache can be queried for
    non-tree linkages) but with ``is_namespace=False``.

    Args:
        prefix_counts: Map of prefix -> count of distinct tasks whose
                       tags include that prefix *or any descendant of it*.
                       Computed once per sync across the whole vault.
        tag_list_for_this_task: Tags parsed from this task's line.
        threshold: Minimum count for discovery-based classification.
    """
    result: list[tuple[str, bool]] = []
    for tag in tag_list_for_this_task:
        if _is_reserved(tag):
            result.append((tag, False))
            continue
        if _is_opt_in(tag):
            result.append((tag, True))
            continue
        rescued = any(
            prefix_counts.get(p, 0) >= threshold for p in _tag_prefixes(tag)
        )
        result.append((tag, rescued))
    return result


def _read_master_list() -> str | None:
    """Read the master task list, preferring the bridge, falling back to fs."""
    # Try bridge first (keeps Obsidian's view consistent)
    try:
        from work_buddy.obsidian import bridge

        if bridge.is_available():
            content = bridge.read_file(MASTER_TASK_FILE)
            if content is not None:
                return content
    except Exception:
        pass

    # Fallback: direct filesystem read
    cfg = load_config()
    vault_root = cfg.get("vault_root", "")
    if not vault_root:
        return None

    fs_path = Path(vault_root) / MASTER_TASK_FILE
    if fs_path.exists():
        return fs_path.read_text(encoding="utf-8")

    return None


def _parse_file_tasks(content: str) -> dict[str, dict[str, Any]]:
    """Parse task lines from the master list into {task_id: info} dict.

    Only includes lines that have a 🆔 identifier.

    Extracted fields:

    - ``is_done`` / ``note_uuid`` / ``raw_tags`` / ``description`` — the
      pre-Slice-N basics; the reconciliation loop has always tracked these.
    - ``deadline_date`` — ISO date from ``📅 YYYY-MM-DD`` (or ``None``).
    - ``urgency`` — ``"high"`` / ``"medium"`` / ``"low"`` from
      ``⏫`` / ``🔼`` / ``🔽`` (or ``None`` when no urgency emoji is present).
    - ``completed_at`` — ISO date from ``✅ YYYY-MM-DD`` (or ``None``).

    Emoji extraction here mirrors the columns the store carries for
    deadline / urgency / completed_at. The drift loops in ``task_sync``
    reconcile these parsed values into the canonical SQLite columns.
    """
    tasks: dict[str, dict[str, Any]] = {}

    for i, line in enumerate(content.split("\n")):
        line_stripped = line.strip()
        if not line_stripped.startswith("- ["):
            continue

        m = TASK_ID_RE.search(line_stripped)
        if not m:
            continue

        task_id = m.group(1)
        is_done = line_stripped.startswith("- [x]")

        note_match = NOTE_WIKILINK_RE.search(line_stripped)
        note_uuid = note_match.group(1) if note_match else None

        raw_tags = extract_tags_from_line(line_stripped)
        description = extract_description_from_line(line_stripped)

        # Plugin-emoji extraction. Each match yields just the date or the
        # glyph; we strip the leading emoji + whitespace so the column
        # stores the bare ISO date or canonical urgency level.
        due_match = DUE_DATE_RE.search(line_stripped)
        deadline_date = (
            due_match.group().replace("📅", "").strip() if due_match else None
        )

        done_match = DONE_DATE_RE.search(line_stripped)
        completed_at = (
            done_match.group().replace("✅", "").strip() if done_match else None
        )

        urgency_match = URGENCY_EMOJI_RE.search(line_stripped)
        urgency = (
            _URGENCY_EMOJI_TO_LEVEL.get(urgency_match.group())
            if urgency_match
            else None
        )

        tasks[task_id] = {
            "line_number": i + 1,
            "is_done": is_done,
            "line": line_stripped,
            "note_uuid": note_uuid,
            "raw_tags": raw_tags,
            "description": description,
            "deadline_date": deadline_date,
            "urgency": urgency,
            "completed_at": completed_at,
        }

    return tasks


def _rebuild_tag_cache(
    file_tasks: dict[str, dict[str, Any]],
    surviving_ids: set[str],
) -> int:
    """Rebuild the ``task_tags`` cache from parsed line data.

    Only tasks still present in both the file and store (``surviving_ids``)
    are written; tasks deleted this sync run are cleaned up separately
    via the FK cascade. Returns the number of tasks whose tag rows were
    (re)written.
    """
    threshold = _namespace_threshold()

    # Prefix frequency across all parsed tasks: for each tag, we credit every
    # ancestor prefix (so `research/electricrag/x` contributes one task-count
    # to `research`, `research/electricrag`, and `research/electricrag/x`).
    # Using a set per task avoids double-counting when two tags share a prefix
    # on the same task. This is what lets the classifier rescue rare leaves
    # whose parent prefix is popular.
    prefix_counts: dict[str, int] = {}
    for info in file_tasks.values():
        task_prefixes: set[str] = set()
        for tag in info.get("raw_tags", []):
            task_prefixes.update(_tag_prefixes(tag))
        for p in task_prefixes:
            prefix_counts[p] = prefix_counts.get(p, 0) + 1

    written = 0
    for task_id in surviving_ids:
        info = file_tasks.get(task_id)
        if not info:
            continue
        classified = classify_tags(
            prefix_counts,
            info.get("raw_tags", []),
            threshold=threshold,
        )
        try:
            store.set_task_tags(task_id, classified)
            written += 1
        except Exception as exc:
            logger.warning("task_sync: failed to write tag cache for %s: %s", task_id, exc)

    return written


@frozen_task_mutation_boundary
def task_sync() -> dict[str, Any]:
    """Reconcile the master task list against the SQLite task store.

    Delegates to
    :class:`~work_buddy.obsidian.tasks.markdown_db.TaskMarkdownDB` — the
    markdown-canonical sync abstraction (see ``architecture/markdown-db``).
    ``task_sync`` is kept as the stable entry point: the ``task_sync``
    capability and the dashboard Sync button both invoke this name, and
    the return shape (``status`` plus per-category counts) is preserved.

    The reconciliation itself — orphan handling, the per-field drift
    loop, the tag-cache rebuild, and the ``task_sync_status`` freshness
    write — is :meth:`MarkdownDB.reconcile_drift` followed by
    :meth:`TaskMarkdownDB.post_reconcile`. The master task list remains
    the canonical surface; the store follows the file.

    Imported lazily to avoid an import cycle — ``markdown_db`` imports
    this module's parsers (``_parse_file_tasks`` / ``_read_master_list``
    / ``_rebuild_tag_cache``) at module load.
    """
    from work_buddy.obsidian.tasks.markdown_db import reconcile_tasks
    return reconcile_tasks()
