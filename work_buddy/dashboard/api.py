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
import threading
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

# Snapshot cache for get_system_state(). The build runs a requirement
# sweep (and reads probe results) — work that can take 20s+ when an
# optional dependency is offline and several requirement checks stack up
# their timeouts. The endpoint serves the cached snapshot and refreshes
# it on a background thread once stale, so no request after the first
# ever pays that cost.
_STATE_CACHE_TTL = 5.0  # seconds before a served snapshot is refreshed
_state_cache: dict[str, Any] | None = None
_state_cache_ts: float = 0.0
_state_cache_lock = threading.Lock()
_state_refreshing = False


def _maybe_refresh_probes() -> None:
    """Kick off a tool-probe refresh in the background if stale (>60s).

    Runs in the dashboard process (not the MCP server), writing fresh
    results to tool_status.json for the HealthEngine to pick up.
    ``probe_all()`` can stall for ~10s when an optional service (e.g. the
    Obsidian bridge) is offline, so it runs on a background thread — the
    request never waits on it; the next request sees the fresh data.
    """
    global _last_probe_refresh
    now = time.time()
    if now - _last_probe_refresh < _PROBE_REFRESH_INTERVAL:
        return
    # Claim the slot before spawning so a burst of requests starts only
    # one refresh thread.
    _last_probe_refresh = now

    def _refresh() -> None:
        try:
            from work_buddy.tools import _register_default_probes, probe_all
            _register_default_probes()
            probe_all(force=True)
        except Exception as exc:
            logger.debug("Probe refresh failed: %s", exc)

    threading.Thread(target=_refresh, name="probe-refresh", daemon=True).start()


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
            # `status` lets the sparkline renderer visually distinguish
            # "unreachable" (port closed — Obsidian not running) from
            # "timeout" (port open, bridge hung — Obsidian lagging).
            # Previously both were conflated under a single bar-fail
            # class, so you couldn't tell from the graph whether your
            # spike was "closed the app" or "something's slow."
            {"ts": s["ts"], "ms": s["latency_ms"],
             "ok": s["status"] == "healthy",
             "status": s["status"]}
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


def _kick_state_refresh() -> None:
    """Rebuild the system-state snapshot on a background thread.

    Single-flight: a refresh already in progress is not duplicated.
    Keeps the slow ``_build_system_state`` off the request path entirely
    once the cache is warm.
    """
    global _state_refreshing
    with _state_cache_lock:
        if _state_refreshing:
            return
        _state_refreshing = True

    def _refresh() -> None:
        global _state_cache, _state_cache_ts, _state_refreshing
        try:
            fresh = _build_system_state()
            _state_cache = fresh
            _state_cache_ts = time.time()
        except Exception as exc:
            logger.warning("Background system-state refresh failed: %s", exc)
        finally:
            _state_refreshing = False

    threading.Thread(
        target=_refresh, name="system-state-refresh", daemon=True,
    ).start()


def get_system_state() -> dict[str, Any]:
    """Aggregated system snapshot, served from a background-refreshed cache.

    ``_build_system_state`` runs a requirement sweep and reads probe
    results — work that can take 20s+ when an optional dependency is
    offline. Serving the (possibly slightly stale) cached snapshot and
    refreshing it on a background thread keeps that cost off every
    request after the first. Real-time changes still arrive via SSE —
    this endpoint is the periodic reconcile, where a few seconds of
    staleness is invisible.
    """
    global _state_cache, _state_cache_ts
    cache = _state_cache
    if cache is not None:
        if time.time() - _state_cache_ts >= _STATE_CACHE_TTL:
            _kick_state_refresh()  # stale — refresh for next time
        return cache
    # Cold start: nothing cached yet. Build once synchronously (the lock
    # makes concurrent first-callers wait for the single build rather
    # than each starting their own).
    with _state_cache_lock:
        if _state_cache is not None:
            return _state_cache
        _state_cache = _build_system_state()
        _state_cache_ts = time.time()
        return _state_cache


