"""Dashboard API — data aggregation from work-buddy subsystems.

Each ``get_*`` function returns a JSON-serializable dict/list.
Data sources:
    - ``sidecar_state.json`` for service health and job state
    - Obsidian Tasks for task summaries
    - Agent session manifests for session info
    - Contract markdown files for contract summaries
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from collections import deque
from urllib.error import URLError
from urllib.request import Request, urlopen

from work_buddy.config import load_config
from work_buddy.paths import data_dir, resolve

logger = logging.getLogger(__name__)

_cfg = load_config()

_REPO_ROOT = Path(__file__).parent.parent.parent
_STATE_FILE = resolve("runtime/sidecar-state")
from work_buddy.contracts import get_contracts_dir

_CONTRACTS_DIR = get_contracts_dir()
_AGENTS_DIR = data_dir("agents")

# Rolling bridge latency samples (kept in dashboard process memory)
_BRIDGE_HISTORY: deque[dict[str, Any]] = deque(maxlen=60)  # ~30min at 30s refresh


_PROBE_REFRESH_INTERVAL = 60  # seconds between tool probe refreshes
_last_probe_refresh: float = 0.0


def _maybe_refresh_probes() -> None:
    """Re-run tool probes if stale (>60s since last refresh).

    This runs in the dashboard process, not the MCP server. It writes
    fresh results to tool_status.json so the HealthEngine picks them up.
    Probes are lightweight HTTP/TCP checks (~0.5-1s total).
    """
    global _last_probe_refresh
    now = time.time()
    if now - _last_probe_refresh < _PROBE_REFRESH_INTERVAL:
        return
    _last_probe_refresh = now
    try:
        from work_buddy.tools import _register_default_probes, probe_all
        _register_default_probes()
        probe_all(force=True)
    except Exception as exc:
        logger.debug("Probe refresh failed: %s", exc)


def _read_sidecar_state() -> dict[str, Any]:
    """Read sidecar_state.json, return empty dict on failure."""
    try:
        if _STATE_FILE.exists():
            return json.loads(_STATE_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Failed to read sidecar state: %s", exc)
    return {}


_BRIDGE_GAP_THRESHOLD = 300  # 5 minutes — reset history if gap exceeds this


def get_bridge_status() -> dict[str, Any]:
    """Ping the Obsidian bridge and track latency."""
    bridge_port = _cfg.get("obsidian", {}).get("bridge_port", 27125)
    url = f"http://127.0.0.1:{bridge_port}/health"
    ts = time.time()

    # Reset history if there's been a long gap (e.g., laptop sleep)
    if _BRIDGE_HISTORY and (ts - _BRIDGE_HISTORY[-1]["ts"]) > _BRIDGE_GAP_THRESHOLD:
        _BRIDGE_HISTORY.clear()
    try:
        req = Request(url)
        with urlopen(req, timeout=5) as resp:
            latency_ms = round((time.time() - ts) * 1000, 1)
            data = json.loads(resp.read().decode())
            sample = {
                "ts": ts,
                "status": "healthy",
                "latency_ms": latency_ms,
                "vault": data.get("vault", ""),
                "plugin_version": data.get("version", ""),
            }
            _BRIDGE_HISTORY.append(sample)
            return _bridge_stats(sample)
    except (TimeoutError, OSError, URLError) as exc:
        latency_ms = round((time.time() - ts) * 1000, 1)
        err_type = "timeout" if isinstance(exc, TimeoutError) or "timed out" in str(exc) else "unreachable"
        sample = {"ts": ts, "status": err_type, "latency_ms": latency_ms}
        _BRIDGE_HISTORY.append(sample)
        return _bridge_stats(sample)
    except Exception as exc:
        sample = {"ts": ts, "status": "error", "latency_ms": 0, "error": str(exc)}
        _BRIDGE_HISTORY.append(sample)
        return {**sample, "samples": len(_BRIDGE_HISTORY), "history": []}


def _bridge_stats(sample: dict[str, Any]) -> dict[str, Any]:
    """Compute EMA, trend direction, and history from bridge samples."""
    # Skip the first sample after dashboard restart — cold-start artifact
    samples = list(_BRIDGE_HISTORY)
    usable = samples[1:] if len(samples) > 1 else samples
    latencies = [s["latency_ms"] for s in usable]

    # Fast EMA (α=0.18, ~10-sample half-life) — recent trend
    # Slow EMA (α=0.06, ~30-sample half-life) — baseline
    ema_fast = latencies[0]
    ema_slow = latencies[0]
    for v in latencies[1:]:
        ema_fast = 0.18 * v + 0.82 * ema_fast
        ema_slow = 0.06 * v + 0.94 * ema_slow

    # Trend: compare fast vs slow EMA
    # >30% above baseline = worsening, >30% below = improving, else stable
    if len(latencies) < 5 or ema_slow < 1:
        trend = "stable"
    elif ema_fast > ema_slow * 1.3:
        trend = "up"  # worsening
    elif ema_fast < ema_slow * 0.7:
        trend = "down"  # improving
    else:
        trend = "stable"

    return {
        **sample,
        "ema_ms": round(ema_fast, 1),
        "max_ms": round(max(latencies), 1),
        "trend": trend,
        "samples": len(latencies),
        "history": [
            {"ts": s["ts"], "ms": s["latency_ms"], "ok": s["status"] == "healthy"}
            for s in usable
        ],
    }


_LEDGER_FILE = resolve("chrome/ledger")
_CHROME_SNAPSHOT_INTERVAL = 5 * 60  # 5 minutes


def get_chrome_status() -> dict[str, Any]:
    """Chrome extension health derived from ledger freshness."""
    try:
        if not _LEDGER_FILE.exists():
            return {"status": "unreachable", "last_snapshot": None, "snapshot_count": 0}

        data = json.loads(_LEDGER_FILE.read_text(encoding="utf-8"))
        snapshots = data if isinstance(data, list) else data.get("snapshots", [])
        if not snapshots:
            return {"status": "unreachable", "last_snapshot": None, "snapshot_count": 0}

        # Find latest snapshot
        latest = max(snapshots, key=lambda s: s.get("captured_at", ""))
        latest_ts = latest.get("captured_at", "")
        tab_count = latest.get("tab_count", len(latest.get("tabs", [])))

        # Compute age
        try:
            from datetime import datetime, timezone
            dt = datetime.fromisoformat(latest_ts.replace("Z", "+00:00"))
            age_seconds = (datetime.now(timezone.utc) - dt).total_seconds()
        except Exception:
            age_seconds = float("inf")

        # Derive status from age relative to snapshot interval
        if age_seconds < _CHROME_SNAPSHOT_INTERVAL * 2:  # < 10 min
            status = "healthy"
        elif age_seconds < _CHROME_SNAPSHOT_INTERVAL * 6:  # < 30 min
            status = "stale"
        else:
            status = "unreachable"

        return {
            "status": status,
            "last_snapshot": latest_ts,
            "age_seconds": round(age_seconds),
            "snapshot_count": len(snapshots),
            "tab_count": tab_count,
        }
    except Exception as exc:
        logger.warning("Failed to read Chrome ledger status: %s", exc)
        return {"status": "error", "last_snapshot": None, "snapshot_count": 0, "error": str(exc)}


def _describe_cron(expr: str) -> str:
    """Human-readable cron description, falling back to the raw expression."""
    try:
        from cron_descriptor import get_description
        return get_description(expr)
    except Exception:
        return expr


def get_system_state() -> dict[str, Any]:
    """Aggregated system snapshot: services, jobs, uptime."""
    state = _read_sidecar_state()
    if not state:
        return {"status": "unavailable", "services": {}, "jobs": [], "uptime_seconds": 0}

    started = state.get("started_at", 0)
    uptime = time.time() - started if started else 0

    # Enrich jobs with human-readable schedule descriptions
    jobs = state.get("jobs", [])
    for job in jobs:
        if isinstance(job, dict) and job.get("schedule"):
            job["schedule_desc"] = _describe_cron(job["schedule"])

    result = {
        "status": "running",
        "pid": state.get("pid", 0),
        "uptime_seconds": round(uptime),
        "last_tick_at": state.get("last_tick_at", 0),
        "exclusion_active": state.get("exclusion_active", False),
        "read_only": _cfg.get("dashboard", {}).get("read_only", False),
        "services": state.get("services", {}),
        "jobs": jobs,
        "events": state.get("events", []),
        "bridge": get_bridge_status(),
        "chrome": get_chrome_status(),
    }

    # Refresh tool probes periodically (60s TTL) so the health view
    # stays current even though the MCP server only probes at startup.
    _maybe_refresh_probes()

    # Unified health view (merges tool probes + sidecar state)
    try:
        from work_buddy.health.engine import HealthEngine
        engine = HealthEngine()
        result["health"] = engine.get_all()
    except Exception as exc:
        logger.warning("Failed to build health view: %s", exc)
        result["health"] = None

    # Feature preferences (what the user wants/doesn't want)
    try:
        from work_buddy.health.preferences import load_preferences
        prefs = load_preferences()
        result["preferences"] = {
            cid: p.to_dict() for cid, p in prefs.items()
        }
    except Exception as exc:
        logger.warning("Failed to load preferences: %s", exc)
        result["preferences"] = {}

    # Requirements summary (configuration-time validation)
    try:
        from work_buddy.health.requirements import RequirementChecker
        checker = RequirementChecker()
        bootstrap = checker.check_bootstrap()
        all_reqs = checker.check_all(include_unwanted=False)
        result["requirements"] = {
            "bootstrap": checker.summarize(bootstrap),
            "all": checker.summarize(all_reqs),
        }
    except Exception as exc:
        logger.warning("Failed to check requirements: %s", exc)
        result["requirements"] = None

    return result


def get_tasks_summary() -> dict[str, Any]:
    """Task summary from Obsidian Tasks.

    Reads the master task list directly — avoids bridge dependency
    so the dashboard stays operational even when Obsidian is closed.
    """
    vault_root = _cfg.get("vault_root", "")
    if not vault_root:
        return []
    tasks_file = Path(vault_root) / "tasks" / "master-task-list.md"
    tasks: list[dict[str, Any]] = []

    try:
        if not tasks_file.exists():
            return {"tasks": [], "counts": {}}

        # Patterns for stripping metadata from display text
        _note_link_re = re.compile(r"\[\[([0-9a-f-]+)\|[^\]]*\]\]")
        _tag_re = re.compile(r"#\S+")
        # Obsidian Tasks emoji: dated (📅⏳🛫✅❌➕) and dateless (⏫🔼🔽⏬🔺)
        _emoji_dated_re = re.compile(r"([📅⏳🛫✅❌➕])\s*(\d{4}-\d{2}-\d{2})")
        _emoji_plain_re = re.compile(r"[⏫🔼🔽⏬🔺]")
        _priority_labels = {
            "⏫": "Highest",
            "🔼": "High",
            "🔽": "Low",
            "⏬": "Lowest",
        }
        _task_id_re = re.compile(r"t-[0-9a-f]+")
        _emoji_labels = {
            "📅": "Due",
            "⏳": "Scheduled",
            "🛫": "Start",
            "✅": "Done",
            "❌": "Cancelled",
            "➕": "Created",
        }

        for line in tasks_file.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped.startswith("- ["):
                continue
            done = stripped[3] == "x"
            full_text = stripped[6:].strip()  # after "- [x] " or "- [ ] "

            # Extract dated emoji markers from full line BEFORE splitting
            markers = [
                {"emoji": em.group(1), "label": _emoji_labels.get(em.group(1), ""), "date": em.group(2)}
                for em in _emoji_dated_re.finditer(full_text)
            ]
            # Extract Obsidian Tasks priority emojis (undated)
            for ch in _emoji_plain_re.findall(full_text):
                if ch in _priority_labels:
                    markers.append({"emoji": ch, "label": _priority_labels[ch], "date": ""})

            text = full_text

            # Extract task ID if present (strip trailing emoji metadata)
            task_id = ""
            if "🆔 " in text:
                parts = text.split("🆔 ")
                id_part = parts[-1].strip()
                m_id = _task_id_re.match(id_part)
                task_id = m_id.group(0) if m_id else id_part
                text = parts[0].strip()

            # Extract note UUID from [[uuid|📓]] link
            note_id = ""
            m = _note_link_re.search(text)
            if m:
                note_id = m.group(1)

            # Extract urgency emoji
            urgency = "none"
            for emoji, level in [("🔺", "high"), ("🔼", "medium"), ("🔽", "low")]:
                if emoji in text:
                    urgency = level
                    break

            # Extract state tag
            state = "inbox"
            for tag in ["#todo/focused", "#todo/next", "#todo/waiting", "#todo/someday", "#todo/blocked"]:
                if tag in text:
                    state = tag.split("/")[-1]
                    break
            if done:
                state = "done"

            # Clean display text: strip note links, tags, emoji metadata
            display = _note_link_re.sub("", text)
            display = _tag_re.sub("", display)
            display = _emoji_dated_re.sub("", display)
            display = _emoji_plain_re.sub("", display)
            display = re.sub(r"\s{2,}", " ", display).strip()

            tasks.append({
                "id": task_id,
                "text": display[:120],
                "markers": markers,
                "note_id": note_id,
                "done": done,
                "state": state,
                "urgency": urgency,
            })
    except Exception as exc:
        logger.warning("Failed to read tasks: %s", exc)
        return {"tasks": [], "counts": {}, "error": str(exc)}

    # Count by state
    counts: dict[str, int] = {}
    for t in tasks:
        s = t["state"]
        counts[s] = counts.get(s, 0) + 1

    return {"tasks": tasks, "counts": counts}


def get_sessions_summary() -> dict[str, Any]:
    """Summary of agent session directories."""
    sessions: list[dict[str, Any]] = []

    try:
        if not _AGENTS_DIR.exists():
            return {"sessions": []}

        for session_dir in sorted(_AGENTS_DIR.iterdir(), reverse=True):
            if not session_dir.is_dir():
                continue
            manifest_file = session_dir / "manifest.json"
            if not manifest_file.exists():
                continue

            try:
                manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
                sessions.append({
                    "session_id": manifest.get("session_id", session_dir.name),
                    "created_at": manifest.get("created_at", ""),
                    "task_id": manifest.get("assigned_task", {}).get("task_id", ""),
                    "task_text": manifest.get("assigned_task", {}).get("task_text", "")[:80],
                })
            except Exception:
                sessions.append({
                    "session_id": session_dir.name,
                    "created_at": "",
                    "task_id": "",
                    "task_text": "",
                })
    except Exception as exc:
        logger.warning("Failed to read sessions: %s", exc)
        return {"sessions": [], "error": str(exc)}

    return {"sessions": sessions[:20]}  # cap at 20 most recent


def _format_chat_duration(start: str | None, end: str | None) -> str:
    """Format a human-readable duration from ISO timestamps."""
    if not start or not end:
        return ""
    try:
        from datetime import datetime as _dt
        t0 = _dt.fromisoformat(start.replace("Z", "+00:00"))
        t1 = _dt.fromisoformat(end.replace("Z", "+00:00"))
        total_s = int((t1 - t0).total_seconds())
        if total_s < 60:
            return f"{total_s}s"
        mins = total_s // 60
        if mins < 60:
            return f"{mins}m"
        return f"{mins // 60}h {mins % 60}m"
    except Exception:
        return ""


def _resolve_repo_name(project_slug: str, fallback_name: str) -> str:
    """Resolve a project slug to the parent repo name.

    Resolves subdirectory sessions (e.g. ``my-project-feature-x`` →
    ``my-project``) via the canonical project name resolver.
    Falls back to *fallback_name* if the resolver is unavailable.
    """
    if not project_slug:
        return fallback_name
    try:
        from work_buddy.collectors.chat_collector import project_name_from_slug
        return project_name_from_slug(project_slug)
    except Exception:
        return fallback_name


def get_chats_summary(days: int = 14) -> dict[str, Any]:
    """Rich chat list from Claude Code JSONL sessions.

    Calls the chat_collector parser which caches per-file to avoid
    re-parsing unchanged JSONL files.
    """
    try:
        from work_buddy.collectors.chat_collector import _get_claude_code_conversations
    except ImportError as exc:
        logger.warning("chat_collector unavailable: %s", exc)
        return {"chats": [], "total": 0, "error": str(exc)}

    try:
        raw = _get_claude_code_conversations(days)
    except Exception as exc:
        logger.warning("Failed to load chats: %s", exc)
        return {"chats": [], "total": 0, "error": str(exc)}

    chats: list[dict[str, Any]] = []
    for c in raw:
        tool_names = c.get("tool_names", {})
        top_tools = [
            name for name, _ in sorted(tool_names.items(), key=lambda x: x[1], reverse=True)[:3]
        ]
        msg_count = c.get("user_msg_count", 0) + c.get("assistant_text_count", 0)
        chats.append({
            "session_id": c.get("full_session_id", c.get("session_id", "")),
            "short_id": c.get("session_id", "")[:8],
            "first_message": (c.get("first_user_message") or "")[:120],
            "message_count": msg_count,
            "tool_count": c.get("tool_use_count", 0),
            "top_tools": top_tools,
            "start_time": c.get("start_time"),
            "end_time": c.get("end_time"),
            "duration": _format_chat_duration(c.get("start_time"), c.get("end_time")),
            "project_name": _resolve_repo_name(
                c.get("project_slug", ""), c.get("project_name", "")
            ),
        })

    # Sort by start_time descending (most recent first)
    chats.sort(key=lambda x: x.get("start_time") or "", reverse=True)

    return {"chats": chats, "total": len(chats)}


def get_contracts_summary() -> dict[str, Any]:
    """Active contract summaries from contracts/ directory."""
    contracts: list[dict[str, Any]] = []

    try:
        if not _CONTRACTS_DIR.exists():
            return {"contracts": []}

        for md_file in sorted(_CONTRACTS_DIR.glob("*.md")):
            if md_file.name.startswith("_"):
                continue  # skip templates

            try:
                content = md_file.read_text(encoding="utf-8")
                # Parse YAML frontmatter
                if content.startswith("---"):
                    end = content.index("---", 3)
                    frontmatter = content[3:end].strip()
                    contract: dict[str, Any] = {"file": md_file.name}

                    for line in frontmatter.splitlines():
                        if ":" in line:
                            key, val = line.split(":", 1)
                            contract[key.strip()] = val.strip().strip('"').strip("'")

                    # Only include active contracts
                    status = contract.get("status", "active")
                    if status in ("active", "in-progress", "stalled"):
                        contracts.append({
                            "file": md_file.name,
                            "title": contract.get("title", md_file.stem),
                            "status": status,
                            "type": contract.get("type", ""),
                            "deadline": contract.get("deadline", ""),
                            "priority": contract.get("priority", ""),
                        })
            except Exception:
                continue
    except Exception as exc:
        logger.warning("Failed to read contracts: %s", exc)
        return {"contracts": [], "error": str(exc)}

    return {"contracts": contracts}


# ---------------------------------------------------------------------------
# Command palette
# ---------------------------------------------------------------------------

def _matches_filter(command_id: str, filter_cfg: dict) -> bool:
    """Check if a command ID passes the filter configuration.

    Patterns ending in ``*`` match via startswith; exact strings match exactly.
    """
    mode = filter_cfg.get("mode", "allowlist")
    patterns: list[str] = filter_cfg.get("patterns", [])
    if not patterns:
        return True  # no filter = allow all

    matched = any(
        (command_id.startswith(p[:-1]) if p.endswith("*") else command_id == p)
        for p in patterns
    )
    return matched if mode == "allowlist" else not matched


def _obsidian_commands(cfg: dict) -> list[dict]:
    """Fetch Obsidian commands, filtered by config."""
    try:
        from work_buddy.obsidian.commands import ObsidianCommands

        vault_root = Path(cfg.get("vault_root", ""))
        if not vault_root.exists():
            return []
        cmds = ObsidianCommands(vault_root)
        if not cmds.is_available():
            return []

        filter_cfg = cfg.get("command_palette", {}).get("obsidian_filter", {})
        results = []
        for cmd in cmds.list_commands():
            cid = cmd.get("id", "")
            if not _matches_filter(cid, filter_cfg):
                continue
            # Derive category from command ID prefix (e.g. "editor:toggle-bold" → "editor")
            category = cid.split(":")[0] if ":" in cid else "general"
            results.append({
                "id": f"obsidian::{cid}",
                "name": cmd.get("name", cid),
                "provider": "obsidian",
                "category": category,
                "description": "",
                "has_params": False,
                "parameters": {},
                "command_type": "inline",
            })
        return results
    except Exception as exc:
        logger.warning("Failed to fetch Obsidian commands: %s", exc)
        return []


def _workbuddy_commands(cfg: dict) -> list[dict]:
    """Fetch work-buddy capabilities from the MCP registry."""
    try:
        from work_buddy.mcp_server.registry import (
            Capability,
            WorkflowDefinition,
            get_registry,
        )

        wb_cfg = cfg.get("command_palette", {}).get("workbuddy_filter", {})
        exclude_cats: list[str] = wb_cfg.get("exclude_categories", [])
        exclude_workflows: bool = wb_cfg.get("exclude_workflows", False)

        results = []
        for name, entry in get_registry().items():
            if isinstance(entry, WorkflowDefinition):
                if exclude_workflows:
                    continue
                results.append({
                    "id": f"work-buddy::{name}",
                    "name": name,
                    "provider": "work-buddy",
                    "category": "workflow",
                    "description": entry.description,
                    "has_params": False,
                    "parameters": {},
                    "command_type": "workflow",
                    "slash_command": entry.slash_command,
                })
            elif isinstance(entry, Capability):
                if entry.category in exclude_cats:
                    continue
                has_params = bool(entry.parameters)
                results.append({
                    "id": f"work-buddy::{name}",
                    "name": name,
                    "provider": "work-buddy",
                    "category": entry.category,
                    "description": entry.description,
                    "has_params": has_params,
                    "parameters": entry.parameters if entry.parameters else {},
                    "command_type": "parameterized" if has_params else "inline",
                    "slash_command": entry.slash_command,
                })
        return results
    except Exception as exc:
        logger.warning("Failed to fetch work-buddy capabilities: %s", exc)
        return []


def get_palette_commands(cfg: dict) -> dict:
    """Aggregate commands from all providers."""
    obs = _obsidian_commands(cfg)
    wb = _workbuddy_commands(cfg)
    return {
        "commands": obs + wb,
        "providers": {
            "obsidian": {"available": len(obs) > 0, "count": len(obs)},
            "work-buddy": {"available": len(wb) > 0, "count": len(wb)},
        },
    }
