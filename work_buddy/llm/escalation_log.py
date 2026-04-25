"""Append-only, structured per-job LLM-escalation observability log.

Every resolved LLM job — whether it walked one tier or four — produces a
single record describing the full attempt chain so we can answer:

* "What fraction of journal scans escalated past LOCAL_FAST this week?"
* "Did this triage verdict come from Haiku or Sonnet?"
* "How long did each tier take before falling through?"

The structure is deliberately a sibling of, not a superset of, the
existing per-call cost log (:mod:`work_buddy.llm.cost`). The cost log
is per-API-call (one tier, one billable record); this log is per-job
(one user-facing operation, possibly many tier attempts). Bundling them
would have forced an awkward "child rows under a parent row" shape on
the JSONL writer; keeping them separate lets each stay simple.

There are two emitters today:

* :class:`work_buddy.llm.runner_v2.LLMRunner` writes a record after every
  call resolves — covering backend-error escalation chains.
* Adapter loops that escalate **across** :class:`LLMRunner` calls
  (e.g. :func:`work_buddy.triage.adapters.journal._segment_with_escalation`
  and :func:`work_buddy.triage.verdict_call.call_for_verdict`) write a
  separate record covering their adapter-level chain. Their attempts
  include adapter-side outcomes such as ``"validation_failed"`` that
  ``LLMRunner`` cannot see on its own.

Records share a JSON shape::

    {
      "timestamp": "2026-04-25T03:15:22.118Z",
      "source":    "llm_runner" | "journal_segmenter" | "verdict_call" | "...",
      "trace_id":  "<correlation id, optional>",
      "task_id":   "<call-site identifier, optional>",
      "attempts":  [
        {"tier": "...", "model": "...", "outcome": "...",
         "error_kind": "...", "error": "...", "elapsed_ms": 0,
         "input_tokens": 0, "output_tokens": 0}
      ],
      "final_tier":    "<tier of the resolving attempt>",
      "final_outcome": "success" | "backend_error" | "validation_failed" | ...,
      "metadata":      {<source-specific context, optional>}
    }
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from work_buddy.llm.response import TierAttempt
from work_buddy.paths import resolve

logger = logging.getLogger(__name__)


# A simple lock for serial appends. Multi-process writers won't be
# protected by this, but no current writer runs in a separate process —
# the sidecar, retry sweep, and dashboard all share the same Python
# runtime when this module is loaded.
_lock = threading.Lock()

# Old-record cleanup is handled by ``work_buddy.artifacts.prune_escalation_log``
# registered against ``logs/escalations`` in :data:`work_buddy.paths.PRUNERS`.
# That pruner runs as part of the scheduled artifact-cleanup job and culls
# records older than ``window_days`` (default 30). No file-level rotation
# logic lives here on purpose — adding both record-level pruning and
# size-based rotation would let the two policies disagree.


def _log_path() -> Path:
    return resolve("logs/escalations")


def _attempt_to_dict(a: TierAttempt | dict[str, Any]) -> dict[str, Any]:
    """Coerce a :class:`TierAttempt` (or already-a-dict) to a plain dict."""
    if isinstance(a, TierAttempt):
        d = asdict(a)
    elif isinstance(a, dict):
        d = dict(a)
    else:
        return {"raw": str(a)}
    # ErrorKind enums become strings.
    ek = d.get("error_kind")
    if ek is not None and not isinstance(ek, str):
        d["error_kind"] = getattr(ek, "value", str(ek))
    return d


def log_escalation(
    *,
    source: str,
    attempts: list[TierAttempt | dict[str, Any]],
    final_outcome: str,
    final_tier: str | None = None,
    trace_id: str | None = None,
    task_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Append a single escalation record.

    Best-effort: a write failure logs a warning and returns; it never
    raises out, since the caller is on the LLM hot path.
    """
    if not attempts:
        return
    record: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "source": source,
        "attempts": [_attempt_to_dict(a) for a in attempts],
        "final_outcome": final_outcome,
    }
    if final_tier is not None:
        record["final_tier"] = final_tier
    elif attempts:
        last = _attempt_to_dict(attempts[-1])
        record["final_tier"] = last.get("tier", "")
    if trace_id:
        record["trace_id"] = trace_id
    if task_id:
        record["task_id"] = task_id
    if metadata:
        record["metadata"] = metadata

    try:
        path = _log_path()
        with _lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
    except OSError as exc:
        logger.warning("Failed to write escalation log: %s", exc)


def read_escalations(
    *,
    limit: int | None = None,
    trace_id: str | None = None,
    final_outcome: str | None = None,
    source: str | None = None,
) -> list[dict[str, Any]]:
    """Return recent escalation records, newest first.

    Args:
        limit: Maximum records to return (after filtering). ``None`` =
            no cap.
        trace_id: Match-exact correlation id filter.
        final_outcome: Match-exact filter (``"success"`` / ``"backend_error"`` /
            ``"validation_failed"`` / ...).
        source: Match-exact source filter.

    Records that fail to parse as JSON are silently skipped — the log is
    plain JSONL but a partial-write or external corruption shouldn't
    take the whole reader down.
    """
    path = _log_path()
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if trace_id and rec.get("trace_id") != trace_id:
                continue
            if final_outcome and rec.get("final_outcome") != final_outcome:
                continue
            if source and rec.get("source") != source:
                continue
            out.append(rec)
    except OSError as exc:
        logger.debug("Escalation log unreadable: %s", exc)
        return []

    out.reverse()  # newest first
    if limit is not None and limit >= 0:
        out = out[:limit]
    return out


def stream_escalations(*, source: str | None = None) -> Iterable[dict[str, Any]]:
    """Yield every parseable record (oldest first) for analytics callers."""
    path = _log_path()
    if not path.exists():
        return
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if source and rec.get("source") != source:
                    continue
                yield rec
    except OSError as exc:
        logger.debug("Escalation log unreadable: %s", exc)


def summarize_escalations(*, limit: int | None = None) -> dict[str, Any]:
    """Aggregate the recent log into per-source / per-outcome counters.

    Useful for the dashboard or a quick sanity check via ``wb_run``.
    """
    by_source: dict[str, int] = {}
    by_outcome: dict[str, int] = {}
    by_final_tier: dict[str, int] = {}
    escalated_past_first = 0
    total = 0
    for rec in read_escalations(limit=limit):
        total += 1
        by_source[rec.get("source", "unknown")] = (
            by_source.get(rec.get("source", "unknown"), 0) + 1
        )
        out = rec.get("final_outcome", "unknown")
        by_outcome[out] = by_outcome.get(out, 0) + 1
        ft = rec.get("final_tier", "unknown")
        by_final_tier[ft] = by_final_tier.get(ft, 0) + 1
        if len(rec.get("attempts", [])) > 1:
            escalated_past_first += 1
    return {
        "total": total,
        "escalated_past_first": escalated_past_first,
        "by_source": by_source,
        "by_outcome": by_outcome,
        "by_final_tier": by_final_tier,
    }
