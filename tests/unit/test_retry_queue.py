"""Comprehensive tests for the retry queue system.

Covers:
- Error classification (errors.py)
- Operation record enqueue/prune (gateway.py)
- Retry sweep logic (retry_sweep.py)
- Workflow DAG RETRY_PENDING status (workflow.py)
- Conductor resume/fail after retry (conductor.py)
- Config loading
"""

import json
import os
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from work_buddy.errors import classify_error, compute_retry_delay, is_transient_result
from work_buddy.workflow import TaskStatus, WorkflowDAG


# ---------------------------------------------------------------------------
# 1. Error classification (errors.py)
# ---------------------------------------------------------------------------

class TestClassifyError:
    """Test classify_error() with all exception categories."""

    # --- Transient by type ---
    def test_timeout_error(self):
        assert classify_error(TimeoutError("timed out")) == "transient"

    def test_timeout_error_no_message(self):
        assert classify_error(TimeoutError()) == "transient"

    def test_connection_refused(self):
        assert classify_error(ConnectionRefusedError()) == "transient"

    def test_connection_reset(self):
        assert classify_error(ConnectionResetError()) == "transient"

    def test_connection_aborted(self):
        assert classify_error(ConnectionAbortedError()) == "transient"

    # --- Transient by message pattern ---
    def test_runtime_error_bridge_via_runtime_pattern(self):
        """RuntimeError with 'not available' message — caught by the
        RuntimeError-specific 'not available' / 'not running' pattern,
        NOT by the legacy 'bridge' string match (which CP9 removed)."""
        assert classify_error(RuntimeError("Obsidian bridge not available")) == "transient"

    def test_runtime_error_timeout(self):
        assert classify_error(RuntimeError("Request timed out after 15s")) == "transient"

    def test_oserror_connection_refused(self):
        assert classify_error(OSError("connection refused")) == "transient"

    def test_runtime_error_not_running(self):
        assert classify_error(RuntimeError("Obsidian not running")) == "transient"

    def test_runtime_error_unreachable(self):
        assert classify_error(RuntimeError("Service unreachable")) == "transient"

    def test_oserror_winerror_via_connection_refused(self):
        """WinError 10061 surfaces as 'connection refused' in the message —
        caught by the 'connection refused' pattern; the bare 'winerror 10061'
        pattern was removed in CP9 (subsumed by the typed-exception path
        for Obsidian and by 'connection refused' for non-Obsidian)."""
        assert classify_error(OSError("WinError 10061: connection refused")) == "transient"

    def test_urlopen_error_via_timeout(self):
        """'urlopen error: timed out' matches the 'timed out' pattern;
        the bare 'urlopen error' pattern was removed in CP9 (Obsidian's
        urllib failures now come through the typed-exception path)."""
        assert classify_error(RuntimeError("urlopen error: timed out")) == "transient"

    # --- Permanent by type ---
    def test_type_error(self):
        assert classify_error(TypeError("missing arg")) == "permanent"

    def test_key_error(self):
        assert classify_error(KeyError("bad_key")) == "permanent"

    def test_value_error(self):
        assert classify_error(ValueError("invalid")) == "permanent"

    def test_attribute_error(self):
        assert classify_error(AttributeError("no such attr")) == "permanent"

    def test_import_error(self):
        assert classify_error(ImportError("no module")) == "permanent"

    def test_permission_error(self):
        assert classify_error(PermissionError("denied")) == "permanent"

    def test_file_not_found(self):
        assert classify_error(FileNotFoundError("nope")) == "permanent"

    # --- Unknown ---
    def test_generic_runtime_error(self):
        assert classify_error(RuntimeError("something went wrong")) == "unknown"

    def test_generic_exception(self):
        assert classify_error(Exception("mystery")) == "unknown"

    def test_custom_exception(self):
        class CustomError(Exception):
            pass
        assert classify_error(CustomError("custom")) == "unknown"

    # --- URLError wrapping ---
    def test_urlerror_wrapping_timeout(self):
        """URLError wrapping a timeout should be transient."""
        from urllib.error import URLError
        exc = URLError(reason=TimeoutError("timed out"))
        assert classify_error(exc) == "transient"

    def test_urlerror_wrapping_connection_refused(self):
        from urllib.error import URLError
        exc = URLError(reason=ConnectionRefusedError())
        assert classify_error(exc) == "transient"


class TestClassifyTypedObsidianErrors:
    """Typed ObsidianError subclasses are classified by their ``error_kind``,
    bypassing string-pattern matching entirely.

    Permanent ("user must act out of band" — retrying without user action
    never succeeds, so the op is NOT auto-enqueued): ObsidianRefused (4xx
    other than 409), ObsidianNotRunning (app closed), ObsidianPluginMissing,
    ObsidianPluginDisabled.
    Transient (a temporarily-unavailable bridge that auto-recovers via the
    retry queue): startup race, timeout, server error, editor conflict, etc.
    """

    def test_obsidian_not_running_is_permanent(self):
        """Co-migrated from transient: a deliberately-closed Obsidian is a
        user-must-act condition. Auto-retrying it 5x just churns and emits an
        exhaustion notification; fail fast instead. (A *booting* bridge is
        ObsidianStartupRace, which stays transient and still auto-recovers.)"""
        from work_buddy.obsidian.errors import ObsidianNotRunning
        assert classify_error(ObsidianNotRunning()) == "permanent"

    def test_obsidian_plugin_missing_is_permanent(self):
        """Co-migrated from transient: the user must install the plugin —
        retrying never helps."""
        from work_buddy.obsidian.errors import ObsidianPluginMissing
        assert classify_error(ObsidianPluginMissing()) == "permanent"

    def test_obsidian_plugin_disabled_is_permanent(self):
        """Co-migrated from transient: the user must enable the plugin —
        retrying never helps."""
        from work_buddy.obsidian.errors import ObsidianPluginDisabled
        assert classify_error(ObsidianPluginDisabled()) == "permanent"

    def test_obsidian_startup_race_is_transient(self):
        from work_buddy.obsidian.errors import ObsidianStartupRace
        assert classify_error(ObsidianStartupRace()) == "transient"

    def test_obsidian_unreachable_base_is_transient(self):
        from work_buddy.obsidian.errors import ObsidianUnreachable
        assert classify_error(ObsidianUnreachable()) == "transient"

    def test_obsidian_timeout_is_transient(self):
        from work_buddy.obsidian.errors import ObsidianTimeout
        assert classify_error(ObsidianTimeout()) == "transient"

    def test_obsidian_post_write_uncertain_is_transient(self):
        """PostWriteUncertain is transient at the classification layer.
        The gateway's CP5 verify happens BEFORE classification — by the
        time classify_error sees this exception, the verify already
        decided it's a real failure and should be enqueued."""
        from work_buddy.obsidian.errors import ObsidianPostWriteUncertain
        assert classify_error(ObsidianPostWriteUncertain("x.md")) == "transient"

    def test_obsidian_editor_conflict_is_transient(self):
        """The user finishes typing → next retry succeeds. Worth retrying."""
        from work_buddy.obsidian.errors import ObsidianEditorConflict
        assert classify_error(ObsidianEditorConflict("x.md")) == "transient"

    def test_obsidian_server_error_is_transient(self):
        from work_buddy.obsidian.errors import ObsidianServerError
        assert classify_error(ObsidianServerError(503)) == "transient"

    def test_obsidian_http_error_base_is_transient(self):
        """Generic HTTPError (shouldn't normally be raised — subclasses
        cover the meaningful cases) is treated as transient by default."""
        from work_buddy.obsidian.errors import ObsidianHTTPError
        assert classify_error(ObsidianHTTPError(599)) == "transient"

    # --- The one permanent type ---

    def test_obsidian_refused_is_permanent(self):
        """4xx other than 409 — structural refusal, no retry will help."""
        from work_buddy.obsidian.errors import ObsidianRefused
        assert classify_error(ObsidianRefused(403)) == "permanent"

    def test_obsidian_refused_404_is_permanent(self):
        from work_buddy.obsidian.errors import ObsidianRefused
        assert classify_error(ObsidianRefused(404)) == "permanent"

    # --- isinstance is the fast path, not name-matching ---

    def test_typed_path_wins_over_message_matching(self):
        """A typed exception whose message DOESN'T contain transient
        keywords should still be transient (the type wins)."""
        from work_buddy.obsidian.errors import ObsidianTimeout
        # Construct with an explicit message that lacks transient keywords.
        # If isinstance is the resolver, this stays transient regardless.
        exc = ObsidianTimeout("xyzzy")
        assert "timeout" not in str(exc).lower()
        assert "timed" not in str(exc).lower()
        # isinstance(exc, ObsidianError) wins over the message-pattern path.
        assert classify_error(exc) == "transient"

    def test_obsidian_error_base_is_transient(self):
        """Plain ObsidianError (rarely raised) — defaults to transient
        rather than 'unknown' so retry queues don't drop the signal."""
        from work_buddy.obsidian.errors import ObsidianError
        assert classify_error(ObsidianError("generic")) == "transient"


