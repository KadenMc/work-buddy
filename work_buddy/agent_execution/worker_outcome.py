"""Small, content-free exit contract for an exactly owned hosted worker."""

from __future__ import annotations

import json
from enum import IntEnum
from typing import Any

MAX_WORKER_RESULT_BYTES = 64 * 1024


class WorkerExitCode(IntEnum):
    """Reserved worker outcomes, independent of the child CLI's exit codes."""

    SUCCESS = 0
    FAILED = 1
    INVALID_REQUEST = 2
    AUTH_REQUIRED = 3


_CLAUDE_AUTH_REQUIRED_RESULTS = frozenset(
    {"Failed to authenticate: OAuth session expired and could not be refreshed"}
)


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate worker result field")
        result[key] = value
    return result


def _invalid_json_constant(_value: str) -> None:
    raise ValueError("Invalid worker result constant")


def classify_claude_worker_exit(
    *,
    returncode: int,
    stdout: bytes | str | None,
) -> WorkerExitCode:
    """Classify only a bounded, genuine failed Claude result envelope.

    This deliberately does not search arbitrary output for authentication
    words. Successful model prose, stderr, unknown failures, and incomplete
    output cannot authorize an authentication-specific user-facing message.
    Claude can report ``subtype=success`` alongside a real authentication
    failure; the CLI exit code and strict ``is_error`` flag are authoritative.
    No content from this parser is returned or logged.
    """

    if returncode == 0:
        return WorkerExitCode.SUCCESS
    if not isinstance(stdout, (bytes, str)) or len(stdout) > MAX_WORKER_RESULT_BYTES:
        return WorkerExitCode.FAILED
    try:
        raw = stdout.encode("utf-8") if isinstance(stdout, str) else stdout
        if len(raw) > MAX_WORKER_RESULT_BYTES:
            return WorkerExitCode.FAILED
        result = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_invalid_json_constant,
        )
    except (UnicodeError, ValueError, RecursionError):
        return WorkerExitCode.FAILED
    if (
        not isinstance(result, dict)
        or result.get("type") != "result"
        or result.get("is_error") is not True
    ):
        return WorkerExitCode.FAILED
    diagnostic = result.get("result")
    if (
        isinstance(diagnostic, str)
        and diagnostic.strip() in _CLAUDE_AUTH_REQUIRED_RESULTS
    ):
        return WorkerExitCode.AUTH_REQUIRED
    return WorkerExitCode.FAILED
