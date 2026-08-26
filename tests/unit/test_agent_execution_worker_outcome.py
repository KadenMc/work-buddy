"""Strict, bounded interpretation of private CLI results into safe exit codes."""

from __future__ import annotations

import json

import pytest

from work_buddy.agent_execution.worker_outcome import (
    MAX_WORKER_RESULT_BYTES,
    WorkerExitCode,
    classify_claude_worker_exit,
)

_AUTH_FAILURE = (
    "Failed to authenticate: OAuth session expired and could not be refreshed"
)


def _result(**overrides: object) -> str:
    return json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": True,
            "result": _AUTH_FAILURE,
            **overrides,
        }
    )


@pytest.mark.parametrize("as_bytes", [False, True])
def test_expired_auth_result_ignores_misleading_success_subtype(
    as_bytes: bool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    payload = _result()
    outcome = classify_claude_worker_exit(
        returncode=1,
        stdout=payload.encode("utf-8") if as_bytes else payload,
    )

    assert outcome is WorkerExitCode.AUTH_REQUIRED
    assert int(outcome) == 3
    assert _AUTH_FAILURE not in caplog.text


@pytest.mark.parametrize(
    "payload",
    [
        None,
        b"",
        b"\xff",
        "not-json",
        '{"type":"result"',
        "null",
        "[]",
        _result() + _result(),
        _result(is_error=False),
        _result(is_error=1),
        _result(type="assistant"),
        _result(result="Unknown CLI failure"),
        _result(result=f"The user wrote: {_AUTH_FAILURE}"),
        _result(result={"message": _AUTH_FAILURE}),
        _result(result=None),
        _result(detail=float("nan")),
        _result().replace(
            '"is_error": true', '"is_error": false, "is_error": true'
        ),
        _result() + " " * MAX_WORKER_RESULT_BYTES,
        _result(detail="é" * (MAX_WORKER_RESULT_BYTES // 2)).replace("\\u00e9", "é"),
        "[" * 2000 + "0" + "]" * 2000,
    ],
    ids=[
        "incomplete",
        "empty",
        "invalid-utf8",
        "plain-text",
        "truncated-json",
        "null",
        "array",
        "multiple-results",
        "not-an-error",
        "nonboolean-error-flag",
        "not-a-result",
        "unknown-error",
        "quoted-user-text",
        "nested-result-text",
        "missing-result-text",
        "non-json-nan",
        "duplicate-field",
        "oversized-ascii",
        "oversized-utf8",
        "excessive-json-depth",
    ],
)
def test_unknown_or_untrusted_output_stays_generic(
    payload: bytes | str | None,
) -> None:
    assert (
        classify_claude_worker_exit(returncode=1, stdout=payload)
        is WorkerExitCode.FAILED
    )


@pytest.mark.parametrize(
    "payload", [_result(), _result(is_error=False), _AUTH_FAILURE]
)
def test_successful_auth_looking_output_cannot_request_reauthentication(
    payload: str,
) -> None:
    assert (
        classify_claude_worker_exit(returncode=0, stdout=payload)
        is WorkerExitCode.SUCCESS
    )


def test_reserved_worker_exit_is_not_inferred_from_child_exit_number() -> None:
    assert (
        classify_claude_worker_exit(returncode=3, stdout=_result(result="unknown"))
        is WorkerExitCode.FAILED
    )
    assert tuple(int(code) for code in WorkerExitCode) == (0, 1, 2, 3)