class TestIsTransientResult:
    """Test is_transient_result() with various return value patterns."""

    def test_none_is_transient(self):
        assert is_transient_result(None) is True

    def test_error_timeout(self):
        assert is_transient_result({"error": "Bridge timed out"}) is True

    def test_error_connection_refused(self):
        assert is_transient_result({"error": "connection refused"}) is True

    def test_error_unreachable(self):
        assert is_transient_result({"error": "Service unreachable"}) is True

    def test_error_bridge_via_unreachable(self):
        """Pre-CP9 the bare 'bridge' string was a transient pattern.
        Post-CP9 it isn't — Obsidian failures use error_kind instead.
        A non-Obsidian message containing 'unreachable' still matches
        though, which covers most legacy bridge-failure-style messages."""
        assert is_transient_result({"error": "bridge unreachable"}) is True

    def test_error_permanent(self):
        assert is_transient_result({"error": "Invalid parameter: foo"}) is False

    def test_success_false_with_transient_message(self):
        assert is_transient_result({"success": False, "message": "bridge unreachable"}) is True

    def test_success_false_with_permanent_message(self):
        assert is_transient_result({"success": False, "message": "bad input"}) is False

    def test_success_true(self):
        assert is_transient_result({"success": True}) is False

    def test_string_result(self):
        assert is_transient_result("just a string") is False

    def test_int_result(self):
        assert is_transient_result(42) is False

    def test_empty_dict(self):
        assert is_transient_result({}) is False

    def test_dict_no_error_key(self):
        assert is_transient_result({"data": [1, 2, 3]}) is False


class TestIsTransientResultErrorKind:
    """CP3: result dicts can carry an `error_kind` field (set by the
    gateway in CP4 when an ObsidianError is caught). When present, it
    wins over any string-pattern matching."""

    @pytest.mark.parametrize("error_kind", [
        "obsidian_unreachable",
        "obsidian_startup_race",
        "obsidian_timeout",
        "obsidian_post_write_uncertain",
        "obsidian_editor_conflict",
        "obsidian_server_error",
        "obsidian_http_error",
        "obsidian_unknown",
    ])
    def test_transient_obsidian_kinds(self, error_kind):
        result = {"error": "anything", "error_kind": error_kind}
        assert is_transient_result(result) is True

    @pytest.mark.parametrize("error_kind", [
        "obsidian_refused",          # 4xx other than 409 — structural refusal
        "obsidian_not_running",      # app closed — user must open it
        "obsidian_plugin_missing",   # plugin not installed — user must install
        "obsidian_plugin_disabled",  # plugin disabled — user must enable
    ])
    def test_permanent_obsidian_kinds(self, error_kind):
        """"User must act out of band" kinds are NOT retried: a result dict
        carrying one of these is permanent, so the gateway won't auto-enqueue
        it (no 5x churn + exhaustion notification on a deliberately-closed
        bridge). Co-migrated: not_running / plugin_missing / plugin_disabled
        moved here from the transient set."""
        result = {"error": "anything", "error_kind": error_kind}
        assert is_transient_result(result) is False

    def test_error_kind_wins_over_transient_message(self):
        """error_kind=obsidian_refused beats a misleading 'timed out'
        message — the structured signal is the source of truth."""
        result = {
            "error": "operation timed out somewhere upstream",
            "error_kind": "obsidian_refused",
        }
        assert is_transient_result(result) is False

    def test_error_kind_wins_over_permanent_message(self):
        """error_kind=obsidian_timeout beats a misleading 'Invalid'
        message — gateway populates kind from the actual exception."""
        result = {
            "error": "Invalid foo",  # would match no pattern
            "error_kind": "obsidian_timeout",
        }
        assert is_transient_result(result) is True

    def test_unknown_error_kind_falls_through_to_message(self):
        """An error_kind value we don't recognize falls back to message
        matching as a safety net."""
        result = {
            "error": "bridge unreachable",
            "error_kind": "some_future_kind_not_in_lists",
        }
        # Falls through to message matching → "bridge" or "unreachable" matches.
        assert is_transient_result(result) is True

    def test_unknown_error_kind_with_permanent_message(self):
        result = {
            "error": "bad input",
            "error_kind": "some_future_kind",
        }
        assert is_transient_result(result) is False

    def test_error_kind_present_but_not_a_string(self):
        """Defensive: error_kind=42 shouldn't crash; falls through to
        message matching."""
        result = {"error": "timed out", "error_kind": 42}
        assert is_transient_result(result) is True


class TestComputeRetryDelay:
    """Test compute_retry_delay() with all backoff strategies."""

    def test_fixed_10s_constant(self):
        for attempt in range(1, 10):
            assert compute_retry_delay(attempt, "fixed_10s") == 10

    def test_exponential_growth(self):
        assert compute_retry_delay(1, "exponential") == 10
        assert compute_retry_delay(2, "exponential") == 20
        assert compute_retry_delay(3, "exponential") == 40
        assert compute_retry_delay(4, "exponential") == 80

    def test_exponential_cap(self):
        assert compute_retry_delay(5, "exponential") == 120
        assert compute_retry_delay(10, "exponential") == 120

    def test_adaptive_schedule(self):
        expected = [10, 20, 45, 90, 120]
        for i, delay in enumerate(expected, 1):
            assert compute_retry_delay(i, "adaptive") == delay

    def test_adaptive_cap(self):
        assert compute_retry_delay(6, "adaptive") == 120
        assert compute_retry_delay(100, "adaptive") == 120

    def test_unknown_strategy_fallback(self):
        assert compute_retry_delay(1, "nonexistent") == 10


# ---------------------------------------------------------------------------
# 2. Operation record enqueue/prune (gateway.py)
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_ops_dir(tmp_path):
    """Create a temporary operations directory and patch gateway to use it."""
    ops_dir = tmp_path / "operations"
    ops_dir.mkdir()

    with patch("work_buddy.mcp_server.tools.gateway._get_operations_dir", return_value=ops_dir):
        yield ops_dir


