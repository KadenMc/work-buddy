"""Transitional home for the legacy ``prune_*`` callables.

These functions were previously defined directly in
``work_buddy/artifacts.py`` and are referenced by string in
``work_buddy/paths.py:PRUNERS``. They remain importable from
:mod:`work_buddy.artifacts` (re-exported from the package's
``__init__.py``) so the existing string-based registry keeps working.

Each function will be replaced in Phase D of the artifact-system
unification by an :class:`Artifact` registration in the consumer
module, after which this module can be removed entirely (Phase G).
Until then, ``run_pruners`` walks ``paths.PRUNERS`` and dispatches
into the callables here.
"""

from __future__ import annotations

import importlib
import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from work_buddy.paths import data_dir


def run_pruners(dry_run: bool = True) -> list[dict[str, Any]]:
    """Execute all registered pruners from ``paths.PRUNERS``.

    Each pruner is imported lazily and called with the resolved file
    path and its default config. Errors are captured into the returned
    result list rather than raised, so one failing pruner doesn't abort
    the whole sweep.
    """
    from work_buddy.paths import PRUNERS, resolve

    results: list[dict[str, Any]] = []
    for resource_id, (callable_path, default_config) in PRUNERS.items():
        # Resolve path: try RESOURCES registry first, fall back to data_dir
        try:
            path = resolve(resource_id)
        except KeyError:
            # Not a registered singleton — treat as a data_dir category
            # (e.g. "agents/sessions" → data_dir("agents"))
            category = resource_id.split("/")[0]
            path = data_dir(category)
        if not path.exists():
            results.append({"resource": resource_id, "skipped": "path not found"})
            continue

        # Import the pruner function lazily
        module_path, func_name = callable_path.rsplit(".", 1)
        try:
            mod = importlib.import_module(module_path)
            prune_fn = getattr(mod, func_name)
        except (ImportError, AttributeError) as exc:
            results.append({"resource": resource_id, "error": str(exc)})
            continue

        try:
            result = prune_fn(path, default_config, dry_run=dry_run)
            result["resource"] = resource_id
            results.append(result)
        except Exception as exc:
            results.append({"resource": resource_id, "error": str(exc)})

    return results


# ---------------------------------------------------------------------------
# Per-resource pruner functions
# ---------------------------------------------------------------------------


def prune_escalation_log(
    path: Path, config: dict[str, Any], *, dry_run: bool = True
) -> dict[str, Any]:
    """Prune LLM escalation records older than the rolling window.

    The escalation log (``<data_root>/logs/escalations.log``) is JSONL —
    one record per resolved LLM job, keyed by ``timestamp``.

    Tolerates malformed lines: anything that doesn't parse as JSON is
    preserved verbatim through the prune (defensive against partial-line
    writes). Tolerates lines without ``timestamp``: kept regardless.
    """
    window_days = config.get("window_days", 30)
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=window_days)
    ).isoformat(timespec="milliseconds")

    if not path.exists():
        return {"pruned": 0, "remaining": 0, "bytes_before": 0, "bytes_after": 0}

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {"pruned": 0, "remaining": 0, "bytes_before": 0, "bytes_after": 0}

    bytes_before = path.stat().st_size

    kept_lines: list[str] = []
    pruned_count = 0
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            # Preserve malformed lines verbatim.
            kept_lines.append(line)
            continue
        ts = rec.get("timestamp", "")
        if ts and ts < cutoff:
            pruned_count += 1
            continue
        kept_lines.append(line)

    if pruned_count == 0:
        return {
            "pruned": 0,
            "remaining": len(kept_lines),
            "bytes_before": bytes_before,
            "bytes_after": bytes_before,
        }

    if not dry_run:
        new_text = "\n".join(kept_lines) + ("\n" if kept_lines else "")
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(new_text, encoding="utf-8")
        temp.replace(path)
        bytes_after = path.stat().st_size
    else:
        bytes_after = len("\n".join(kept_lines).encode("utf-8"))

    return {
        "pruned": pruned_count,
        "remaining": len(kept_lines),
        "bytes_before": bytes_before,
        "bytes_after": bytes_after,
    }


