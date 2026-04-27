"""Task mutation API — programmatic task state changes.

Architecture:
- work-buddy metadata (state, urgency, complexity, contract) → SQLite store (store.py)
- Plugin-owned data (checkbox, dates, priority emojis) → markdown file
- Task identification → 🆔 t-<hex> in the task line, primary key in store

The markdown task line stays clean: #todo, text, #projects/*, 🆔, and plugin emojis.
All categorical metadata that was previously in #tasker/* tags now lives in the store.
"""

from __future__ import annotations

import re
import uuid
from datetime import date
from typing import Any, Callable

from work_buddy.consent import requires_consent
from work_buddy.logging_config import get_logger
from work_buddy.obsidian import bridge
from work_buddy.obsidian.errors import ObsidianError
from work_buddy.obsidian.retry import bridge_failure, bridge_retry
from work_buddy.obsidian.tasks.env import _escape_js, _run_js
from work_buddy.obsidian.tasks import store

logger = get_logger(__name__)

# ── Constants ───────────────────────────────────────────────────

MASTER_TASK_FILE = "tasks/master-task-list.md"
ARCHIVE_FILE = "tasks/archive.md"
TASK_NOTES_DIR = "tasks/notes"
TASK_NOTE_TEMPLATE = "templates/task_note.md"

# ── Regex patterns ──────────────────────────────────────────────

# Legacy inline tags (for stripping from old tasks during migration)
STATE_TAG_RE = re.compile(r"\s*#tasker/state/\w+")
URGENCY_TAG_RE = re.compile(r"\s*#tasker/urgency/\w+")
COMPLEXITY_TAG_RE = re.compile(r"\s*#tasker/complexity/\w+")

# Plugin-owned patterns
DUE_DATE_RE = re.compile(r"📅\s*\d{4}-\d{2}-\d{2}")
DONE_DATE_RE = re.compile(r"✅\s*\d{4}-\d{2}-\d{2}")
CHECKBOX_RE = re.compile(r"^(- \[)([ x])(\])")
URGENCY_EMOJI_RE = re.compile(r"[🔼⏫]")
TASK_ID_RE = re.compile(r"🆔\s*(t-[0-9a-f]+)")

# User-supplied namespace tags must match this shape (no leading '#').
# Mirrors sync.TAG_RE but as an anchored full-match pattern.
NAMESPACE_TAG_RE = re.compile(r"^[a-z0-9][a-z0-9_/-]*$", re.IGNORECASE)


# ── Description extraction ──────────────────────────────────────
#
# Slice 3: derive a clean human-readable description from a task line by
# stripping the structural noise (checkbox, hashtags, wikilinks, plugin
# emojis, 🆔). The resulting text is what gets stored in
# ``task_metadata.description`` and what ``task_search`` queries against.
#
# The same regex chain was previously inlined inside
# ``_load_task_payload`` (line ~1046 prior to Slice 3) — moved here so
# ``task_sync``, ``task_update_description``, and ``_load_task_payload``
# share a single canonical extractor.

# Wikilinks like [[uuid|📓]] embedded in the line.
_DESC_WIKILINK_RE = re.compile(r"\[\[[^\]]+\]\]")
# Any remaining hashtag (#todo, #projects/x, #foo, etc.).
_DESC_HASHTAG_RE = re.compile(r"#\S+")
# Leading checkbox marker.
_DESC_CHECKBOX_RE = re.compile(r"^\s*-\s*\[.\]\s*")
# Plugin emojis with their adjacent payload tokens. Each gets its own
# pattern so adjacent emojis (e.g. ``🔼 🆔 t-...``) all get stripped
# rather than the first one greedily consuming the second.
# Match any `t-<alphanumeric>` after 🆔 — production IDs are hex via
# generate_task_id(), but the regex is permissive so a malformed legacy
# ID still gets stripped from the description.
_DESC_TASK_ID_RE = re.compile(r"🆔\s*t-[a-z0-9]+", re.IGNORECASE)
_DESC_DUE_DATE_RE = re.compile(r"📅\s*\d{4}-\d{2}-\d{2}")
_DESC_DONE_DATE_RE = re.compile(r"✅\s*\d{4}-\d{2}-\d{2}")
_DESC_URGENCY_EMOJI_RE = re.compile(r"[🔼⏫]")


# Lookahead-only pattern for the FIRST structural boundary that ends
# the human-readable description portion of a task line. Lookaheads so
# the boundary itself isn't consumed — we want the position, not the
# token.
#
# Boundaries:
#   - ``[[`` — a wikilink (the task-note link in particular)
#   - ``#\S`` — any hashtag (the leading ``#todo`` is stripped by the
#     prefix match before this regex runs)
#   - 🆔 / 📅 / ✅ — plugin emojis with adjacent payloads
#   - 🔼 / ⏫ — urgency emojis
_DESC_BOUNDARY_LOOKAHEAD = re.compile(r"(?=\[\[|#\S|🆔|📅|✅|🔼|⏫)")

# Match the line prefix that precedes the description.
_DESC_LINE_PREFIX_RE = re.compile(r"^(\s*-\s*\[.\]\s*#todo\s+)")


def replace_description_in_line(line: str, new_description: str) -> str:
    """Rewrite the description text in-place on a task line.

    Preserves: checkbox state, the ``#todo`` marker, all hashtags
    (``#projects/*``, namespace tags), wikilinks (note links and any
    others the user has added), the 🆔 marker and ID, plugin emojis
    (📅 due date, ✅ done date, 🔼/⏫ urgency).

    Boundary detection: the description is the run between ``#todo``
    and the first structural marker (``[[``, ``#<non-space>``, plugin
    emoji). For tasks created via ``create_task`` the description never
    contains these characters, so the boundary is unambiguous. For
    user-hand-edited tasks where the description contains a ``#`` (e.g.
    issue references like "fix #123") or ``[[``, the rewrite boundary
    will be earlier than expected — those tokens get pushed into the
    "metadata suffix" and end up appearing after the new description.
    Document this caveat in ``task_update_description``'s docstring.

    If ``line`` doesn't match the standard task-line shape, returns
    the line unchanged. Callers should treat that as a no-op.
    """
    prefix_match = _DESC_LINE_PREFIX_RE.match(line)
    if not prefix_match:
        return line

    prefix = prefix_match.group(1)
    rest = line[prefix_match.end():]

    boundary = _DESC_BOUNDARY_LOOKAHEAD.search(rest)
    if boundary is None:
        # Description-only line (no metadata after — unusual but fine).
        suffix = ""
    else:
        suffix = rest[boundary.start():]

    new_desc = new_description.strip().replace("\n", " ").replace("\r", " ")
    new_desc = re.sub(r"\s+", " ", new_desc)

    if suffix:
        return f"{prefix}{new_desc} {suffix}".rstrip()
    return f"{prefix}{new_desc}".rstrip()