class TestEnqueueForRetry:
    """Test _enqueue_for_retry() operation record updates."""

    def _make_op_record(self, ops_dir, op_id="op_test123", name="test_cap"):
        """Create a minimal operation record file."""
        now = datetime.now(timezone.utc)
        record = {
            "operation_id": op_id,
            "type": "capability",
            "name": name,
            "params": {"key": "value"},
            "retry_policy": "replay",
            "status": "running",
            "result": None,
            "error": None,
            "attempt": 1,
            "session_id": "test-session",
            "locked_until": (now + timedelta(seconds=90)).isoformat(),
            "created_at": now.isoformat(),
            "completed_at": None,
        }
        path = ops_dir / f"{op_id}.json"
        path.write_text(json.dumps(record, indent=2))
        return record

    def test_enqueue_sets_retry_fields(self, tmp_ops_dir):
        from work_buddy.mcp_server.tools.gateway import _enqueue_for_retry, _load_operation

        with patch("work_buddy.mcp_server.tools.gateway._load_operation") as mock_load, \
             patch("work_buddy.mcp_server.tools.gateway._update_operation") as mock_update:
            record = self._make_op_record(tmp_ops_dir)
            mock_load.return_value = record.copy()

            _enqueue_for_retry(
                "op_test123", "TimeoutError: timed out", "transient",
                delay_seconds=10,
                max_retries=5,
                backoff_strategy="adaptive",
                originating_session_id="session-abc",
            )

            assert mock_update.called
            updated = mock_update.call_args[0][0]
            # Canonical fields
            assert updated["queued"] is True
            assert updated["queue_reason"] == "retry"
            # Legacy alias kept for transitional compatibility
            assert updated["queued_for_retry"] is True
            assert updated["status"] == "failed"
            assert updated["error"] == "TimeoutError: timed out"
            assert updated["max_retries"] == 5
            assert updated["backoff_strategy"] == "adaptive"
            assert updated["error_class"] == "transient"
            assert updated["originating_session_id"] == "session-abc"
            assert updated["retry_at"] is not None
            assert len(updated["retry_history"]) == 1
            assert updated["retry_history"][0]["error_class"] == "transient"

    def test_enqueue_with_error_kind_persists(self, tmp_ops_dir):
        """CP4: error_kind is stored on op record + retry_history entry."""
        from work_buddy.mcp_server.tools.gateway import _enqueue_for_retry

        with patch("work_buddy.mcp_server.tools.gateway._load_operation") as mock_load, \
             patch("work_buddy.mcp_server.tools.gateway._update_operation") as mock_update:
            record = self._make_op_record(tmp_ops_dir)
            mock_load.return_value = record.copy()

            _enqueue_for_retry(
                "op_test123", "ObsidianTimeout: HTTP hung", "transient",
                originating_session_id="session-abc",
                error_kind="obsidian_timeout",
            )

            updated = mock_update.call_args[0][0]
            assert updated["error_kind"] == "obsidian_timeout"
            # Stored on retry_history too so cross-attempt diffing works.
            assert updated["retry_history"][0]["error_kind"] == "obsidian_timeout"

    def test_enqueue_without_error_kind_omits_field(self, tmp_ops_dir):
        """Non-Obsidian failures don't carry error_kind — field should
        not be added at all (vs being added with value None)."""
        from work_buddy.mcp_server.tools.gateway import _enqueue_for_retry

        with patch("work_buddy.mcp_server.tools.gateway._load_operation") as mock_load, \
             patch("work_buddy.mcp_server.tools.gateway._update_operation") as mock_update:
            record = self._make_op_record(tmp_ops_dir)
            mock_load.return_value = record.copy()

            _enqueue_for_retry(
                "op_test123", "Generic timeout", "transient",
                originating_session_id="session-abc",
                # no error_kind kwarg
            )

            updated = mock_update.call_args[0][0]
            assert "error_kind" not in updated
            assert "error_kind" not in updated["retry_history"][0]

    # CP-A7: persist the PostWriteUncertain carrier so the retry sweep
    # can pre-verify before replaying the read-modify-write capability.

    def test_enqueue_persists_pwu_carrier(self, tmp_ops_dir):
        from work_buddy.mcp_server.tools.gateway import _enqueue_for_retry

        carrier = {
            "path": "notes/x.md",
            "content_hint": "hello world",
            "write_mode": "insert",
        }
        with patch("work_buddy.mcp_server.tools.gateway._load_operation") as mock_load, \
             patch("work_buddy.mcp_server.tools.gateway._update_operation") as mock_update:
            record = self._make_op_record(tmp_ops_dir)
            mock_load.return_value = record.copy()

            _enqueue_for_retry(
                "op_test123",
                "ObsidianPostWriteUncertain: ...",
                "transient",
                error_kind="obsidian_post_write_uncertain",
                pwu_carrier=carrier,
            )

            updated = mock_update.call_args[0][0]
            assert updated["pwu_carrier"] == carrier

    def test_enqueue_without_pwu_carrier_omits_field(self, tmp_ops_dir):
        """Non-PWU failures don't carry pwu_carrier — field absent."""
        from work_buddy.mcp_server.tools.gateway import _enqueue_for_retry

        with patch("work_buddy.mcp_server.tools.gateway._load_operation") as mock_load, \
             patch("work_buddy.mcp_server.tools.gateway._update_operation") as mock_update:
            record = self._make_op_record(tmp_ops_dir)
            mock_load.return_value = record.copy()

            _enqueue_for_retry(
                "op_test123", "ObsidianTimeout: ...", "transient",
                error_kind="obsidian_timeout",
                # no pwu_carrier kwarg
            )

            updated = mock_update.call_args[0][0]
            assert "pwu_carrier" not in updated

    def test_enqueue_nonexistent_op_is_noop(self, tmp_ops_dir):
        from work_buddy.mcp_server.tools.gateway import _enqueue_for_retry

        with patch("work_buddy.mcp_server.tools.gateway._load_operation", return_value=None), \
             patch("work_buddy.mcp_server.tools.gateway._update_operation") as mock_update:
            _enqueue_for_retry("op_nonexistent", "error", "transient")
            assert not mock_update.called

    def test_enqueue_uses_config_defaults(self, tmp_ops_dir):
        from work_buddy.mcp_server.tools.gateway import _enqueue_for_retry

        record = self._make_op_record(tmp_ops_dir)
        with patch("work_buddy.mcp_server.tools.gateway._load_operation", return_value=record.copy()), \
             patch("work_buddy.mcp_server.tools.gateway._update_operation") as mock_update, \
             patch("work_buddy.config.load_config", return_value={
                 "sidecar": {"retry_queue": {"max_retries": 7, "default_backoff": "exponential"}}
             }):
            _enqueue_for_retry("op_test123", "error", "transient")
            updated = mock_update.call_args[0][0]
            assert updated["max_retries"] == 7
            assert updated["backoff_strategy"] == "exponential"