def prune_chrome_ledger(
    path: Path, config: dict[str, Any], *, dry_run: bool = True
) -> dict[str, Any]:
    """Prune chrome tab ledger snapshots older than the rolling window."""
    window_days = config.get("window_days", 7)
    cutoff = (datetime.now() - timedelta(days=window_days)).isoformat()

    try:
        raw = path.read_text(encoding="utf-8")
        snapshots = json.loads(raw)
    except (json.JSONDecodeError, OSError):
        return {"pruned": 0, "remaining": 0, "bytes_before": 0, "bytes_after": 0}

    # Handle both list format and {"snapshots": [...]} format
    if isinstance(snapshots, dict):
        snapshot_list = snapshots.get("snapshots", [])
    elif isinstance(snapshots, list):
        snapshot_list = snapshots
    else:
        return {"pruned": 0, "remaining": 0, "bytes_before": 0, "bytes_after": 0}

    bytes_before = path.stat().st_size
    kept = [s for s in snapshot_list if s.get("captured_at", "") >= cutoff]
    pruned_count = len(snapshot_list) - len(kept)

    if pruned_count == 0:
        return {
            "pruned": 0,
            "remaining": len(kept),
            "bytes_before": bytes_before,
            "bytes_after": bytes_before,
        }

    if not dry_run:
        new_data = json.dumps(kept, ensure_ascii=False)
        temp = path.with_suffix(".tmp")
        temp.write_text(new_data, encoding="utf-8")
        temp.replace(path)
        bytes_after = path.stat().st_size
    else:
        bytes_after = len(json.dumps(kept, ensure_ascii=False).encode("utf-8"))

    return {
        "pruned": pruned_count,
        "remaining": len(kept),
        "bytes_before": bytes_before,
        "bytes_after": bytes_after,
    }


def prune_llm_cache(
    path: Path, config: dict[str, Any], *, dry_run: bool = True
) -> dict[str, Any]:
    """Prune expired entries from the LLM result cache.

    Each entry has an ``expires_at`` ISO timestamp. Entries past expiry
    are removed.
    """
    try:
        raw = path.read_text(encoding="utf-8")
        cache = json.loads(raw)
    except (json.JSONDecodeError, OSError):
        return {"pruned": 0, "remaining": 0, "bytes_before": 0, "bytes_after": 0}

    if not isinstance(cache, dict):
        return {"pruned": 0, "remaining": 0, "bytes_before": 0, "bytes_after": 0}

    bytes_before = path.stat().st_size
    now = datetime.now()
    kept: dict[str, Any] = {}
    pruned_count = 0

    for task_id, entry in cache.items():
        expires_at = entry.get("expires_at", "")
        if expires_at:
            try:
                # Boundary-inclusive (<=) — see t-96e45c67.
                if datetime.fromisoformat(expires_at) <= now:
                    pruned_count += 1
                    continue
            except ValueError:
                pass
        kept[task_id] = entry

    if pruned_count == 0:
        return {
            "pruned": 0,
            "remaining": len(kept),
            "bytes_before": bytes_before,
            "bytes_after": bytes_before,
        }

    if not dry_run:
        new_data = json.dumps(kept, ensure_ascii=False)
        temp = path.with_suffix(".tmp")
        temp.write_text(new_data, encoding="utf-8")
        temp.replace(path)
        bytes_after = path.stat().st_size
    else:
        bytes_after = len(json.dumps(kept, ensure_ascii=False).encode("utf-8"))

    return {
        "pruned": pruned_count,
        "remaining": len(kept),
        "bytes_before": bytes_before,
        "bytes_after": bytes_after,
    }


def prune_stale_sessions(
    path: Path, config: dict[str, Any], *, dry_run: bool = True
) -> dict[str, Any]:
    """Remove agent session directories that haven't been active recently.

    A session is stale if its ``manifest.json`` ``created_at`` is older
    than ``max_age_days`` AND no file in the directory has been modified
    within that window.
    """
    max_age_days = config.get("max_age_days", 14)
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)

    if not path.is_dir():
        return {"pruned": 0, "remaining": 0, "bytes_freed": 0}

    pruned = 0
    remaining = 0
    bytes_freed = 0
    pruned_dirs: list[str] = []

    for entry in path.iterdir():
        if not entry.is_dir():
            continue
        # Skip non-session directories (consent/, operations/, logs/, etc.)
        manifest = entry / "manifest.json"
        if not manifest.exists():
            continue

        # Check creation time from manifest
        try:
            meta = json.loads(manifest.read_text(encoding="utf-8"))
            created_str = meta.get("created_at", "")
            created = datetime.fromisoformat(created_str)
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
        except (json.JSONDecodeError, ValueError, OSError):
            remaining += 1
            continue

        if created >= cutoff:
            remaining += 1
            continue

        # Check if any file was modified recently (activity = not stale)
        latest_mod = created
        for f in entry.rglob("*"):
            if f.is_file():
                try:
                    mtime = datetime.fromtimestamp(
                        f.stat().st_mtime, tz=timezone.utc
                    )
                    if mtime > latest_mod:
                        latest_mod = mtime
                except OSError:
                    continue

        if latest_mod >= cutoff:
            remaining += 1
            continue

        # Session is stale — calculate size and optionally remove
        dir_size = sum(
            f.stat().st_size for f in entry.rglob("*") if f.is_file()
        )
        pruned_dirs.append(entry.name)
        bytes_freed += dir_size
        pruned += 1

        if not dry_run:
            shutil.rmtree(entry, ignore_errors=True)

    return {
        "pruned": pruned,
        "remaining": remaining,
        "bytes_freed": bytes_freed,
        "pruned_sessions": pruned_dirs,
    }


