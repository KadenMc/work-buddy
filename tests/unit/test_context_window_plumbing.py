"""Window plumbing through the context pipeline.

Covers the canonical `parse_time_bound` parser, the `ContextRequest`
since/until fields and their participation in the cache bucket key,
`_window_bounds` precedence, and the `collect_bundle` /
`collect_scoped_context` threading that carries a precise window (rather than
a day-granular scalar) to the sources.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

from work_buddy.context.cache import bucket_key
from work_buddy.context.sources._markdown_wrapper import _window_bounds
from work_buddy.context.types import ContextRequest
from work_buddy.timefmt import parse_time_bound, to_local_naive

UTC = timezone.utc


# ---------------------------------------------------------------------------
# parse_time_bound
# ---------------------------------------------------------------------------


def test_parse_time_bound_relative_shorthand():
    now = datetime(2026, 7, 8, 12, 0, tzinfo=UTC)
    assert parse_time_bound("2h", now=now) == datetime(2026, 7, 8, 10, 0, tzinfo=UTC)
    assert parse_time_bound("30m", now=now) == datetime(2026, 7, 8, 11, 30, tzinfo=UTC)
    assert parse_time_bound("1d", now=now) == datetime(2026, 7, 7, 12, 0, tzinfo=UTC)
    # Long-form units and surrounding whitespace both parse.
    assert parse_time_bound(" 3 days ", now=now) == datetime(2026, 7, 5, 12, 0, tzinfo=UTC)


def test_parse_time_bound_aware_iso_converts_to_utc():
    # An offset-aware ISO string is converted to UTC deterministically,
    # independent of the configured user timezone.
    assert parse_time_bound("2026-07-07T10:40:00-04:00") == datetime(
        2026, 7, 7, 14, 40, tzinfo=UTC
    )


def test_parse_time_bound_naive_iso_is_local_wall_clock():
    # A naive ISO string is read as the user's local wall clock (the journal /
    # collector convention). Round-tripping through to_local_naive returns the
    # same wall-clock time regardless of what USER_TZ is on this machine.
    got = parse_time_bound("2026-07-07T10:40:00")
    assert got is not None and got.tzinfo is not None
    assert to_local_naive(got) == datetime(2026, 7, 7, 10, 40)


def test_parse_time_bound_empty_inputs_are_none():
    assert parse_time_bound(None) is None
    assert parse_time_bound("") is None


def test_parse_time_bound_naive_now_treated_as_utc():
    # A naive `now` is interpreted as UTC rather than crashing on tz-aware math.
    got = parse_time_bound("1h", now=datetime(2026, 7, 8, 12, 0))
    assert got == datetime(2026, 7, 8, 11, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# _window_bounds precedence
# ---------------------------------------------------------------------------


def test_window_bounds_explicit_window_wins():
    since = datetime(2026, 7, 7, 10, 40, tzinfo=UTC)
    until = datetime(2026, 7, 8, 5, 0, tzinfo=UTC)
    # Explicit since/until beat target_date/window_days.
    req = ContextRequest(
        since=since, until=until, target_date=date(2026, 1, 1), window_days=3
    )
    assert _window_bounds(req) == (since, until)


def test_window_bounds_target_date_fallback():
    req = ContextRequest(target_date=date(2026, 7, 7), window_days=1)
    assert _window_bounds(req) == (date(2026, 7, 6), date(2026, 7, 8))


def test_window_bounds_none_when_unset():
    assert _window_bounds(ContextRequest()) == (None, None)


# ---------------------------------------------------------------------------
# bucket_key participation
# ---------------------------------------------------------------------------


def test_bucket_key_varies_with_window():
    since = datetime(2026, 7, 7, 10, 40, tzinfo=UTC)
    a = ContextRequest(since=since)
    b = ContextRequest(since=since.replace(hour=11))
    # Different windows -> different cache buckets (no cross-window reuse).
    assert bucket_key("chat", a) != bucket_key("chat", b)
    # Same window -> same bucket.
    assert bucket_key("chat", a) == bucket_key("chat", ContextRequest(since=since))
    # A windowed request differs from a target_date/window_days one.
    assert bucket_key("chat", a) != bucket_key(
        "chat", ContextRequest(target_date=date(2026, 7, 7), window_days=1)
    )


# ---------------------------------------------------------------------------
# collect_scoped_context (journal collect step)
# ---------------------------------------------------------------------------


def test_collect_scoped_context_passes_exact_window(monkeypatch):
    import work_buddy.mcp_server.context_wrappers as cw

    captured: dict = {}

    def fake_collect_bundle(**kwargs):
        captured.update(kwargs)
        return {"bundle_path": "/tmp/bundle"}

    monkeypatch.setattr(cw, "collect_bundle", fake_collect_bundle)

    from work_buddy.journal import collect_scoped_context

    result = collect_scoped_context(
        {
            "ambiguous": False,
            "collect_since": "2026-07-07T10:40:00",
            "collect_until": "2026-07-08T05:00:00",
        }
    )
    assert result["collected"] is True
    assert captured.get("since") == "2026-07-07T10:40:00"
    assert captured.get("until") == "2026-07-08T05:00:00"
    # The exact window is passed, not the old day-granular scalar.
    assert "hours" not in captured and "days" not in captured


def test_collect_scoped_context_ambiguous_short_circuits(monkeypatch):
    import work_buddy.mcp_server.context_wrappers as cw

    calls = {"n": 0}

    def fake_collect_bundle(**kwargs):
        calls["n"] += 1
        return {"bundle_path": "x"}

    monkeypatch.setattr(cw, "collect_bundle", fake_collect_bundle)

    from work_buddy.journal import collect_scoped_context

    result = collect_scoped_context({"ambiguous": True, "hint": "today or yesterday?"})
    assert result["collected"] is False
    assert result["ambiguous"] is True
    assert calls["n"] == 0  # no collection against a guessed window


# ---------------------------------------------------------------------------
# collect_bundle scalar back-compat + explicit window
# ---------------------------------------------------------------------------


def test_collect_bundle_hours_backcompat(monkeypatch):
    import work_buddy.collect as collect_mod

    captured: dict = {}

    def fake_run_collection(cfg, only=None, dry_run=False, *, since=None, until=None):
        captured["cfg"] = cfg
        captured["since"] = since
        captured["until"] = until
        return Path("/tmp/bundle")

    monkeypatch.setattr(collect_mod, "run_collection", fake_run_collection)

    from work_buddy.mcp_server.context_wrappers import collect_bundle

    out = collect_bundle(hours=24)
    assert out["bundle_path"] == Path("/tmp/bundle").as_posix()
    # No explicit window; the scalar path still drives day-granular knobs.
    assert captured["since"] is None and captured["until"] is None
    assert captured["cfg"]["chats"]["claude_history_days"] == 1  # 24h -> max(1, int(1.0))
    # The knob added for the summary source is covered by the scalar path too.
    assert captured["cfg"]["agent_session_summary"]["days"] == 1


def test_collect_bundle_explicit_window_parsed(monkeypatch):
    import work_buddy.collect as collect_mod

    captured: dict = {}

    def fake_run_collection(cfg, only=None, dry_run=False, *, since=None, until=None):
        captured["since"] = since
        captured["until"] = until
        return Path("/tmp/bundle")

    monkeypatch.setattr(collect_mod, "run_collection", fake_run_collection)

    from work_buddy.mcp_server.context_wrappers import collect_bundle

    collect_bundle(since="2026-07-07T10:40:00", until="2026-07-08T05:00:00")
    assert captured["since"] is not None and captured["since"].tzinfo is not None
    assert captured["until"] is not None and captured["until"].tzinfo is not None


def test_collect_bundle_overrides_merge_into_section(monkeypatch):
    import work_buddy.collect as collect_mod

    captured: dict = {}

    def fake_run_collection(cfg, only=None, dry_run=False, *, since=None, until=None):
        captured["cfg"] = cfg
        return Path("/tmp/bundle")

    monkeypatch.setattr(collect_mod, "run_collection", fake_run_collection)

    from work_buddy.mcp_server.context_wrappers import collect_bundle

    collect_bundle(overrides={"chats": {"include_agent_conversations": False}})
    assert captured["cfg"]["chats"]["include_agent_conversations"] is False
