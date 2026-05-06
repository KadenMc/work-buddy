"""Periodic vault reconnaissance with delta detection and agent escalation.

Runs on cron (default daily at 02:00). Calls vault_recon, appends to a rolling
ledger, computes deltas against prior snapshots, applies five curated
significance rules, and writes a one-shot type:prompt job for the scheduler
to fire when a rule trips.

Pattern mirrors work_buddy/collectors/chrome_ledger.py: write-once snapshots,
all derived views (deltas, significance) computed at read time. Storage is
small — typical snapshot is 5-20 KB; 60-day window holds ~60-120 snapshots.

Data flow:
    cron → vault_recon_collect()
        → vault_recon() (single bridge call)
        → append snapshot to ledger
        → compute delta vs prior snapshots
        → apply 5 significance rules
        → for each fire (after dedup): write one-shot job to .data/user_jobs/
"""
from __future__ import annotations

import json
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from work_buddy.logging_config import get_logger
from work_buddy.paths import resolve

logger = get_logger(__name__)

# ── Storage layout ──────────────────────────────────────────────
#   .data/vault_recon/snapshots.json           - rolling list of snapshots
#   .data/vault_recon/latest.json              - fast-read pointer (most recent)
#   .data/vault_recon/escalation_history.jsonl - append-only firing log
#   .data/user_jobs/vault-investigation-*.md   - one-shot prompt jobs

_DEFAULT_WINDOW_DAYS = 60

# ── Significance rule constants ─────────────────────────────────

_NEW_TYPE_MIN_INSTANCES = 5
_NEW_TYPE_LOOKBACK_SNAPSHOTS = 14
_NEW_TYPE_PRIOR_THRESHOLD = 2

_NEW_TAG_FAMILY_MIN_NEW = 10
_NEW_TAG_FAMILY_PRIOR_LOOKBACK_DAYS = 7

_STUCK_STATE_MIN_DAYS = 30
_TERMINAL_STATUSES = {
    "completed", "complete", "current", "done", "finished", "finalized",
    "actioned", "closed", "archived", "shipped", "published", "released",
    "x",
}

_PATH_SPIKE_MIN_RATIO = 3.0
_PATH_SPIKE_BASELINE_LOOKBACK = 7
_PATH_SPIKE_NEW_REGION_MIN = 5

_BACKLOG_MIN_RUN = 7
_BACKLOG_MIN_FINAL_COUNT = 3

_ESCALATION_SUPPRESS_DAYS = 7


# ── Path helpers ────────────────────────────────────────────────


def _ledger_dir() -> Path:
    p = resolve("vault_recon")
    p.mkdir(parents=True, exist_ok=True)
    return p


def _user_jobs_dir() -> Path:
    p = resolve("user_jobs")
    p.mkdir(parents=True, exist_ok=True)
    return p


def _snapshots_path() -> Path:
    return _ledger_dir() / "snapshots.json"


def _latest_path() -> Path:
    return _ledger_dir() / "latest.json"


def _escalation_history_path() -> Path:
    return _ledger_dir() / "escalation_history.jsonl"


# ── Ledger I/O ──────────────────────────────────────────────────


def _read_snapshots() -> list[dict]:
    p = _snapshots_path()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to read vault_recon snapshots: %s", e)
        return []


