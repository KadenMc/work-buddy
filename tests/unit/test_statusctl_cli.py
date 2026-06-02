"""The statusctl CLI: argv shorthand, exit-code mapping, wait loop.

Domain reads are faked so these tests exercise the CLI's control flow and
exit-code contract without touching the consent/operation stores.
"""

from __future__ import annotations

import json

import pytest

from work_buddy.statusctl import cli


@pytest.fixture
def fake_consent(monkeypatch):
    state = {"view": {"request_id": "req_x", "state": "pending",
                      "terminal": False, "operation": "task_toggle"}}

    def fake(request_id, *, session_id=None):
        v = dict(state["view"])
        v["request_id"] = request_id
        return v

    monkeypatch.setattr("work_buddy.consent_status.consent_status", fake)
    return state


@pytest.fixture
def fake_op(monkeypatch):
    state = {"view": {"operation_id": "op_x", "state": "running",
                      "terminal": False, "name": "do_thing"}}

    def fake(op_id):
        v = dict(state["view"])
        v["operation_id"] = op_id
        return v

    monkeypatch.setattr("work_buddy.operations_read.operation_status", fake)
    return state


# --- argv preprocessing -----------------------------------------------------

def test_preprocess_injects_status_verb():
    assert cli._preprocess(["consent", "req_1"]) == ["consent", "status", "req_1"]
    assert cli._preprocess(["op", "op_1"]) == ["op", "status", "op_1"]


def test_preprocess_leaves_explicit_verbs():
    assert cli._preprocess(["consent", "wait", "req_1"]) == ["consent", "wait", "req_1"]
    assert cli._preprocess(["consent", "status", "req_1"]) == ["consent", "status", "req_1"]


def test_preprocess_handles_leading_option():
    assert cli._preprocess(["--json", "consent", "req_1"]) == \
        ["--json", "consent", "status", "req_1"]


# --- one-shot status --------------------------------------------------------

def test_oneshot_status_exits_zero(fake_consent, capsys):
    fake_consent["view"]["state"] = "pending"
    assert cli.main(["consent", "status", "req_1"]) == cli.EXIT_OK
    assert "pending" in capsys.readouterr().out


def test_oneshot_not_found_still_exits_zero(fake_consent, capsys):
    fake_consent["view"]["state"] = "not_found"
    assert cli.main(["consent", "req_1"]) == cli.EXIT_OK  # shorthand form
    assert "not_found" in capsys.readouterr().out


def test_oneshot_json_includes_exit_code(fake_consent, capsys):
    fake_consent["view"]["state"] = "granted"
    assert cli.main(["--json", "consent", "status", "req_1"]) == cli.EXIT_OK
    data = json.loads(capsys.readouterr().out)
    assert data["state"] == "granted"
    assert data["exit_code"] == 0


# --- wait: exit-code vocabulary ---------------------------------------------

@pytest.mark.parametrize("state,code", [
    ("granted", cli.EXIT_OK),
    ("denied", cli.EXIT_NEGATIVE),
    ("expired", cli.EXIT_TIMEOUT),
    ("not_found", cli.EXIT_NOT_FOUND),
])
def test_consent_wait_exit_codes(fake_consent, state, code):
    fake_consent["view"]["state"] = state
    fake_consent["view"]["terminal"] = True
    assert cli.main(["consent", "wait", "req_1", "--timeout", "0"]) == code


def test_consent_wait_pending_times_out(fake_consent):
    fake_consent["view"]["state"] = "pending"
    assert cli.main(["consent", "wait", "req_1", "--timeout", "0"]) == cli.EXIT_TIMEOUT


@pytest.mark.parametrize("state,code", [
    ("completed", cli.EXIT_OK),
    ("failed", cli.EXIT_NEGATIVE),
    ("not_found", cli.EXIT_NOT_FOUND),
])
def test_op_wait_exit_codes(fake_op, state, code):
    fake_op["view"]["state"] = state
    fake_op["view"]["terminal"] = state in ("completed", "failed", "not_found")
    assert cli.main(["op", "wait", "op_1", "--timeout", "0"]) == code


def test_op_wait_running_times_out(fake_op):
    fake_op["view"]["state"] = "running"
    assert cli.main(["op", "wait", "op_1", "--timeout", "0"]) == cli.EXIT_TIMEOUT


def test_op_stale_keeps_waiting_then_times_out(fake_op):
    fake_op["view"]["state"] = "stale"
    assert cli.main(["op", "wait", "op_1", "--timeout", "0"]) == cli.EXIT_TIMEOUT


# --- wait: actually loops ---------------------------------------------------

def test_wait_loops_until_resolved(monkeypatch):
    seq = ["pending", "pending", "granted"]

    def fake(request_id, *, session_id=None):
        s = seq.pop(0) if len(seq) > 1 else seq[0]
        return {"request_id": request_id, "state": s,
                "terminal": s != "pending", "operation": "x"}

    monkeypatch.setattr("work_buddy.consent_status.consent_status", fake)
    monkeypatch.setattr(cli.time, "sleep", lambda *_: None)
    rc = cli.main(["consent", "wait", "req_1", "--timeout", "30", "--poll-interval", "1"])
    assert rc == cli.EXIT_OK


# --- poll cadence -----------------------------------------------------------

def test_next_interval_tiers():
    assert cli._next_interval(0, None) == 2.0
    assert cli._next_interval(60, None) == 5.0
    assert cli._next_interval(600, None) == 15.0
    assert cli._next_interval(600, 3.0) == 3.0  # override pins it


# --- usage errors -----------------------------------------------------------

def test_missing_domain_is_usage_error():
    # argparse exits 2 on missing required subcommand
    assert cli.main([]) == 2
