"""The shared refresh orchestrator + LLM injection seam.

`run_refresh` is the written-once core: discover candidates, filter to stale,
render each, call the LLM, parse, persist, isolate errors. Bounded by
`max_items`.

The orchestrator dispatches per-item or batch based on the summarizer's
`BATCHED` capability — the batch path issues one LLM call for N items.

`as_caller` adapts a legacy bare-callable LLM stub (used by existing conv_obs
tests) into an `LLMCaller`-conforming object so test stubs need no changes.
`default_llm_caller` is the production caller wrapping `LLMRunner` at
`ModelTier.FRONTIER_FAST`.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

from work_buddy.summarization.protocol import (
    DiscoveryWindow,
    LLMCallResult,
    LLMCaller,
    Provenance,
    SummarizationError,
    SummaryCapability,
    SummaryNode,
)
from work_buddy.summarization.summarizer import RefreshReport, Summarizer

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Provenance assembly
# ---------------------------------------------------------------------------


def build_provenance(
    summarizer: Summarizer,
    llm_result: LLMCallResult | None,
    profile: str | None,
) -> Provenance:
    """Assemble a `Provenance` from the strategy, the store, and the LLM
    response. The orchestrator is the only place this is built — provenance
    is uniform, not a pluggable axis."""
    return Provenance(
        model=llm_result.model if llm_result else None,
        backend=llm_result.backend if llm_result else None,
        profile=profile,
        generated_at=Provenance.now_iso(),
        prompt_version=summarizer.strategy.prompt_version,
        summary_schema_version=summarizer.strategy.schema_version,
        selection_version=summarizer.store.selection_version,
        cache_version=summarizer.store.cache_version,
    )


def build_error_provenance(
    summarizer: Summarizer,
    profile: str | None,
) -> Provenance:
    """Provenance stamp for a record-error path (no LLM response available)."""
    return build_provenance(summarizer, None, profile)


# ---------------------------------------------------------------------------
# The refresh core
# ---------------------------------------------------------------------------


def run_refresh(
    summarizer: Summarizer,
    *,
    window: DiscoveryWindow,
    llm_caller: LLMCaller,
    profile: str | None = None,
) -> RefreshReport:
    """Run a bounded refresh pass over `summarizer`.

    Discover → filter-to-stale (unless `force`) → for each item up to
    `window.max_items`: render → LLM → parse → save. Errors per-item are
    caught and recorded; other items continue.

    Dispatches to the batch path when `BATCHED` is in the summarizer's
    capabilities (source AND strategy must both declare it — enforced by
    `Summarizer._validate_coherence`).
    """
    candidates = summarizer.source.discover(window)
    if window.force:
        stale = list(candidates)
    else:
        stale = summarizer.store.select_stale(candidates)

    report = RefreshReport(
        summarizer=summarizer.name,
        total_candidates=len(candidates),
        skipped_fresh=len(candidates) - len(stale),
    )

    # Cap to max_items.
    stale_capped = stale[: window.max_items] if window.max_items > 0 else stale

    if SummaryCapability.BATCHED in summarizer.capabilities:
        _run_refresh_batch(summarizer, stale_capped, llm_caller, profile, report)
    else:
        _run_refresh_per_item(
            summarizer, stale_capped, llm_caller, profile, report,
        )

    return report


def _run_refresh_per_item(
    summarizer: Summarizer,
    stale: list[tuple[str, Any]],
    llm_caller: LLMCaller,
    profile: str | None,
    report: RefreshReport,
) -> None:
    """Per-item refresh path — one LLM call per item."""
    for item_id, token in stale:
        try:
            body = summarizer.source.render(item_id)
            if body is None:
                continue

            result = llm_caller.call(
                system=summarizer.strategy.system_prompt,
                user=body,
                output_schema=summarizer.strategy.output_schema,
                profile=profile,
                max_tokens=1024,
                trace_id=f"summarization.{summarizer.name}",
            )

            if result.is_error():
                raise SummarizationError(result.error or "llm error")

            node = summarizer.strategy.parse(
                result.structured_output, result.content,
            )
            prov = build_provenance(summarizer, result, profile)
            summarizer.store.save(item_id, node, prov, token)
            report.summarized += 1
        except Exception as exc:
            report.errored += 1
            report.errors.append((item_id, str(exc)))
            try:
                summarizer.store.record_error(
                    item_id,
                    str(exc),
                    build_error_provenance(summarizer, profile),
                )
            except Exception as inner:
                # Defensive — error-recording itself shouldn't break the pass.
                logger.warning(
                    "Failed to record error for %s/%s: %s",
                    summarizer.name, item_id, inner,
                )


def _run_refresh_batch(
    summarizer: Summarizer,
    stale: list[tuple[str, Any]],
    llm_caller: LLMCaller,
    profile: str | None,
    report: RefreshReport,
) -> None:
    """Batch refresh path — one LLM call for all stale items.

    Source.render_batch produces per-item prompt texts; the orchestrator
    labels and concatenates them into one user prompt. Strategy.parse_batch
    is called once with the response and returns per-item trees aligned with
    the stale order. Items whose render or parse is `None` are silently
    skipped (not counted as errors).
    """
    if not stale:
        return

    item_ids = [item_id for item_id, _ in stale]
    tokens = [token for _, token in stale]
    rendered = summarizer.source.render_batch(item_ids)

    # Filter to non-None renders. Track index mapping back to the original
    # stale list so we save the right items.
    rendered_items: list[tuple[str, Any, str, int]] = []
    for idx, (iid, tok, body) in enumerate(zip(item_ids, tokens, rendered)):
        if body is not None:
            rendered_items.append((iid, tok, body, idx))

    if not rendered_items:
        return

    # Build the combined user prompt with item markers.
    parts: list[str] = []
    for batch_idx, (iid, _tok, body, _orig_idx) in enumerate(rendered_items):
        parts.append(f"## Item {batch_idx}: {iid}\n{body}")
    user_prompt = "\n\n".join(parts)

    # Batched strategies expose `batch_output_schema`; fall back to the
    # single-item schema if a strategy declares BATCHED without one.
    batch_schema = (
        getattr(summarizer.strategy, "batch_output_schema", None)
        or summarizer.strategy.output_schema
    )
    try:
        result = llm_caller.call(
            system=summarizer.strategy.system_prompt,
            user=user_prompt,
            output_schema=batch_schema,
            profile=profile,
            max_tokens=4096,
            trace_id=f"summarization.{summarizer.name}.batch",
        )

        if result.is_error():
            raise SummarizationError(result.error or "llm error")

        batch_ids = [iid for iid, _, _, _ in rendered_items]
        nodes = summarizer.strategy.parse_batch(
            result.structured_output, result.content, batch_ids,
        )
    except Exception as exc:
        # Whole batch failed → record an error for every item that was sent.
        for iid, _tok, _body, _idx in rendered_items:
            report.errored += 1
            report.errors.append((iid, str(exc)))
            try:
                summarizer.store.record_error(
                    iid,
                    str(exc),
                    build_error_provenance(summarizer, profile),
                )
            except Exception as inner:
                logger.warning(
                    "Failed to record batch error for %s/%s: %s",
                    summarizer.name, iid, inner,
                )
        return

    # Per-item save.
    prov = build_provenance(summarizer, result, profile)
    for (iid, token, _body, _orig_idx), node in zip(rendered_items, nodes):
        if node is None:
            report.errored += 1
            report.errors.append((iid, "missing from batch response"))
            try:
                summarizer.store.record_error(
                    iid,
                    "missing from batch response",
                    prov,
                )
            except Exception:
                pass
            continue
        try:
            summarizer.store.save(iid, node, prov, token)
            report.summarized += 1
        except Exception as exc:
            report.errored += 1
            report.errors.append((iid, str(exc)))
            try:
                summarizer.store.record_error(iid, str(exc), prov)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# LLM injection seam
# ---------------------------------------------------------------------------


def as_caller(fn: Callable[..., Any] | None) -> LLMCaller | None:
    """Adapt a legacy bare-callable LLM stub into an `LLMCaller`.

    Legacy shape (used by existing conv_obs tests):
        def fn(*, system, user, output_schema=None, profile=None) -> Any

    The return value is normalized: bare dict → `structured_output`;
    JSON-decodable str → `structured_output` + `content`; arbitrary str →
    `content`; `LLMResponse`-shaped object → fields read off via `getattr`;
    `None` → `error="None response"`.

    Returns `None` when `fn` is `None`, letting callers do
    `as_caller(maybe_fn) or default_llm_caller()`.
    """
    if fn is None:
        return None

    class _Adapter:
        def call(
            self,
            *,
            system: str,
            user: str,
            output_schema: dict[str, Any] | None = None,
            profile: str | None = None,
            max_tokens: int | None = None,
            trace_id: str | None = None,
        ) -> LLMCallResult:
            try:
                resp = fn(
                    system=system,
                    user=user,
                    output_schema=output_schema,
                    profile=profile,
                )
            except Exception as exc:
                return LLMCallResult(error=str(exc))
            return _normalize_legacy_response(resp)

    return _Adapter()


def _normalize_legacy_response(resp: Any) -> LLMCallResult:
    """Normalize whatever a legacy `llm_call` stub returned into an
    `LLMCallResult`.

    Folds in the same logic conv_obs's `_coerce_response` used to do, so
    existing test stubs (which return bare dicts) work unchanged.
    """
    if resp is None:
        return LLMCallResult(error="None response")

    if isinstance(resp, dict):
        return LLMCallResult(structured_output=resp)

    if isinstance(resp, str):
        try:
            parsed = json.loads(resp)
            if isinstance(parsed, dict):
                return LLMCallResult(structured_output=parsed, content=resp)
        except (ValueError, TypeError):
            pass
        return LLMCallResult(content=resp)

    # Response-shaped object.
    structured = getattr(resp, "structured_output", None)
    if structured is None:
        # Legacy `.parsed` attribute (older runner).
        parsed_attr = getattr(resp, "parsed", None)
        if isinstance(parsed_attr, dict):
            structured = parsed_attr
        elif isinstance(parsed_attr, str):
            try:
                maybe = json.loads(parsed_attr)
                if isinstance(maybe, dict):
                    structured = maybe
            except (ValueError, TypeError):
                pass

    content = getattr(resp, "content", "") or ""
    model = getattr(resp, "model", None)
    backend = getattr(resp, "backend", None)
    error = getattr(resp, "error", None)
    is_err_method = getattr(resp, "is_error", None)
    if callable(is_err_method):
        try:
            if is_err_method():
                error = error or "llm error"
        except Exception:
            pass

    return LLMCallResult(
        structured_output=structured if isinstance(structured, dict) else None,
        content=content,
        model=model,
        backend=backend,
        error=error,
    )


def default_llm_caller() -> LLMCaller:
    """Production LLM caller wrapping `LLMRunner` at
    `ModelTier.FRONTIER_FAST`.

    The framework's `Store` is responsible for caching — this caller does NOT
    pass `cache_ttl_minutes` to `LLMRunner` to avoid double-caching.
    """
    from work_buddy.llm.runner_v2 import LLMRunner
    from work_buddy.llm.tiers import ModelTier

    runner = LLMRunner()

    class _Default:
        def call(
            self,
            *,
            system: str,
            user: str,
            output_schema: dict[str, Any] | None = None,
            profile: str | None = None,
            max_tokens: int | None = None,
            trace_id: str | None = None,
        ) -> LLMCallResult:
            try:
                resp = runner.call(
                    tier=ModelTier.FRONTIER_FAST,
                    system=system,
                    user=user,
                    output_schema=output_schema,
                    max_tokens=max_tokens or 1024,
                    trace_id=trace_id,
                )
            except Exception as exc:
                return LLMCallResult(error=str(exc))

            return LLMCallResult(
                structured_output=resp.structured_output,
                content=resp.content,
                model=resp.model or None,
                backend=resp.backend or None,
                error=resp.error if resp.is_error() else None,
            )

    return _Default()
