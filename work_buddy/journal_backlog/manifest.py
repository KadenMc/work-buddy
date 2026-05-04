"""Per-thread tag/summary manifest builder for the backlog pipeline.

Walks line-range thread dicts and uses :class:`LLMRunner` to produce a
manifest entry per thread: ``{id, tags, summary}``. The cluster step
downstream uses these entries (specifically the ``tags`` field) to
group related threads via Jaccard similarity.

Failure handling: per-thread LLM errors don't abort the run. The failed
thread's entry has ``tags=[]``, ``summary=""``, and an ``error`` field
describing what went wrong. Callers can decide to (a) cluster on the
partial manifest (skipping errored entries) or (b) abort the workflow
and prompt the user.

Cache: each per-thread call benefits from the content-aware LLM cache —
the same thread (identical raw_text) won't re-invoke the model on a
subsequent run within the TTL window.
"""

from __future__ import annotations

from typing import Any

from work_buddy.llm import ErrorKind, LLMResponse, LLMRunner, ModelTier
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Prompt + schema
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are tagging one thread from a daily running-notes journal. Read the
thread and return a JSON object with two fields:

  - "tags": an array of 1 to 6 short noun-phrase tags. Use lowercase,
    hyphenated style (e.g. "tax-prep", "etf-tracking", "advisor-meeting").
    Tags should overlap deliberately for related threads — they're used
    to cluster threads later, so pick tags you'd reuse across multiple
    threads about the same topic.
  - "summary": a single sentence (≤ 25 words) describing what the thread
    is about. Plain English; no leading bullet or quotation.

Return only the JSON object. No prose, no markdown fences.
"""

_MANIFEST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["tags", "summary"],
    "properties": {
        "tags": {
            # Anthropic's structured-output validator rejects ``minItems``
            # / ``maxItems`` on arrays. Keep the constraint in the system
            # prompt instead: "1 to 6 tags".
            "type": "array",
            "items": {"type": "string"},
        },
        "summary": {"type": "string"},
    },
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_thread_manifest(
    threads: list[dict[str, Any]],
    *,
    tier: ModelTier = ModelTier.FRONTIER_FAST,
    cache_ttl_minutes: int | None = None,
) -> list[dict[str, Any]]:
    """Generate manifest entries (`{id, tags, summary}`) for each thread.

    Args:
        threads: Thread dicts from
            :func:`work_buddy.journal_backlog.segment.build_threads_from_line_ranges`
            (or any producer of the standard thread-dict shape with
            ``id`` and ``raw_text``).
        tier: Starting LLM tier. Defaults to ``FRONTIER_FAST`` (Haiku) —
            the task is short-form classification + summary, well within
            Haiku's wheelhouse.
        cache_ttl_minutes: Optional override for the runner's cache TTL.
            ``None`` uses the runner default.

    Returns:
        A list of manifest entries the same length as ``threads``, in
        the same order. Successful entries have non-empty ``tags`` and
        ``summary``; failed entries have empty ``tags`` / ``summary``
        plus an ``error`` field.
    """
    runner = LLMRunner()
    out: list[dict[str, Any]] = []

    for thread in threads:
        tid = thread.get("id", "")
        raw_text = thread.get("raw_text", "") or ""

        if not isinstance(tid, str) or not tid:
            logger.warning("manifest: skipping thread with missing id: %r", thread)
            continue

        entry = _tag_one(
            runner=runner,
            tid=tid,
            raw_text=raw_text,
            tier=tier,
            cache_ttl_minutes=cache_ttl_minutes,
        )
        out.append(entry)

    return out


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _tag_one(
    *,
    runner: LLMRunner,
    tid: str,
    raw_text: str,
    tier: ModelTier,
    cache_ttl_minutes: int | None,
) -> dict[str, Any]:
    """Single-thread tag/summary call. Always returns a manifest-shape dict."""
    user_prompt = (
        f"Thread to tag (id={tid}):\n"
        f"---\n"
        f"{raw_text.strip()}\n"
        f"---\n"
        f"Return the JSON object now."
    )

    resp: LLMResponse = runner.call(
        tier=tier,
        system=_SYSTEM_PROMPT,
        user=user_prompt,
        output_schema=_MANIFEST_SCHEMA,
        escalate_on=[
            ErrorKind.TIMEOUT,
            ErrorKind.CONTEXT_EXCEEDED,
            ErrorKind.EMPTY_CONTENT,
            ErrorKind.RATE_LIMITED,
        ],
        escalate_to=[ModelTier.FRONTIER_BALANCED],
        cache_ttl_minutes=cache_ttl_minutes,
    )

    if resp.is_error():
        logger.warning(
            "manifest: tagging failed for thread %s: kind=%s msg=%s",
            tid,
            resp.error_kind.value if resp.error_kind else "unknown",
            resp.error,
        )
        return _failed_entry(tid, resp.error or "LLM call failed")

    parsed = resp.structured_output or {}
    missing = [
        f for f in ("tags", "summary")
        if not parsed.get(f) and not (f == "summary" and isinstance(parsed.get(f), str))
    ]
    # 'summary' may legitimately be empty string per schema; treat absent
    # (not-empty-string) as a failure but accept "" as valid.
    if "tags" not in parsed or not isinstance(parsed.get("tags"), list) or not parsed["tags"]:
        logger.warning(
            "manifest: thread %s missing or empty 'tags' field in response",
            tid,
        )
        return _failed_entry(tid, "Manifest 'tags' field missing or empty")
    if "summary" not in parsed:
        logger.warning(
            "manifest: thread %s missing 'summary' field in response", tid,
        )
        return _failed_entry(tid, "Manifest 'summary' field missing")

    return {
        "id": tid,
        "tags": list(parsed["tags"]),
        "summary": str(parsed.get("summary", "")),
    }


def _failed_entry(tid: str, error: str) -> dict[str, Any]:
    return {
        "id": tid,
        "tags": [],
        "summary": "",
        "error": error,
    }