def _build_system_state() -> dict[str, Any]:
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

    from work_buddy.health.preferences import is_wanted

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
        # The bridge probe is skipped entirely when Obsidian is explicitly
        # opted out — no point pinging a bridge the user disabled, and it
        # stops the in-process latency history from growing. Undecided
        # (``None``) still probes; the gated card is hidden separately by
        # the card registry. See architecture/feature-cards.
        "bridge": None if is_wanted("obsidian") is False else get_bridge_status(),
        "chrome": get_chrome_status(),
    }

    # Per-source warn/error event tally. The dashboard's Settings control
    # graph joins this to each component via its ``sidecar_service`` to
    # render a per-component event chip. Naturally bounded by the sidecar
    # event ring buffer, so no explicit time window is needed.
    from collections import Counter
    _evt_counts = Counter(
        e["source"]
        for e in result["events"]
        if isinstance(e, dict)
        and e.get("source")
        and e.get("level") in ("error", "warn")
    )
    result["event_counts_by_source"] = dict(_evt_counts)

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


# Pre-compiled task-line parser state — shared across calls so we don't
# rebuild the same regex set on every dashboard refresh.
_TASK_NOTE_LINK_RE = re.compile(r"\[\[([0-9a-f-]+)\|[^\]]*\]\]")
_TASK_TAG_RE = re.compile(r"#\S+")
# Obsidian Tasks emoji: dated (📅⏳🛫✅❌➕) and dateless (⏫🔼🔽⏬🔺)
_TASK_EMOJI_DATED_RE = re.compile(r"([📅⏳🛫✅❌➕])\s*(\d{4}-\d{2}-\d{2})")
_TASK_EMOJI_PLAIN_RE = re.compile(r"[⏫🔼🔽⏬🔺]")
_TASK_PRIORITY_LABELS = {
    "⏫": "Highest",
    "🔼": "High",
    "🔽": "Low",
    "⏬": "Lowest",
}
_TASK_ID_RE = re.compile(r"t-[0-9a-f]+")
_TASK_EMOJI_LABELS = {
    "📅": "Due",
    "⏳": "Scheduled",
    "🛫": "Start",
    "✅": "Done",
    "❌": "Cancelled",
    "➕": "Created",
}


def _parse_task_line(line: str) -> dict[str, Any] | None:
    """Parse a single ``- [ ] ...`` / ``- [x] ...`` task line.

    Returns the same dict shape ``get_tasks_summary`` emits, or ``None``
    for non-task lines. Pure: no file IO, no SQLite enrichment. Extracted
    so both master-task-list.md and archive.md feed the same parser.
    """
    stripped = line.strip()
    if not stripped.startswith("- ["):
        return None
    done = stripped[3] == "x"
    full_text = stripped[6:].strip()  # after "- [x] " or "- [ ] "

    markers = [
        {
            "emoji": em.group(1),
            "label": _TASK_EMOJI_LABELS.get(em.group(1), ""),
            "date": em.group(2),
        }
        for em in _TASK_EMOJI_DATED_RE.finditer(full_text)
    ]
    for ch in _TASK_EMOJI_PLAIN_RE.findall(full_text):
        if ch in _TASK_PRIORITY_LABELS:
            markers.append(
                {"emoji": ch, "label": _TASK_PRIORITY_LABELS[ch], "date": ""}
            )

    text = full_text
    task_id = ""
    if "🆔 " in text:
        parts = text.split("🆔 ")
        id_part = parts[-1].strip()
        m_id = _TASK_ID_RE.match(id_part)
        task_id = m_id.group(0) if m_id else id_part
        text = parts[0].strip()

    note_id = ""
    m = _TASK_NOTE_LINK_RE.search(text)
    if m:
        note_id = m.group(1)

    urgency = "none"
    for emoji, level in [("🔺", "high"), ("🔼", "medium"), ("🔽", "low")]:
        if emoji in text:
            urgency = level
            break

    state = "inbox"
    for tag in ["#todo/focused", "#todo/next", "#todo/waiting", "#todo/someday", "#todo/blocked"]:
        if tag in text:
            state = tag.split("/")[-1]
            break
    if done:
        state = "done"

    display = _TASK_NOTE_LINK_RE.sub("", text)
    display = _TASK_TAG_RE.sub("", display)
    display = _TASK_EMOJI_DATED_RE.sub("", display)
    display = _TASK_EMOJI_PLAIN_RE.sub("", display)
    display = re.sub(r"\s{2,}", " ", display).strip()

    return {
        "id": task_id,
        "text": display[:120],
        "markers": markers,
        "note_id": note_id,
        "done": done,
        "state": state,
        "urgency": urgency,
    }


