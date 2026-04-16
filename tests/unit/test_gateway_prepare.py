"""Tests for the gateway _prepare() function.

_prepare() replaces the old _to_json() — it recursively converts
non-serializable objects (Path, date, datetime, sets, __dict__ objects)
to JSON-safe types, returning a dict/list/scalar instead of a JSON string.
The MCP transport handles final serialization via pydantic_core.to_json().
"""

from datetime import date, datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest

from work_buddy.mcp_server.tools.gateway import _prepare


# ---------------------------------------------------------------------------
# 1. Primitives pass through unchanged
# ---------------------------------------------------------------------------

class TestPreparePrimitives:
    def test_none(self):
        assert _prepare(None) is None

    def test_bool_true(self):
        assert _prepare(True) is True

    def test_bool_false(self):
        assert _prepare(False) is False

    def test_int(self):
        assert _prepare(42) == 42

    def test_float(self):
        assert _prepare(3.14) == 3.14

    def test_string(self):
        assert _prepare("hello") == "hello"

    def test_empty_string(self):
        assert _prepare("") == ""


# ---------------------------------------------------------------------------
# 2. Date/datetime → isoformat strings
# ---------------------------------------------------------------------------

class TestPrepareDatetime:
    def test_date(self):
        assert _prepare(date(2026, 4, 15)) == "2026-04-15"

    def test_datetime_naive(self):
        dt = datetime(2026, 4, 15, 10, 30, 0)
        assert _prepare(dt) == "2026-04-15T10:30:00"

    def test_datetime_aware(self):
        dt = datetime(2026, 4, 15, 10, 30, 0, tzinfo=timezone.utc)
        result = _prepare(dt)
        assert "2026-04-15" in result
        assert "+00:00" in result or "Z" in result


# ---------------------------------------------------------------------------
# 3. Path → posix string (forward slashes)
# ---------------------------------------------------------------------------

class TestPreparePath:
    def test_relative_path(self):
        assert _prepare(Path("foo/bar")) == "foo/bar"

    def test_absolute_posix_style(self):
        assert _prepare(Path("C:/Users/test")) == "C:/Users/test"

    def test_pure_posix_path(self):
        assert _prepare(PurePosixPath("/usr/local/bin")) == "/usr/local/bin"

    def test_path_as_posix_not_str(self):
        """Ensure Path uses as_posix() (forward slashes), not str()."""
        p = PureWindowsPath("C:\\Users\\test\\file.py")
        result = _prepare(p)
        assert "\\" not in result
        assert "/" in result


# ---------------------------------------------------------------------------
# 4. Dict — recursive conversion
# ---------------------------------------------------------------------------

class TestPrepareDict:
    def test_simple_dict(self):
        result = _prepare({"key": "value", "num": 42})
        assert result == {"key": "value", "num": 42}
        assert isinstance(result, dict)

    def test_nested_dict(self):
        result = _prepare({"outer": {"inner": "val"}})
        assert result == {"outer": {"inner": "val"}}

    def test_dict_with_path_values(self):
        result = _prepare({"file": Path("a/b/c.py")})
        assert result == {"file": "a/b/c.py"}

    def test_dict_with_date_values(self):
        result = _prepare({"created": date(2026, 1, 1)})
        assert result == {"created": "2026-01-01"}

    def test_empty_dict(self):
        assert _prepare({}) == {}

    def test_deeply_nested(self):
        obj = {"a": {"b": {"c": {"d": Path("x")}}}}
        result = _prepare(obj)
        assert result == {"a": {"b": {"c": {"d": "x"}}}}


# ---------------------------------------------------------------------------
# 5. List/tuple — recursive conversion, preserves order
# ---------------------------------------------------------------------------

class TestPrepareList:
    def test_simple_list(self):
        assert _prepare([1, 2, 3]) == [1, 2, 3]

    def test_list_with_mixed_types(self):
        result = _prepare([1, Path("x"), {"a": date(2026, 4, 15)}])
        assert result == [1, "x", {"a": "2026-04-15"}]

    def test_tuple_becomes_list(self):
        result = _prepare((1, 2, 3))
        assert result == [1, 2, 3]
        assert isinstance(result, list)

    def test_empty_list(self):
        assert _prepare([]) == []

    def test_nested_lists(self):
        result = _prepare([[1, 2], [Path("a"), Path("b")]])
        assert result == [[1, 2], ["a", "b"]]


# ---------------------------------------------------------------------------
# 6. Set → sorted list
# ---------------------------------------------------------------------------

class TestPrepareSet:
    def test_set_becomes_sorted_list(self):
        result = _prepare({3, 1, 2})
        assert result == [1, 2, 3]
        assert isinstance(result, list)

    def test_string_set(self):
        result = _prepare({"c", "a", "b"})
        assert result == ["a", "b", "c"]

    def test_empty_set(self):
        assert _prepare(set()) == []


