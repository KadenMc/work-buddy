"""Unit tests for the events consent/policy gate."""

from __future__ import annotations

import work_buddy.consent as consent
from work_buddy.events.policy import policy_check
from work_buddy.events.protocol import RunContext


def test_none_action_allows():
    assert policy_check(None, RunContext(seq=1)) == "allow"


def test_granted_allows(monkeypatch):
    monkeypatch.setattr(consent._cache, "is_granted", lambda op, **k: True)
    assert policy_check("evt.deliver", RunContext(seq=1)) == "allow"


def test_ungranted_prompts(monkeypatch):
    monkeypatch.setattr(consent._cache, "is_granted", lambda op, **k: False)
    assert policy_check("evt.deliver", RunContext(seq=1)) == "prompt"


def test_high_weight_is_passed_to_cache(monkeypatch):
    seen: dict[str, str] = {}

    def fake(op, *, consent_weight="low", **k):
        seen["weight"] = consent_weight
        return False

    monkeypatch.setattr(consent._cache, "is_granted", fake)
    decision = policy_check("evt.deliver", RunContext(seq=1), consent_weight="high")
    assert decision == "prompt"
    assert seen["weight"] == "high"


def test_consent_error_prompts(monkeypatch):
    def boom(op, **k):
        raise RuntimeError("consent backend down")

    monkeypatch.setattr(consent._cache, "is_granted", boom)
    assert policy_check("evt.deliver", RunContext(seq=1)) == "prompt"
