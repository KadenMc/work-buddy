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

Tier escalation: if the first-tier model's output fails
content validation (coverage misses, overlap without ``multi``,
etc.), the adapter re-issues the call at the next tier in the
configured ``segment.tier_chain`` (default: ``LOCAL_FAST`` →
``FRONTIER_FAST``). Segmentation is a mechanical grouping task —
when a small local model can't produce a valid partition, the
right move is a bigger brain, not another shot at the same one.

Graceful degradation: if every tier in the chain errors or fails
validation, the adapter returns an empty candidate list with a
per-tier audit trail in the log. The producer then treats the
pass as a skipped / empty run rather than hard-failing.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from work_buddy.logging_config import get_logger
from work_buddy.triage.items import TriageItem

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You group lines of a running-notes section into threads by topic.

Input: the running notes with each line prefixed by its 1-based line number and a pipe, e.g.:
    1| - first line content
    2| - second line content
    3|

Output: a JSON object of the form
    {"groups": [
        [1, "3-5", 9],
        ["10-13"],
        [14, 15]
    ]}

Each inner array is one thread. Entries can be:
  - a plain integer — a single line number (e.g. ``9``)
  - an inclusive range string ``"N-M"`` — lines N through M (e.g. ``"3-5"`` means lines 3, 4, and 5)

For contiguous runs, prefer ranges — they're terser and less error-prone
than enumerating each line. Mix freely: ``[1, "3-5", 9]`` is group {1, 3, 4, 5, 9}.

Rules:
1. Every input CONTENT line must appear in at least one group. Blank
   lines and structural separator lines (lines containing only ``---``)
   may be left out — they carry boundary information, not content.
2. A line may appear in more than one group if it legitimately bridges
   two threads. No extra flag is needed; overlap itself encodes the
   multi-thread signal.