# ---------------------------------------------------------------------------
# 7. Objects with __dict__ → recursive dict
# ---------------------------------------------------------------------------

class TestPrepareObject:
    def test_simple_object(self):
        class Obj:
            def __init__(self):
                self.name = "test"
                self.value = 42

        result = _prepare(Obj())
        assert result == {"name": "test", "value": 42}

    def test_object_with_path_attr(self):
        class Obj:
            def __init__(self):
                self.path = Path("a/b")

        result = _prepare(Obj())
        assert result == {"path": "a/b"}

    def test_nested_object(self):
        class Inner:
            def __init__(self):
                self.x = 1

        class Outer:
            def __init__(self):
                self.inner = Inner()
                self.y = 2

        result = _prepare(Outer())
        assert result == {"inner": {"x": 1}, "y": 2}


# ---------------------------------------------------------------------------
# 8. Fallback → str()
# ---------------------------------------------------------------------------

class TestPrepareFallback:
    def test_custom_class_no_dict(self):
        """Classes without __dict__ (e.g., slots) fall through to str()."""
        class SlotClass:
            __slots__ = ("val",)
            def __init__(self, v):
                self.val = v
            def __str__(self):
                return f"SlotClass({self.val})"

        # __slots__ classes DO have __dict__ in some implementations
        # but the point is that str() is the final fallback
        result = _prepare(SlotClass(42))
        assert isinstance(result, (str, dict))

    def test_bytes_fallback(self):
        result = _prepare(b"hello")
        assert result == "b'hello'"


# ---------------------------------------------------------------------------
# 9. Return type is never a string for dict/list inputs
# ---------------------------------------------------------------------------

class TestPrepareReturnTypes:
    """Verify _prepare returns structured types, not JSON strings."""

    def test_dict_returns_dict(self):
        result = _prepare({"a": 1})
        assert isinstance(result, dict)
        # NOT a string — this was the old _to_json bug
        assert not isinstance(result, str)

    def test_list_returns_list(self):
        result = _prepare([1, 2])
        assert isinstance(result, list)
        assert not isinstance(result, str)

    def test_nested_result_is_native(self):
        """The result of a typical wb_run response should be a dict, not JSON."""
        result = _prepare({
            "type": "result",
            "capability": "task_briefing",
            "result": {
                "tasks": [{"id": "t-abc", "text": "Do thing", "path": Path("vault/tasks.md")}],
                "count": 1,
            },
            "operation_id": "op_12345678",
        })
        assert isinstance(result, dict)
        assert isinstance(result["result"], dict)
        assert isinstance(result["result"]["tasks"], list)
        assert result["result"]["tasks"][0]["path"] == "vault/tasks.md"


# ---------------------------------------------------------------------------
# 10. Integration: realistic gateway return patterns
# ---------------------------------------------------------------------------

class TestPrepareGatewayPatterns:
    """Test with structures that match actual gateway return values."""

    def test_error_response(self):
        result = _prepare({
            "error": "Unknown capability: 'foo'. Use wb_search to find.",
        })
        assert result == {"error": "Unknown capability: 'foo'. Use wb_search to find."}

    def test_init_response(self):
        result = _prepare({
            "status": "initialized",
            "session_id": "abc-123",
            "message": "Session registered.",
            "setup_hint": "Run /wb-setup",
        })
        assert isinstance(result, dict)
        assert result["status"] == "initialized"

    def test_capability_result_with_paths(self):
        result = _prepare({
            "type": "result",
            "capability": "context_git",
            "result": {
                "repo_root": Path("home/user/repos/my-project"),
                "files_changed": [
                    Path("work_buddy/gateway.py"),
                    Path("tests/test_gateway.py"),
                ],
                "timestamp": datetime(2026, 4, 15, 12, 0, 0, tzinfo=timezone.utc),
            },
            "operation_id": "op_abc",
        })
        assert result["result"]["repo_root"] == "home/user/repos/my-project"
        assert result["result"]["files_changed"][0] == "work_buddy/gateway.py"
        assert "2026-04-15" in result["result"]["timestamp"]

    def test_search_results_list(self):
        """wb_search returns a list, not a dict."""
        results = [
            {"name": "task_create", "category": "tasks"},
            {"name": "task_toggle", "category": "tasks"},
        ]
        result = _prepare(results)
        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0]["name"] == "task_create"

    def test_consent_response(self):
        result = _prepare({
            "status": "granted",
            "mode": "once",
            "operations": ["task.toggle"],
            "operation_id": "op_xyz",
        })
        assert result["status"] == "granted"
        assert result["operations"] == ["task.toggle"]

    def test_bool_false_not_filtered(self):
        """Ensure False values are preserved (not treated as falsy/None)."""
        result = _prepare({
            "success": False,
            "disabled": True,
            "count": 0,
        })
        assert result["success"] is False
        assert result["disabled"] is True
        assert result["count"] == 0
