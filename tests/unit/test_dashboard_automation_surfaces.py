"""Slice 4 dashboard surfaces: Review Queue + Daily Log API tests.

Two endpoints:

* ``GET /api/automation/review-queue`` — task_metadata rows whose
  operating tier resolves to 3 ("execute and review output").
* ``GET /api/automation/daily-log`` — tier-4 task state-change events
  in the last N days, grouped by category prefix.

Tests cover:

1. Review Queue surfaces only tier-3 tasks (filters out tier-1/2/4).
2. Review Queue carries the typed pipeline_blocker per ROADMAP §3.3
   when the resolver capped below achievable.
3. Review Queue sorts items with blockers first.
4. Daily Log filters to tier-4 only.
5. Daily Log groups events by category prefix and sorts within a
   category most-recent-first.
6. Daily Log honours the ``days`` query param (with 1..7 clamp).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from work_buddy.automation.risk import RiskProfile
from work_buddy.dashboard import service as dash_service
from work_buddy.obsidian.tasks import store


@pytest.fixture
def _isolated_store(monkeypatch, tmp_path):
    """Point the task-metadata store at a fresh sqlite file per test."""
    db_file = tmp_path / "tasks.sqlite"
    monkeypatch.setattr(store, "_db_path", lambda: db_file)
    return db_file


@pytest.fixture
def client(_isolated_store):
    """Flask test client for the dashboard service."""
    dash_service.app.config["TESTING"] = True
    with dash_service.app.test_client() as c:
        yield c


# ---------------------------------------------------------------------------
# Review Queue
# ---------------------------------------------------------------------------


def _seed_task(
    task_id: str,
    *,
    description: str,
    state: str = "inbox",
    profile: RiskProfile | None = None,
    achievable: int | None = None,
    last_actor: str | None = None,
    tags: list[tuple[str, bool]] | None = None,
):
    """Insert a single task with optional Slice-4 risk metadata."""
    store.create(
        task_id=task_id,
        state=state,
        description=description,
        risk_profile_json=profile.to_json() if profile else None,
        automation_tier_achievable=achievable,
        last_actor=last_actor,
    )
    if tags:
        store.set_task_tags(task_id, tags)


def test_review_queue_empty(client):
    resp = client.get("/api/automation/review-queue")
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["status"] == "ok"
    assert body["count"] == 0
    assert body["items"] == []


def test_review_queue_surfaces_tier_three_tasks(client):
    """Critical-accuracy task → operating tier 3 → in queue."""
    _seed_task(
        "t-tier3",
        description="summarize the paper",
        profile=RiskProfile(accuracy="critical"),
        achievable=4,
    )
    # Tier-4 work — should be filtered out.
    _seed_task(
        "t-tier4",
        description="close stale tabs",
        profile=RiskProfile(),
        achievable=4,
    )
    # Tier-2 work (irreversible amplifier) — also filtered out.
    _seed_task(
        "t-tier2",
        description="send email",
        profile=RiskProfile(reversibility="irreversible"),
        achievable=4,
    )

    resp = client.get("/api/automation/review-queue")
    body = resp.get_json()
    assert body["count"] == 1
    item = body["items"][0]
    assert item["task_id"] == "t-tier3"
    assert item["operating"] == 3
    assert item["achievable"] == 4
    assert item["allowed_under_risk"] == 3
    # Pipeline blocker carries typed reason for "we capped below
    # achievable" — risk_threshold_exceeded for dimension caps.
    blocker = item["pipeline_blocker"]
    assert blocker is not None
    assert blocker["kind"] == "risk_threshold_exceeded"
    assert blocker["tone"] == "blocked"
    assert "accuracy" in (item.get("capped_by") or [])


def test_review_queue_excludes_legacy_null_profile_tasks(client):
    """Tasks without ``risk_profile_json`` AND without ``automation_tier_
    achievable`` are legacy fallbacks — they default to tier-3 via the
    safe profile but the user never asked for them to be reviewed.
    Filtering them out keeps the Review Queue meaningful (only items
    Clarify intentionally classified appear)."""
    # Legacy task — both columns NULL.
    store.create(task_id="t-legacy", state="inbox", description="legacy task")
    # Properly classified task with the same effective tier.
    _seed_task(
        "t-classified",
        description="critical accuracy work",
        profile=RiskProfile(accuracy="critical"),
        achievable=4,
    )
    # Task with ONLY a cached achievable (no profile JSON) should also
    # appear — that's an explicit pin from a caller.
    store.create(
        task_id="t-pinned",
        state="inbox",
        description="explicit tier 3 pin",
        automation_tier_achievable=3,
    )
    resp = client.get("/api/automation/review-queue")
    body = resp.get_json()
    ids = {item["task_id"] for item in body["items"]}
    assert ids == {"t-classified", "t-pinned"}
    assert body["count"] == 2


def test_review_queue_excludes_done_tasks(client):
    _seed_task(
        "t-done",
        description="already done",
        state="done",
        profile=RiskProfile(accuracy="critical"),
        achievable=4,
    )
    resp = client.get("/api/automation/review-queue")
    assert resp.get_json()["count"] == 0


def test_review_queue_sorts_blockers_first(client):
    """Tasks with a pipeline blocker bubble to the top."""
    # Two tier-3 tasks: one with achievable=3 (no blocker), one with
    # achievable=4 + critical accuracy (blocker fires).
    _seed_task(
        "t-no-blocker",
        description="default tier 3",
        profile=RiskProfile(),
        achievable=3,
    )
    _seed_task(
        "t-blocker",
        description="critical accuracy",
        profile=RiskProfile(accuracy="critical"),
        achievable=4,
    )
    resp = client.get("/api/automation/review-queue")
    body = resp.get_json()
    assert body["count"] == 2
    # Blocker first.
    assert body["items"][0]["task_id"] == "t-blocker"
    assert body["items"][1]["task_id"] == "t-no-blocker"


# ---------------------------------------------------------------------------
# Daily Log
# ---------------------------------------------------------------------------


def _record_state_change(
    task_id: str, *, old: str | None, new: str, when: datetime,
    reason: str | None = None,
):
    """Manually insert a row into task_state_history at a pinned time."""
    conn = store.get_connection()
    try:
        conn.execute(
            """INSERT INTO task_state_history
               (task_id, old_state, new_state, changed_at, reason)
               VALUES (?, ?, ?, ?, ?)""",
            (task_id, old, new, when.isoformat(), reason),
        )
        conn.commit()
    finally:
        conn.close()


def test_daily_log_empty(client):
    resp = client.get("/api/automation/daily-log")
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["status"] == "ok"
    assert body["total_events"] == 0
    assert body["categories"] == []


def test_daily_log_filters_to_tier_four(client):
    """Only tier-4 tasks' events appear in the log."""
    now = datetime.now(timezone.utc)
    # Tier-4 task — default safe profile + cached achievable=4.
    _seed_task(
        "t-tier4",
        description="autonomous sweep",
        profile=RiskProfile(),
        achievable=4,
        last_actor="agent",
        tags=[("system/maintenance", True)],
    )
    _record_state_change(
        "t-tier4", old="inbox", new="done",
        when=now - timedelta(hours=2), reason="autosweep",
    )
    # Tier-3 task — events should NOT appear in daily log.
    _seed_task(
        "t-tier3",
        description="review summary",
        profile=RiskProfile(accuracy="critical"),
        achievable=4,
        tags=[("admin/files", True)],
    )
    _record_state_change(
        "t-tier3", old="inbox", new="focused",
        when=now - timedelta(hours=3),
    )

    resp = client.get("/api/automation/daily-log")
    body = resp.get_json()
    assert body["total_events"] == 1
    cat = body["categories"][0]
    assert cat["category"] == "system"
    assert cat["count"] == 1
    ev = cat["events"][0]
    assert ev["task_id"] == "t-tier4"
    assert ev["new_state"] == "done"
    assert ev["last_actor"] == "agent"


