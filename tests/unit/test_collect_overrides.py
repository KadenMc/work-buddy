"""Unit tests for collect._expand_overrides — global time shorthand expansion."""

import pytest

from work_buddy.collect import _expand_overrides, _TIME_TARGETS


class TestExpandOverrides:
    def test_hours_expansion(self):
        """hours=6 should expand to 0.25 days for all targets."""
        result = _expand_overrides(["hours=6"])
        assert len(result) == len(_TIME_TARGETS)
        # All should be based on 6/24 = 0.25 days
        for entry in result:
            key, _, val = entry.partition("=")
            assert key in {t[0] for t in _TIME_TARGETS}

    def test_days_expansion(self):
        """days=3 should expand to 3 for float targets, max(1, int) for int targets."""
        result = _expand_overrides(["days=3"])
        lookup = {}
        for entry in result:
            key, _, val = entry.partition("=")
            lookup[key] = val

        assert float(lookup["git.detail_days"]) == 3.0
        assert float(lookup["git.active_days"]) == 3.0
        assert int(lookup["obsidian.journal_days"]) == 3
        assert float(lookup["obsidian.recent_modified_days"]) == 3.0

    def test_specific_overrides_win(self):
        """Specific overrides should come after global expansions."""
        result = _expand_overrides(["hours=6", "git.detail_days=1"])
        # The last entry for git.detail_days should be the specific override
        git_entries = [r for r in result if r.startswith("git.detail_days=")]
        assert len(git_entries) == 2
        # OmegaConf from_dotlist: last value wins
        assert git_entries[-1] == "git.detail_days=1"

    def test_passthrough_non_global(self):
        """Non-global overrides pass through unchanged."""
        result = _expand_overrides(["obsidian.bridge_port=9999"])
        assert result == ["obsidian.bridge_port=9999"]

    def test_mixed_global_and_specific(self):
        """Mixed overrides: globals expand first, specifics appended last."""
        result = _expand_overrides(["days=1", "chats.specstory_days=14"])
        # Globals come first, then specifics
        assert result[-1] == "chats.specstory_days=14"
        # Should have len(_TIME_TARGETS) globals + 1 specific
        assert len(result) == len(_TIME_TARGETS) + 1

    def test_empty_overrides(self):
        result = _expand_overrides([])
        assert result == []

    def test_hours_fractional(self):
        """hours=12 -> 0.5 days. Int-coerced targets should be max(1, int(0.5)) = 1."""
        result = _expand_overrides(["hours=12"])
        lookup = {}
        for entry in result:
            key, _, val = entry.partition("=")
            lookup[key] = val

        assert float(lookup["git.detail_days"]) == pytest.approx(0.5)
        # journal_days is int-coerced with max(1, ...), so 0.5 -> max(1, 0) = 1
        assert int(lookup["obsidian.journal_days"]) == 1

    def test_small_hours_clamps_to_1(self):
        """hours=1 -> 1/24 days. Int targets should clamp to 1."""
        result = _expand_overrides(["hours=1"])
        lookup = {}
        for entry in result:
            key, _, val = entry.partition("=")
            lookup[key] = val

        assert int(lookup["chats.claude_history_days"]) == 1