def extract_description_from_line(line: str) -> str:
    """Pull the clean human-readable description out of a task line.

    Strips: checkbox, all hashtags, wikilinks (including the
    ``[[uuid|📓]]`` task-note link), the 🆔 + ID, 📅 + due date, ✅ + done
    date, and urgency emojis. Collapses whitespace and trims.

    Returns an empty string for lines that aren't task lines or that
    contain no text after stripping. Used by ``task_sync`` to populate
    ``task_metadata.description``, by ``task_update_description`` to
    derive the new description after rewrite, and by
    ``_load_task_payload`` for legacy fallback.
    """
    if not line:
        return ""
    text = _DESC_CHECKBOX_RE.sub("", line)
    text = _DESC_WIKILINK_RE.sub("", text)
    text = _DESC_HASHTAG_RE.sub("", text)
    text = _DESC_TASK_ID_RE.sub("", text)
    text = _DESC_DUE_DATE_RE.sub("", text)
    text = _DESC_DONE_DATE_RE.sub("", text)
    text = _DESC_URGENCY_EMOJI_RE.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _normalize_tags(tags: list[str] | None) -> list[str]:
    """Accept a list of tag strings with or without leading '#'.

    Strips leading '#', rejects empties and malformed tokens, de-dupes
    (case-insensitive, preserving first-seen order). Returns a list of
    normalized tag names (no '#').

    Raises ValueError on a malformed tag so create_task fails loudly.
    """
    if not tags:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for raw in tags:
        if not isinstance(raw, str):
            raise ValueError(f"Tag must be a string, got {type(raw).__name__}: {raw!r}")
        tag = raw.strip().lstrip("#").strip()
        if not tag:
            raise ValueError(f"Tag is empty or whitespace: {raw!r}")
        if not NAMESPACE_TAG_RE.match(tag):
            raise ValueError(
                f"Tag {raw!r} is malformed — use lowercase letters, digits, '-', '_', "
                f"and '/' for nesting (e.g. 'paper/ecg-classifier')."
            )
        key = tag.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(tag)
    return out


# ── ID generation ───────────────────────────────────────────────


def generate_task_id() -> str:
    """Generate a short unique task ID (e.g., 't-a3f8c1e2')."""
    return "t-" + uuid.uuid4().hex[:8]


def _prepend_task(content: str, task_line: str) -> str:
    """Insert a new task line at the top of the task list.

    Finds the first ``- [ ]`` line and inserts before it,
    preserving any header/frontmatter above.  Falls back to
    appending if no existing task lines are found.
    """
    lines = content.split("\n")
    for i, line in enumerate(lines):
        if line.strip().startswith("- ["):
            lines.insert(i, task_line)
            return "\n".join(lines)
    # No existing tasks — append
    if content and not content.endswith("\n"):
        content += "\n"
    return content + task_line + "\n"


def _validate_task_text(task_text: str) -> str:
    """Validate and clean task text for the one-liner task format.

    Obsidian Tasks are single markdown lines (``- [ ] ...``).
    Multi-line text would corrupt the master task list.
    """
    if "\n" in task_text or "\r" in task_text:
        raise ValueError(
            "task_text must be a single line. Use the 'summary' parameter "
            "to attach detailed/multi-line content as a linked note."
        )
    return task_text.strip()


# ── Core engine ─────────────────────────────────────────────────


def _resolve_task_identity(
    task_id: str | None, description_match: str | None
) -> None:
    """Validate that at least one identifier is provided."""
    if not task_id and not description_match:
        raise ValueError("Must provide either task_id or description_match")


def _resolve_task_id_from_description(description_match: str) -> str | None:
    """Look up a task_id by description via the store (Slice E).

    Bridge-independent: queries ``store.search_by_description`` directly,
    so callers using ``description_match=`` get the atomic-write path
    (Slice C) automatically once we promote them to a task_id.

    Returns:
        task_id on a unique match, or None if zero matches OR multiple
        matches. Ambiguous matches are surfaced as None — callers can
        fall back to the file-scan engine which raises a structured
        ambiguity error.

    Pre-Slice-3 store rows may have NULL descriptions; those are
    filtered out by ``search_by_description`` so the file-scan
    fallback path picks them up.
    """
    if not description_match:
        return None
    try:
        rows = store.search_by_description(description_match, limit=2)
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning(
            "_resolve_task_id_from_description: store query failed: %s",
            exc,
        )
        return None
    if len(rows) != 1:
        return None
    return rows[0]["task_id"]


def _find_task_line(
    lines: list[str],
    task_id: str | None = None,
    description_match: str | None = None,
) -> tuple[int, str] | None:
    """Find a task line by ID or description substring.

    Returns (line_index, line_text) or None if not found.
    Raises ValueError on ambiguous description match.
    """
    if task_id:
        id_pattern = f"🆔 {task_id}"
        for i, line in enumerate(lines):
            if id_pattern in line:
                return (i, line)

    if description_match:
        lower = description_match.lower()
        matches = [(i, line) for i, line in enumerate(lines) if lower in line.lower()]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            previews = [m[1].strip()[:80] for m in matches[:5]]
            raise ValueError(
                f"Ambiguous match: '{description_match}' matched {len(matches)} lines. "
                f"Previews: {previews}"
            )

    return None


def _find_and_replace_task_line(
    file_path: str,
    task_id: str | None,
    description_match: str | None,
    transform_fn: Callable[[str], str],
) -> dict[str, Any]:
    """Core file mutation engine. Read file, find task, transform, write back.

    Slice C: when ``task_id`` is provided, the write goes through
    :func:`bridge.atomic_replace_line_by_task_id` — Obsidian's
    ``app.vault.process()`` atomic API — closing the read-modify-write
    race against concurrent user edits. The legacy
    ``bridge.read_file`` + ``bridge.write_file`` pair is the fallback
    when the atomic path fails for connectivity reasons OR when only
    ``description_match`` is provided (the atomic path needs an ID to
    locate the line in fresh content).

    Conflict semantics:
      - Atomic write detects ``conflict`` (user edited the line between
        our read and the write). On conflict, we re-read the fresh
        line, re-apply the transform, and retry the atomic write ONCE.
        If the second attempt still conflicts, we surface the conflict
        as a structured result (``success=False, message="..."``) — the
        caller decides whether to escalate.
      - The atomic write is preferred over the legacy path even when
        the file is not currently open in an editor (no downside —
        same disk write either way; up-side is correctness when the
        editor IS open).
    """
    _resolve_task_identity(task_id, description_match)

    # Slice E: if only description_match was given, try store-resolve
    # first. Promotes the call to a task_id-aware path (which can use
    # the atomic write of Slice C), and short-circuits the file scan
    # for tasks that the store knows about. Falls through to the
    # legacy scan below for store-NULL / ambiguous / unknown cases.
    if not task_id and description_match:
        store_resolved = _resolve_task_id_from_description(description_match)
        if store_resolved:
            task_id = store_resolved

    content = bridge.read_file(file_path)
    if content is None:
        return bridge_failure(f"Could not read {file_path}")

    lines = content.split("\n")
    found = _find_task_line(lines, task_id, description_match)

    if found is None:
        identifier = task_id or description_match
        return {"success": False, "message": f"Task not found: {identifier}"}

    idx, old_line = found
    new_line = transform_fn(old_line)

    if old_line == new_line:
        return {
            "success": True,
            "message": "No changes needed",
            "old_line": old_line.strip(),
            "new_line": new_line.strip(),
            "file": file_path,
            "line_number": idx + 1,
        }

    # Slice C: atomic path when task_id is known.
    if task_id:
        atomic_result = _atomic_write_with_conflict_retry(
            file_path=file_path,
            task_id=task_id,
            expected_old_line=old_line,
            new_line=new_line,
            transform_fn=transform_fn,
            initial_idx=idx,
        )
        if atomic_result is not None:
            return atomic_result
        # atomic_result is None → fall back to legacy below.
        logger.info(
            "atomic_write fell through for %s:%s — using legacy "
            "read-modify-write (race risk surfaced)",
            file_path, task_id,
        )

    # Legacy fallback path: read-modify-write via bridge.write_file.
    # Race-vulnerable; used only when (a) only description_match was
    # given, or (b) the atomic path was unreachable.
    lines[idx] = new_line
    new_content = "\n".join(lines)

    # Post-CP6: bridge.write_file raises typed ObsidianError on failure
    # (instead of returning False). The @bridge_retry decorator on the
    # caller catches transient subclasses (ObsidianTimeout,
    # ObsidianUnreachable) and retries; other types propagate to the
    # gateway's classifier.
    bridge.write_file(file_path, new_content)

    logger.info("Task line mutated (legacy path) in %s:%d", file_path, idx + 1)
    return {
        "success": True,
        "old_line": old_line.strip(),
        "new_line": new_line.strip(),
        "file": file_path,
        "line_number": idx + 1,
        "atomic": False,
    }