class TestCompleteOperationErrorKind:
    """CP4: _complete_operation persists error_kind alongside the error string.

    Lives in its own class because it tests _complete_operation, not
    _enqueue_for_retry. (Tests for _enqueue's error_kind support are
    in TestEnqueueForRetry above.)
    """

    def test_complete_with_error_kind(self, tmp_path):
        from work_buddy.mcp_server.tools.gateway import _complete_operation

        ops_dir = tmp_path / "operations"
        ops_dir.mkdir()
        op_id = "op_test_kind"
        record = {
            "operation_id": op_id,
            "type": "capability",
            "name": "test_cap",
            "status": "running",
        }
        (ops_dir / f"{op_id}.json").write_text(json.dumps(record))

        with patch(
            "work_buddy.mcp_server.tools.gateway._get_operations_dir",
            return_value=ops_dir,
        ):
            _complete_operation(
                op_id, error="ObsidianTimeout: x",
                error_kind="obsidian_timeout",
            )

        loaded = json.loads((ops_dir / f"{op_id}.json").read_text())
        assert loaded["status"] == "failed"
        assert loaded["error"] == "ObsidianTimeout: x"
        assert loaded["error_kind"] == "obsidian_timeout"

    def test_complete_without_error_kind_omits_field(self, tmp_path):
        from work_buddy.mcp_server.tools.gateway import _complete_operation

        ops_dir = tmp_path / "operations"
        ops_dir.mkdir()
        op_id = "op_test_no_kind"
        record = {
            "operation_id": op_id,
            "type": "capability",
            "name": "test_cap",
            "status": "running",
        }
        (ops_dir / f"{op_id}.json").write_text(json.dumps(record))

        with patch(
            "work_buddy.mcp_server.tools.gateway._get_operations_dir",
            return_value=ops_dir,
        ):
            _complete_operation(op_id, error="Generic")  # no error_kind

        loaded = json.loads((ops_dir / f"{op_id}.json").read_text())
        assert "error_kind" not in loaded

    def test_complete_success_no_error_kind(self, tmp_path):
        """Success path doesn't add error_kind."""
        from work_buddy.mcp_server.tools.gateway import _complete_operation

        ops_dir = tmp_path / "operations"
        ops_dir.mkdir()
        op_id = "op_test_success"
        record = {
            "operation_id": op_id,
            "type": "capability",
            "name": "test_cap",
            "status": "running",
        }
        (ops_dir / f"{op_id}.json").write_text(json.dumps(record))

        with patch(
            "work_buddy.mcp_server.tools.gateway._get_operations_dir",
            return_value=ops_dir,
        ):
            _complete_operation(op_id, result={"data": "ok"})

        loaded = json.loads((ops_dir / f"{op_id}.json").read_text())
        assert loaded["status"] == "completed"
        assert "error_kind" not in loaded


class TestPruneOldOperations:
    """Test _prune_old_operations() preserves queued retry ops."""

    def test_prune_skips_queued_retry_ops(self, tmp_ops_dir):
        from work_buddy.mcp_server.tools.gateway import _prune_old_operations

        now = datetime.now(timezone.utc)
        old_time = (now - timedelta(hours=2)).isoformat()

        # Old completed op — should be pruned
        completed = {
            "operation_id": "op_old_done",
            "status": "completed",
            "completed_at": old_time,
        }
        (tmp_ops_dir / "op_old_done.json").write_text(json.dumps(completed))

        # Old failed op queued for retry — should NOT be pruned
        queued = {
            "operation_id": "op_old_queued",
            "status": "failed",
            "completed_at": old_time,
            "queued_for_retry": True,
        }
        (tmp_ops_dir / "op_old_queued.json").write_text(json.dumps(queued))

        _prune_old_operations()

        assert not (tmp_ops_dir / "op_old_done.json").exists(), "Completed op should be pruned"
        assert (tmp_ops_dir / "op_old_queued.json").exists(), "Queued retry op should survive"


# ---------------------------------------------------------------------------
# 3. Retry sweep (retry_sweep.py)
# ---------------------------------------------------------------------------

@pytest.fixture
def sweep_ops_dir(tmp_path):
    """Create a temp ops dir and patch retry_sweep to use it."""
    ops_dir = tmp_path / "operations"
    ops_dir.mkdir()
    with patch("work_buddy.sidecar.retry_sweep._get_operations_dir", return_value=ops_dir):
        yield ops_dir


def _make_queued_op(
    ops_dir,
    op_id=None,
    name="sidecar_status",
    retry_at_offset=-5,
    attempt=1,
    max_retries=3,
    strategy="adaptive",
    workflow_context=None,
):
    """Create a queued-for-retry operation record."""
    if op_id is None:
        op_id = f"op_{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc)
    record = {
        "operation_id": op_id,
        "type": "capability",
        "name": name,
        "params": {},
        "retry_policy": "replay",
        "status": "failed",
        "result": None,
        "error": "TimeoutError: timed out",
        "attempt": attempt,
        "session_id": "test-session",
        "locked_until": None,
        "created_at": now.isoformat(),
        "completed_at": now.isoformat(),
        "queued_for_retry": True,
        "retry_at": (now + timedelta(seconds=retry_at_offset)).isoformat(),
        "max_retries": max_retries,
        "backoff_strategy": strategy,
        "error_class": "transient",
        "originating_session_id": "test-session-123",
        "workflow_context": workflow_context,
        "retry_history": [],
    }
    path = ops_dir / f"{op_id}.json"
    path.write_text(json.dumps(record, indent=2))
    return record, path


class TestRetrySweepNotifications:
    """Retry-queue notifications: success FYIs are born ``resolved`` (so they don't
    block the recipient's Stop hook or accumulate); exhaustion notices stay
    ``pending`` so they surface once."""

    def test_on_success_emits_resolved_status(self, sweep_ops_dir):
        from work_buddy.sidecar.retry_sweep import RetrySweep
        sweep = RetrySweep()
        record, _ = _make_queued_op(sweep_ops_dir, name="summary_search")
        with patch("work_buddy.messaging.client.is_service_running", return_value=True), \
             patch("work_buddy.messaging.client.send_message") as mock_send:
            sweep._on_success(record, {"result": "ok"})
        assert mock_send.call_count == 1
        kwargs = mock_send.call_args.kwargs
        assert kwargs["type"] == "retry_success"
        assert kwargs["status"] == "resolved"
        assert kwargs["priority"] == "normal"
        assert kwargs["recipient_session"] == "test-session-123"

    def test_on_exhausted_stays_pending(self, sweep_ops_dir):
        from work_buddy.sidecar.retry_sweep import RetrySweep
        sweep = RetrySweep()
        # queue_reason != "retry" keeps the user-notification surface path quiet,
        # so the test isolates the messaging emission.
        record, _ = _make_queued_op(sweep_ops_dir, name="summary_search")
        record["queue_reason"] = "deferred_submit"
        with patch("work_buddy.messaging.client.is_service_running", return_value=True), \
             patch("work_buddy.messaging.client.send_message") as mock_send:
            sweep._on_exhausted(record, {"error": "TimeoutError"})
        assert mock_send.call_count == 1
        kwargs = mock_send.call_args.kwargs
        assert kwargs["type"] == "retry_exhausted"
        assert kwargs["priority"] == "high"
        # No terminal status override → born pending so the Stop hook surfaces it once.
        assert kwargs.get("status", "pending") == "pending"


