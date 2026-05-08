"""Encoding-safety tests for setup_logging().

Ensures sys.stdout / sys.stderr get reconfigured so non-ASCII log
output never crashes regardless of the surrounding console codec.
"""

import io
import logging
import sys

import pytest

from work_buddy import logging_config


@pytest.fixture(autouse=True)
def _reset_logging_config():
    logging_config._configured = False
    yield
    logging_config._configured = False
    logging.getLogger("work_buddy").handlers.clear()


def _cp1252_stream() -> io.TextIOWrapper:
    """Mimic a Windows-default child-process stdout - the precondition
    that triggers the cp1252 UnicodeEncodeError class of bug."""
    return io.TextIOWrapper(io.BytesIO(), encoding="cp1252", write_through=True)


def test_setup_reconfigures_stdout_to_utf8_backslashreplace(monkeypatch):
    monkeypatch.setattr(sys, "stdout", _cp1252_stream())
    logging_config.setup_logging()
    assert sys.stdout.encoding.lower() == "utf-8"
    assert sys.stdout.errors == "backslashreplace"


def test_setup_reconfigures_stderr_to_utf8_backslashreplace(monkeypatch):
    monkeypatch.setattr(sys, "stderr", _cp1252_stream())
    logging_config.setup_logging()
    assert sys.stderr.encoding.lower() == "utf-8"
    assert sys.stderr.errors == "backslashreplace"


def test_setup_handles_unreconfigurable_stream(monkeypatch):
    """Streams without .reconfigure (test harnesses, exotic captures)
    must not break setup."""
    class _NoReconfigure:
        encoding = "cp1252"
        def write(self, s):
            return len(s)
        def flush(self):
            pass
    monkeypatch.setattr(sys, "stdout", _NoReconfigure())
    logging_config.setup_logging()  # must not raise


def test_setup_is_idempotent(monkeypatch):
    monkeypatch.setattr(sys, "stdout", _cp1252_stream())
    logging_config.setup_logging()
    logging_config.setup_logging()  # must not raise


def test_log_with_non_ascii_under_cp1252_does_not_crash(monkeypatch, tmp_path):
    """Class-level regression test. Install a cp1252 sys.stdout, run
    setup_logging, emit non-ASCII through the logger. Without the
    reconfigure block this would raise UnicodeEncodeError."""
    monkeypatch.setattr(logging_config, "_get_log_dir", lambda: tmp_path)
    fake_stdout = _cp1252_stream()
    monkeypatch.setattr(sys, "stdout", fake_stdout)

    # Logger name must be under "work_buddy.*" so it inherits the handlers
    # setup_logging attaches to that logger. A bare "test.encoding" wouldn't.
    log = logging_config.get_logger("work_buddy.test.encoding")
    log.info("smoke - arrow %s em-dash %s", "→", "—")

    for h in logging.getLogger("work_buddy").handlers:
        h.flush()
    fake_stdout.flush()

    raw = fake_stdout.buffer.getvalue()
    assert raw, "no bytes written - handler chain may be broken"
    raw.decode("utf-8")  # raises if not valid utf-8