def _atomic_write_with_conflict_retry(
    *,
    file_path: str,
    task_id: str,
    expected_old_line: str,
    new_line: str,
    transform_fn: Callable[[str], str],
    initial_idx: int,
) -> dict[str, Any] | None:
    """Run the atomic write with a single conflict-retry attempt.

    Returns:
      - dict on success or definitive conflict (caller surfaces directly)
      - None if the atomic path itself failed for connectivity reasons —
        caller falls back to legacy read-modify-write.
    """
    from work_buddy.obsidian.errors import (
        ObsidianError,
        ObsidianPostWriteUncertain,
    )

    try:
        atomic = bridge.atomic_replace_line_by_task_id(
            file_path=file_path,
            task_id=task_id,
            expected_old_line=expected_old_line,
            new_line=new_line,
        )
    except ObsidianPostWriteUncertain:
        # The /eval timed out client-side after sending the body. The
        # vault state is uncertain — propagate so the gateway's
        # verify-then-decide path runs.
        raise
    except ObsidianError as exc:
        # Connectivity / plugin / refused — fall back to legacy.
        logger.info(
            "atomic_replace_line_by_task_id raised %s; falling back to legacy path",
            type(exc).__name__,
        )
        return None
    except RuntimeError as exc:
        # eval threw inside Obsidian — fall back to legacy.
        logger.warning(
            "atomic_replace_line_by_task_id JS error: %s; falling back to legacy path",
            exc,
        )
        return None

    if atomic.get("error") == "bridge_returned_none":
        return None

    if atomic.get("error") == "file_not_found":
        return {
            "success": False,
            "message": f"File not found in vault: {file_path}",
            "file": file_path,
        }

    if not atomic.get("found"):
        # Task no longer in the file (deleted between our read and the
        # atomic write). Surface as not-found rather than retrying.
        return {
            "success": False,
            "message": f"Task not found: {task_id}",
            "file": file_path,
        }

    if atomic.get("conflict"):
        # User edited the line between our read and the atomic write.
        # Re-read, re-apply the transform, retry once.
        fresh_old = atomic.get("old_line") or ""
        fresh_new = transform_fn(fresh_old)
        if fresh_old == fresh_new:
            # Transform is now a no-op against the fresh content.
            return {
                "success": True,
                "message": "No changes needed (after conflict-resolve)",
                "old_line": fresh_old.strip(),
                "new_line": fresh_new.strip(),
                "file": file_path,
                "line_number": atomic.get("line_number"),
                "atomic": True,
                "conflict_resolved": True,
            }
        try:
            retry = bridge.atomic_replace_line_by_task_id(
                file_path=file_path,
                task_id=task_id,
                expected_old_line=fresh_old,
                new_line=fresh_new,
            )
        except ObsidianError as exc:
            logger.warning(
                "atomic conflict-retry raised %s; falling back to legacy path",
                type(exc).__name__,
            )
            return None
        if retry.get("conflict"):
            # Two consecutive conflicts — escalate. The user is editing
            # the line concurrently; we shouldn't keep stomping.
            return {
                "success": False,
                "message": (
                    f"Concurrent edit detected on task line for {task_id}. "
                    f"User-edited the line during our atomic write retry. "
                    f"Try again."
                ),
                "file": file_path,
                "line_number": retry.get("line_number"),
                "old_line": (retry.get("old_line") or "").strip(),
                "atomic": True,
                "conflict": True,
            }
        if not retry.get("replaced"):
            # Retry didn't actually write — likely the line vanished or
            # transformed equality. Surface what we know.
            return {
                "success": True,
                "message": "No changes needed (after conflict-resolve)",
                "old_line": fresh_old.strip(),
                "new_line": fresh_new.strip(),
                "file": file_path,
                "line_number": retry.get("line_number"),
                "atomic": True,
                "conflict_resolved": True,
            }
        logger.info(
            "Atomic conflict-retry resolved for %s:%s",
            file_path, task_id,
        )
        return {
            "success": True,
            "old_line": fresh_old.strip(),
            "new_line": fresh_new.strip(),
            "file": file_path,
            "line_number": retry.get("line_number"),
            "atomic": True,
            "conflict_resolved": True,
        }

    if not atomic.get("replaced"):
        # Found, no conflict, no replace — line was already equal to
        # new_line on the fresh read.
        return {
            "success": True,
            "message": "No changes needed",
            "old_line": (atomic.get("old_line") or "").strip(),
            "new_line": new_line.strip(),
            "file": file_path,
            "line_number": atomic.get("line_number"),
            "atomic": True,
        }

    logger.info(
        "Task line mutated (atomic) in %s:%d",
        file_path, atomic.get("line_number") or initial_idx + 1,
    )
    return {
        "success": True,
        "old_line": (atomic.get("old_line") or "").strip(),
        "new_line": new_line.strip(),
        "file": file_path,
        "line_number": atomic.get("line_number") or (initial_idx + 1),
        "atomic": True,
    }


def _extract_task_id(line: str) -> str | None:
    """Extract the 🆔 task ID from a task line, if present."""
    m = TASK_ID_RE.search(line)
    return m.group(1) if m else None