class TestRetrySweepIsReady:
    """Test RetrySweep._is_ready() filtering logic."""

    def test_ready_when_retry_at_past(self, sweep_ops_dir):
        from work_buddy.sidecar.retry_sweep import RetrySweep
        sweep = RetrySweep()
        record, _ = _make_queued_op(sweep_ops_dir, retry_at_offset=-10)
        assert sweep._is_ready(record, datetime.now(timezone.utc)) is True

    def test_not_ready_when_retry_at_future(self, sweep_ops_dir):
        from work_buddy.sidecar.retry_sweep import RetrySweep
        sweep = RetrySweep()
        record, _ = _make_queued_op(sweep_ops_dir, retry_at_offset=60)
        assert sweep._is_ready(record, datetime.now(timezone.utc)) is False

    def test_not_ready_when_not_queued(self, sweep_ops_dir):
        from work_buddy.sidecar.retry_sweep import RetrySweep
        sweep = RetrySweep()
        record, _ = _make_queued_op(sweep_ops_dir)
        record["queued_for_retry"] = False
        assert sweep._is_ready(record, datetime.now(timezone.utc)) is False

    def test_not_ready_when_status_not_failed(self, sweep_ops_dir):
        from work_buddy.sidecar.retry_sweep import RetrySweep
        sweep = RetrySweep()
        record, _ = _make_queued_op(sweep_ops_dir)
        record["status"] = "completed"
        assert sweep._is_ready(record, datetime.now(timezone.utc)) is False

    def test_not_ready_when_attempts_exhausted(self, sweep_ops_dir):
        from work_buddy.sidecar.retry_sweep import RetrySweep
        sweep = RetrySweep()
        record, _ = _make_queued_op(sweep_ops_dir, attempt=3, max_retries=3)
        assert sweep._is_ready(record, datetime.now(timezone.utc)) is False

    def test_not_ready_when_lease_active(self, sweep_ops_dir):
        from work_buddy.sidecar.retry_sweep import RetrySweep
        sweep = RetrySweep()
        record, _ = _make_queued_op(sweep_ops_dir)
        record["locked_until"] = (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()
        assert sweep._is_ready(record, datetime.now(timezone.utc)) is False

    def test_not_ready_when_too_old(self, sweep_ops_dir):
        from work_buddy.sidecar.retry_sweep import RetrySweep
        sweep = RetrySweep(config={"sidecar": {"retry_queue": {"max_retry_age_minutes": 5}}})
        record, _ = _make_queued_op(sweep_ops_dir)
        record["created_at"] = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        assert sweep._is_ready(record, datetime.now(timezone.utc)) is False

    def test_not_ready_when_disabled(self, sweep_ops_dir):
        from work_buddy.sidecar.retry_sweep import RetrySweep
        sweep = RetrySweep(config={"sidecar": {"retry_queue": {"enabled": False}}})
        _make_queued_op(sweep_ops_dir)
        results = sweep.sweep()
        assert results == []


class TestRetrySweepReplay:
    """Test the sweep's replay execution path."""

    def test_replay_success(self, sweep_ops_dir):
        from work_buddy.sidecar.retry_sweep import RetrySweep
        sweep = RetrySweep()
        record, path = _make_queued_op(sweep_ops_dir, name="sidecar_status")

        # Mock the registry to return a capability that succeeds
        mock_entry = MagicMock()
        mock_entry.callable = MagicMock(return_value={"status": "ok"})

        with patch("work_buddy.sidecar.retry_sweep.RetrySweep._on_success") as mock_success:
            from work_buddy.mcp_server.registry import Capability
            with patch("work_buddy.mcp_server.registry.get_registry", return_value={
                "sidecar_status": mock_entry
            }):
                with patch("work_buddy.mcp_server.registry.Capability", Capability):
                    # Make isinstance check work
                    mock_entry.__class__ = Capability
                    result = sweep._replay(record)

        assert result["success"] is True
        assert result["result"] == {"status": "ok"}

    def test_replay_failure_exception(self, sweep_ops_dir):
        from work_buddy.sidecar.retry_sweep import RetrySweep
        sweep = RetrySweep()
        record, path = _make_queued_op(sweep_ops_dir)

        mock_entry = MagicMock()
        mock_entry.callable = MagicMock(side_effect=TimeoutError("still timing out"))

        from work_buddy.mcp_server.registry import Capability
        with patch("work_buddy.mcp_server.registry.get_registry", return_value={
            "sidecar_status": mock_entry
        }):
            mock_entry.__class__ = Capability
            result = sweep._replay(record)

        assert result["success"] is False
        assert "TimeoutError" in result["error"]
        assert result["transient"] is True

    def test_replay_capability_not_found(self, sweep_ops_dir):
        from work_buddy.sidecar.retry_sweep import RetrySweep
        sweep = RetrySweep()
        record, _ = _make_queued_op(sweep_ops_dir, name="nonexistent_cap")

        with patch("work_buddy.mcp_server.registry.get_registry", return_value={}), \
             patch("work_buddy.mcp_server.registry.get_disabled_registry", return_value={}):
            result = sweep._replay(record)

        assert result["success"] is False
        assert "not found" in result["error"]

    def test_replay_disabled_capability_recovers_via_recheck(self, sweep_ops_dir):
        """Slice C.5 regression test: when a capability is in the disabled
        registry (transient probe failure), the sidecar must re-probe
        via the existing CP-A3 ``recheck_disabled_capability`` mechanism
        (per-capability re-probe, NOT a full registry invalidation).

        Live failure 2026-04-28: a `task_create` retry exhausted with
        "Capability 'task_create' not found in registry" because the
        sidecar's cached registry had task_create disabled (obsidian
        probe transient-failed at build time). The original write had
        landed; the sidecar's failure mode just looked dramatic.

        Post-fix: detect the disabled-state, call
        ``recheck_disabled_capability(name)`` (which re-probes only
        that capability's missing tools), and on success the active
        registry has the capability and replay proceeds normally."""
        from work_buddy.sidecar.retry_sweep import RetrySweep
        from work_buddy.mcp_server.registry import Capability
        sweep = RetrySweep()
        record, _ = _make_queued_op(sweep_ops_dir, name="task_create")

        good_entry = Capability(
            name="task_create",
            description="real",
            category="tasks",
            parameters={},
            callable=MagicMock(return_value={"success": True, "task_id": "t-abc"}),
        )
        # Initial registry state: empty. After recheck_disabled_capability
        # runs and returns True (recovery succeeded), the registry has
        # the capability.
        active_registry_states = [{}, {"task_create": good_entry}]
        recheck_calls: list[str] = []

        def _get_registry_side():
            # First call (before recheck) returns empty; second call
            # (after recheck claims True) returns populated.
            return active_registry_states[min(len(recheck_calls), 1)]

        def _recheck_side(name, *, force=False):
            recheck_calls.append(name)
            return True

        with patch("work_buddy.mcp_server.registry.get_registry",
                   side_effect=_get_registry_side), \
             patch("work_buddy.mcp_server.registry.get_disabled_registry",
                   return_value={"task_create": good_entry}), \
             patch("work_buddy.recovery.recheck_disabled_capability",
                   side_effect=_recheck_side):
            result = sweep._replay(record)

        assert result["success"] is True
        assert recheck_calls == ["task_create"], (
            "recheck_disabled_capability should have been called once "
            "for the disabled capability"
        )

    def test_replay_disabled_capability_falls_back_when_recheck_fails(
        self, sweep_ops_dir,
    ):
        """When recheck_disabled_capability returns False (probe still
        failing), fall back to the disabled entry's callable. The
        bridge call inside should raise a typed transient exception,
        which @bridge_retry treats correctly. This avoids the
        misleading not-found-in-registry path."""
        from work_buddy.sidecar.retry_sweep import RetrySweep
        from work_buddy.mcp_server.registry import Capability
        sweep = RetrySweep()
        record, _ = _make_queued_op(sweep_ops_dir, name="task_create")

        disabled_entry = Capability(
            name="task_create",
            description="disabled placeholder",
            category="tasks",
            parameters={},
            callable=MagicMock(return_value={"success": True, "task_id": "t-x"}),
        )

        with patch("work_buddy.mcp_server.registry.get_registry", return_value={}), \
             patch("work_buddy.mcp_server.registry.get_disabled_registry",
                   return_value={"task_create": disabled_entry}), \
             patch("work_buddy.recovery.recheck_disabled_capability",
                   return_value=False):
            result = sweep._replay(record)

        # Disabled entry's callable was invoked.
        assert result["success"] is True
        disabled_entry.callable.assert_called_once()

    def test_replay_soft_transient_failure(self, sweep_ops_dir):
        from work_buddy.sidecar.retry_sweep import RetrySweep
        sweep = RetrySweep()
        record, _ = _make_queued_op(sweep_ops_dir)

        mock_entry = MagicMock()
        mock_entry.callable = MagicMock(return_value={"error": "bridge timed out"})

        from work_buddy.mcp_server.registry import Capability
        with patch("work_buddy.mcp_server.registry.get_registry", return_value={
            "sidecar_status": mock_entry
        }):
            mock_entry.__class__ = Capability
            result = sweep._replay(record)

        assert result["success"] is False
        assert result["transient"] is True


class TestRetrySweepCPA7PreVerify:
    """CP-A7: when an op record carries pwu_carrier, _replay must
    pre-verify BEFORE invoking the capability. Skips replay on verified
    (avoids double-write); falls through on absent/indeterminate."""

    def test_pre_verify_skips_replay_when_verified(self, sweep_ops_dir):
        """Verified-by-filesystem → mark complete, capability NEVER called."""
        from work_buddy.sidecar.retry_sweep import RetrySweep
        sweep = RetrySweep()
        record, _ = _make_queued_op(sweep_ops_dir, name="vault_write_at_location")
        record["pwu_carrier"] = {
            "path": "notes/x.md",
            "content_hint": "hello world",
            "write_mode": "insert",
        }

        mock_entry = MagicMock()
        mock_entry.callable = MagicMock(return_value={"status": "ok"})

        from work_buddy.mcp_server.registry import Capability
        with patch(
            "work_buddy.mcp_server.registry.get_registry",
            return_value={"vault_write_at_location": mock_entry},
        ), patch(
            "work_buddy.obsidian.post_write_verify.verify_post_write",
            return_value="verified",
        ):
            mock_entry.__class__ = Capability
            result = sweep._replay(record)

        # The capability MUST NOT have been called.
        mock_entry.callable.assert_not_called()
        assert result["success"] is True
        assert result["result"]["status"] == "ok"
        assert result["result"]["post_write_recovery"] is True
        assert "CP-A7 pre-verify" in result["result"]["warning"]

    def test_pre_verify_falls_through_when_absent(self, sweep_ops_dir):
        """Absent → capability runs as normal (no skip)."""
        from work_buddy.sidecar.retry_sweep import RetrySweep
        sweep = RetrySweep()
        record, _ = _make_queued_op(sweep_ops_dir, name="vault_write_at_location")
        record["pwu_carrier"] = {
            "path": "notes/x.md",
            "content_hint": "missing fragment",
            "write_mode": "insert",
        }

        mock_entry = MagicMock()
        mock_entry.callable = MagicMock(return_value={"status": "ok"})

        from work_buddy.mcp_server.registry import Capability
        with patch(
            "work_buddy.mcp_server.registry.get_registry",
            return_value={"vault_write_at_location": mock_entry},
        ), patch(
            "work_buddy.obsidian.post_write_verify.verify_post_write",
            return_value="absent",
        ):
            mock_entry.__class__ = Capability
            result = sweep._replay(record)

        # Capability WAS called (verify said absent → normal replay).
        mock_entry.callable.assert_called_once()
        assert result["success"] is True

    def test_pre_verify_falls_through_when_indeterminate(self, sweep_ops_dir):
        from work_buddy.sidecar.retry_sweep import RetrySweep
        sweep = RetrySweep()
        record, _ = _make_queued_op(sweep_ops_dir, name="vault_write_at_location")
        record["pwu_carrier"] = {
            "path": "notes/x.md",
            "content_hint": "anything",
            "write_mode": "insert",
        }

        mock_entry = MagicMock()
        mock_entry.callable = MagicMock(return_value={"status": "ok"})

        from work_buddy.mcp_server.registry import Capability
        with patch(
            "work_buddy.mcp_server.registry.get_registry",
            return_value={"vault_write_at_location": mock_entry},
        ), patch(
            "work_buddy.obsidian.post_write_verify.verify_post_write",
            return_value="indeterminate",
        ):
            mock_entry.__class__ = Capability
            result = sweep._replay(record)

        mock_entry.callable.assert_called_once()
        assert result["success"] is True

    def test_no_pre_verify_when_no_carrier(self, sweep_ops_dir):
        """Op records without pwu_carrier never call verify_post_write."""
        from work_buddy.sidecar.retry_sweep import RetrySweep
        sweep = RetrySweep()
        record, _ = _make_queued_op(sweep_ops_dir, name="vault_write_at_location")
        # No pwu_carrier set.

        mock_entry = MagicMock()
        mock_entry.callable = MagicMock(return_value={"status": "ok"})

        from work_buddy.mcp_server.registry import Capability
        with patch(
            "work_buddy.mcp_server.registry.get_registry",
            return_value={"vault_write_at_location": mock_entry},
        ), patch(
            "work_buddy.obsidian.post_write_verify.verify_post_write",
        ) as mock_verify:
            mock_entry.__class__ = Capability
            sweep._replay(record)

        # verify_post_write must not have been called for non-PWU ops.
        mock_verify.assert_not_called()
        mock_entry.callable.assert_called_once()

    def test_replay_persists_pwu_carrier_on_post_write_uncertain(self, sweep_ops_dir):
        """When the sweep's own bridge call raises PWU and verify says
        absent, the carrier MUST be persisted on the record so the NEXT
        sweep tick can pre-verify."""
        from work_buddy.obsidian.errors import ObsidianPostWriteUncertain
        from work_buddy.sidecar.retry_sweep import RetrySweep, _write_record

        sweep = RetrySweep()
        record, path = _make_queued_op(sweep_ops_dir, name="vault_write_at_location")

        mock_entry = MagicMock()
        mock_entry.callable = MagicMock(side_effect=ObsidianPostWriteUncertain(
            "notes/y.md", content_hint="fresh hint", write_mode="insert",
        ))

        from work_buddy.mcp_server.registry import Capability
        with patch(
            "work_buddy.mcp_server.registry.get_registry",
            return_value={"vault_write_at_location": mock_entry},
        ), patch(
            "work_buddy.obsidian.post_write_verify.verify_post_write",
            return_value="absent",  # Force fall-through to failure path
        ):
            mock_entry.__class__ = Capability
            result = sweep._replay(record)

        assert result["success"] is False

        # The persisted record must now carry pwu_carrier with the
        # exception's fields, so the NEXT sweep tick can pre-verify.
        persisted = json.loads(path.read_text())
        assert persisted["pwu_carrier"] == {
            "path": "notes/y.md",
            "content_hint": "fresh hint",
            "write_mode": "insert",
        }


class TestRetrySweepScheduleNext:
    """Test _schedule_next() backoff computation."""

    def test_schedule_next_adaptive(self, sweep_ops_dir):
        from work_buddy.sidecar.retry_sweep import RetrySweep
        sweep = RetrySweep()
        record, path = _make_queued_op(sweep_ops_dir, attempt=2, strategy="adaptive")

        sweep._schedule_next(record, "still failing")

        updated = json.loads(path.read_text())
        assert updated["status"] == "failed"
        assert updated["retry_at"] is not None
        assert len(updated["retry_history"]) == 1
        # Attempt 2 with adaptive → 20s delay
        retry_at = datetime.fromisoformat(updated["retry_at"])
        # Should be roughly 20s in the future (with some tolerance)
        now = datetime.now(timezone.utc)
        delta = (retry_at - now).total_seconds()
        assert 15 < delta < 25, f"Expected ~20s delay, got {delta}s"

    def test_schedule_next_exponential(self, sweep_ops_dir):
        from work_buddy.sidecar.retry_sweep import RetrySweep
        sweep = RetrySweep()
        record, path = _make_queued_op(sweep_ops_dir, attempt=3, strategy="exponential")

        sweep._schedule_next(record, "still failing")

        updated = json.loads(path.read_text())
        retry_at = datetime.fromisoformat(updated["retry_at"])
        now = datetime.now(timezone.utc)
        delta = (retry_at - now).total_seconds()
        # Attempt 3 with exponential → 40s delay
        assert 35 < delta < 45, f"Expected ~40s delay, got {delta}s"


class TestRetrySweepFullCycle:
    """End-to-end sweep cycle tests."""

    def test_sweep_success_clears_queue(self, sweep_ops_dir):
        """Successful retry should mark completed and clear queue flag."""
        from work_buddy.sidecar.retry_sweep import RetrySweep
        sweep = RetrySweep()
        record, path = _make_queued_op(sweep_ops_dir, op_id="op_cycle_ok")

        mock_entry = MagicMock()
        mock_entry.callable = MagicMock(return_value={"status": "ok"})

        from work_buddy.mcp_server.registry import Capability
        with patch("work_buddy.mcp_server.registry.get_registry", return_value={
            "sidecar_status": mock_entry
        }), patch.object(sweep, "_on_success"):
            mock_entry.__class__ = Capability
            results = sweep.sweep()

        assert len(results) == 1
        assert results[0]["success"] is True

        updated = json.loads(path.read_text())
        assert updated["status"] == "completed"
        assert updated["queued_for_retry"] is False

    def test_sweep_failure_schedules_next(self, sweep_ops_dir):
        """Failed retry should schedule the next attempt."""
        from work_buddy.sidecar.retry_sweep import RetrySweep
        sweep = RetrySweep()
        record, path = _make_queued_op(sweep_ops_dir, op_id="op_cycle_fail", attempt=1, max_retries=5)

        mock_entry = MagicMock()
        mock_entry.callable = MagicMock(side_effect=TimeoutError("nope"))

        from work_buddy.mcp_server.registry import Capability
        with patch("work_buddy.mcp_server.registry.get_registry", return_value={
            "sidecar_status": mock_entry
        }):
            mock_entry.__class__ = Capability
            results = sweep.sweep()

        assert len(results) == 1
        assert results[0]["success"] is False

        updated = json.loads(path.read_text())
        assert updated["status"] == "failed"
        assert updated["queued_for_retry"] is True  # still queued
        assert updated["attempt"] == 2
        assert updated["retry_at"] is not None

    def test_sweep_exhaustion_clears_queue(self, sweep_ops_dir):
        """Exhausted retries should clear queue and notify."""
        from work_buddy.sidecar.retry_sweep import RetrySweep
        sweep = RetrySweep()
        # attempt=2, max_retries=3 → next attempt (3) will be the last
        record, path = _make_queued_op(
            sweep_ops_dir, op_id="op_cycle_exhaust", attempt=2, max_retries=3
        )

        mock_entry = MagicMock()
        mock_entry.callable = MagicMock(side_effect=TimeoutError("still down"))

        from work_buddy.mcp_server.registry import Capability
        with patch("work_buddy.mcp_server.registry.get_registry", return_value={
            "sidecar_status": mock_entry
        }), patch.object(sweep, "_on_exhausted") as mock_exhausted:
            mock_entry.__class__ = Capability
            results = sweep.sweep()

        assert len(results) == 1
        assert results[0]["success"] is False
        assert mock_exhausted.called

    def test_sweep_skips_not_ready(self, sweep_ops_dir):
        """Operations not ready for retry should be skipped."""
        from work_buddy.sidecar.retry_sweep import RetrySweep
        sweep = RetrySweep()
        # retry_at in the future → not ready
        _make_queued_op(sweep_ops_dir, retry_at_offset=300)

        results = sweep.sweep()
        assert results == []

    def test_sweep_multiple_ops(self, sweep_ops_dir):
        """Multiple ready operations should all be processed."""
        from work_buddy.sidecar.retry_sweep import RetrySweep
        sweep = RetrySweep()
        _make_queued_op(sweep_ops_dir, op_id="op_multi_1")
        _make_queued_op(sweep_ops_dir, op_id="op_multi_2")

        mock_entry = MagicMock()
        mock_entry.callable = MagicMock(return_value={"status": "ok"})

        from work_buddy.mcp_server.registry import Capability
        with patch("work_buddy.mcp_server.registry.get_registry", return_value={
            "sidecar_status": mock_entry
        }), patch.object(sweep, "_on_success"):
            mock_entry.__class__ = Capability
            results = sweep.sweep()

        assert len(results) == 2
        assert all(r["success"] for r in results)


# ---------------------------------------------------------------------------
# 4. Workflow DAG RETRY_PENDING (workflow.py)
# ---------------------------------------------------------------------------

class TestRetryPendingStatus:
    """Test RETRY_PENDING integration with WorkflowDAG."""

    def test_retry_pending_exists(self):
        assert TaskStatus.RETRY_PENDING.value == "retry_pending"

    def test_retry_pending_blocks_dependents(self):
        """A retry_pending predecessor should keep dependents blocked."""
        dag = WorkflowDAG(name="test:retry", description="test")
        dag.add_task("a", name="Step A")
        dag.add_task("b", name="Step B", depends_on=["a"])

        dag.start_task("a")
        # Manually set to retry_pending (simulating what the conductor would do)
        dag._graph.nodes["a"]["status"] = TaskStatus.RETRY_PENDING.value
        dag._update_availability()

        b_status = dag._graph.nodes["b"]["status"]
        assert b_status == TaskStatus.BLOCKED.value, \
            f"Dependent should be BLOCKED when predecessor is RETRY_PENDING, got {b_status}"

    def test_retry_pending_not_complete(self):
        """Workflow with a retry_pending step should NOT be considered complete."""
        dag = WorkflowDAG(name="test:retry2", description="test")
        dag.add_task("a", name="Step A")
        dag.add_task("b", name="Step B")

        dag.start_task("a")
        dag._graph.nodes["a"]["status"] = TaskStatus.RETRY_PENDING.value
        dag.start_task("b")
        dag.complete_task("b", result="done")

        assert not dag.is_complete(), "Workflow should not be complete with retry_pending step"

    def test_retry_pending_in_summary(self):
        """RETRY_PENDING should have an icon in the summary."""
        dag = WorkflowDAG(name="test:retry3", description="test")
        dag.add_task("a", name="Step A")
        dag.start_task("a")
        dag._graph.nodes["a"]["status"] = TaskStatus.RETRY_PENDING.value

        summary = dag.summary()
        assert "RQ" in summary, f"Expected RQ icon in summary, got: {summary}"


# ---------------------------------------------------------------------------
# 5. Conductor resume/fail (conductor.py)
# ---------------------------------------------------------------------------

class TestConductorResumeAfterRetry:
    """Test resume_after_retry() and fail_after_retry_exhaustion()."""

    def test_resume_nonexistent_workflow(self):
        from work_buddy.mcp_server.conductor import resume_after_retry
        result = resume_after_retry("wf_nonexistent", "step1", {"data": "test"})
        assert "error" in result
        assert "not active" in result["error"]

    def test_fail_nonexistent_workflow(self):
        from work_buddy.mcp_server.conductor import fail_after_retry_exhaustion
        result = fail_after_retry_exhaustion("wf_nonexistent", "step1", "all retries failed")
        assert "error" in result

    def test_resume_with_active_workflow(self):
        from work_buddy.mcp_server.conductor import (
            _ACTIVE_RUNS, resume_after_retry,
        )

        dag = WorkflowDAG(name="test:resume", description="test")
        dag.add_task("s1", name="Step 1")
        dag.add_task("s2", name="Step 2", depends_on=["s1"])

        dag.start_task("s1")
        dag._graph.nodes["s1"]["status"] = TaskStatus.RETRY_PENDING.value

        run_id = "wf_test_resume"
        _ACTIVE_RUNS[run_id] = dag

        try:
            result = resume_after_retry(run_id, "s1", {"value": 42})

            assert result.get("resumed") is True
            assert dag._graph.nodes["s1"]["status"] == TaskStatus.COMPLETED.value
            assert dag._graph.nodes["s2"]["status"] == TaskStatus.AVAILABLE.value
        finally:
            _ACTIVE_RUNS.pop(run_id, None)

    def test_resume_wrong_status_rejected(self):
        from work_buddy.mcp_server.conductor import _ACTIVE_RUNS, resume_after_retry

        dag = WorkflowDAG(name="test:wrong_status", description="test")
        dag.add_task("s1", name="Step 1")
        dag.start_task("s1")
        # Status is RUNNING, not RETRY_PENDING

        run_id = "wf_test_wrong"
        _ACTIVE_RUNS[run_id] = dag

        try:
            result = resume_after_retry(run_id, "s1", {})
            assert "error" in result
            assert "not retry_pending" in result["error"]
        finally:
            _ACTIVE_RUNS.pop(run_id, None)

    def test_fail_with_active_workflow(self):
        from work_buddy.mcp_server.conductor import (
            _ACTIVE_RUNS, fail_after_retry_exhaustion,
        )

        dag = WorkflowDAG(name="test:fail", description="test")
        dag.add_task("s1", name="Step 1")
        dag.start_task("s1")
        dag._graph.nodes["s1"]["status"] = TaskStatus.RETRY_PENDING.value

        run_id = "wf_test_fail"
        _ACTIVE_RUNS[run_id] = dag

        try:
            result = fail_after_retry_exhaustion(run_id, "s1", "exhausted")
            assert result.get("failed") is True
            assert dag._graph.nodes["s1"]["status"] == TaskStatus.FAILED.value
        finally:
            _ACTIVE_RUNS.pop(run_id, None)


# ---------------------------------------------------------------------------
# 6. Config loading
# ---------------------------------------------------------------------------

class TestRetryQueueConfig:
    """Test that retry queue config loads from config.yaml."""

    def test_config_loads(self):
        from work_buddy.config import load_config
        cfg = load_config()
        rq = cfg.get("sidecar", {}).get("retry_queue", {})
        if not rq:
            pytest.skip("config.yaml not present (CI environment)")
        assert rq.get("enabled") is True
        assert isinstance(rq.get("max_retries"), int)
        assert rq.get("default_backoff") in ("adaptive", "fixed_10s", "exponential")
        assert isinstance(rq.get("max_retry_age_minutes"), int)


# ---------------------------------------------------------------------------
# 7. Observability (_retry_queue_summary)
# ---------------------------------------------------------------------------

class TestRetryQueueSummary:
    def test_summary_empty(self, tmp_ops_dir):
        from work_buddy.mcp_server.tools.gateway import _retry_queue_summary
        result = _retry_queue_summary()
        assert result == {"queued": 0}

    def test_summary_with_queued_ops(self, tmp_ops_dir):
        from work_buddy.mcp_server.tools.gateway import _retry_queue_summary

        now = datetime.now(timezone.utc)
        retry_at = (now + timedelta(seconds=30)).isoformat()

        queued = {
            "operation_id": "op_q1",
            "status": "failed",
            "queued_for_retry": True,
            "retry_at": retry_at,
            "attempt": 1,
            "max_retries": 5,
        }
        (tmp_ops_dir / "op_q1.json").write_text(json.dumps(queued))

        result = _retry_queue_summary()
        assert result["queued"] == 1
        assert result["next_retry_at"] == retry_at

    def test_summary_exhausted_separate(self, tmp_ops_dir):
        from work_buddy.mcp_server.tools.gateway import _retry_queue_summary

        exhausted = {
            "operation_id": "op_ex",
            "status": "failed",
            "queued_for_retry": True,
            "attempt": 5,
            "max_retries": 5,
        }
        (tmp_ops_dir / "op_ex.json").write_text(json.dumps(exhausted))

        result = _retry_queue_summary()
        assert result["queued"] == 0
        assert result.get("exhausted") == 1


# ---------------------------------------------------------------------------
# 8. _result_error detects {success: False} pattern (Bug fix)
# ---------------------------------------------------------------------------

class TestResultErrorSuccessFalse:
    """Test that _result_error() catches {success: False} results."""

    def test_error_key_still_works(self):
        from work_buddy.mcp_server.tools.gateway import _result_error
        assert _result_error({"error": "something broke"}) == "something broke"

    def test_success_false_with_message(self):
        from work_buddy.mcp_server.tools.gateway import _result_error
        result = {"success": False, "message": "Failed to write note"}
        assert _result_error(result) == "Failed to write note"

    def test_success_false_without_message(self):
        from work_buddy.mcp_server.tools.gateway import _result_error
        result = {"success": False}
        assert _result_error(result) == "Operation returned success=false"

    def test_success_false_empty_message(self):
        from work_buddy.mcp_server.tools.gateway import _result_error
        result = {"success": False, "message": ""}
        assert _result_error(result) == "Operation returned success=false"

    def test_success_true_returns_none(self):
        from work_buddy.mcp_server.tools.gateway import _result_error
        assert _result_error({"success": True, "data": "ok"}) is None

    def test_no_error_no_success_returns_none(self):
        from work_buddy.mcp_server.tools.gateway import _result_error
        assert _result_error({"data": [1, 2, 3]}) is None

    def test_non_dict_returns_none(self):
        from work_buddy.mcp_server.tools.gateway import _result_error
        assert _result_error("just a string") is None
        assert _result_error(42) is None
        assert _result_error(None) is None

    def test_error_key_takes_priority_over_success_false(self):
        """When both 'error' and 'success: False' are present, 'error' wins."""
        from work_buddy.mcp_server.tools.gateway import _result_error
        result = {"error": "explicit error", "success": False, "message": "msg"}
        assert _result_error(result) == "explicit error"


# ---------------------------------------------------------------------------
# 9. retry_sweep._replay detects {success: False} (Bug fix)
# ---------------------------------------------------------------------------

class TestRetrySweepReplaySuccessFalse:
    """Test that retry_sweep detects {success: False, message: ...} results."""

    def test_replay_success_false_transient(self, sweep_ops_dir):
        """success=False with a transient message should be a transient failure."""
        from work_buddy.sidecar.retry_sweep import RetrySweep
        sweep = RetrySweep()
        record, _ = _make_queued_op(sweep_ops_dir)

        mock_entry = MagicMock()
        mock_entry.callable = MagicMock(
            return_value={"success": False, "message": "Failed to write note: bridge timed out"}
        )

        from work_buddy.mcp_server.registry import Capability
        with patch("work_buddy.mcp_server.registry.get_registry", return_value={
            "sidecar_status": mock_entry
        }):
            mock_entry.__class__ = Capability
            result = sweep._replay(record)

        assert result["success"] is False
        assert result["transient"] is True
        assert "bridge" in result["error"].lower()

    def test_replay_success_false_permanent(self, sweep_ops_dir):
        """success=False with a non-transient message should be a permanent failure."""
        from work_buddy.sidecar.retry_sweep import RetrySweep
        sweep = RetrySweep()
        record, _ = _make_queued_op(sweep_ops_dir)

        mock_entry = MagicMock()
        mock_entry.callable = MagicMock(
            return_value={"success": False, "message": "Invalid parameter: bad_key"}
        )

        from work_buddy.mcp_server.registry import Capability
        with patch("work_buddy.mcp_server.registry.get_registry", return_value={
            "sidecar_status": mock_entry
        }):
            mock_entry.__class__ = Capability
            result = sweep._replay(record)

        assert result["success"] is False
        assert result["transient"] is False
