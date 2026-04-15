"""Unit tests for the @bridge_retry decorator, bridge_failure protocol,
and obsidian_retry capability."""

from __future__ import annotations

from unittest.mock import patch, MagicMock
import pytest

from work_buddy.obsidian.retry import (
    bridge_retry,
    bridge_failure,
    is_bridge_failure,
    obsidian_retry,
)


# ---------------------------------------------------------------------------
# bridge_failure protocol
# ---------------------------------------------------------------------------

class TestBridgeFailureProtocol:
    """The bridge_failure/is_bridge_failure contract."""

    def test_bridge_failure_creates_marked_dict(self):
        result = bridge_failure("Could not read file")
        assert result["success"] is False
        assert result["message"] == "Could not read file"
        assert is_bridge_failure(result)

    def test_is_bridge_failure_rejects_normal_failure(self):
        """A normal failure dict without the marker is not a bridge failure."""
        assert not is_bridge_failure({"success": False, "message": "Task not found"})

    def test_is_bridge_failure_rejects_none(self):
        assert not is_bridge_failure(None)

    def test_is_bridge_failure_rejects_string(self):
        assert not is_bridge_failure("error")

    def test_is_bridge_failure_rejects_success(self):
        """Even if someone accidentally sets the marker on a success dict."""
        assert is_bridge_failure({"success": True, "_bridge_transient": True})


# ---------------------------------------------------------------------------
# @bridge_retry decorator — exception path (existing behavior)
# ---------------------------------------------------------------------------

class TestBridgeRetryExceptions:
    """Exception-based retry (transient raises)."""

    def test_success_on_first_attempt(self):
        call_count = 0

        @bridge_retry(max_retries=3, wait_seconds=0)
        def succeeds():
            nonlocal call_count
            call_count += 1
            return {"ok": True}

        result = succeeds()
        assert result == {"ok": True}
        assert call_count == 1

    def test_transparent_return(self):
        @bridge_retry(max_retries=3, wait_seconds=0)
        def returns_string():
            return "plain value"

        assert returns_string() == "plain value"

    def test_preserves_function_metadata(self):
        @bridge_retry()
        def my_function():
            """My docstring."""
            pass

        assert my_function.__name__ == "my_function"
        assert my_function.__doc__ == "My docstring."

    def test_args_and_kwargs_forwarded(self):
        @bridge_retry(max_retries=1, wait_seconds=0)
        def echo(a, b, key=None):
            return (a, b, key)

        assert echo(1, 2, key="three") == (1, 2, "three")

    @patch("work_buddy.errors.classify_error", return_value="transient")
    @patch("work_buddy.obsidian.bridge.get_latency_context", return_value="test")
    @patch("work_buddy.obsidian.bridge.is_available", return_value=True)
    def test_retries_on_transient_error(self, mock_avail, mock_latency, mock_classify):
        attempts = []

        @bridge_retry(max_retries=3, wait_seconds=0)
        def fails_then_succeeds():
            attempts.append(1)
            if len(attempts) < 2:
                raise ConnectionError("bridge timeout")
            return {"ok": True}

        result = fails_then_succeeds()
        assert result == {"ok": True}
        assert len(attempts) == 2

    @patch("work_buddy.errors.classify_error", return_value="permanent")
    @patch("work_buddy.obsidian.bridge.get_latency_context", return_value="test")
    @patch("work_buddy.obsidian.bridge.is_available", return_value=True)
    def test_no_retry_on_permanent_error(self, mock_avail, mock_latency, mock_classify):
        attempts = []

        @bridge_retry(max_retries=3, wait_seconds=0)
        def always_fails():
            attempts.append(1)
            raise TypeError("bad argument")

        with pytest.raises(TypeError, match="bad argument"):
            always_fails()
        assert len(attempts) == 1

    @patch("work_buddy.errors.classify_error", return_value="transient")
    @patch("work_buddy.obsidian.bridge.get_latency_context", return_value="test")
    @patch("work_buddy.obsidian.bridge.is_available", return_value=True)
    def test_exhaustion_reraises(self, mock_avail, mock_latency, mock_classify):
        attempts = []

        @bridge_retry(max_retries=2, wait_seconds=0)
        def always_transient():
            attempts.append(1)
            raise ConnectionError("still broken")

        with pytest.raises(ConnectionError, match="still broken"):
            always_transient()
        assert len(attempts) == 2

    @patch("work_buddy.errors.classify_error", return_value="transient")
    @patch("work_buddy.obsidian.bridge.get_latency_context", return_value="3 failures")
    @patch("work_buddy.obsidian.bridge.is_available", return_value=False)
    def test_bridge_unavailable_skips_call(self, mock_avail, mock_latency, mock_classify):
        call_count = 0

        @bridge_retry(max_retries=2, wait_seconds=0)
        def should_not_be_called_twice():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionError("first attempt fails")
            return {"ok": True}

        with pytest.raises((ConnectionError, RuntimeError)):
            should_not_be_called_twice()
        assert call_count == 1

    @patch("work_buddy.errors.classify_error", return_value="transient")
    @patch("work_buddy.obsidian.bridge.get_latency_context", return_value="test")
    @patch("work_buddy.obsidian.bridge.is_available", return_value=True)
    def test_third_attempt_success(self, mock_avail, mock_latency, mock_classify):
        attempts = []

        @bridge_retry(max_retries=3, wait_seconds=0)
        def third_time_charm():
            attempts.append(1)
            if len(attempts) < 3:
                raise ConnectionError("not yet")
            return "finally"

        assert third_time_charm() == "finally"
        assert len(attempts) == 3