def _strip_legacy_tags(line: str) -> str:
    """Remove legacy inline metadata tags from a line.

    Strips #tasker/state/*, #tasker/urgency/*, #tasker/complexity/*,
    and #tasker/noted — all now tracked in the SQLite store (note_uuid).
    """
    line = STATE_TAG_RE.sub("", line)
    line = URGENCY_TAG_RE.sub("", line)
    line = COMPLEXITY_TAG_RE.sub("", line)
    line = re.sub(r"\s*#tasker/noted\b", "", line)
    # Also strip urgency emojis that were paired with the tags
    line = URGENCY_EMOJI_RE.sub("", line)
    # Clean up double spaces
    line = re.sub(r"  +", " ", line).rstrip()
    return line


def _rewrite_namespace_tags(line: str, new_tags: list[str]) -> str:
    """Replace the set of namespace tags on a task line.

    Preserves: checkbox, leading text, `[[...|📓]]` wikilink, `#todo`,
    `#projects/<slug>`, `#tasker/*`, plugin emojis (🆔, 📅, ✅, priority).
    Strips: any other `#<tag>` on the line (i.e. existing user-supplied
    namespace tags) plus `#ns/...` and `#task/...` opt-in tokens.
    Inserts the new `#<tag>` list immediately before the `🆔` token (or
    at the end of the line if 🆔 is missing).
    """
    # Reserved tokens we never strip. Kept in sync with sync.py's
    # RESERVED_TAG_EXACT / RESERVED_TAG_PREFIXES: `wb/` is the canonical
    # user namespace prefix and must be rewritable, but the specific
    # inline-todo markers `wb/todo` and `wb/done` are preserved.
    def _is_preserved(tag: str) -> bool:
        tl = tag.lower()
        if tl in ("todo", "wb/todo", "wb/done"):
            return True
        for prefix in ("projects/", "tasker/"):
            if tl.startswith(prefix):
                return True
        return False

    # Walk the line tokenwise so we don't disturb wikilinks or emoji.
    tokens = line.split(" ")
    kept: list[str] = []
    for tok in tokens:
        # Strip stray trailing punctuation? Not needed — task lines are
        # space-separated by construction in create_task.
        if tok.startswith("#"):
            tag_body = tok[1:]
            if NAMESPACE_TAG_RE.match(tag_body) and not _is_preserved(tag_body):
                # This is a namespace-or-opt-in tag; drop it.
                continue
        kept.append(tok)

    # Insert new tags before the 🆔 token.
    validated = []
    for t in new_tags:
        t = t.strip().lstrip("#").strip()
        if not t:
            continue
        if not NAMESPACE_TAG_RE.match(t):
            raise ValueError(f"Tag {t!r} is malformed")
        validated.append(f"#{t}")

    # Dedupe tokens we're about to add in case caller passed dupes.
    seen: set[str] = set()
    dedup: list[str] = []
    for tok in validated:
        key = tok.lower()
        if key in seen:
            continue
        seen.add(key)
        dedup.append(tok)

    # Find 🆔 position in kept tokens.
    id_idx = -1
    for i, tok in enumerate(kept):
        if tok == "🆔":
            id_idx = i
            break
        if TASK_ID_RE.search(tok):
            # e.g. the 🆔 and ID got glued into a single token.
            id_idx = i
            break

    if id_idx >= 0:
        new_tokens = kept[:id_idx] + dedup + kept[id_idx:]
    else:
        new_tokens = kept + dedup

    # Collapse any runs of blank tokens we produced by dropping tags.
    new_tokens = [t for t in new_tokens if t != ""]

    return " ".join(new_tokens)


@bridge_retry()
def set_task_tags_on_line(
    task_id: str,
    namespace_tags: list[str],
) -> dict[str, Any]:
    """Replace the namespace tags on a task line in the master list.

    The task line's `#todo`, `#projects/<slug>`, `#tasker/*`, `#wb/*`,
    wikilink, 🆔, and plugin emojis are preserved. Existing user-namespace
    tags (anything else matching `#<tag>`) are stripped, and ``namespace_tags``
    are inserted before the 🆔 marker.

    After the markdown write, the ``task_tags`` cache for this task is
    refreshed from the new line to keep SQLite in sync immediately; the
    next ``task_sync`` will re-verify classification.
    """
    normalized = _normalize_tags(namespace_tags)

    def _transform(old: str) -> str:
        return _rewrite_namespace_tags(old, normalized)

    result = _find_and_replace_task_line(
        file_path=MASTER_TASK_FILE,
        task_id=task_id,
        description_match=None,
        transform_fn=_transform,
    )

    # Refresh the tag cache for this task from the new token list.
    if result.get("success"):
        try:
            # Seed as namespacey (user-supplied intent). task_sync will
            # reclassify on its next run.
            store.set_task_tags(task_id, [(t, True) for t in normalized])
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning(
                "set_task_tags_on_line: cache refresh failed for %s: %s",
                task_id, exc,
            )

    return result


# ── Public API ──────────────────────────────────────────────────


def verify_task(
    *,
    task_id: str | None = None,
    description_match: str | None = None,
) -> dict[str, Any]:
    """Verify a task exists in the Tasks plugin cache.

    Returns task details from the live cache. Also enriches with
    store metadata if the task has an ID in the store.
    """
    _resolve_task_identity(task_id, description_match)
    bridge.require_available()
    from work_buddy.obsidian.tasks.env import _load_js
    js = _load_js("get_task_line.js")
    js = js.replace("__TASK_ID__", _escape_js(task_id) if task_id else "")
    js = js.replace("__DESC_MATCH__", _escape_js(description_match) if description_match else "")
    result = bridge.eval_js(js, timeout=15)
    if result is None:
        return {"found": False, "reason": "eval_js returned None"}

    # Enrich with store metadata
    if result.get("found") and result.get("has_id"):
        tid = task_id or _extract_task_id(result.get("original_markdown", ""))
        if tid:
            meta = store.get(tid)
            if meta:
                result["store"] = meta

    return result


