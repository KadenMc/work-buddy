"""Unit tests for the Werkzeug probe-log filter helper."""

from __future__ import annotations

import io
import logging

import pytest

from work_buddy.web.access_log_filter import install_probe_log_filter


@pytest.fixture
def werkzeug_capture():
    """Set up a capture handler on the werkzeug logger.

    Important: this leaves any pre-existing filters from earlier tests in
    place. ``install_probe_log_filter`` adds a fresh filter each call;
    we clear them in cleanup to keep tests independent.
    """
    logger = logging.getLogger("werkzeug")
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

    # Snapshot existing filters so we can restore them
    original_filters = list(logger.filters)

    yield logger, buf

    logger.removeHandler(handler)
    # Wipe filters added during the test, restore originals
    logger.filters = list(original_filters)


def _emit(logger, lines):
    for line in lines:
        logger.info(line)


def test_filter_silences_bare_get_health(werkzeug_capture):
    """A ``GET /health`` line with whitespace before HTTP/1.1 is silenced."""
    logger, buf = werkzeug_capture
    install_probe_log_filter(["/health"])
    _emit(logger, [
        '127.0.0.1 - - [05/May/2026 11:19:24] "GET /health HTTP/1.1" 200 -',
    ])
    assert "/health" not in buf.getvalue()


def test_filter_silences_querystring_get_messages(werkzeug_capture):
    """A ``GET /messages?...`` poll with query string is silenced."""
    logger, buf = werkzeug_capture
    install_probe_log_filter(["/messages"])
    _emit(logger, [
        '127.0.0.1 - - [05/May/2026 11:19:24] "GET /messages?recipient=x HTTP/1.1" 204 -',
    ])
    assert "/messages" not in buf.getvalue()


def test_filter_passes_post_messages(werkzeug_capture):
    """POSTs to a silenced path are preserved (writes are signal, not noise)."""
    logger, buf = werkzeug_capture
    install_probe_log_filter(["/messages"])
    _emit(logger, [
        '127.0.0.1 - - [05/May/2026 11:19:24] "POST /messages HTTP/1.1" 201 -',
    ])
    assert "POST /messages" in buf.getvalue()


def test_filter_passes_unrelated_paths(werkzeug_capture):
    """Endpoints not in the probe list are unaffected."""
    logger, buf = werkzeug_capture
    install_probe_log_filter(["/health"])
    _emit(logger, [
        '127.0.0.1 - - [05/May/2026 11:19:24] "GET /api/costs HTTP/1.1" 200 -',
        '127.0.0.1 - - [05/May/2026 11:19:24] "GET /healthcheck HTTP/1.1" 200 -',
    ])
    out = buf.getvalue()
    assert "/api/costs" in out
    assert "/healthcheck" in out  # near-match must not be filtered


def test_filter_silences_head_probes(werkzeug_capture):
    """HEAD requests to probe endpoints are silenced (some monitors use HEAD)."""
    logger, buf = werkzeug_capture
    install_probe_log_filter(["/health"])
    _emit(logger, [
        '127.0.0.1 - - [05/May/2026 11:19:24] "HEAD /health HTTP/1.1" 200 -',
    ])
    assert "/health" not in buf.getvalue()


def test_empty_probe_list_no_op(werkzeug_capture):
    """Calling with an empty list is a no-op (no filter added)."""
    logger, buf = werkzeug_capture
    before = len(logger.filters)
    install_probe_log_filter([])
    after = len(logger.filters)
    assert before == after
    _emit(logger, [
        '127.0.0.1 - - [05/May/2026 11:19:24] "GET /health HTTP/1.1" 200 -',
    ])
    # No filter installed → line passes through
    assert "/health" in buf.getvalue()


def test_multiple_probe_paths(werkzeug_capture):
    """Multiple paths in one install call all get filtered."""
    logger, buf = werkzeug_capture
    install_probe_log_filter(["/health", "/messages"])
    _emit(logger, [
        '127.0.0.1 - - [05/May/2026 11:19:24] "GET /health HTTP/1.1" 200 -',
        '127.0.0.1 - - [05/May/2026 11:19:24] "GET /messages HTTP/1.1" 200 -',
        '127.0.0.1 - - [05/May/2026 11:19:24] "GET /other HTTP/1.1" 200 -',
    ])
    out = buf.getvalue()
    assert "/health" not in out
    # /messages should be filtered (silent), /other should remain
    assert "/other" in out
    # Only the unfiltered line should remain
    assert out.count("\n") == 1