def prune_old_logs(
    path: Path, config: dict[str, Any], *, dry_run: bool = True
) -> dict[str, Any]:
    """Delete log files older than ``max_age_days``."""
    max_age_days = config.get("max_age_days", 7)
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)

    if not path.is_dir():
        return {"pruned": 0, "remaining": 0, "bytes_freed": 0}

    pruned = 0
    remaining = 0
    bytes_freed = 0
    pruned_files: list[str] = []

    for f in path.rglob("*"):
        if not f.is_file():
            continue
        try:
            mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
        except OSError:
            remaining += 1
            continue

        if mtime >= cutoff:
            remaining += 1
            continue

        size = f.stat().st_size
        pruned_files.append(f.name)
        bytes_freed += size
        pruned += 1

        if not dry_run:
            f.unlink(missing_ok=True)

    return {
        "pruned": pruned,
        "remaining": remaining,
        "bytes_freed": bytes_freed,
        "pruned_files": pruned_files,
    }


def prune_messages_db(
    path: Path, config: dict[str, Any], *, dry_run: bool = True
) -> dict[str, Any]:
    """Delete terminal-status messages older than ``ttl_days`` from messages.db.

    Status guard (denylist): preserves ``pending`` rows. Cleans orphaned
    ``message_reads`` in the same transaction. Runs ``VACUUM`` after a
    live delete to actually reclaim bytes.
    """
    import sqlite3

    ttl_days = config.get("ttl_days", 30)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=ttl_days)).isoformat()
    bytes_before = path.stat().st_size if path.exists() else 0

    conn = sqlite3.connect(str(path))
    try:
        candidate_sql = (
            "SELECT COUNT(*) FROM messages "
            "WHERE created_at < ? "
            "AND status IS NOT NULL AND status != 'pending'"
        )
        candidate_count = conn.execute(candidate_sql, (cutoff,)).fetchone()[0]

        if dry_run or candidate_count == 0:
            return {
                "pruned": candidate_count,
                "remaining": -1,
                "bytes_before": bytes_before,
                "bytes_after": bytes_before,
            }

        cur = conn.execute(
            "DELETE FROM messages "
            "WHERE created_at < ? "
            "AND status IS NOT NULL AND status != 'pending'",
            (cutoff,),
        )
        n = cur.rowcount
        conn.execute(
            "DELETE FROM message_reads "
            "WHERE message_id NOT IN (SELECT id FROM messages)"
        )
        conn.commit()
        conn.execute("VACUUM")
    finally:
        conn.close()

    bytes_after = path.stat().st_size if path.exists() else 0
    return {
        "pruned": n,
        "remaining": -1,
        "bytes_before": bytes_before,
        "bytes_after": bytes_after,
    }


def prune_claude_code_usage_db(
    path: Path, config: dict[str, Any], *, dry_run: bool = True
) -> dict[str, Any]:
    """Roll up old `turns` rows in the Claude Code usage DB.

    Delegates to :func:`work_buddy.llm.claude_code_usage.rollup.rollup_old_turns`.
    """
    import sqlite3

    from work_buddy.llm.claude_code_usage.rollup import rollup_old_turns

    days = config.get("days_to_keep_full", 90)
    bytes_before = path.stat().st_size if path.exists() else 0
    if not path.exists():
        return {
            "rollup_groups": 0,
            "rolled_turns": 0,
            "bytes_before": 0,
            "bytes_after": 0,
        }
    conn = sqlite3.connect(str(path))
    try:
        result = rollup_old_turns(conn, days_to_keep_full=days, dry_run=dry_run)
    finally:
        conn.close()
    bytes_after = path.stat().st_size if path.exists() else 0
    return {
        **result,
        "bytes_before": bytes_before,
        "bytes_after": bytes_after,
    }


# Legacy-name alias kept for any tests/code that called the original
# private name on the module.
_run_pruners = run_pruners