@requires_consent(
    operation="tasks.update_task",
    reason="Update task metadata in the work-buddy store.",
    risk="moderate",
    default_ttl=30,
)
@bridge_retry()
def update_task(
    *,
    task_id: str | None = None,
    description_match: str | None = None,
    state: str | None = None,
    urgency: str | None = None,
    complexity: str | None = None,
    contract: str | None = None,
    snooze_until: str | None = None,
    due_date: str | None = None,
    reason: str | None = None,
    file_path: str | None = None,
) -> dict[str, Any]:
    """Update task metadata — state, urgency, due date, contract, any combination.

    State, urgency, complexity, contract, snooze_until → stored in SQLite.
    Due date → written to the markdown file (plugin-owned emoji format).

    **Cannot set state='done'.** Use task_toggle for completion — it handles
    the checkbox, done date, and store state atomically with clean failure
    semantics when the bridge is down.

    Args:
        task_id: Task ID (e.g., 't-a3f8c1e2'). Preferred.
        description_match: Description substring. Fallback for tasks without IDs.
        state: New state — 'inbox', 'mit', 'focused', or 'snoozed'. NOT 'done'.
        urgency: New urgency — 'low', 'medium', or 'high'.
        complexity: New complexity — 'simple', 'moderate', 'complex', or None.
        contract: Contract slug this task serves, or None.
        snooze_until: ISO date to wake snoozed task, or None.
        due_date: Due date as 'YYYY-MM-DD' (written to file, plugin-owned).
        reason: Why the state is changing (recorded in history).
        file_path: Vault-relative path. Default: tasks/master-task-list.md.
    """
    _resolve_task_identity(task_id, description_match)

    # Completion state changes go through task_toggle, not here.
    if state == "done":
        return {
            "success": False,
            "message": (
                "task_change_state cannot set state='done' — use task_toggle instead. "
                "Example: wb_run(\"task_toggle\", {\"task_id\": \"<id>\", \"done\": true})"
            ),
        }

    has_store_update = any(v is not None for v in [state, urgency, complexity, contract, snooze_until])
    has_file_update = due_date is not None

    if not has_store_update and due_date is None:
        return {"success": False, "message": "No fields to update"}

    result: dict[str, Any] = {"success": True}

    # Slice E: try the store first to resolve description_match → task_id.
    # Bridge-independent and gives the atomic write path (Slice C) a
    # task_id to work with. Falls through to the legacy file-scan
    # below if the store has no unique hit (NULL description, ambiguous
    # match, or pre-Slice-3 row).
    if not task_id and description_match:
        task_id = _resolve_task_id_from_description(description_match)

    # Resolve task_id from file if still not known after store lookup
    # (legacy task with NULL description, or no store record at all).
    if not task_id:
        fp = file_path or MASTER_TASK_FILE
        content = bridge.read_file(fp)
        if content:
            found = _find_task_line(content.split("\n"), None, description_match)
            if found:
                task_id = _extract_task_id(found[1])

    # --- File updates FIRST (source of truth) ---
    fp = file_path or MASTER_TASK_FILE

    if due_date is not None:
        def set_due(line: str) -> str:
            if DUE_DATE_RE.search(line):
                return DUE_DATE_RE.sub(f"📅 {due_date}", line)
            return line.rstrip() + f" 📅 {due_date}"

        file_result = _find_and_replace_task_line(fp, task_id, description_match, set_due)
        result.update(file_result)

    # --- Store update AFTER file (store follows file) ---
    if has_store_update and task_id:
        store_kwargs: dict[str, Any] = {}
        if state is not None:
            store_kwargs["state"] = state
        if urgency is not None:
            store_kwargs["urgency"] = urgency
        if complexity is not None:
            store_kwargs["complexity"] = complexity
        if contract is not None:
            store_kwargs["contract"] = contract
        if snooze_until is not None:
            store_kwargs["snooze_until"] = snooze_until
        if reason:
            store_kwargs["reason"] = reason

        if store_kwargs:
            store_result = store.update(task_id, **store_kwargs)
            result["store_updated"] = store_result.get("changed", False)
        else:
            result["store_updated"] = False

    result["task_id"] = task_id
    return result



def _toggle_via_plugin_api(task_line: str, file_path: str) -> str | None:
    """Use Tasks plugin apiV1 to toggle. Returns toggled line or None."""
    try:
        escaped_line = _escape_js(task_line)
        escaped_path = _escape_js(file_path)
        result = _run_js(
            "toggle_via_api.js",
            {"__TASK_LINE__": escaped_line, "__FILE_PATH__": escaped_path},
            timeout=10,
        )
        if isinstance(result, dict) and result.get("success"):
            return result["toggled"]
        return None
    except Exception:
        return None


@requires_consent(
    operation="tasks.archive",
    reason="Move completed tasks from master list to archive file.",
    risk="moderate",
    default_ttl=15,
)
@bridge_retry()
def archive_completed(older_than_days: int = 0) -> dict[str, Any]:
    """Archive completed tasks from the master list to tasks/archive.md.

    Also marks tasks as archived in the store.
    """
    content = bridge.read_file(MASTER_TASK_FILE)
    if content is None:
        return bridge_failure(f"Could not read {MASTER_TASK_FILE}")

    lines = content.split("\n")
    today = date.today()

    keep_lines: list[str] = []
    archive_lines: list[str] = []
    archived_ids: list[str] = []

    for line in lines:
        if re.match(r"^- \[x\]", line):
            should_archive = True

            if older_than_days > 0:
                done_match = DONE_DATE_RE.search(line)
                if done_match:
                    done_str = done_match.group().replace("✅", "").strip()
                    try:
                        done_dt = date.fromisoformat(done_str)
                        should_archive = (today - done_dt).days >= older_than_days
                    except ValueError:
                        should_archive = True

            if should_archive:
                archive_lines.append(line)
                tid = _extract_task_id(line)
                if tid:
                    archived_ids.append(tid)
                continue

        keep_lines.append(line)

    if not archive_lines:
        return {"success": True, "archived_count": 0, "message": "No tasks to archive"}

    archive_header = f"\n## Archived {today.isoformat()}\n\n"
    archive_content = archive_header + "\n".join(archive_lines) + "\n"

    existing_archive = bridge.read_file(ARCHIVE_FILE)
    if existing_archive:
        new_archive = existing_archive.rstrip() + "\n" + archive_content
    else:
        new_archive = f"# Task Archive\n{archive_content}"

    # Post-CP6: bridge.write_file raises typed exceptions on failure.
    # The @bridge_retry decorator catches transients and retries the
    # whole function — note that this means the archive file may end
    # up with duplicated rows on retry (same risk as the pre-CP6 code:
    # both writes have to succeed for the result to be consistent).
    # Acceptable today; future hardening could move both writes into
    # an atomic markdown transaction if the bridge gains one.
    bridge.write_file(ARCHIVE_FILE, new_archive)

    new_master = "\n".join(keep_lines)
    bridge.write_file(MASTER_TASK_FILE, new_master)

    # Mark archived in store
    for tid in archived_ids:
        try:
            store.mark_archived(tid)
        except Exception:
            pass  # Best effort

    logger.info("Archived %d tasks to %s", len(archive_lines), ARCHIVE_FILE)
    return {
        "success": True,
        "archived_count": len(archive_lines),
        "remaining_count": len([l for l in keep_lines if l.strip().startswith("- [")]),
        "archive_file": ARCHIVE_FILE,
        "archived_ids": archived_ids,
    }