def _store_row_to_display(row: dict[str, Any]) -> dict[str, Any]:
    """Convert a ``task_metadata`` row into the dashboard's display dict.

    Derives the markers list from emoji-equivalent columns:
    ``deadline_date`` → 📅, ``completed_at`` → ✅, ``urgency`` →
    ⏫/🔼/🔽. SQLite is the canonical source — the dashboard reads
    these directly from ``task_metadata`` rather than re-parsing them
    out of the markdown line.
    """
    markers: list[dict[str, str]] = []
    if row.get("deadline_date"):
        markers.append({
            "emoji": "📅", "label": "Due",
            "date":  row["deadline_date"],
        })
    if row.get("completed_at"):
        # completed_at can be a full ISO timestamp or just YYYY-MM-DD;
        # truncate to the date for the marker display.
        date = str(row["completed_at"])[:10]
        markers.append({
            "emoji": "✅", "label": "Done", "date": date,
        })
    urgency = row.get("urgency")
    urgency_emoji_map = {
        "high":   ("⏫", "Highest"),
        "medium": ("🔼", "High"),
        "low":    ("🔽", "Low"),
    }
    if urgency in urgency_emoji_map:
        em, lbl = urgency_emoji_map[urgency]
        markers.append({"emoji": em, "label": lbl, "date": ""})

    # Archived flag sticky: row gets state="archived" if archived_at set.
    archived = bool(row.get("archived_at"))
    state = "archived" if archived else row.get("state", "inbox")

    text = row.get("description") or ""

    return {
        "id":         row.get("task_id", ""),
        "text":       text[:120] if text else "",
        "markers":    markers,
        "note_id":    row.get("note_uuid") or "",
        "done":       state == "done",
        "state":      state,
        "urgency":    urgency or "none",
        "archived":   archived,
    }


def get_tasks_summary() -> dict[str, Any]:
    """Task summary, SQLite-primary.

    Reads the canonical task_metadata table for tracked rows, and
    appends ID-less legacy lines from master-task-list.md / archive.md
    as thin entries (state derived from checkbox; ID column renders as
    ``—`` on the frontend). The markdown files are joined in only to
    surface legacy lines that were never written into the store.
    """
    from datetime import datetime, timedelta, timezone
    from work_buddy.obsidian.tasks import store as tasks_store

    tasks: list[dict[str, Any]] = []

    # ── Tracked tasks: canonical from SQLite ────────────────────────
    try:
        store_rows = tasks_store.query(include_archived=True)
    except Exception as exc:
        logger.warning("Failed to read task_metadata: %s", exc)
        return {"tasks": [], "counts": {}, "error": str(exc)}

    for row in store_rows:
        tasks.append(_store_row_to_display(row))

    # ── Legacy ID-less rows: append thinly from markdown ────────────
    # These are tasks the user authored directly in Obsidian that
    # haven't yet been picked up by ``task_sync`` (so they have no
    # 🆔 ID), OR that come from archive.md and pre-date the tracking
    # era. The frontend renders an empty `id` field as "—" in the
    # ID column.
    vault_root = _cfg.get("vault_root", "")
    if vault_root:
        tasks_dir = Path(vault_root) / "tasks"
        for path, mark_archived in [
            (tasks_dir / "master-task-list.md", False),
            (tasks_dir / "archive.md",          True),
        ]:
            if not path.exists():
                continue
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    parsed = _parse_task_line(line)
                    if parsed is None or parsed.get("id"):
                        continue  # has ID → already in SQLite-derived rows
                    if mark_archived:
                        parsed["state"] = "archived"
                        parsed["archived"] = True
                    else:
                        parsed["archived"] = False
                    tasks.append(parsed)
            except Exception as exc:
                logger.debug("Legacy markdown scan skipped (%s): %s", path.name, exc)

    # ── Per-row enrichment: is_recent + automation tier + last_actor ─
    try:
        recent_days = int(
            _cfg.get("tasks", {}).get("namespace_recent_days", 14)
        )
        cutoff_iso = (
            datetime.now(timezone.utc) - timedelta(days=max(0, recent_days))
        ).isoformat()
        store_by_id = {r["task_id"]: r for r in store_rows}

        try:
            from work_buddy.automation.risk import resolve_operating_tier
            _have_resolver = True
        except Exception:  # pragma: no cover — defensive
            _have_resolver = False

        for t in tasks:
            row = store_by_id.get(t.get("id"))
            if not row:
                t["is_recent"] = False
                continue
            t["is_recent"] = bool(row.get("created_at", "") >= cutoff_iso)
            t["last_actor"] = row.get("last_actor")
            if _have_resolver and t.get("state") not in {"done", "archived"}:
                decision = resolve_operating_tier(row, config=_cfg)
                t["operating_tier"] = decision.operating
                t["achievable_tier"] = decision.achievable
                if decision.pipeline_blocker:
                    t["pipeline_blocker"] = decision.pipeline_blocker
    except Exception as exc:
        logger.debug("Task enrichment skipped: %s", exc)

    # Attach namespace tags per task (is_namespace=1 only) so the dashboard
    # tree can be built + counted client-side from the same payload that
    # drives the list. One bulk query, no N+1.
    try:
        from work_buddy.obsidian.tasks import store as tasks_store
        conn = tasks_store.get_connection()
        try:
            rows = conn.execute(
                "SELECT task_id, tag FROM task_tags WHERE is_namespace = 1"
            ).fetchall()
        finally:
            conn.close()
        tags_by_task: dict[str, list[str]] = {}
        for r in rows:
            tags_by_task.setdefault(r["task_id"], []).append(r["tag"])
        for t in tasks:
            t["tags"] = tags_by_task.get(t.get("id"), [])
    except Exception as exc:
        logger.debug("Task tag enrichment skipped: %s", exc)
        for t in tasks:
            t.setdefault("tags", [])

    # Count by (enriched) state
    counts: dict[str, int] = {}
    for t in tasks:
        s = t["state"]
        counts[s] = counts.get(s, 0) + 1

    # Surface the most recent task_sync timestamp so the frontend's
    # inline filter status can render "synced Xm ago".
    synced_at: str | None = None
    try:
        from work_buddy.obsidian.tasks import store as _tasks_store
        sst = _tasks_store.get_sync_status()
        if sst:
            synced_at = sst.get("last_full_sync_at")
    except Exception as exc:
        logger.debug("get_sync_status skipped: %s", exc)

    return {"tasks": tasks, "counts": counts, "synced_at": synced_at}