3. Do NOT include the line content in your output — only line numbers.
4. Return only the JSON object. No prose, no markdown fences.
"""


def _segmentation_user_prompt(*, numbered_text: str) -> str:
    return (
        f"=== BEGIN NUMBERED NOTES ===\n{numbered_text}\n"
        f"=== END NUMBERED NOTES ==="
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def collect_same_day_candidates(
    *,
    journal_date: str | None = None,
    profile: str,
    max_threads: int | None = None,
    segment_max_tokens: int | None = None,
    segment_temperature: float | None = None,
    segment_cache_ttl_minutes: int | None = None,
    tier_chain: list[Any] | None = None,
    trace_id: str | None = None,
) -> tuple[list[TriageItem], str | None]:
    """Return ``(items, content_hash)`` for the producer.

    Args:
        journal_date: ``YYYY-MM-DD`` or ``None`` for today.
        profile: Local LLM profile name used for segmentation.
        max_threads: Upper bound on how many threads we'll propagate
            downstream. ``None`` → load from feature config.
        segment_max_tokens: Token budget for the segmentation call.
            ``None`` → load from feature config.
        segment_temperature: Sampling temperature for segmentation.
            ``None`` → load from feature config.
        segment_cache_ttl_minutes: LLM-cache TTL. ``None`` → load
            from feature config.
        tier_chain: Ordered list of ``ModelTier`` values (or their
            string names) to try when a tier's output fails content
            validation. ``None`` → load from feature config.

    Returns:
        ``(items, content_hash)``. ``items`` is an empty list when
        there is no content or segmentation failed irrecoverably;
        ``content_hash`` is a short stable hash of the same-day
        input (or ``None`` when there was nothing to hash).
    """
    from work_buddy.journal_backlog import read_running_notes
    from work_buddy.journal_backlog.segment import strip_banners
    from work_buddy.llm import ModelTier
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
    if segment_max_tokens is None:
        segment_max_tokens = seg_cfg.get("max_tokens", 8192)
    if segment_temperature is None:
        segment_temperature = seg_cfg.get("temperature", 0.0)
    if segment_cache_ttl_minutes is None:
        segment_cache_ttl_minutes = seg_cfg.get("cache_ttl_minutes", 60)

    # Resolve tier chain: explicit arg → config → hard-coded fallback.
    raw_chain = (
        tier_chain if tier_chain is not None
        else seg_cfg.get("tier_chain", ["local_fast", "frontier_fast"])
    )
    resolved_chain: list[ModelTier] = []
    for entry in raw_chain or []:
        if isinstance(entry, ModelTier):
            resolved_chain.append(entry)
            continue
        try:
            resolved_chain.append(ModelTier(entry))
        except ValueError:
            logger.warning(
                "journal adapter: ignoring unknown tier %r in tier_chain",
                entry,
            )
    if not resolved_chain:
        resolved_chain = [ModelTier.LOCAL_FAST]

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

    threads = _segment_with_escalation(
        original_text=cleaned,
        banner_date_map=banner_date_map,
        profile=profile,
        tier_chain=resolved_chain,
        max_tokens=segment_max_tokens,
        temperature=segment_temperature,
        cache_ttl_minutes=segment_cache_ttl_minutes,
        journal_date=journal_date,
        trace_id=trace_id,
    )
    if threads is None:
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


_SYSTEM_PROMPT_HASH = hashlib.sha256(
    _SYSTEM_PROMPT.encode("utf-8")
).hexdigest()[:12]


def _segment_with_escalation(
    *,
    original_text: str,
    banner_date_map: list[tuple[int, str]] | None,
    profile: str,
    tier_chain: list[Any],
    max_tokens: int = 8192,
    temperature: float = 0.0,
    cache_ttl_minutes: int = 60,
    journal_date: str | None = None,
    trace_id: str | None = None,
) -> list[dict[str, Any]] | None:
    """Run segmentation, escalating through ``tier_chain`` on failure.

    Each tier sees the same clean prompt — no repair instructions, no
    redundant bookkeeping constraints on the model. On content-validation
    failure, the loop records a structured per-tier outcome and moves
    to the next tier. If every tier exhausts, emits one aggregated log
    line carrying the full audit trail (tier, outcome, error categories,
    sample) and returns ``None``.

    The LLM's job is partition-only: return line-number groups. Ids are
    generated locally after validation; the ``has_multi_flag`` is
    derived from line overlap between groups. See
    :func:`build_threads_from_line_ranges`.

    ### Caching

    Two layers, in order of precedence:

    1. **Content-addressable segmentation cache** — keyed by the *content
       set* of the input lines (line content hashes, not line numbers).
       Survives line reordering, blank-line edits, and whitespace-only
       changes. Stores groups as content-hash sets and translates back
       to current line numbers on lookup. Misses on any meaningful
       content change → falls through to a fresh LLM call.
    2. **LLM-prompt cache (disabled here)** — the generic
       :mod:`work_buddy.llm.cache` lives at the prompt level. Disabled
       on this path (``cache_ttl_minutes=0``) because its SimHash
       fuzzy-match would happily serve stale line-number partitions on
       small content edits — the very failure mode the segmentation
       cache exists to fix.

    Returns the list of thread dicts produced by
    :func:`build_threads_from_line_ranges`, or ``None`` if no tier
    produced a valid segmentation.
    """
    from work_buddy.journal_backlog.segment import (
        build_threads_from_line_ranges,
        number_lines,
        validate_line_range_segmentation,
    )
    from work_buddy.journal_backlog.segmentation_cache import (
        get_cached_segmentation,
        put_segmentation,
    )

    numbered, original_lines = number_lines(original_text)
    user_prompt = _segmentation_user_prompt(numbered_text=numbered)

    # Auto-generate a trace_id when the caller didn't supply one so the
    # escalation log always has a correlation token tying together the
    # adapter-level chain and the per-tier LLMRunner call records.
    if trace_id is None:
        import uuid as _uuid
        trace_id = (f"journal_segment:{journal_date}:{_uuid.uuid4().hex[:8]}"
                    if journal_date else f"journal_segment:{_uuid.uuid4().hex[:8]}")

    # Content-addressable cache lookup BEFORE any LLM call. Keyed on
    # the system_hash + the content-set of original_lines.
    cached_groups = get_cached_segmentation(
        original_lines=original_lines,
        system_hash=_SYSTEM_PROMPT_HASH,
    )
    if cached_groups is not None:
        # Sanity-validate against the current line numbers — coverage
        # check might catch a translation edge case (e.g. a content hash
        # that mapped to multiple positions in current input but only
        # one in cached input).
        check = validate_line_range_segmentation(
            {"groups": cached_groups}, original_lines,
        )
        if check.get("valid"):
            logger.info(
                "journal adapter: segmentation cache hit (content-addressable)",
            )
            return build_threads_from_line_ranges(
                check, original_lines, banner_date_map=banner_date_map,
            )
        # Cached entry exists but doesn't validate against current
        # input. Rare; fall through to LLM. Don't surface as an error.

    call_kwargs = {
        "profile": profile,
        "max_tokens": max_tokens,
        "temperature": temperature,
        # Disable the LLM-prompt cache for segmenter calls. We cache at
        # the content-addressable layer above; the prompt-level cache
        # would fuzzy-match on small content edits and serve stale line
        # numbers — the exact bug this rework exists to fix.
        "cache_ttl_minutes": 0,
    }

    attempts: list[dict[str, Any]] = []

    def _emit_log(final_outcome: str, final_tier: str) -> None:
        """Best-effort write to the structured escalation log."""
        try:
            from work_buddy.llm.escalation_log import log_escalation
            log_escalation(
                source="journal_segmenter",
                attempts=attempts,
                final_outcome=final_outcome,
                final_tier=final_tier,
                trace_id=trace_id,
                task_id=(f"journal_segment:{journal_date}"
                         if journal_date else "journal_segment"),
                metadata={"journal_date": journal_date},
            )
        except Exception:  # noqa: BLE001
            logger.debug("escalation log write skipped", exc_info=True)

    for tier in tier_chain:
        parsed, failure, attempt_meta = _call_segmenter(
            system=_SYSTEM_PROMPT,
            user=user_prompt,
            tier=tier,
            trace_id=trace_id,
            **call_kwargs,
        )
        tier_str = getattr(tier, "value", str(tier))
        if parsed is None:
            attempts.append({
                "tier": tier_str,
                "model": attempt_meta.get("model", ""),
                "outcome": failure or "llm_error_or_unparseable",
                "error_kind": failure,
                "elapsed_ms": attempt_meta.get("elapsed_ms", 0),
                "input_tokens": attempt_meta.get("input_tokens", 0),
                "output_tokens": attempt_meta.get("output_tokens", 0),
            })
            continue

        result = validate_line_range_segmentation(parsed, original_lines)
        if result.get("valid"):
            attempts.append({
                "tier": tier_str,
                "model": attempt_meta.get("model", ""),
                "outcome": "success",
                "elapsed_ms": attempt_meta.get("elapsed_ms", 0),
                "input_tokens": attempt_meta.get("input_tokens", 0),
                "output_tokens": attempt_meta.get("output_tokens", 0),
            })
            _emit_log("success", tier_str)
            # Persist to the content-addressable cache so subsequent
            # runs on the same content (any ordering) reuse this work.
            try:
                put_segmentation(
                    original_lines=original_lines,
                    system_hash=_SYSTEM_PROMPT_HASH,
                    groups=result["groups"],
                    ttl_minutes=cache_ttl_minutes if cache_ttl_minutes > 0 else 60,
                )
            except Exception as exc:
                # Cache write is best-effort — never block on a write failure.
                logger.warning(
                    "journal adapter: segmentation cache write failed: %s", exc,
                )
            return build_threads_from_line_ranges(
                result, original_lines, banner_date_map=banner_date_map,
            )

        errors = result.get("errors", []) or []
        grouped = _group_validation_errors(errors)
        attempts.append({
            "tier": tier_str,
            "model": attempt_meta.get("model", ""),
            "outcome": "validation_failed",
            "error_kind": "validation_failed",
            "elapsed_ms": attempt_meta.get("elapsed_ms", 0),
            "input_tokens": attempt_meta.get("input_tokens", 0),
            "output_tokens": attempt_meta.get("output_tokens", 0),
            "error_count": len(errors),
            "categories": list(grouped.keys()),
            "sample": [e[:120] for e in errors[:3]],
        })

    logger.info(
        "journal adapter: segmentation failed across all tiers for "
        "date=%s: %s",
        journal_date,
        attempts,
    )
    _emit_log("exhausted",
              attempts[-1].get("tier", "") if attempts else "")
    return None


def _group_validation_errors(errors: list[str]) -> dict[str, list[str]]:
    """Bucket validator error strings into categories for logging.

    Categories map to the failure modes the line-range validator can
    produce: missing coverage (non-blank lines unassigned), bad shape
    (JSON didn't parse as ``{"groups": [[...]]}``), bad line numbers
    (out of range or non-integer), and a catch-all.
    """
    grouped: dict[str, list[str]] = {
        "missing_coverage": [],
        "bad_shape": [],
        "bad_line": [],
        "other": [],
    }
    for err in errors:
        lower = err.lower()
        if "not assigned" in lower:
            grouped["missing_coverage"].append(err)
        elif "groups" in lower and ("missing" in lower or "not a list" in lower or "not an array" in lower):
            grouped["bad_shape"].append(err)
        elif "out of range" in lower or "non-integer" in lower:
            grouped["bad_line"].append(err)
        else:
            grouped["other"].append(err)
    return {k: v for k, v in grouped.items() if v}


def _call_segmenter(
    *,
    system: str,
    user: str,
    tier: Any,
    profile: str,
    max_tokens: int,
    temperature: float,
    cache_ttl_minutes: int,
    trace_id: str | None = None,
) -> tuple[dict[str, Any] | None, str | None, dict[str, Any]]:
    """Run one LLM call at ``tier`` and parse the JSON response.

    Returns ``(parsed_json, None, meta)`` on success, or
    ``(None, failure_kind, meta)`` on any failure. ``failure_kind`` is a
    short string drawn from ``LLMResponse.error_kind`` when the
    runner reports an error, or a local tag (``"empty_content"``,
    ``"unparseable"``) for adapter-side failures.

    ``meta`` always contains ``model``, ``elapsed_ms``, ``input_tokens``,
    and ``output_tokens`` so the adapter-level escalation log can record
    per-tier accounting even when the parse failed.

    We DO NOT pass ``output_schema=`` here. Empirically, LM Studio's
    openai-compat ``response_format: json_schema`` path breaks for
    reasoning models (Qwen3.5-9B etc.): the grammar enforcement
    interferes with the internal thinking phase and the endpoint
    returns empty content despite nonzero output tokens. The prompt
    asks for JSON directly; :func:`validate_line_range_segmentation`
    is our real safety net against malformed output.

    ``profile`` is advisory: :class:`LLMRunner` resolves the concrete
    model from the tier binding, not the profile string. Callers who
    need a non-default profile should override the binding in
    ``config.local.yaml`` under ``llm.tiers``.
    """
    from work_buddy.llm import LLMRunner
    import time as _time

    if profile and profile not in ("local_general",):
        logger.debug(
            "journal adapter: profile=%r override won't take effect — "
            "LLMRunner uses the tier binding for %s",
            profile,
            getattr(tier, "value", tier),
        )

    t0 = _time.time()
    resp = LLMRunner().call(
        tier=tier,
        system=system,
        user=user,
        max_tokens=max_tokens,
        temperature=temperature,
        cache_ttl_minutes=cache_ttl_minutes,
        trace_id=trace_id,
    )
    elapsed_ms = int((_time.time() - t0) * 1000)
    meta: dict[str, Any] = {
        "model": resp.model,
        "elapsed_ms": elapsed_ms,
        "input_tokens": resp.input_tokens,
        "output_tokens": resp.output_tokens,
    }

    if resp.is_error():
        kind = resp.error_kind.value if resp.error_kind else "unknown"
        logger.warning(
            "journal adapter: segmentation llm_call error at tier=%s: "
            "kind=%s msg=%s",
            getattr(tier, "value", tier), kind, resp.error,
        )
        return None, kind, meta

    content = (resp.content or "").strip()
    if not content:
        return None, "empty_content", meta
    if content.startswith("```"):
        content = content.split("\n", 1)[1] if "\n" in content else content
        if content.endswith("```"):
            content = content[: -3].rstrip("\n")

    # Models sometimes emit leading/trailing prose despite the prompt.
    # Locate the outermost {...} and parse that.
    try:
        return json.loads(content), None, meta
    except (json.JSONDecodeError, ValueError):
        brace_start = content.find("{")
        brace_end = content.rfind("}")
        if brace_start >= 0 and brace_end > brace_start:
            try:
                return json.loads(content[brace_start : brace_end + 1]), None, meta
            except (json.JSONDecodeError, ValueError):
                pass
        logger.warning(
            "journal adapter: segmentation response unparseable at "
            "tier=%s (len=%d)",
            getattr(tier, "value", tier), len(content),
        )
        return None, "unparseable", meta


def _derive_label(text: str, *, max_chars: int = 72) -> str:
    """First non-empty line, truncated — a human-friendly label."""
    for line in (text or "").splitlines():
        stripped = line.strip().lstrip("-*+# ").strip()
        if stripped:
            if len(stripped) > max_chars:
                return stripped[: max_chars - 1] + "…"
            return stripped
    return "(empty thread)"