@requires_consent(
    operation="tasks.create_task",
    reason="Create a new task in the master task list.",
    risk="moderate",
    default_ttl=30,
)
@bridge_retry()
def create_task(
    task_text: str,
    urgency: str = "medium",
    project: str | None = None,
    due_date: str | None = None,
    contract: str | None = None,
    summary: str | None = None,
    tags: list[str] | None = None,
    *,
    # Slice 2 GTD vocabulary (optional; defaults match a legacy task) ---
    task_kind: str = "task",
    density: str = "sparse",
    outcome_text: str | None = None,
    next_action_text: str | None = None,
    definition_of_done: str | None = None,
    creation_effort: str = "developed",
    user_involvement: str = "high",
    creation_provenance: str = "manual",
    has_deadline: bool = False,
    deadline_date: str | None = None,
    has_dependency: bool = False,
    dependency_hint: str | None = None,
) -> dict[str, Any]:
    """Create a new task with an auto-generated ID, optionally with a linked note.

    If ``summary`` is provided, a note file is created and linked to the task.
    Metadata (state, urgency, contract, plus Slice 2 GTD vocabulary) goes
    to the SQLite store. The task line has: #todo, text, note link,
    #projects/*, user namespace tags, 🆔, plugin emojis.

    ``tags`` is a list of user-defined namespace tags (without leading '#'),
    e.g. ``["paper/ecg-classifier", "experiment/augmentation"]``. The tokens
    are appended to the task line before the 🆔 marker. They will be picked
    up by the next ``task_sync`` into the ``task_tags`` cache and classified
    according to the reserved-prefix / opt-in / discovery-threshold rules.

    Slice 2 args (kind / density / outcome_text / next_action_text /
    definition_of_done / creation_effort / user_involvement /
    creation_provenance / deadline / dependency) are optional and default
    to "looks like a legacy manually-authored task." Agent-driven creators
    should set ``creation_provenance`` (e.g.
    ``agent_inferred_from_journal``) and lower ``user_involvement``.

    This function is idempotent on retry: it checks for existing note files
    and task lines before writing, so the retry capability can safely replay it.
    """
    task_text = _validate_task_text(task_text)
    if urgency not in store.VALID_URGENCIES:
        raise ValueError(f"Invalid urgency {urgency!r}")
    if task_kind not in store.VALID_TASK_KINDS:
        raise ValueError(f"Invalid task_kind {task_kind!r}")
    if density not in store.VALID_DENSITIES:
        raise ValueError(f"Invalid density {density!r}")
    if creation_effort not in store.VALID_CREATION_EFFORTS:
        raise ValueError(f"Invalid creation_effort {creation_effort!r}")
    if user_involvement not in store.VALID_USER_INVOLVEMENTS:
        raise ValueError(f"Invalid user_involvement {user_involvement!r}")
    namespace_tags = _normalize_tags(tags)

    task_id = generate_task_id()
    note_uuid: str | None = None
    note_path: str | None = None

    # --- Note creation (optional) ---
    if summary:
        note_uuid = str(uuid.uuid4())
        note_path = f"{TASK_NOTES_DIR}/{note_uuid}.md"
        today = date.today().isoformat()

        template = bridge.read_file(TASK_NOTE_TEMPLATE)
        if template:
            note_content = template.replace("{{VALUE:Title}}", task_text)
            note_content = note_content.replace("created: 2025-10-08", f"created: {today}")
        else:
            note_content = (
                f"---\ntype: task-note\ncreated: {today}\nstatus: open\n---\n"
                f"# {task_text}\n\n## Summary\n{summary}\n\n"
                f"## Details\n\n## Action items\n\n## Artifacts & References\n"
            )

        if "A one-paragraph description." in note_content:
            note_content = note_content.replace("A one-paragraph description.", summary)

        note_tags = ["#todo"]
        if project:
            note_tags.append(f"#projects/{project}")
        for t in namespace_tags:
            note_tags.append(f"#{t}")
        note_content = note_content.replace(
            "---\n\n#", f"---\n{' '.join(note_tags)}\n\n#", 1
        )

        # Post-CP6: bridge.write_file raises typed exceptions on failure;
        # @bridge_retry catches transients and replays the whole function.
        bridge.write_file(note_path, note_content)

    # --- Task line ---
    parts = [f"- [ ] #todo {task_text}"]
    if note_uuid:
        parts.append(f"[[{note_uuid}|📓]]")
    if project:
        parts.append(f"#projects/{project}")
    for t in namespace_tags:
        parts.append(f"#{t}")
    parts.append(f"🆔 {task_id}")
    if due_date:
        parts.append(f"📅 {due_date}")
    task_line = " ".join(parts)

    content = bridge.read_file(MASTER_TASK_FILE)
    if content is None:
        return bridge_failure(f"Could not read {MASTER_TASK_FILE}")

    # Idempotent: skip prepend if task_id already present (retry safety)
    if task_id not in content:
        content = _prepend_task(content, task_line)
        # Post-CP6: bridge.write_file raises on failure (see above).
        bridge.write_file(MASTER_TASK_FILE, content)

    # --- Store record ---
    if store.get(task_id) is None:
        # Slice 3: derive the description from the just-built task line
        # so the store's text column is populated immediately. Without
        # this, the description would stay NULL until the next
        # task_sync run (~30 minute window). The line we built above is
        # authoritative for what the file now contains, so deriving from
        # it locally is consistent with the file-source-of-truth rule.
        derived_description = extract_description_from_line(task_line)
        store.create(
            task_id=task_id,
            state="inbox",
            urgency=urgency,
            contract=contract,
            note_uuid=note_uuid,
            task_kind=task_kind,
            density=density,
            outcome_text=outcome_text,
            next_action_text=next_action_text,
            definition_of_done=definition_of_done,
            creation_effort=creation_effort,
            user_involvement=user_involvement,
            creation_provenance=creation_provenance,
            has_deadline=has_deadline,
            deadline_date=deadline_date,
            has_dependency=has_dependency,
            dependency_hint=dependency_hint,
            description=derived_description,
        )

    # --- Seed tag cache ---
    # Mark user-supplied tags as namespacey by default (they were explicitly
    # provided). The projects/<slug> token, if any, is also cached but not
    # flagged as a namespace. The next task_sync will reclassify according
    # to the full rule set (reserved prefixes / discovery threshold).
    seed_tags: list[tuple[str, bool]] = []
    if project:
        seed_tags.append((f"projects/{project}", False))
    for t in namespace_tags:
        seed_tags.append((t, True))
    if seed_tags:
        try:
            store.set_task_tags(task_id, seed_tags)
        except Exception as exc:  # pragma: no cover — defensive; next sync heals
            logger.warning("create_task: failed to seed tag cache for %s: %s", task_id, exc)

    # --- Verify ---
    verified = _verify_task_creation(task_id, note_path)

    logger.info("Created task: %s (id=%s, verified=%s)", task_text[:60], task_id, verified)
    result: dict[str, Any] = {
        "success": True,
        "task_line": task_line,
        "task_id": task_id,
        "file": MASTER_TASK_FILE,
        "verified": verified,
    }
    if note_path:
        result["note_path"] = note_path
        result["note_uuid"] = note_uuid
    return result


def _verify_task_creation(task_id: str, note_path: str | None) -> dict[str, bool]:
    """Quick verification that all writes landed. Returns per-target status."""
    result: dict[str, bool] = {}

    # Check task line in master list
    master_content = bridge.read_file(MASTER_TASK_FILE)
    result["task_line"] = master_content is not None and task_id in master_content

    # Check store record
    result["store"] = store.get(task_id) is not None

    # Check note file (if applicable)
    if note_path:
        result["note"] = bridge.read_file(note_path) is not None

    return result


# ── Complete / Delete ─────────────────────────────────────────