def list_namespaces(recent_days: int = 14) -> dict[str, Any]:
    """Return every namespacey tag in the cache with its open-task count.

    Backed by ``task_tags`` (populated by ``task_sync``). Returns a flat
    list ordered by tag ascending; the frontend is responsible for
    rendering the tree (split on ``/``).

    Each row carries ``count`` (open tasks on that exact tag) and
    ``recent_count`` (open tasks whose ``created_at`` falls in the last
    ``recent_days`` days). The frontend builds a relevance score from
    these for tree ordering.
    """
    try:
        from work_buddy.obsidian.tasks import store as tasks_store
        rows = tasks_store.distinct_namespace_tags(recent_days=recent_days)
        return {
            "namespaces": rows,
            "count": len(rows),
            "recent_days": int(recent_days),
        }
    except Exception as exc:
        logger.warning("Failed to list namespaces: %s", exc)
        return {"namespaces": [], "count": 0, "recent_days": int(recent_days), "error": str(exc)}


def get_tasks_by_namespace(
    namespace: str,
    include_descendants: bool = True,
) -> dict[str, Any]:
    """Return the flat-task view filtered to a namespace tag.

    With ``include_descendants=True`` (default), matches ``namespace`` and
    any path starting with ``namespace + '/'``. The returned tasks carry
    the same shape as ``get_tasks_summary``; additionally this response
    includes a ``descendants`` list of child-tag counts for tree-UI hints.
    """
    namespace = (namespace or "").strip().strip("#").strip("/")
    if not namespace:
        return {"namespace": "", "count": 0, "tasks": [], "descendants": []}

    try:
        from work_buddy.obsidian.tasks import store as tasks_store
        matched_ids = set(
            tasks_store.tasks_with_tag(namespace, prefix_match=include_descendants)
        )
    except Exception as exc:
        logger.warning("Failed to query task_tags for %r: %s", namespace, exc)
        return {
            "namespace": namespace,
            "count": 0,
            "tasks": [],
            "descendants": [],
            "error": str(exc),
        }

    # Reuse the full parsed task list; the id set is small relative to
    # vault size, and this keeps a single display-text formatter.
    summary = get_tasks_summary()
    all_tasks = summary.get("tasks", []) if isinstance(summary, dict) else []
    filtered = [t for t in all_tasks if t.get("id") in matched_ids]

    # Descendant namespaces (one level below) for UI drill-down.
    descendants: dict[str, int] = {}
    try:
        from work_buddy.obsidian.tasks import store as tasks_store
        prefix = namespace + "/"
        for row in tasks_store.distinct_namespace_tags():
            tag = row["tag"]
            if tag.startswith(prefix):
                remainder = tag[len(prefix):]
                # Collapse to the immediate child segment.
                child = remainder.split("/", 1)[0]
                child_tag = prefix + child
                descendants[child_tag] = descendants.get(child_tag, 0) + int(row["count"])
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("Failed to compute descendants for %r: %s", namespace, exc)

    return {
        "namespace": namespace,
        "count": len(filtered),
        "tasks": filtered,
        "descendants": [
            {"tag": tag, "count": count}
            for tag, count in sorted(descendants.items())
        ],
    }


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


