"""Unit tests for the @bridge_retry decorator and obsidian_retry capability."""

from __future__ import annotations

from unittest.mock import patch, MagicMock
import pytest

from work_buddy.obsidian.retry import bridge_retry, obsidian_retry


# ---------------------------------------------------------------------------
# @bridge_retry decorator
# ---------------------------------------------------------------------------

class TestBridgeRetryDecorator:
    """Tests for the @bridge_retry decorator."""

    def test_success_on_first_attempt(self):
        """Decorated function succeeds immediately — no retry overhead."""
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
        """Return value passes through without wrapping."""
        @bridge_retry(max_retries=3, wait_seconds=0)
        def returns_string():
            return "plain value"

        assert returns_string() == "plain value"

    def test_preserves_function_metadata(self):
        """functools.wraps preserves __name__ and __doc__."""
        @bridge_retry()
        def my_function():
            """My docstring."""
            pass

        assert my_function.__name__ == "my_function"
        assert my_function.__doc__ == "My docstring."

    def test_args_and_kwargs_forwarded(self):
        """Positional and keyword args are forwarded correctly."""
        @bridge_retry(max_retries=1, wait_seconds=0)
        def echo(a, b, key=None):
            return (a, b, key)

        assert echo(1, 2, key="three") == (1, 2, "three")

    @patch("work_buddy.errors.classify_error", return_value="transient")
    @patch("work_buddy.obsidian.bridge.get_latency_context", return_value="test")
    @patch("work_buddy.obsidian.bridge.is_available", return_value=True)
    def test_retries_on_transient_error(self, mock_avail, mock_latency, mock_classify):
        """Transient errors trigger retry; succeeds on second attempt."""
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
        """Permanent errors raise immediately without retry."""
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
        """After max_retries, the last exception is re-raised."""
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
        """When bridge is unavailable on retry, the function isn't called."""
        call_count = 0

        @bridge_retry(max_retries=2, wait_seconds=0)
        def should_not_be_called_twice():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionError("first attempt fails")
            return {"ok": True}

        # First attempt: bridge check skipped (attempt == 1).
        # Function runs, raises transient.
        # Second attempt: bridge check fails → raises RuntimeError.
        with pytest.raises((ConnectionError, RuntimeError)):
            should_not_be_called_twice()
        # Function was called exactly once (first attempt only)
        assert call_count == 1

    @patch("work_buddy.errors.classify_error", return_value="transient")
    @patch("work_buddy.obsidian.bridge.get_latency_context", return_value="test")
    @patch("work_buddy.obsidian.bridge.is_available", return_value=True)
    def test_third_attempt_success(self, mock_avail, mock_latency, mock_classify):
        """Succeeds on the third attempt after two transient failures."""
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
# obsidian_retry capability
# ---------------------------------------------------------------------------

class TestObsidianRetryCapability:
    """Tests for the obsidian_retry MCP capability.

    obsidian_retry uses deferred imports inside the function body, so we
    patch at the source modules (bridge, errors, registry).
    """

    @patch("work_buddy.errors.is_transient_result", return_value=False)
    @patch("work_buddy.obsidian.bridge.get_latency_context", return_value="OK")
    @patch("work_buddy.obsidian.bridge.is_available", return_value=True)
    @patch("work_buddy.mcp_server.registry.get_entry")
    def test_success_returns_result(self, mock_get_entry, mock_avail, mock_latency, mock_transient):
        """Successful operation returns clean result without latency info."""
        mock_entry = MagicMock()
        mock_entry.callable = MagicMock(return_value={"task_id": "t-abc"})
        mock_get_entry.return_value = mock_entry

        result = obsidian_retry(
            capability="task_create",
            params={"task_text": "test"},
            max_retries=3,
            wait_seconds=0,
        )

        assert result["success"] is True
        assert result["result"] == {"task_id": "t-abc"}
        assert result["attempts"] == 1
        assert "latency_context" not in result

    @patch("work_buddy.mcp_server.registry.get_entry", return_value=None)
    def test_unknown_capability(self, mock_get_entry):
        """Unknown capability returns error without attempting."""
        result = obsidian_retry(capability="nonexistent", params={})

        assert result["success"] is False
        assert "Unknown capability" in result["error"]
        assert result["attempts"] == 0

    @patch("work_buddy.obsidian.bridge.get_latency_context", return_value="5 failures")
    @patch("work_buddy.obsidian.bridge.is_available", return_value=False)
    @patch("work_buddy.mcp_server.registry.get_entry")
    def test_bridge_unavailable_exhaustion(self, mock_get_entry, mock_avail, mock_latency):
        """All retries fail because bridge is never available."""
        mock_entry = MagicMock()
        mock_get_entry.return_value = mock_entry

        result = obsidian_retry(
            capability="task_create",
            params={"task_text": "test"},
            max_retries=2,
            wait_seconds=0,
        )

        assert result["success"] is False
        assert "latency_context" in result
        assert result["attempts"] == 2
        mock_entry.callable.assert_not_called()

    @patch("work_buddy.errors.classify_error", return_value="permanent")
    @patch("work_buddy.obsidian.bridge.get_latency_context", return_value="OK")
    @patch("work_buddy.obsidian.bridge.is_available", return_value=True)
    @patch("work_buddy.mcp_server.registry.get_entry")
    def test_permanent_error_no_retry(self, mock_get_entry, mock_avail, mock_latency, mock_classify):
        """Permanent errors stop retry immediately."""
        mock_entry = MagicMock()
        mock_entry.callable.side_effect = TypeError("bad arg")
        mock_get_entry.return_value = mock_entry

        result = obsidian_retry(
            capability="task_create",
            params={"task_text": "test"},
            max_retries=3,
            wait_seconds=0,
        )

        assert result["success"] is False
        assert result["error_class"] == "permanent"
        assert result["attempts"] == 1

    @patch("work_buddy.errors.is_transient_result", return_value=False)
    @patch("work_buddy.obsidian.bridge.get_latency_context", return_value="OK")
    @patch("work_buddy.obsidian.bridge.is_available", return_value=True)
    @patch("work_buddy.mcp_server.registry.get_entry")
    def test_json_string_params_parsed(self, mock_get_entry, mock_avail, mock_latency, mock_transient):
        """JSON string params are parsed into dict."""
        mock_entry = MagicMock()
        mock_entry.callable.return_value = {"ok": True}
        mock_get_entry.return_value = mock_entry

        result = obsidian_retry(
            capability="test",
            params='{"key": "value"}',
            max_retries=1,
            wait_seconds=0,
        )

        assert result["success"] is True
        mock_entry.callable.assert_called_once_with(key="value")
