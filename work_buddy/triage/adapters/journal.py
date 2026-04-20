"""Journal adapter — turns same-day Running Notes into TriageItems.

Called by ``BackgroundTriageProducer``. Steps:

  1. Extract the Running Notes section from today's (or a given
     date's) journal and keep only the journal-date-native content
     via ``read_running_notes(same_day=True)``.
  2. Strip carry-over banners for a clean block.
  3. Number the lines, feed the numbered text to the configured
     local LLM profile with a JSON-Schema-constrained output that
     asks only for ``{thread_id → line numbers}`` mappings. Output
     size is O(threads), not O(input), and content drift is
     impossible because the model never emits content.
  4. Reconstruct thread objects from the line-range map and turn
     each into a :class:`TriageItem` with ``source="journal_thread"``
     and a stable ``journal_<tid>`` id.

One repair retry: if validation fails (missing coverage, overlapping
threads without ``multi``, etc.), a structured repair prompt is
built and the call is retried once.

Graceful degradation: if the local profile is not reachable or
segmentation fails twice in a row, the adapter returns an empty
candidate list. The producer then treats the pass as a skipped /
empty run rather than hard-failing.
"""

from __future__ import annotations

import json
from typing import Any

from work_buddy.logging_config import get_logger
from work_buddy.triage.items import TriageItem

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You group lines of a running-notes section into threads.

Input: the running notes with each line prefixed by its 1-based line number and a pipe, e.g.:
    1| - first line content
    2| - second line content
    3|

Output: a JSON object of the form
    {"threads": [
        {"id": "t_xxxxxx", "lines": [1, 2, 5]},
        {"id": "t_yyyyyy", "lines": [3, 4]}
    ]}

Rules:
1. Use ONLY thread ids from the provided pool.
2. Every input CONTENT line must appear in at least one thread. Blank
   lines and structural separator lines (lines containing only ``---``)
   may be left out — they carry boundary information, not content.
3. A line can appear in at most one thread UNLESS it legitimately bridges
   two threads — in that case add the line to BOTH threads and set
   "multi": true on both.