def _load_tldrs_from_framework(sids: list[str]) -> list[dict[str, Any]]:
    """Batch-load TLDRs for `sids` from the framework's summarization.db.

    Joins `summary_items` (status='ok') with `summary_nodes` (level=0 →
    root `summary` is the tldr). Covers both v1 (LayeredDisclosureStrategy)
    and v2 (IncrementalLayeredStrategy) rows identically.

    Returns dicts shaped like the historical legacy query result so the
    caller iterates `session_id` + `tldr` keys.

    (Legacy `session_summaries` fallback removed after the 2026-05-28
    one-shot migration moved all legacy rows into the framework DB.)
    """
    if not sids:
        return []

    results: dict[str, str] = {}
    try:
        from work_buddy.summarization.db import get_connection as get_summ_conn
        sconn = get_summ_conn()
        try:
            placeholders = ",".join(["?"] * len(sids))
            rows = sconn.execute(
                f"""
                SELECT i.item_id AS session_id, n.summary AS tldr
                FROM summary_items i
                JOIN summary_nodes n
                  ON n.namespace = i.namespace
                 AND n.item_id   = i.item_id
                 AND n.level     = 0
                WHERE i.namespace = 'conversation_session'
                  AND i.status   = 'ok'
                  AND i.item_id IN ({placeholders})
                """,
                sids,
            ).fetchall()
            for r in rows:
                if r["tldr"]:
                    results[r["session_id"]] = r["tldr"]
        finally:
            sconn.close()
    except Exception as exc:
        logger.debug("framework tldr batch load failed: %s", exc)

    return [{"session_id": sid, "tldr": tldr} for sid, tldr in results.items()]