def test_daily_log_groups_by_category_prefix(client):
    """First-segment of the task's namespace tag is the bucket."""
    now = datetime.now(timezone.utc)
    for tid, tag in [
        ("t-a", "system/maintenance"),
        ("t-b", "system/index"),
        ("t-c", "admin/files"),
    ]:
        _seed_task(
            tid, description=tid,
            profile=RiskProfile(), achievable=4,
            tags=[(tag, True)],
        )
        _record_state_change(
            tid, old="inbox", new="done",
            when=now - timedelta(hours=1),
        )

    resp = client.get("/api/automation/daily-log")
    body = resp.get_json()
    cats = {c["category"]: c["count"] for c in body["categories"]}
    assert cats == {"system": 2, "admin": 1}


def test_daily_log_days_param_is_clamped(client):
    """``days`` clamps to [1, 7]; values outside the range fall back."""
    resp = client.get("/api/automation/daily-log?days=999")
    assert resp.get_json()["window_days"] == 7
    resp = client.get("/api/automation/daily-log?days=0")
    assert resp.get_json()["window_days"] == 1
    resp = client.get("/api/automation/daily-log?days=garbage")
    assert resp.get_json()["window_days"] == 1
    resp = client.get("/api/automation/daily-log?days=3")
    assert resp.get_json()["window_days"] == 3


def test_daily_log_within_category_sort_is_recent_first(client):
    """Events within a category are most-recent-first for skim readability."""
    now = datetime.now(timezone.utc)
    _seed_task(
        "t-a", description="A",
        profile=RiskProfile(), achievable=4,
        tags=[("system/sweep", True)],
    )
    _seed_task(
        "t-b", description="B",
        profile=RiskProfile(), achievable=4,
        tags=[("system/sweep", True)],
    )
    _record_state_change(
        "t-a", old="inbox", new="done",
        when=now - timedelta(hours=4),
    )
    _record_state_change(
        "t-b", old="inbox", new="done",
        when=now - timedelta(hours=1),
    )

    resp = client.get("/api/automation/daily-log")
    body = resp.get_json()
    cat = body["categories"][0]
    # Most recent first.
    assert cat["events"][0]["task_id"] == "t-b"
    assert cat["events"][1]["task_id"] == "t-a"