def _write_snapshots(snapshots: list[dict]) -> None:
    p = _snapshots_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(snapshots, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def _write_latest(snapshot: dict) -> None:
    p = _latest_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def _prune_old(snapshots: list[dict], window_days: int) -> list[dict]:
    cutoff = datetime.now(timezone.utc).timestamp() - window_days * 86400
    out = []
    for s in snapshots:
        ts = s.get("snapshot_ts")
        if not ts:
            continue
        try:
            t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if t.timestamp() >= cutoff:
                out.append(s)
        except ValueError:
            continue
    return out


# ── Escalation history ──────────────────────────────────────────


def _read_escalation_history() -> list[dict]:
    p = _escalation_history_path()
    if not p.exists():
        return []
    out = []
    try:
        with p.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError as e:
        logger.warning("Failed to read escalation history: %s", e)
    return out


def _append_escalation_history(entry: dict) -> None:
    p = _escalation_history_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _is_recent_escalation(rule_name: str, focus_key: str, history: list[dict]) -> bool:
    """Has this (rule, focus) tuple been escalated within the suppression window?"""
    cutoff = datetime.now(timezone.utc).timestamp() - _ESCALATION_SUPPRESS_DAYS * 86400
    for entry in history:
        if entry.get("rule") != rule_name or entry.get("focus") != focus_key:
            continue
        ts = entry.get("ts")
        if not ts:
            continue
        try:
            t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if t.timestamp() >= cutoff:
                return True
        except ValueError:
            continue
    return False


# ── Time helpers ────────────────────────────────────────────────


def _find_snapshot_n_days_ago(history: list[dict], days: int) -> dict | None:
    """Find the snapshot at-or-before N days before now.

    Returns None if no snapshot is old enough — rules that need temporal
    distance (stuck_state, new_tag_family) must skip rather than fall back
    to a recent snapshot, which would produce spurious fires when the ledger
    is young.
    """
    if not history:
        return None
    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    for snap in reversed(history):
        ts = snap.get("snapshot_ts")
        if not ts:
            continue
        try:
            t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            continue
        if t.timestamp() <= cutoff:
            return snap
    return None


def _flatten_tag_tree_d2(tree: dict) -> dict[str, int]:
    """Flatten a tag tree to {'#root/child': count} at depth 2 (or '#root' if no children)."""
    out = {}
    for root, root_node in (tree or {}).items():
        children = (root_node or {}).get("children") or {}
        if not children:
            out[f"#{root}"] = (root_node or {}).get("_count", 0)
            continue
        for child, child_node in children.items():
            out[f"#{root}/{child}"] = (child_node or {}).get("_count", 0)
    return out


# ── Significance rules ──────────────────────────────────────────


def rule_new_type(current: dict, history: list[dict]) -> list[dict]:
    """A frontmatter ``type`` value seen <2 times in last 14 snapshots, now >=5 instances.

    Requires at least 3 prior snapshots before firing — otherwise the bootstrap
    snapshot would mark every existing type as "new" and escalate dozens of
    spurious investigations.
    """
    fires = []
    if len(history) < 3:
        return fires
    fm_values = (current.get("frontmatter_values") or {}).get("type")
    if not fm_values:
        return fires
    current_types = {v["value"]: v["count"] for v in fm_values.get("values", [])}

    lookback = history[-_NEW_TYPE_LOOKBACK_SNAPSHOTS:] if history else []
    historical_max = {}
    for snap in lookback:
        snap_fm = (snap.get("frontmatter_values") or {}).get("type")
        if not snap_fm:
            continue
        for v in snap_fm.get("values", []):
            historical_max[v["value"]] = max(historical_max.get(v["value"], 0), v["count"])

    for type_value, count in current_types.items():
        prior = historical_max.get(type_value, 0)
        if prior < _NEW_TYPE_PRIOR_THRESHOLD and count >= _NEW_TYPE_MIN_INSTANCES:
            fires.append({
                "rule": "new_type",
                "focus": f"type:{type_value}",
                "evidence": {
                    "type_value": type_value,
                    "current_count": count,
                    "historical_max": prior,
                    "lookback_snapshots": len(lookback),
                },
                "suggested_focus": {"frontmatter_key": "type", "value": type_value},
            })
    return fires


def rule_new_tag_family(current: dict, history: list[dict]) -> list[dict]:
    """A depth-2 tag prefix with 0 mentions ~7 days ago, now >=10 mentions."""
    fires = []
    current_d2 = _flatten_tag_tree_d2(current.get("tag_tree") or {})
    prior_snapshot = _find_snapshot_n_days_ago(history, _NEW_TAG_FAMILY_PRIOR_LOOKBACK_DAYS)
    if prior_snapshot is None:
        return fires
    prior_d2 = _flatten_tag_tree_d2(prior_snapshot.get("tag_tree") or {})

    for tag_prefix, count in current_d2.items():
        prior_count = prior_d2.get(tag_prefix, 0)
        if prior_count == 0 and count >= _NEW_TAG_FAMILY_MIN_NEW:
            fires.append({
                "rule": "new_tag_family",
                "focus": tag_prefix,
                "evidence": {
                    "tag_prefix": tag_prefix,
                    "current_count": count,
                    "prior_count": prior_count,
                },
                "suggested_focus": {"tag_prefix": tag_prefix},
            })
    return fires


def rule_stuck_state(current: dict, history: list[dict]) -> list[dict]:
    """Same type x non-terminal status cell unchanged in count for >=30 days."""
    fires = []
    current_tbs = current.get("type_by_status") or {}
    if not current_tbs:
        return fires

    snap_30d_ago = _find_snapshot_n_days_ago(history, _STUCK_STATE_MIN_DAYS)
    if snap_30d_ago is None:
        return fires
    prior_tbs = snap_30d_ago.get("type_by_status") or {}

    for type_value, status_counts in current_tbs.items():
        for status, count in status_counts.items():
            if status.lower() in _TERMINAL_STATUSES or status == "(none)":
                continue
            if count < 1:
                continue
            prior_count = (prior_tbs.get(type_value) or {}).get(status, 0)
            if prior_count == count and count >= 1:
                fires.append({
                    "rule": "stuck_state",
                    "focus": f"{type_value}:{status}",
                    "evidence": {
                        "type": type_value,
                        "status": status,
                        "count": count,
                        "stuck_since_snapshot_ts": snap_30d_ago.get("snapshot_ts"),
                    },
                    "suggested_focus": {"type": type_value, "status": status},
                })
    return fires


def rule_path_activity_spike(current: dict, history: list[dict]) -> list[dict]:
    """recent_activity_by_path[path] >= 3x median of last 7 snapshots' values for that path."""
    fires = []
    current_recent = current.get("recent_activity_by_path") or {}
    if not current_recent:
        return fires

    lookback = history[-_PATH_SPIKE_BASELINE_LOOKBACK:] if history else []
    if len(lookback) < 3:
        return fires

    historical_per_path: dict[str, list[int]] = defaultdict(list)
    for snap in lookback:
        snap_recent = snap.get("recent_activity_by_path") or {}
        for path, count in snap_recent.items():
            historical_per_path[path].append(count)
        for path in current_recent.keys():
            if path not in snap_recent:
                historical_per_path[path].append(0)

    for path, count in current_recent.items():
        baseline = historical_per_path.get(path, [])
        if not baseline:
            continue
        median = statistics.median(baseline)
        if median == 0:
            if count >= _PATH_SPIKE_NEW_REGION_MIN:
                fires.append({
                    "rule": "path_activity_spike",
                    "focus": f"path:{path}",
                    "evidence": {
                        "path": path,
                        "current_count": count,
                        "baseline_median": median,
                        "ratio": "infinite",
                    },
                    "suggested_focus": {"path": path},
                })
        elif count / median >= _PATH_SPIKE_MIN_RATIO:
            fires.append({
                "rule": "path_activity_spike",
                "focus": f"path:{path}",
                "evidence": {
                    "path": path,
                    "current_count": count,
                    "baseline_median": median,
                    "ratio": round(count / median, 2),
                },
                "suggested_focus": {"path": path},
            })
    return fires


def rule_status_backlog_growing(current: dict, history: list[dict]) -> list[dict]:
    """A type x non-terminal status cell count growing monotonically for >=7 snapshots."""
    fires = []
    current_tbs = current.get("type_by_status") or {}
    if not current_tbs:
        return fires
    if len(history) < _BACKLOG_MIN_RUN:
        return fires

    recent = history[-_BACKLOG_MIN_RUN:]
    for type_value, status_counts in current_tbs.items():
        for status, count in status_counts.items():
            if status.lower() in _TERMINAL_STATUSES or status == "(none)":
                continue
            series = []
            for snap in recent:
                snap_count = (snap.get("type_by_status") or {}).get(type_value, {}).get(status, 0)
                series.append(snap_count)
            series.append(count)
            if all(b >= a for a, b in zip(series, series[1:])):
                if series[-1] > series[0] and series[-1] >= _BACKLOG_MIN_FINAL_COUNT:
                    fires.append({
                        "rule": "status_backlog_growing",
                        "focus": f"{type_value}:{status}",
                        "evidence": {
                            "type": type_value,
                            "status": status,
                            "trend": series,
                        },
                        "suggested_focus": {"type": type_value, "status": status},
                    })
    return fires


_RULES = (
    rule_new_type,
    rule_new_tag_family,
    rule_stuck_state,
    rule_path_activity_spike,
    rule_status_backlog_growing,
)


# ── Investigation job spawn ─────────────────────────────────────


def _spawn_investigation_job(rule: dict, latest_path: str) -> str:
    """Write a one-shot ``type: prompt`` job to ``.data/user_jobs/``.

    The scheduler hot-reloads every 30s and fires this on next tick. The
    spawned agent reads its own prompt body, loads `vault/investigation-directions`,
    investigates the delta, and surfaces a proposal via ``request_send``.

    Returns the job filename for traceability.
    """
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_focus = "".join(
        c if c.isalnum() or c in "-_" else "-" for c in rule["focus"]
    )[:40].strip("-")
    job_name = f"vault-investigation-{safe_focus}-{ts}"
    user_jobs = _user_jobs_dir()
    user_jobs.mkdir(parents=True, exist_ok=True)
    job_path = user_jobs / f"{job_name}.md"

    body_lines = [
        "You are a spawned investigation agent. The vault-recon collector "
        "detected a significant delta:",
        "",
        f"- Rule: {rule['rule']}",
        f"- Focus: {rule['focus']}",
        f"- Evidence: {json.dumps(rule['evidence'], ensure_ascii=False)}",
        f"- Suggested focus: {json.dumps(rule['suggested_focus'], ensure_ascii=False)}",
        f"- Latest snapshot: {latest_path}",
        "",
        "Load `vault/investigation-directions` for the protocol. Verify the "
        "delta is real (not a measurement artifact), characterize the pattern, "
        "draft a concrete proposal, and surface via `request_send` with "
        "consent-gated choices. Append to "
        "`.data/vault_recon/escalation_history.jsonl` if you take any action so "
        "duplicates suppress for 7 days.",
        "",
        "You are headless_ephemeral — short-lived. Exit after surfacing or "
        "verifying the delta is an artifact.",
    ]
    body = "\n".join(body_lines)
    indented = "\n".join("  " + line for line in body.split("\n"))

    frontmatter = (
        "---\n"
        f"# Auto-generated by vault_recon_collector at {ts}\n"
        "# One-shot investigation job spawned by significance rule firing\n"
        'schedule: "* * * * *"\n'
        "recurring: false\n"
        "type: prompt\n"
        "spawn_mode: headless_ephemeral\n"
        "enabled: true\n"
        "prompt: |\n"
    )
    # Closing --- terminates the YAML frontmatter; without it the parser
    # rejects the file and the scheduler silently drops it on hot-reload.
    job_path.write_text(frontmatter + indented + "\n---\n", encoding="utf-8")
    return str(job_path)


# ── Consent surfacing (TEMPORARY — see task t-3f15e2b3) ─────────
#
# Without this, a delta firing → job written to .data/user_jobs/ →
# scheduler hot-reloads → executor sees no `sidecar:agent_spawn` consent →
# returns consent_required → job rots silently. The user sees nothing.
#
# Stop-gap: before writing the job, check consent. If missing, surface a
# rich consent_request that includes the delta context. The user gets a
# real modal asking "spawn investigation for this delta?" with the usual
# allow-once / always / N-min / deny options.
#
# Proper fix is the autonomy budget/rate/anomaly system in t-3f15e2b3.


def _agent_spawn_consent_granted() -> bool:
    """Check whether sidecar:agent_spawn is currently granted."""
    try:
        from work_buddy.consent import ConsentCache
        return ConsentCache().is_granted("sidecar:agent_spawn")
    except Exception as e:
        logger.warning("agent_spawn consent check failed: %s — denying", e)
        return False


def _consent_request_history_path() -> Path:
    return _ledger_dir() / "consent_request_history.jsonl"


def _is_recent_consent_request(rule_name: str, focus_key: str) -> bool:
    """Has a consent request been surfaced for this (rule, focus) within the
    suppression window? Prevents asking the user the same question every
    collector run while a previous request is still pending."""
    p = _consent_request_history_path()
    if not p.exists():
        return False
    cutoff = datetime.now(timezone.utc).timestamp() - _ESCALATION_SUPPRESS_DAYS * 86400
    try:
        with p.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("rule") != rule_name or entry.get("focus") != focus_key:
                    continue
                ts = entry.get("ts")
                if not ts:
                    continue
                try:
                    t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    if t.timestamp() >= cutoff:
                        return True
                except ValueError:
                    continue
    except OSError as e:
        logger.warning("Failed to read consent_request_history: %s", e)
    return False


def _record_consent_request(rule_name: str, focus_key: str, request_id: str) -> None:
    p = _consent_request_history_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "rule": rule_name,
        "focus": focus_key,
        "request_id": request_id,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _surface_consent_request_for_investigation(rule: dict) -> str:
    """Surface a consent_request to the user with the delta context.

    Two-step:
      1. ``create_consent_request`` persists the request in the notification store.
      2. ``SurfaceDispatcher.deliver`` pushes it to all available surfaces
         (dashboard / telegram / obsidian).

    Without step 2 the request sits invisible in the store; the user never
    sees a modal. ``create_consent_request`` alone is not enough.

    Returns the request_id, or empty string on failure.
    """
    try:
        from work_buddy.consent import create_consent_request
        result = create_consent_request(
            operation="sidecar:agent_spawn",
            reason=(
                f"vault-recon detected: {rule['rule']} on {rule['focus']}. "
                f"Spawn an investigation agent to characterize the change "
                f"and propose action via request_send? Granting 'always' "
                f"will let future investigations spawn silently."
            ),
            risk="low",
            default_ttl=60,
            requester="sidecar:vault_recon_collector",
            context={
                "rule": rule["rule"],
                "focus": rule["focus"],
                "evidence": rule.get("evidence", {}),
            },
        )
        request_id = result.get("request_id", "")
        if not request_id:
            return ""

        # Step 2: deliver to surfaces (dashboard / telegram / obsidian).
        try:
            from work_buddy.notifications.dispatcher import SurfaceDispatcher
            from work_buddy.notifications.store import (
                get_notification, mark_delivered,
            )
            notif = get_notification(request_id)
            if notif is None:
                logger.warning(
                    "Consent request created but notification %s not found for delivery",
                    request_id,
                )
            else:
                dispatcher = SurfaceDispatcher.from_config()
                results = dispatcher.deliver(notif, mark_delivered_fn=mark_delivered)
                delivered = [s for s, ok in results.items() if ok]
                logger.info(
                    "Consent request %s delivered to surfaces: %s",
                    request_id, delivered or "<none>",
                )
        except Exception as e:
            logger.error(
                "Consent request %s persisted but surface delivery failed: %s",
                request_id, e,
            )

        logger.info(
            "Surfaced consent request for vault-recon spawn: "
            "rule=%s focus=%s request_id=%s",
            rule["rule"], rule["focus"], request_id,
        )
        return request_id
    except Exception as e:
        logger.error(
            "Failed to surface consent request for vault-recon spawn: %s", e
        )
        return ""


# ── Main entry point ────────────────────────────────────────────


def vault_recon_collect(
    window_days: int = _DEFAULT_WINDOW_DAYS,
    skip_escalation: bool = False,
) -> dict:
    """Take a snapshot, append to ledger, compute deltas, escalate significant changes.

    On rule firing, the collector either:
    - Writes a one-shot type:prompt investigation job (if sidecar:agent_spawn
      consent is granted) — scheduler hot-reloads and fires it.
    - Surfaces a consent_request with delta context (if consent is missing) —
      user can grant via modal, then the next run proceeds. This is a
      temporary path until the autonomy budget system (t-3f15e2b3) lands.

    Args:
        window_days: Retention window in days (default 60).
        skip_escalation: If True, evaluate rules but don't spawn investigation
            jobs OR surface consent requests. Useful for dry runs / testing.

    Returns:
        Summary dict with snapshot_ts, ledger_size, pages_walked, rules_fired,
        escalations_spawned, consent_requested, fires_detail, spawns_detail,
        consent_requests_detail.
    """
    from work_buddy.obsidian.datacore.env import vault_recon

    try:
        snapshot = vault_recon()
    except Exception as e:
        logger.error("vault_recon failed: %s", e)
        return {"error": str(e), "stage": "snapshot"}

    if "error" in snapshot or "walk_error" in snapshot:
        err = snapshot.get("error") or snapshot.get("walk_error")
        logger.warning("vault_recon returned error: %s", err)
        return {"error": err, "stage": "snapshot", "snapshot_ts": snapshot.get("snapshot_ts")}

    history = _read_snapshots()
    history.append(snapshot)
    history = _prune_old(history, window_days)
    _write_snapshots(history)
    _write_latest(snapshot)

    prior_history = history[:-1] if len(history) > 1 else []
    fires = []
    for rule_fn in _RULES:
        try:
            fires.extend(rule_fn(snapshot, prior_history))
        except Exception as e:
            logger.error("rule %s raised: %s", rule_fn.__name__, e)

    escalation_history = _read_escalation_history()
    consent_granted = _agent_spawn_consent_granted()
    spawns = []
    consent_requests = []
    for rule in fires:
        if _is_recent_escalation(rule["rule"], rule["focus"], escalation_history):
            continue
        if skip_escalation:
            continue

        if not consent_granted:
            # TEMPORARY consent-surfacing path — see t-3f15e2b3 for the proper
            # autonomy budget / rate / anomaly system.
            if _is_recent_consent_request(rule["rule"], rule["focus"]):
                continue
            request_id = _surface_consent_request_for_investigation(rule)
            if request_id:
                _record_consent_request(rule["rule"], rule["focus"], request_id)
                consent_requests.append({
                    "rule": rule["rule"],
                    "focus": rule["focus"],
                    "request_id": request_id,
                })
            continue

        try:
            job_path = _spawn_investigation_job(rule, str(_latest_path()))
            entry = {
                "rule": rule["rule"],
                "focus": rule["focus"],
                "ts": datetime.now(timezone.utc).isoformat(),
                "job_path": job_path,
            }
            _append_escalation_history(entry)
            spawns.append(entry)
        except Exception as e:
            logger.error("Failed to spawn investigation for %s: %s", rule, e)

    return {
        "snapshot_ts": snapshot.get("snapshot_ts"),
        "ledger_size": len(history),
        "pages_walked": snapshot.get("pages_walked"),
        "rules_fired": len(fires),
        "escalations_spawned": len(spawns),
        "consent_requested": len(consent_requests),
        "fires_detail": [{"rule": f["rule"], "focus": f["focus"]} for f in fires],
        "spawns_detail": spawns,
        "consent_requests_detail": consent_requests,
    }