def _load_observability_for_sessions(
    session_ids: set[str],
) -> dict[str, dict[str, Any]]:
    """Batch-load observability fields for a set of session_ids.

    Returns ``{session_id: {commit_count, unfinished_count,
    commits_by_repo, latest_committed_at, tldr, has_writes}}``. Empty
    dict for sessions with no rows in the conversation_observability
    DB. Reads each backing table once (no per-session round trip),
    then derives ``unfinished_count`` from the deduped commit-files
    map (one ``git show --name-only`` per unique SHA, parallelized).

    Falls back gracefully (returns ``{}``) if the
    ``conversation_observability`` package is unavailable or the DB
    isn't reachable; the caller can still render the legacy chat
    fields.
    """
    if not session_ids:
        return {}
    try:
        from work_buddy.conversation_observability.db import get_connection
        from work_buddy.conversation_observability.writes import (
            _committed_files_for_sessions,
        )
        from work_buddy.config import load_config
    except Exception as exc:
        logger.debug("conversation_observability unavailable: %s", exc)
        return {}

    sids = list(session_ids)
    placeholders = ",".join(["?"] * len(sids))

    try:
        conn = get_connection()
    except Exception as exc:
        logger.debug("conversation_observability DB unreachable: %s", exc)
        return {}

    try:
        commit_rows = conn.execute(
            f"SELECT session_id, sha, repo_name, committed_at "
            f"FROM session_commits WHERE session_id IN ({placeholders})",
            sids,
        ).fetchall()
        write_rows = conn.execute(
            f"SELECT session_id, file_path "
            f"FROM session_file_writes WHERE session_id IN ({placeholders})",
            sids,
        ).fetchall()
        pr_rows = conn.execute(
            f"SELECT session_id, pr_number, pr_url, action, ts "
            f"FROM session_prs WHERE session_id IN ({placeholders}) "
            f"ORDER BY ts DESC",
            sids,
        ).fetchall()
        # Read tldrs from summarization.db (the framework). Legacy
        # `session_summaries` table was emptied + dropped on 2026-05-28
        # after a one-shot migration moved its 94 rows into the framework.
        summary_rows = _load_tldrs_from_framework(sids)
    finally:
        conn.close()

    # Aggregate commits per session.
    commits_by_sid: dict[str, list[dict[str, Any]]] = {sid: [] for sid in sids}
    for r in commit_rows:
        commits_by_sid[r["session_id"]].append(dict(r))

    # Aggregate writes per session.
    writes_by_sid: dict[str, set[str]] = {sid: set() for sid in sids}
    for r in write_rows:
        writes_by_sid[r["session_id"]].add(r["file_path"])

    # Aggregate PR activity per session (already ts-desc ordered).
    prs_by_sid: dict[str, list[dict[str, Any]]] = {sid: [] for sid in sids}
    for r in pr_rows:
        prs_by_sid[r["session_id"]].append(dict(r))

    # Enrich PR rows with title + current state (OPEN/MERGED/CLOSED) from
    # GitHub. The JSONL only yields number/url/action; title and merge
    # state live on GitHub. Best-effort + cached per repo — offline or
    # un-authenticated gh just leaves title/state absent.
    _pr_repos = {
        p["repo"] for prs in prs_by_sid.values() for p in prs if p.get("repo")
    }
    if _pr_repos:
        _pr_meta = _load_pr_meta_for_repos(_pr_repos)
        for prs in prs_by_sid.values():
            for p in prs:
                meta = _pr_meta.get((p.get("repo"), p.get("pr_number")))
                if meta:
                    p["title"] = meta.get("title")
                    p["state"] = meta.get("state")

    # Aggregate task assignments per session (separate DB — best-effort
    # so a tasks-store hiccup never blocks chat rendering).
    tasks_by_sid = _load_tasks_for_sessions(set(sids))

    # Resolve committed-files-per-session in one parallel pass.
    # We also need ``repos_root`` later to infer per-session repo
    # membership from the committed-file paths.
    repos_root: Path | None = None
    try:
        cfg = load_config()
        repos_root = Path(cfg["repos_root"])
    except Exception as exc:
        logger.debug("repos_root unavailable: %s", exc)

    sids_with_writes = {sid for sid, paths in writes_by_sid.items() if paths}
    if sids_with_writes and repos_root is not None:
        try:
            committed_by_sid = _committed_files_for_sessions(
                sids_with_writes, repos_root,
            )
        except Exception as exc:
            logger.debug("committed-files resolution failed: %s", exc)
            committed_by_sid = {sid: set() for sid in sids_with_writes}
    else:
        committed_by_sid = {}

    # Closure over the resolved repos_root for repo-name inference.
    def _infer_repos_from_paths(paths: set[str]) -> set[str]:
        if not paths or repos_root is None:
            return set()
        try:
            root_str = repos_root.resolve().as_posix().rstrip("/") + "/"
        except Exception:
            return set()
        repos: set[str] = set()
        for p in paths:
            if p.startswith(root_str):
                rel = p[len(root_str):]
                first = rel.split("/", 1)[0] if rel else ""
                if first:
                    repos.add(first)
        return repos

    # tldr lookup.
    tldr_by_sid: dict[str, str] = {
        r["session_id"]: r["tldr"] for r in summary_rows if r["tldr"]
    }

    # Build per-session result.
    result: dict[str, dict[str, Any]] = {}
    for sid in sids:
        commits = commits_by_sid[sid]
        writes = writes_by_sid[sid]
        committed_paths = committed_by_sid.get(sid, set())

        # Historical "unfinished" signal: files this session wrote that
        # this session didn't itself commit. Stable forever; doesn't
        # care about other agents' or the user's later git activity.
        unfinished = {p for p in writes if p not in committed_paths}

        commits_by_repo: dict[str, int] = {}
        latest_committed_at: str | None = None
        for c in commits:
            repo = c.get("repo_name") or "(unknown)"
            commits_by_repo[repo] = commits_by_repo.get(repo, 0) + 1
            ts = c.get("committed_at")
            if ts and (latest_committed_at is None or ts > latest_committed_at):
                latest_committed_at = ts

        # `session_commits.repo_name` is currently NULL for all rows
        # (the parser doesn't infer per-commit repo from a Bash tool
        # call's cwd). Infer the SET of repos this session committed
        # to from the file paths in ``committed_paths`` — uses already
        # -resolved data so no extra subprocesses. Used to drive the
        # "across N repos" suffix on the chat-card commit badge.
        inferred_repos = _infer_repos_from_paths(committed_paths)
        if inferred_repos and commits_by_repo == {"(unknown)": len(commits)}:
            # Replace the placeholder bucket with the inferred set,
            # losing per-repo counts but recovering the repo
            # cardinality the badge needs.
            commits_by_repo = {repo: 0 for repo in inferred_repos}

        session_prs = prs_by_sid.get(sid, [])
        pr_authored = sum(1 for p in session_prs if p["action"] == "created")
        pr_merged = sum(1 for p in session_prs if p["action"] == "merged")
        session_tasks = tasks_by_sid.get(sid, [])

        result[sid] = {
            "commit_count": len(commits),
            "unfinished_count": len(unfinished),
            "commits_by_repo": commits_by_repo,
            "latest_committed_at": latest_committed_at,
            "tldr": tldr_by_sid.get(sid),
            # PR activity (session→PR linkage). authored/merged drive the
            # badge counts; prs_detail backs the side-panel list.
            "pr_authored_count": pr_authored,
            "pr_merged_count": pr_merged,
            "prs_detail": session_prs,
            # Reverse session→tasks linkage. task_count drives the badge;
            # tasks_detail backs the side-panel list.
            "task_count": len(session_tasks),
            "tasks_detail": session_tasks,
            # "Engages git" iff the session committed OR wrote any
            # files via Write/Edit/NotebookEdit. Used by the dashboard
            # to gate badge rendering — chat-only sessions stay slim.
            "engages_git": bool(commits) or bool(writes),
        }
    return result


