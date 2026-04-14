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
    """Core file mutation engine. Read file, find task, transform, write back."""
    _resolve_task_identity(task_id, description_match)

    content = bridge.read_file(file_path)
    if content is None:
        return {"success": False, "message": f"Could not read {file_path}"}

    lines = content.split("\n")
    result = _find_task_line(lines, task_id, description_match)

    if result is None:
        identifier = task_id or description_match
        return {"success": False, "message": f"Task not found: {identifier}"}

    idx, old_line = result
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

    lines[idx] = new_line
    new_content = "\n".join(lines)

    success = bridge.write_file(file_path, new_content)
    if not success:
        return {"success": False, "message": f"Failed to write {file_path}"}

    logger.info("Task line mutated in %s:%d", file_path, idx + 1)
    return {
        "success": True,
        "old_line": old_line.strip(),
        "new_line": new_line.strip(),
        "file": file_path,
        "line_number": idx + 1,
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
    Checkbox state → updated in markdown if state transitions to/from 'done'.

    Args:
        task_id: Task ID (e.g., 't-a3f8c1e2'). Preferred.
        description_match: Description substring. Fallback for tasks without IDs.
        state: New state — 'inbox', 'mit', 'focused', 'snoozed', or 'done'.
        urgency: New urgency — 'low', 'medium', or 'high'.
        complexity: New complexity — 'simple', 'moderate', 'complex', or None.
        contract: Contract slug this task serves, or None.
        snooze_until: ISO date to wake snoozed task, or None.
        due_date: Due date as 'YYYY-MM-DD' (written to file, plugin-owned).
        reason: Why the state is changing (recorded in history).
        file_path: Vault-relative path. Default: tasks/master-task-list.md.
    """
    _resolve_task_identity(task_id, description_match)

    has_store_update = any(v is not None for v in [state, urgency, complexity, contract, snooze_until])
    has_file_update = due_date is not None or state in ("done", None)

    if not has_store_update and due_date is None:
        return {"success": False, "message": "No fields to update"}

    result: dict[str, Any] = {"success": True}

    # Resolve task_id from file if only description_match given
    if not task_id:
        fp = file_path or MASTER_TASK_FILE
        content = bridge.read_file(fp)
        if content:
            found = _find_task_line(content.split("\n"), None, description_match)
            if found:
                task_id = _extract_task_id(found[1])

    # --- File updates FIRST (source of truth) ---
    # Update the file before the store so that if the file write fails,
    # the store stays consistent and task_sync won't revert the change.
    fp = file_path or MASTER_TASK_FILE
    file_update_failed = False

    if due_date is not None:
        def set_due(line: str) -> str:
            if DUE_DATE_RE.search(line):
                return DUE_DATE_RE.sub(f"📅 {due_date}", line)
            return line.rstrip() + f" 📅 {due_date}"

        file_result = _find_and_replace_task_line(fp, task_id, description_match, set_due)
        result.update(file_result)
        if not file_result.get("success"):
            file_update_failed = True

    if state == "done":
        # Check the checkbox and add done date via plugin API or regex
        def mark_done(line: str) -> str:
            toggled = _toggle_via_plugin_api(line, fp)
            if toggled and re.match(r"^- \[x\]", toggled):
                return toggled
            # Fallback: regex
            line = CHECKBOX_RE.sub(r"\g<1>x\3", line)
            if "✅" not in line:
                line = line.rstrip() + f" ✅ {date.today().isoformat()}"
            return line

        # Only toggle if currently unchecked
        content = bridge.read_file(fp)
        if content:
            found = _find_task_line(content.split("\n"), task_id, description_match)
            if found and not re.match(r"^- \[x\]", found[1]):
                file_result = _find_and_replace_task_line(fp, task_id, description_match, mark_done)
                result.update(file_result)
                if not file_result.get("success"):
                    file_update_failed = True

    elif state is not None and state != "done":
        # If transitioning FROM done, uncheck the checkbox
        content = bridge.read_file(fp)
        if content:
            found = _find_task_line(content.split("\n"), task_id, description_match)
            if found and re.match(r"^- \[x\]", found[1]):
                def uncheck(line: str) -> str:
                    line = CHECKBOX_RE.sub(r"\g<1> \3", line)
                    line = DONE_DATE_RE.sub("", line)
                    return re.sub(r"  +", " ", line).rstrip()

                file_result = _find_and_replace_task_line(fp, task_id, description_match, uncheck)
                result.update(file_result)
                if not file_result.get("success"):
                    file_update_failed = True

    # --- Store update AFTER file (store follows file) ---
    # If a checkbox toggle failed in the file, don't update the store state
    # — otherwise task_sync will revert the store to match the file, making
    # the completion appear to "not stick". Other store fields (urgency,
    # complexity, contract) have no file counterpart and can be updated
    # regardless.
    if has_store_update and task_id:
        store_kwargs: dict[str, Any] = {}
        if state is not None:
            if file_update_failed:
                logger.error(
                    "update_task: file write failed for state=%s on %s — "
                    "skipping store state update to stay consistent with file",
                    state, task_id,
                )
            else:
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


@requires_consent(
    operation="tasks.toggle_completion",
    reason="Toggle task completion status.",
    risk="moderate",
    default_ttl=30,
)
def toggle_completion(
    *,
    task_id: str | None = None,
    description_match: str | None = None,
    file_path: str | None = None,
) -> dict[str, Any]:
    """Toggle a task between TODO and DONE.

    Uses the Tasks plugin's apiV1.executeToggleTaskDoneCommand() for the
    checkbox and done date. Updates the store state accordingly.
    """
    _resolve_task_identity(task_id, description_match)

    fp = file_path or MASTER_TASK_FILE
    content = bridge.read_file(fp)
    if content is None:
        return {"success": False, "message": f"Could not read {fp}"}

    lines = content.split("\n")
    result = _find_task_line(lines, task_id, description_match)
    if result is None:
        identifier = task_id or description_match
        return {"success": False, "message": f"Task not found: {identifier}"}

    idx, old_line = result
    is_done = re.match(r"^- \[x\]", old_line) is not None

    # Use plugin API for the toggle
    toggled_line = _toggle_via_plugin_api(old_line, fp)
    method = "apiV1"

    if toggled_line is None:
        # Fallback: regex
        method = "regex_fallback"
        if is_done:
            toggled_line = CHECKBOX_RE.sub(r"\g<1> \3", old_line)
            toggled_line = DONE_DATE_RE.sub("", toggled_line)
            toggled_line = re.sub(r"  +", " ", toggled_line).rstrip()
        else:
            toggled_line = CHECKBOX_RE.sub(r"\g<1>x\3", old_line)
            if "✅" not in toggled_line:
                toggled_line = toggled_line.rstrip() + f" ✅ {date.today().isoformat()}"

    lines[idx] = toggled_line
    new_content = "\n".join(lines)
    if not bridge.write_file(fp, new_content):
        return {"success": False, "message": f"Failed to write {fp}"}

    # Update store
    resolved_id = task_id or _extract_task_id(old_line)
    new_state = "done" if not is_done else "inbox"
    if resolved_id and store.get(resolved_id):
        store.update(resolved_id, state=new_state, reason="toggled")

    logger.info("Task toggled (%s) in %s:%d", method, fp, idx + 1)
    return {
        "success": True,
        "old_line": old_line.strip(),
        "new_line": toggled_line.strip(),
        "file": fp,
        "line_number": idx + 1,
        "method": method,
        "new_state": new_state,
    }


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
def archive_completed(older_than_days: int = 0) -> dict[str, Any]:
    """Archive completed tasks from the master list to tasks/archive.md.

    Also marks tasks as archived in the store.
    """
    content = bridge.read_file(MASTER_TASK_FILE)
    if content is None:
        return {"success": False, "message": f"Could not read {MASTER_TASK_FILE}"}

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

    if not bridge.write_file(ARCHIVE_FILE, new_archive):
        return {"success": False, "message": f"Failed to write {ARCHIVE_FILE}"}

    new_master = "\n".join(keep_lines)
    if not bridge.write_file(MASTER_TASK_FILE, new_master):
        return {
            "success": False,
            "message": f"Archived to {ARCHIVE_FILE} but failed to update master list!",
        }

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
def create_task(
    task_text: str,
    urgency: str = "medium",
    project: str | None = None,
    due_date: str | None = None,
    contract: str | None = None,
    summary: str | None = None,
) -> dict[str, Any]:
    """Create a new task with an auto-generated ID, optionally with a linked note.

    If ``summary`` is provided, a note file is created and linked to the task.
    Metadata (state, urgency, contract) goes to the SQLite store.
    The task line has only: #todo, text, note link, #projects/*, 🆔, plugin emojis.

    This function is idempotent on retry: it checks for existing note files
    and task lines before writing, so wb_retry can safely replay it.
    """
    task_text = _validate_task_text(task_text)
    if urgency not in store.VALID_URGENCIES:
        raise ValueError(f"Invalid urgency {urgency!r}")

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
                f"## Details\n\n## Subtasks\n- [ ] Step 1\n\n## Artifacts & References\n"
            )

        if "A one-paragraph description." in note_content:
            note_content = note_content.replace("A one-paragraph description.", summary)

        tags = ["#todo"]
        if project:
            tags.append(f"#projects/{project}")
        note_content = note_content.replace(
            "---\n\n#", f"---\n{' '.join(tags)}\n\n#", 1
        )

        if not bridge.write_file(note_path, note_content):
            return {
                "success": False,
                "message": f"Failed to write note: {note_path}",
                "bridge": bridge.get_latency_context(),
            }

    # --- Task line ---
    parts = [f"- [ ] #todo {task_text}"]
    if note_uuid:
        parts.append(f"[[{note_uuid}|📓]]")
    if project:
        parts.append(f"#projects/{project}")
    parts.append(f"🆔 {task_id}")
    if due_date:
        parts.append(f"📅 {due_date}")
    task_line = " ".join(parts)

    content = bridge.read_file(MASTER_TASK_FILE)
    if content is None:
        return {
            "success": False,
            "message": f"Could not read {MASTER_TASK_FILE}",
            "bridge": bridge.get_latency_context(),
        }

    # Idempotent: skip prepend if task_id already present (retry safety)
    if task_id not in content:
        content = _prepend_task(content, task_line)
        if not bridge.write_file(MASTER_TASK_FILE, content):
            return {"success": False, "message": f"Failed to write {MASTER_TASK_FILE}"}

    # --- Store record ---
    if store.get(task_id) is None:
        store.create(
            task_id=task_id,
            state="inbox",
            urgency=urgency,
            contract=contract,
            note_uuid=note_uuid,
        )

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
def toggle_task(
    task_id: str,
) -> dict[str, Any]:
    """Toggle a task between TODO and DONE by task ID.

    Uses the Tasks plugin API for the toggle (checkbox + done date),
    with regex fallback. Updates the store state accordingly.
    """
    content = bridge.read_file(MASTER_TASK_FILE)
    if content is None:
        return {"success": False, "message": f"Could not read {MASTER_TASK_FILE}"}

    lines = content.split("\n")
    result = _find_task_line(lines, task_id, None)
    if result is None:
        return {"success": False, "message": f"Task not found: {task_id}"}

    idx, old_line = result
    is_done = re.match(r"^- \[x\]", old_line) is not None

    # Toggle via plugin API, fall back to regex
    toggled = _toggle_via_plugin_api(old_line, MASTER_TASK_FILE)
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
    if not bridge.write_file(MASTER_TASK_FILE, "\n".join(lines)):
        return {"success": False, "message": f"Failed to write {MASTER_TASK_FILE}"}

    new_state = "done" if not is_done else "inbox"
    if store.get(task_id):
        store.update(task_id, state=new_state, reason="toggled")

    logger.info("Task toggled: %s → %s", task_id, new_state)
    return {
        "success": True,
        "task_id": task_id,
        "old_line": old_line.strip(),
        "new_line": toggled.strip(),
        "new_state": new_state,
    }


@requires_consent(
    operation="tasks.delete_task",
    reason="Permanently delete a task: remove line, note file, and store record.",
    risk="high",
    default_ttl=5,
)
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
            if bridge.write_file(MASTER_TASK_FILE, "\n".join(lines)):
                removed["task_line"] = True
            else:
                logger.error("delete_task: bridge.write_file failed for %s", task_id)
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


def strip_legacy_tags_from_line(line: str) -> str:
    """Public helper: strip #tasker/state/*, #tasker/urgency/*, #tasker/complexity/*
    from a task line. Used during migration of old tasks."""
    return _strip_legacy_tags(line)


def assign_task(task_id: str) -> dict[str, Any]:
    """Claim a task for the current agent session and return full context.

    Records the session against the task (idempotent), then returns
    everything the agent needs to start working: task text, metadata,
    note content (if any), and note file path.

    Uses the plugin cache for task details when available, falls back
    to the markdown file directly when the cache is cold.
    """
    from work_buddy.agent_session import _get_session_id

    session_id = _get_session_id()

    # Get store metadata first — this is the hard requirement
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
        if content:
            found = _find_task_line(content.split("\n"), task_id=task_id)
            if found:
                idx, line = found
                original_markdown = line.strip()
                line_number = idx + 1
                # Extract description: strip checkbox, tags, emojis
                desc = re.sub(r"^- \[.\]\s*", "", line)
                desc = re.sub(r"#\S+", "", desc)
                desc = re.sub(r"\[\[[^\]]+\]\]", "", desc)
                desc = re.sub(r"[🆔📅✅🔼⏫]\s*\S*", "", desc)
                task_text = re.sub(r"\s+", " ", desc).strip()

    # Record session assignment (idempotent)
    store.assign_session(task_id, session_id)

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

    # Get all assigned sessions
    sessions = store.get_sessions(task_id)

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
        "assigned_sessions": sessions,
        "session_id": session_id,
    }
