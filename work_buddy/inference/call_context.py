"""Ambient context for one inference call — `call_id` and a human `detail`.

This is the low-level glue that lets provenance be captured without threading
parameters through every layer of the LLM/embedding call stack:

- `run_task` (and the `llm_with_tools` / embedding capture points) bind a
  `call_id` for the duration of a call via :func:`bind_call_id`. Anything
  downstream in the same thread — the broker's `slot()`, the cost logger's
  provenance emit — reads it via :func:`current_call_id`. The broker uses it as
  its `SlotMetrics.id`, so a local call's scheduler-latency row and its
  provenance row share one id (joinable).
- Call sites attach a readily-available one-liner via :func:`inference_detail`
  (e.g. a tab title). The provenance writer reads it via :func:`current_detail`
  and composes the description as ``<call site>: <detail>``.

Lives in `work_buddy.inference` (the lowest common layer) so `broker.py` can
read `call_id` without importing the `llm` package — no layering inversion.

ContextVars are thread- and async-local; the LLM call path is synchronous
within a thread, so a value bound by `run_task` is visible to the `broker.slot`
it calls. Values do NOT cross the `llm_submit` async-replay boundary — a detail
set at submit time is gone by sidecar replay (acceptable; submit can stash it in
its replay params if that ever matters).
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

_call_id: ContextVar[str | None] = ContextVar("inference_call_id", default=None)
_detail: ContextVar[str | None] = ContextVar("inference_detail", default=None)
_call_start: ContextVar[float | None] = ContextVar("inference_call_start", default=None)


def current_call_elapsed_ms() -> float | None:
    """End-to-end elapsed ms since `bind_call_id` was entered, if bound.

    Gives the provenance writer a single latency that works for EVERY provider
    (cloud included) without threading timing through the call path.
    """
    start = _call_start.get()
    return None if start is None else (time.monotonic() - start) * 1000.0


def current_call_id() -> str | None:
    """The `call_id` bound for the in-flight inference call, if any."""
    return _call_id.get()


def current_detail() -> str | None:
    """The caller-supplied detail one-liner for the in-flight call, if any."""
    return _detail.get()


@contextmanager
def bind_call_id(call_id: str) -> Iterator[None]:
    """Bind `call_id` (and stamp a start time) for the duration of a call.

    Set by the capture sites (run_task decorator, embedding capture). The start
    stamp powers :func:`current_call_elapsed_ms` so provenance gets an
    end-to-end latency for any provider.
    """
    id_token = _call_id.set(call_id)
    start_token = _call_start.set(time.monotonic())
    try:
        yield
    finally:
        _call_id.reset(id_token)
        _call_start.reset(start_token)


@contextmanager
def inference_detail(detail: str | None) -> Iterator[None]:
    """Attach a readily-available description detail around an inference call.

    No-op when ``detail`` is None/empty, so call sites can pass through an
    optional value unconditionally.
    """
    if not detail:
        yield
        return
    token = _detail.set(detail)
    try:
        yield
    finally:
        _detail.reset(token)