@requires_consent(
    operation="tasks.toggle_task",
    reason="Toggle a task between TODO and DONE.",
    risk="moderate",
    default_ttl=30,
)
@bridge_retry()
def toggle_task(
    task_id: str,
    done: bool | None = None,
    file_path: str | None = None,
) -> dict[str, Any]:
    """Mark a task complete, incomplete, or toggle its current state.

    This is the single entry point for all completion state changes.
    Handles the checkbox, done date, and store state atomically.
    Returns clean failure when the bridge is down.

    Args:
        task_id: Task ID (e.g., 't-a3f8c1e2').
        done: If True, mark complete. If False, mark incomplete.
              If None (default), toggle the current state.
        file_path: Vault-relative path. Default: tasks/master-task-list.md.

    Uses the Tasks plugin API for the checkbox + done date,
    with regex fallback. Updates the store state accordingly.
    """
    fp = file_path or MASTER_TASK_FILE
    content = bridge.read_file(fp)
    if content is None:
        return bridge_failure(f"Could not read {fp}")

    lines = content.split("\n")
    result = _find_task_line(lines, task_id, None)
    if result is None:
        return {"success": False, "message": f"Task not found: {task_id}"}

    idx, old_line = result
    is_done = re.match(r"^- \[x\]", old_line) is not None

    # If `done` is specified and already matches current state, no-op.
    if done is True and is_done:
        return {
            "success": True,
            "task_id": task_id,
            "old_line": old_line.strip(),
            "new_line": old_line.strip(),
            "new_state": "done",
            "message": "Task is already complete",
        }
    if done is False and not is_done:
        return {
            "success": True,
            "task_id": task_id,
            "old_line": old_line.strip(),
            "new_line": old_line.strip(),
            "new_state": "inbox",
            "message": "Task is already incomplete",
        }

    # Toggle via plugin API, fall back to regex
    toggled = _toggle_via_plugin_api(old_line, fp)
    if toggled is None:
        if is_done:
            toggled = CHECKBOX_RE.sub(r"\g<1> \3", old_line)
            toggled = DONE_DATE_RE.sub("", toggled)
            toggled = re.sub(r"  +", " ", toggled).rstrip()
        else:
            toggled = CHECKBOX_RE.sub(r"\g<1>x\3", old_line)
            if "✅" not in toggled:
                toggled = toggled.rstrip() + f" ✅ {date.today().isoformat()}"

    lines[idx] = toggled
    # Post-CP6: bridge.write_file raises typed exceptions on failure;
    # @bridge_retry catches transients and replays.
    bridge.write_file(fp, "\n".join(lines))

    new_state = "done" if not is_done else "inbox"
    if store.get(task_id):
        store.update(task_id, state=new_state, reason="toggled")

    logger.info("Task toggled: %s → %s in %s:%d", task_id, new_state, fp, idx + 1)
    return {
        "success": True,
        "task_id": task_id,
        "old_line": old_line.strip(),
        "new_line": toggled.strip(),
        "file": fp,
        "line_number": idx + 1,
        "new_state": new_state,
    }


@requires_consent(
    operation="tasks.delete_task",
    reason="Permanently delete a task: remove line, note file, and store record.",
    risk="high",
    default_ttl=5,
)
@bridge_retry()
def delete_task(
    task_id: str,
) -> dict[str, Any]:
    """Permanently delete a task — line, note file, and store record.

    This is destructive and consent-gated. For normal workflow, use
    complete_task + archive instead.
    """
    removed: dict[str, bool] = {}

    # 1. Remove task line from master list
    content = bridge.read_file(MASTER_TASK_FILE)
    if content is not None:
        lines = content.split("\n")
        result = _find_task_line(lines, task_id, None)
        if result is not None:
            idx, _ = result
            del lines[idx]
            # Post-CP6: bridge.write_file raises on failure. delete_task
            # is best-effort across multiple sub-deletes (line, note,
            # store record) so we catch the typed exception here and
            # record the partial state rather than aborting the whole
            # function (which would leave the user with no signal about
            # which parts succeeded).
            try:
                bridge.write_file(MASTER_TASK_FILE, "\n".join(lines))
                removed["task_line"] = True
            except ObsidianError as exc:
                logger.error(
                    "delete_task: bridge.write_file failed for %s (%s)",
                    task_id, exc.error_kind,
                )
                removed["task_line"] = False
        else:
            removed["task_line"] = False
    else:
        removed["task_line"] = False

    # 2. Delete note file (if linked)
    meta = store.get(task_id)
    note_uuid = meta.get("note_uuid") if meta else None
    if note_uuid:
        note_path = f"{TASK_NOTES_DIR}/{note_uuid}.md"
        # Use eval_js to delete via Obsidian (bridge has no delete endpoint)
        try:
            from work_buddy.obsidian.bridge import eval_js
            js = (
                f'const f = app.vault.getAbstractFileByPath("{note_path}");'
                f'if (f) {{ await app.vault.delete(f); return "deleted"; }} else {{ return "not_found"; }}'
            )
            del_result = eval_js(js)
            removed["note"] = del_result == "deleted"
        except Exception:
            removed["note"] = False
    else:
        removed["note"] = False

    # 3. Delete store record — only if the file line was actually removed,
    #    otherwise task_sync will re-create the store record from the file.
    if removed["task_line"]:
        removed["store"] = store.delete(task_id)
    else:
        removed["store"] = False
        logger.warning(
            "delete_task: skipping store deletion for %s — file line not removed, "
            "store.delete would be undone by task_sync",
            task_id,
        )

    logger.info("Task deleted: %s (removed=%s)", task_id, removed)
    return {
        "success": removed["task_line"],
        "task_id": task_id,
        "removed": removed,
    }