4. Do NOT include the line content in your output — only line numbers.
5. Return only the JSON object. No prose, no markdown fences.
"""


def _segmentation_user_prompt(
    *,
    numbered_text: str,
    id_pool: list[str],
) -> str:
    pool_preview = ", ".join(id_pool[:50])
    if len(id_pool) > 50:
        pool_preview += f", …({len(id_pool) - 50} more)"
    return (
        f"Thread id pool ({len(id_pool)} ids): {pool_preview}\n\n"
        f"=== BEGIN NUMBERED NOTES ===\n{numbered_text}\n=== END NUMBERED NOTES ==="
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def collect_same_day_candidates(
    *,
    journal_date: str | None = None,
    profile: str,
    max_threads: int | None = None,
    id_pool_size: int | None = None,
    segment_max_tokens: int | None = None,
    segment_temperature: float | None = None,
    segment_cache_ttl_minutes: int | None = None,
) -> tuple[list[TriageItem], str | None]:
    """Return ``(items, content_hash)`` for the producer.

    Args:
        journal_date: ``YYYY-MM-DD`` or ``None`` for today.
        profile: Local LLM profile name used for segmentation.
        max_threads: Upper bound on how many threads we'll propagate
            downstream. ``None`` → load from feature config.
        id_pool_size: Thread-ID pool size handed to the segmenter.
            ``None`` → load from feature config.
        segment_max_tokens: Token budget for the segmentation call.
            ``None`` → load from feature config.
        segment_temperature: Sampling temperature for segmentation.
            ``None`` → load from feature config.
        segment_cache_ttl_minutes: LLM-cache TTL. ``None`` → load
            from feature config.

    Returns:
        ``(items, content_hash)``. ``items`` is an empty list when
        there is no content or segmentation failed irrecoverably;
        ``content_hash`` is a short stable hash of the same-day
        input (or ``None`` when there was nothing to hash).
    """
    from work_buddy.journal_backlog import read_running_notes
    from work_buddy.journal_backlog.segment import strip_banners
    from work_buddy.triage.background import content_hash as _hash
    from work_buddy.triage.config import (
        adapter_config,
        load_triage_config,
    )

    cfg = load_triage_config()
    seg_cfg = cfg.get("segment", {}) or {}
    ad_cfg = adapter_config(cfg, "journal_triage")

    if max_threads is None:
        max_threads = ad_cfg.get("max_threads", 64)
    if id_pool_size is None:
        id_pool_size = ad_cfg.get("id_pool_size", 64)
    if segment_max_tokens is None:
        segment_max_tokens = seg_cfg.get("max_tokens", 8192)
    if segment_temperature is None:
        segment_temperature = seg_cfg.get("temperature", 0.0)
    if segment_cache_ttl_minutes is None:
        segment_cache_ttl_minutes = seg_cfg.get("cache_ttl_minutes", 60)

    try:
        raw = read_running_notes(same_day=True, journal_date=journal_date)
    except Exception as exc:
        logger.warning("journal adapter: read_running_notes failed: %s", exc)
        return [], None

    if not raw or not raw.strip():
        return [], None

    cleaned, _src_dates, banner_date_map = strip_banners(raw)
    if not cleaned.strip():
        return [], None

    ch = _hash([cleaned])

    threads = _segment_with_repair(
        original_text=cleaned,
        banner_date_map=banner_date_map,
        profile=profile,
        id_pool_size=id_pool_size,
        max_tokens=segment_max_tokens,
        temperature=segment_temperature,
        cache_ttl_minutes=segment_cache_ttl_minutes,
    )
    if threads is None:
        logger.info(
            "journal adapter: segmentation failed for date=%s "
            "(returning empty candidate list)",
            journal_date,
        )
        return [], ch

    if len(threads) > max_threads:
        logger.info(
            "journal adapter: capping %d threads to %d",
            len(threads), max_threads,
        )
        threads = threads[:max_threads]

    items: list[TriageItem] = []
    for th in threads:
        tid = th.get("id", "")
        if not tid:
            continue
        items.append(
            TriageItem(
                id=f"journal_{tid}",
                text=th.get("raw_text", "") or "",
                label=_derive_label(th.get("raw_text", "") or tid),
                source="journal_thread",
                metadata={
                    "thread_id": tid,
                    "line_count": th.get("line_count", 0),
                    "source_dates": th.get("source_dates", []),
                    "has_multi_flag": th.get("has_multi_flag", False),
                    "journal_date": journal_date or "",
                },
            )
        )
    return items, ch


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _segment_with_repair(
    *,
    original_text: str,
    banner_date_map: list[tuple[int, str]] | None,
    profile: str,
    id_pool_size: int = 64,
    max_tokens: int = 8192,
    temperature: float = 0.0,
    cache_ttl_minutes: int = 60,
) -> list[dict[str, Any]] | None:
    """One attempt + one repair retry via the line-range protocol.

    Returns the list of thread dicts produced by
    :func:`build_threads_from_line_ranges`, or ``None`` if
    segmentation failed twice in a row.
    """
    from work_buddy.journal_backlog.segment import (
        build_threads_from_line_ranges,
        generate_thread_ids,
        number_lines,
        repair_line_range_segmentation,
        validate_line_range_segmentation,
    )

    id_pool = generate_thread_ids(count=id_pool_size)
    numbered, original_lines = number_lines(original_text)

    call_kwargs = {
        "profile": profile,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "cache_ttl_minutes": cache_ttl_minutes,
    }

    attempt = _call_segmenter(
        system=_SYSTEM_PROMPT,
        user=_segmentation_user_prompt(
            numbered_text=numbered, id_pool=id_pool,
        ),
        **call_kwargs,
    )
    if attempt is None:
        return None

    result = validate_line_range_segmentation(
        attempt, original_lines, id_pool=id_pool,
    )
    if result.get("valid"):
        return build_threads_from_line_ranges(
            result, original_lines, banner_date_map=banner_date_map,
        )

    repair = repair_line_range_segmentation(
        segmentation=attempt,
        validation_result=result,
        original_lines=original_lines,
        id_pool=id_pool,
    )
    if not repair.get("should_retry"):
        logger.info(
            "journal adapter: segmentation errors non-recoverable "
            "(categories=%s)",
            list(repair.get("errors_grouped", {}).keys()),
        )
        return None

    retry_system = _SYSTEM_PROMPT + "\n\n" + repair["instructions"]
    retry_user = _segmentation_user_prompt(
        numbered_text=numbered,
        id_pool=repair["available_ids"] or id_pool,
    )
    second = _call_segmenter(
        system=retry_system, user=retry_user, **call_kwargs,
    )
    if second is None:
        return None

    result2 = validate_line_range_segmentation(
        second, original_lines, id_pool=id_pool,
    )
    if result2.get("valid"):
        return build_threads_from_line_ranges(
            result2, original_lines, banner_date_map=banner_date_map,
        )
    logger.info(
        "journal adapter: segmentation still invalid after repair "
        "(errors=%d)",
        len(result2.get("errors", [])),
    )
    return None


def _call_segmenter(
    *,
    system: str,
    user: str,
    profile: str,
    max_tokens: int,
    temperature: float,
    cache_ttl_minutes: int,
) -> dict[str, Any] | None:
    """Run the local-profile LLM call and parse the JSON response.

    We DO NOT pass ``output_schema=`` here. Empirically, LM Studio's
    openai-compat ``response_format: json_schema`` path breaks for
    reasoning models (Qwen3.5-9B etc.): the grammar enforcement
    interferes with the internal thinking phase and the endpoint
    returns empty content despite nonzero output tokens. The prompt
    asks for JSON directly; :func:`validate_line_range_segmentation`
    is our real safety net against malformed output.
    """
    from work_buddy.llm.call import llm_call

    result = llm_call(
        system=system,
        user=user,
        profile=profile,
        max_tokens=max_tokens,
        temperature=temperature,
        cache_ttl_minutes=cache_ttl_minutes,
    )
    if result.get("error"):
        logger.warning(
            "journal adapter: segmentation llm_call error: %s",
            result.get("error"),
        )
        return None

    content = (result.get("content") or "").strip()
    if not content:
        return None
    if content.startswith("```"):
        content = content.split("\n", 1)[1] if "\n" in content else content
        if content.endswith("```"):
            content = content[: -3].rstrip("\n")

    # Models sometimes emit leading/trailing prose despite the prompt.
    # Locate the outermost {...} and parse that.
    try:
        import json
        return json.loads(content)
    except (json.JSONDecodeError, ValueError):
        brace_start = content.find("{")
        brace_end = content.rfind("}")
        if brace_start >= 0 and brace_end > brace_start:
            try:
                import json
                return json.loads(content[brace_start : brace_end + 1])
            except (json.JSONDecodeError, ValueError):
                pass
        logger.warning(
            "journal adapter: segmentation response unparseable "
            "(len=%d)", len(content),
        )
        return None


def _derive_label(text: str, *, max_chars: int = 72) -> str:
    """First non-empty line, truncated — a human-friendly label."""
    for line in (text or "").splitlines():
        stripped = line.strip().lstrip("-*+# ").strip()
        if stripped:
            if len(stripped) > max_chars:
                return stripped[: max_chars - 1] + "…"
            return stripped
    return "(empty thread)"
