"""Smoke tests for the step result visibility system."""

from work_buddy.mcp_server.conductor import _apply_visibility, _make_manifest
from work_buddy.mcp_server.registry import ResultVisibility


def test_auto_small_is_full():
    """Auto mode with small result returns it unchanged."""
    small = {"total": 5, "items": ["a", "b"]}
    r = _apply_visibility("test", small, None)
    assert r == small, f"Expected full, got {r}"
    print("PASS: auto + small -> full")


def test_auto_large_is_manifest():
    """Auto mode with large result returns a manifest."""
    large = {"data": "x" * 20000, "meta": "short"}
    r = _apply_visibility("test", large, None)
    assert r.get("_manifest") is True, f"Expected manifest, got {type(r)}"
    assert "_keys" in r, "Missing _keys in manifest"
    print(f"PASS: auto + large -> manifest (keys={r.get('_keys')})")


def test_none_mode_bare_manifest():
    """None mode returns a bare manifest without structure hints."""
    small = {"total": 5, "items": ["a", "b"]}
    r = _apply_visibility("test", small, ResultVisibility(mode="none"))
    assert r.get("_manifest") is True, "Expected manifest"
    assert "_keys" not in r, "none mode should not have _keys"
    print("PASS: none -> bare manifest")


def test_summary_include_keys():
    """Summary mode with include_keys inlines those keys."""
    data = {"total": 5, "items": ["a", "b"]}
    vis = ResultVisibility(mode="summary", include_keys=["total"])
    r = _apply_visibility("test", data, vis)
    assert r.get("_manifest") is True
    assert r.get("_partial", {}).get("total") == 5
    print(f"PASS: summary + include_keys -> partial")


def test_timeout_passes_through():
    """Timeout results are always returned in full regardless of mode."""
    timeout = {"timeout": True, "request_id": "req_123"}
    r = _apply_visibility("test", timeout, ResultVisibility(mode="none"))
    assert r == timeout, "Timeout should pass through unchanged"
    print("PASS: timeout result passes through even with mode=none")


def test_none_value_passes_through():
    """None result passes through unchanged."""
    r = _apply_visibility("test", None, ResultVisibility(mode="none"))
    assert r is None
    print("PASS: None passes through")


def test_full_mode_oversized_becomes_manifest():
    """Full mode still caps results that exceed _STEP_RESULT_CAP."""
    from work_buddy.mcp_server.conductor import _STEP_RESULT_CAP
    huge = {"data": "x" * (_STEP_RESULT_CAP + 1000)}
    r = _apply_visibility("test", huge, ResultVisibility(mode="full"))
    assert r.get("_manifest") is True, "Oversized full should become manifest"
    print("PASS: full + oversized -> manifest")


if __name__ == "__main__":
    test_auto_small_is_full()
    test_auto_large_is_manifest()
    test_none_mode_bare_manifest()
    test_summary_include_keys()
    test_timeout_passes_through()
    test_none_value_passes_through()
    test_full_mode_oversized_becomes_manifest()
    print("\nAll tests passed!")