def _load_tasks_for_sessions(
    session_ids: set[str],
) -> dict[str, list[dict[str, Any]]]:
    """Batch-load task assignments + text/state for a set of sessions.

    One query against the task store (a different DB than
    conversation_observability), joining ``task_sessions`` to
    ``task_metadata`` so the side-panel has text + state without an
    N+1 per-task lookup. Best-effort: returns ``{}`` if the store is
    unavailable, so a tasks-store problem never blocks chat rendering.
    """
    if not session_ids:
        return {}
    try:
        from work_buddy.obsidian.tasks import store
    except Exception as exc:  # pragma: no cover — defensive
        logger.debug("task store unavailable: %s", exc)
        return {}

    sids = list(session_ids)
    placeholders = ",".join(["?"] * len(sids))
    try:
        conn = store.get_connection()
    except Exception as exc:
        logger.debug("task store DB unreachable: %s", exc)
        return {}
    try:
        rows = conn.execute(
            f"""SELECT ts.session_id, ts.task_id, ts.assigned_at,
                       tm.state, tm.urgency, tm.description
                FROM task_sessions ts
                LEFT JOIN task_metadata tm ON tm.task_id = ts.task_id
                WHERE ts.session_id IN ({placeholders})
                ORDER BY ts.assigned_at""",
            sids,
        ).fetchall()
    except Exception as exc:
        logger.debug("task_sessions join failed: %s", exc)
        return {}
    finally:
        conn.close()

    by_sid: dict[str, list[dict[str, Any]]] = {sid: [] for sid in sids}
    for r in rows:
        by_sid[r["session_id"]].append({
            "task_id": r["task_id"],
            "state": r["state"],
            "urgency": r["urgency"],
            "task_text": r["description"],
            "assigned_at": r["assigned_at"],
        })
    return by_sid


# PR title/state cache: repo → (fetched_at, {pr_number: {title, state}}).
# Title is immutable; state (OPEN/MERGED/CLOSED) is mutable, so a short
# TTL keeps merge status reasonably fresh without a gh call per request.
_PR_META_CACHE: dict[str, tuple[float, dict[int, dict[str, Any]]]] = {}
_PR_META_TTL = 120.0