# ---------------------------------------------------------------------------
# @bridge_retry decorator — return-value path (bridge_failure protocol)
# ---------------------------------------------------------------------------

class TestBridgeRetryReturnValue:
    """Return-value retry via bridge_failure() marker."""

    @patch("work_buddy.obsidian.bridge.get_latency_context", return_value="test")
    @patch("work_buddy.obsidian.bridge.is_available", return_value=True)
    def test_retries_on_bridge_failure_return(self, mock_avail, mock_latency):
        """bridge_failure() return triggers retry, success on second attempt."""
        attempts = []

        @bridge_retry(max_retries=3, wait_seconds=0)
        def fails_then_succeeds():
            attempts.append(1)
            if len(attempts) < 2:
                return bridge_failure("Could not read file")
            return {"success": True, "data": "ok"}

        result = fails_then_succeeds()
        assert result == {"success": True, "data": "ok"}
        assert len(attempts) == 2

    @patch("work_buddy.obsidian.bridge.get_latency_context", return_value="test")
    @patch("work_buddy.obsidian.bridge.is_available", return_value=True)
    def test_exhaustion_returns_last_failure(self, mock_avail, mock_latency):
        """On exhaustion, returns the last bridge_failure (never raises)."""
        attempts = []

        @bridge_retry(max_retries=2, wait_seconds=0)
        def always_fails():
            attempts.append(1)
            return bridge_failure(f"attempt {len(attempts)}")

        result = always_fails()
        assert is_bridge_failure(result)
        assert result["message"] == "attempt 2"
        assert len(attempts) == 2

    @patch("work_buddy.obsidian.bridge.get_latency_context", return_value="test")
    @patch("work_buddy.obsidian.bridge.is_available", return_value=True)
    def test_normal_failure_not_retried(self, mock_avail, mock_latency):
        """A normal failure dict (no marker) is returned immediately."""
        attempts = []

        @bridge_retry(max_retries=3, wait_seconds=0)
        def normal_failure():
            attempts.append(1)
            return {"success": False, "message": "Task not found"}

        result = normal_failure()
        assert result == {"success": False, "message": "Task not found"}
        assert len(attempts) == 1  # no retry

    @patch("work_buddy.obsidian.bridge.get_latency_context", return_value="down")
    @patch("work_buddy.obsidian.bridge.is_available", return_value=False)
    def test_bridge_unavailable_returns_failure(self, mock_avail, mock_latency):
        """When bridge is unavailable on retry, returns bridge_failure (no raise)."""
        call_count = 0

        @bridge_retry(max_retries=2, wait_seconds=0)
        def fails_once():
            nonlocal call_count
            call_count += 1
            return bridge_failure("read failed")

        result = fails_once()
        assert is_bridge_failure(result)
        assert call_count == 1  # only called once, second attempt skipped


# ---------------------------------------------------------------------------
# obsidian_retry capability
# ---------------------------------------------------------------------------

class TestObsidianRetryCapability:
    """Tests for the obsidian_retry MCP capability."""

    @patch("work_buddy.obsidian.bridge.get_latency_context", return_value="OK")
    @patch("work_buddy.obsidian.bridge.is_available", return_value=True)
    @patch("work_buddy.mcp_server.registry.get_registry")
    def test_success_returns_result(self, mock_registry, mock_avail, mock_latency):
        mock_entry = MagicMock()
        mock_entry.callable = MagicMock(return_value={"task_id": "t-abc"})
        mock_registry.return_value = {"task_create": mock_entry}

        result = obsidian_retry(
            capability="task_create",
            params={"task_text": "test"},
            max_retries=3,
            wait_seconds=0,
        )

        assert result == {"task_id": "t-abc"}

    @patch("work_buddy.mcp_server.registry.get_registry")
    def test_unknown_capability(self, mock_registry):
        mock_registry.return_value = {}

        result = obsidian_retry(capability="nonexistent", params={})

        assert result["success"] is False
        assert "not found" in result["error"]

    @patch("work_buddy.obsidian.bridge.get_latency_context", return_value="test")
    @patch("work_buddy.obsidian.bridge.is_available", return_value=True)
    @patch("work_buddy.mcp_server.registry.get_registry")
    def test_retries_on_bridge_failure_return(self, mock_registry, mock_avail, mock_latency):
        """bridge_failure returns trigger retry in obsidian_retry too."""
        attempts = []
        mock_entry = MagicMock()
        def side_effect(**kwargs):
            attempts.append(1)
            if len(attempts) < 2:
                return bridge_failure("bridge down")
            return {"success": True}
        mock_entry.callable = side_effect
        mock_registry.return_value = {"my_cap": mock_entry}

        result = obsidian_retry(capability="my_cap", max_retries=3, wait_seconds=0)

        assert result == {"success": True}
        assert len(attempts) == 2
