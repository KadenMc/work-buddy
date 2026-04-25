"""Tests for the Anthropic rate-limit observation capture."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from work_buddy.llm import rate_limits as rl


@pytest.fixture
def isolated_observations(monkeypatch, tmp_path):
    log_path = tmp_path / "rate_limits.json"
    monkeypatch.setattr(rl, "_path", lambda: log_path)
    return log_path


SAMPLE_HEADERS = {
    "anthropic-ratelimit-requests-limit": "50",
    "anthropic-ratelimit-requests-remaining": "47",
    "anthropic-ratelimit-requests-reset": "2026-04-25T22:11:00Z",
    "anthropic-ratelimit-input-tokens-limit": "50000",
    "anthropic-ratelimit-input-tokens-remaining": "49000",
    "anthropic-ratelimit-input-tokens-reset": "2026-04-25T22:11:00Z",
    "anthropic-ratelimit-output-tokens-limit": "8000",
    "anthropic-ratelimit-output-tokens-remaining": "7800",
    "anthropic-ratelimit-output-tokens-reset": "2026-04-25T22:11:00Z",
}


def test_record_extracts_all_three_dimensions(isolated_observations):
    rl.record_observation("claude-sonnet-4-6", SAMPLE_HEADERS)
    obs = rl.read_observations()
    sonnet = obs["claude-sonnet-4-6"]
    assert sonnet["requests"]["limit"] == 50
    assert sonnet["requests"]["remaining"] == 47
    assert sonnet["input_tokens"]["limit"] == 50000
    assert sonnet["input_tokens"]["remaining"] == 49000
    assert sonnet["output_tokens"]["limit"] == 8000
    assert sonnet["output_tokens"]["remaining"] == 7800
    assert "observed_at" in sonnet


def test_record_combined_tokens_dimension_optional(isolated_observations):
    """When combined-tokens headers are absent (most tiers), they're null."""
    rl.record_observation("claude-sonnet-4-6", SAMPLE_HEADERS)
    obs = rl.read_observations()
    combined = obs["claude-sonnet-4-6"]["tokens_combined"]
    assert combined["limit"] is None
    assert combined["remaining"] is None


def test_record_with_combined_tokens_present(isolated_observations):
    headers = dict(SAMPLE_HEADERS)
    headers["anthropic-ratelimit-tokens-limit"] = "100000"
    headers["anthropic-ratelimit-tokens-remaining"] = "98000"
    headers["anthropic-ratelimit-tokens-reset"] = "2026-04-25T22:11:00Z"
    rl.record_observation("claude-sonnet-4-6", headers)
    combined = rl.read_observations()["claude-sonnet-4-6"]["tokens_combined"]
    assert combined["limit"] == 100000
    assert combined["remaining"] == 98000


def test_record_skipped_when_no_anthropic_headers(isolated_observations):
    """A response with zero anthropic-ratelimit-* headers (e.g. local
    backend, or a non-Anthropic call) writes nothing."""
    rl.record_observation("qwen/qwen3-4b", {"server": "lmstudio", "x-foo": "bar"})
    assert rl.read_observations() == {}
    assert not isolated_observations.exists()


def test_record_handles_None_headers(isolated_observations):
    rl.record_observation("claude-sonnet-4-6", None)
    assert rl.read_observations() == {}


def test_record_handles_garbage_header_values(isolated_observations):
    headers = {
        "anthropic-ratelimit-requests-limit": "not-a-number",
        "anthropic-ratelimit-requests-remaining": "",
        # output_tokens-limit is a valid int — keeps the row from being skipped
        "anthropic-ratelimit-output-tokens-limit": "8000",
        "anthropic-ratelimit-output-tokens-remaining": "7800",
    }
    rl.record_observation("claude-sonnet-4-6", headers)
    obs = rl.read_observations()
    sonnet = obs["claude-sonnet-4-6"]
    # The garbage int parse → None; the valid one survives.
    assert sonnet["requests"]["limit"] is None
    assert sonnet["output_tokens"]["limit"] == 8000


def test_record_overwrites_previous_observation(isolated_observations):
    rl.record_observation("claude-sonnet-4-6", SAMPLE_HEADERS)
    newer = dict(SAMPLE_HEADERS)
    newer["anthropic-ratelimit-requests-remaining"] = "12"
    rl.record_observation("claude-sonnet-4-6", newer)
    obs = rl.read_observations()
    assert obs["claude-sonnet-4-6"]["requests"]["remaining"] == 12


def test_record_multiple_models_kept_separate(isolated_observations):
    rl.record_observation("claude-sonnet-4-6", SAMPLE_HEADERS)
    haiku_headers = dict(SAMPLE_HEADERS)
    haiku_headers["anthropic-ratelimit-requests-limit"] = "100"
    rl.record_observation("claude-haiku-4-5", haiku_headers)
    obs = rl.read_observations()
    assert set(obs.keys()) == {"claude-sonnet-4-6", "claude-haiku-4-5"}
    assert obs["claude-sonnet-4-6"]["requests"]["limit"] == 50
    assert obs["claude-haiku-4-5"]["requests"]["limit"] == 100


def test_read_empty_when_file_missing(isolated_observations):
    assert rl.read_observations() == {}


def test_read_empty_when_file_corrupt(isolated_observations):
    isolated_observations.write_text("not json", encoding="utf-8")
    assert rl.read_observations() == {}


def test_record_with_blank_model_name_skipped(isolated_observations):
    rl.record_observation("", SAMPLE_HEADERS)
    rl.record_observation(None, SAMPLE_HEADERS)
    assert rl.read_observations() == {}


# ---------------------------------------------------------------------------
# API endpoint
# ---------------------------------------------------------------------------


def test_api_costs_rate_limits_endpoint_returns_observations(isolated_observations):
    rl.record_observation("claude-sonnet-4-6", SAMPLE_HEADERS)
    from work_buddy.dashboard.service import app
    client = app.test_client()
    resp = client.get("/api/costs/rate-limits")
    assert resp.status_code == 200
    body = resp.get_json()
    assert "observations" in body
    assert "claude-sonnet-4-6" in body["observations"]


def test_api_costs_rate_limits_endpoint_empty_when_no_data(isolated_observations):
    from work_buddy.dashboard.service import app
    client = app.test_client()
    resp = client.get("/api/costs/rate-limits")
    assert resp.status_code == 200
    assert resp.get_json() == {"observations": {}}