def _load_pr_meta_for_repos(
    repos: set[str],
) -> dict[tuple[str, int], dict[str, Any]]:
    """Return ``{(repo, pr_number): {title, state}}`` via ``gh pr list``.

    The session_prs table only carries number/url/action (scraped from
    JSONL); a PR's title and merge state live on GitHub. One ``gh pr
    list`` per repo (cached, short TTL) backfills both. Best-effort:
    missing/un-authenticated ``gh``, offline, or a parse error just
    yields no enrichment — callers fall back to the captured ``action``.
    """
    import subprocess

    out: dict[tuple[str, int], dict[str, Any]] = {}
    for repo in repos:
        cached = _PR_META_CACHE.get(repo)
        if cached and (time.time() - cached[0]) < _PR_META_TTL:
            by_num = cached[1]
        else:
            by_num = {}
            try:
                proc = subprocess.run(
                    [
                        "gh", "pr", "list", "--repo", repo,
                        "--state", "all", "--limit", "400",
                        "--json", "number,title,state",
                    ],
                    capture_output=True, text=True, timeout=15,
                )
                if proc.returncode == 0:
                    for pr in json.loads(proc.stdout or "[]"):
                        by_num[pr["number"]] = {
                            "title": pr.get("title"),
                            "state": pr.get("state"),
                        }
            except (
                subprocess.TimeoutExpired, FileNotFoundError,
                OSError, ValueError,
            ) as exc:
                logger.debug("gh pr list failed for %s: %s", repo, exc)
            _PR_META_CACHE[repo] = (time.time(), by_num)
        for num, meta in by_num.items():
            out[(repo, num)] = meta
    return out


def get_chats_summary(days: int = 14) -> dict[str, Any]:
    """Rich chat list from Claude Code JSONL sessions.

    Calls the chat_collector parser which caches per-file to avoid
    re-parsing unchanged JSONL files. Enriches each chat with
    conversation-observability fields (commit_count, unfinished_count,
    commits_by_repo, latest_committed_at, tldr, engages_git) when the
    DB is available — safe no-op when it isn't.
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

    # Batch-load observability data for every session in scope.
    session_ids = {
        c.get("full_session_id", c.get("session_id", ""))
        for c in raw
        if c.get("full_session_id") or c.get("session_id")
    }
    obs_by_sid = _load_observability_for_sessions(session_ids)

    chats: list[dict[str, Any]] = []
    for c in raw:
        tool_names = c.get("tool_names", {})
        top_tools = [
            name for name, _ in sorted(tool_names.items(), key=lambda x: x[1], reverse=True)[:3]
        ]
        msg_count = c.get("user_msg_count", 0) + c.get("assistant_text_count", 0)
        sid = c.get("full_session_id", c.get("session_id", ""))
        obs = obs_by_sid.get(sid, {})
        chats.append({
            "session_id": sid,
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
            # Conversation-observability enrichment. All optional; the
            # frontend renders badges only when ``engages_git`` is true.
            "commit_count": obs.get("commit_count", 0),
            "unfinished_count": obs.get("unfinished_count", 0),
            "commits_by_repo": obs.get("commits_by_repo", {}),
            "latest_committed_at": obs.get("latest_committed_at"),
            "tldr": obs.get("tldr"),
            "engages_git": obs.get("engages_git", False),
            # session→PR + reverse session→tasks linkage
            "pr_authored_count": obs.get("pr_authored_count", 0),
            "pr_merged_count": obs.get("pr_merged_count", 0),
            "prs_detail": obs.get("prs_detail", []),
            "task_count": obs.get("task_count", 0),
            "tasks_detail": obs.get("tasks_detail", []),
        })

    # Sort by most-recent ACTIVITY (end_time = last message timestamp).
    # The frontend's "Most Recent" sort applies the same key. Falls
    # back to start_time when end_time is missing.
    chats.sort(
        key=lambda x: x.get("end_time") or x.get("start_time") or "",
        reverse=True,
    )

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
                            # Vault-relative path for obsidian:// URI in the dashboard.
                            # Mirrors the config default (contracts.vault_path).
                            "vault_path": f"work-buddy/contracts/{md_file.name}",
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