@requires_consent(
    operation="tasks.update_task",
    reason="Rewrite the description text on a task line.",
    risk="moderate",
    default_ttl=30,
)
@bridge_retry()
def update_task_description(
    task_id: str,
    new_description: str,
    *,
    file_path: str | None = None,
) -> dict[str, Any]:
    """Rewrite the description text on a task line.

    Replaces the human-readable text portion of the task line — the
    span between ``#todo`` and the first structural marker (wikilink,
    hashtag, plugin emoji). All structural tokens are preserved:
    checkbox state, ``#todo``, ``#projects/*``, namespace tags,
    wikilinks (including the task-note link ``[[uuid|📓]]``), 🆔 + ID,
    plugin emojis (📅 due date, ✅ done date, 🔼/⏫ urgency).

    The SQLite store's description column is updated in lockstep — file
    first, store second (same ordering as ``update_task``). If the file
    write fails, the store is not touched.

    This capability exists to give agents a safe way to rewrite task
    text without filesystem-direct ``Edit`` on master-task-list.md, which
    is the read-modify-write race that Slice C addresses. Once Slice C
    ships, this routes through the atomic ``app.vault.process()`` path
    automatically; pre-Slice-C, it goes through the same
    ``_find_and_replace_task_line`` engine that the other mutations
    use.

    Args:
        task_id: Task ID (e.g., 't-a3f8c1e2'). Required.
        new_description: New description text. Whitespace is collapsed
            to single spaces; newlines are stripped (task lines are
            single-line by construction).
        file_path: Vault-relative path. Default:
            ``tasks/master-task-list.md``.

    Returns:
        Dict with ``success``, ``task_id``, ``old_description``,
        ``new_description``, ``file``, ``line_number``, and
        ``store_updated`` keys.

    Caveat: if the *current* description on the line contains a ``#``
    (e.g. issue references like "fix #123") or ``[[``, the rewrite
    boundary detection will treat that as a metadata token. The new
    description still ends up in the description position; the
    "metadata" portion that follows just contains those tokens. In
    practice this is benign — the task line still parses correctly and
    the next ``task_sync`` reclassifies cleanly.
    """
    if not task_id:
        raise ValueError("task_id is required for update_task_description")

    cleaned = (new_description or "").strip()
    if not cleaned:
        return {
            "success": False,
            "task_id": task_id,
            "message": "new_description must not be empty after stripping",
        }

    # Reject newlines — task lines are single-line by construction.
    if "\n" in cleaned or "\r" in cleaned:
        # The replace helper would already collapse these; fail loudly
        # so the caller knows their multiline input got flattened.
        return {
            "success": False,
            "task_id": task_id,
            "message": (
                "new_description must be a single line. Use the linked "
                "task-note for multi-line / detailed content."
            ),
        }

    fp = file_path or MASTER_TASK_FILE

    captured_old: dict[str, str] = {}

    def _transform(old_line: str) -> str:
        captured_old["line"] = old_line
        captured_old["description"] = extract_description_from_line(old_line)
        return replace_description_in_line(old_line, cleaned)

    file_result = _find_and_replace_task_line(
        file_path=fp,
        task_id=task_id,
        description_match=None,
        transform_fn=_transform,
    )

    if not file_result.get("success"):
        return file_result

    # Update the store description to match. File-first, store-second
    # mirrors update_task's ordering: if the file write failed we'd
    # have returned above; if it succeeded we keep the store consistent.
    store_updated = False
    try:
        if store.get(task_id) is not None:
            update_result = store.update(
                task_id,
                description=cleaned,
                reason="task_update_description",
            )
            store_updated = bool(update_result.get("changed"))
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning(
            "update_task_description: store update failed for %s: %s",
            task_id, exc,
        )

    logger.info(
        "Description updated: %s (%r -> %r)",
        task_id,
        captured_old.get("description", ""),
        cleaned,
    )

    response: dict[str, Any] = {
        "success": True,
        "task_id": task_id,
        "old_description": captured_old.get("description", ""),
        "new_description": cleaned,
        "file": file_result.get("file", fp),
        "line_number": file_result.get("line_number"),
        "old_line": file_result.get("old_line"),
        "new_line": file_result.get("new_line"),
        "store_updated": store_updated,
    }
    # Surface Slice-C provenance flags so callers/tests can tell
    # which write path landed (atomic vs legacy fallback) and whether
    # a conflict was resolved.
    if "atomic" in file_result:
        response["atomic"] = file_result["atomic"]
    if file_result.get("conflict_resolved"):
        response["conflict_resolved"] = True
    if file_result.get("message"):
        response["message"] = file_result["message"]
    return response


def strip_legacy_tags_from_line(line: str) -> str:
    """Public helper: strip #tasker/state/*, #tasker/urgency/*, #tasker/complexity/*
    from a task line. Used during migration of old tasks."""
    return _strip_legacy_tags(line)


def _load_task_payload(task_id: str) -> dict[str, Any]:
    """Pure read: resolve task metadata, line, and linked note content.

    Returns the same read-only fields that read_task/assign_task surface,
    with no session-tracking write and no state mutation. On missing
    store record, returns ``{"success": False, "message": ...}``.
    """
    meta = store.get(task_id)
    if meta is None:
        return {
            "success": False,
            "message": f"Task {task_id} has no store record (pre-store legacy task?)",
        }

    # Try plugin cache for task details, fall back to file scan
    task_text = ""
    original_markdown = ""
    file_path = MASTER_TASK_FILE
    line_number = None

    try:
        task_info = verify_task(task_id=task_id)
        if task_info.get("found"):
            task_text = task_info.get("description", "")
            original_markdown = task_info.get("original_markdown", "")
            file_path = task_info.get("file_path", MASTER_TASK_FILE)
            line_number = task_info.get("line_number")
    except Exception:
        pass  # Bridge or plugin unavailable — fall back below

    # Fallback: scan the markdown file directly
    if not task_text:
        content = bridge.read_file(MASTER_TASK_FILE)
        # Filesystem fallback when the bridge is down/flaky — mirrors the note-read path
        if content is None:
            from pathlib import Path
            from work_buddy.config import load_config
            fs_path = Path(load_config()["vault_root"]) / MASTER_TASK_FILE
            if fs_path.exists():
                content = fs_path.read_text(encoding="utf-8")
                logger.info("Read master task list via filesystem fallback: %s", MASTER_TASK_FILE)
        if content:
            found = _find_task_line(content.split("\n"), task_id=task_id)
            if found:
                idx, line = found
                original_markdown = line.strip()
                line_number = idx + 1
                task_text = extract_description_from_line(line)

    # Read note if one exists
    note_path = None
    note_content = None
    if meta.get("note_uuid"):
        note_path = f"{TASK_NOTES_DIR}/{meta['note_uuid']}.md"
        note_content = bridge.read_file(note_path)
        # Fallback: direct filesystem read if bridge unavailable
        if note_content is None:
            from pathlib import Path
            from work_buddy.config import load_config
            fs_path = Path(load_config()["vault_root"]) / note_path
            if fs_path.exists():
                note_content = fs_path.read_text(encoding="utf-8")
                logger.info("Read task note via filesystem fallback: %s", note_path)

    return {
        "success": True,
        "task_id": task_id,
        "task_text": task_text,
        "original_markdown": original_markdown,
        "file": file_path,
        "line_number": line_number,
        "state": meta["state"],
        "urgency": meta["urgency"],
        "complexity": meta.get("complexity"),
        "contract": meta.get("contract"),
        "note_path": note_path,
        "note_content": note_content,
        "assigned_sessions": store.get_sessions(task_id),
    }


@bridge_retry()
def read_task(task_id: str) -> dict[str, Any]:
    """Read a task's full context without claiming it.

    Returns task text, metadata, and linked note content. Does NOT record
    a session assignment — use ``assign_task`` when you intend to claim
    the task for the current session.
    """
    return _load_task_payload(task_id)


@bridge_retry()
def assign_task(task_id: str) -> dict[str, Any]:
    """Claim a task for the current agent session and return full context.

    Composes ``_load_task_payload`` with a session-tracker write. Returns
    everything the agent needs to start working plus the claiming session_id.
    """
    from work_buddy.agent_session import _get_session_id

    payload = _load_task_payload(task_id)
    if not payload.get("success"):
        return payload

    session_id = _get_session_id()
    store.assign_session(task_id, session_id)

    # Refresh the session list so the caller sees their own claim
    payload["assigned_sessions"] = store.get_sessions(task_id)
    payload["session_id"] = session_id
    return payload
