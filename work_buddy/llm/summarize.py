"""Structured page/content summarization — thin shims over the framework.

`PageSummary` and `TypedEntity` are the consumer-facing dataclasses that other
modules (Chrome triage in particular) import directly. They remain owned by
this module.

`summarize` and `summarize_batch` are thin wrappers that delegate prompt /
schema / parse to `work_buddy.summarization.strategies.FlatExtractionStrategy`
and adapt the framework's `SummaryNode` back into a `PageSummary`. They
preserve the previous call signature (including `cache_ttl_minutes`, which
passes through to `LLMRunner`'s built-in cache — appropriate for these direct
callers since they do not compose a framework `Store`).

Internal Chrome triage paths go through
`work_buddy.collectors.chrome_summarizer_binding.summarize_tabs` instead,
which uses the framework's full composer + `TtlCacheStore` for cache +
provenance handling.

Separate from `classify.py` which handles intent classification.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from work_buddy.llm.runner_v2 import LLMRunner
from work_buddy.llm.tiers import ModelTier
from work_buddy.logging_config import get_logger
from work_buddy.summarization.protocol import SummaryNode
from work_buddy.summarization.strategies import FlatExtractionStrategy

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Consumer-facing dataclasses
# ---------------------------------------------------------------------------


@dataclass
class TypedEntity:
    """A named entity extracted from content with type and context."""

    name: str
    type: str
    context: str


@dataclass
class PageSummary:
    """Structured summary of a piece of content.

    Extracted by Haiku from raw text. Consumed by agents for activity
    reconstruction, or fed into ``classify()`` for intent matching.
    """

    content_summary: str
    entities: list[TypedEntity]
    key_claims: list[str]
    user_intent_speculation: str
    user_posture: str

    source_label: str = ""
    cached: bool = False
    tokens: dict[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Public API — thin shims
# ---------------------------------------------------------------------------


def summarize(
    text: str,
    label: str = "",
    *,
    cache_ttl_minutes: int = 30,
    content_hash: str | None = None,
    content_sample: str | None = None,
) -> PageSummary:
    """Summarize a single piece of content into a structured ``PageSummary``.

    Args:
        text: Raw content to summarize (up to ~5KB is used).
        label: Display label for the content (e.g., "Claude [claude.ai]").
        cache_ttl_minutes: Cache TTL for the underlying ``LLMRunner`` cache.
        content_hash: Accepted for API compatibility; not currently plumbed
            to the LLMRunner cache key.
        content_sample: Accepted for API compatibility; not currently plumbed
            to the LLMRunner cache key.

    Returns:
        ``PageSummary`` with structured fields extracted by Haiku.
    """
    strategy = FlatExtractionStrategy()
    task_id = f"summarize:{label}" if label else "summarize:unknown"

    resp = LLMRunner().call(
        tier=ModelTier.FRONTIER_FAST,
        system=strategy.system_prompt,
        user=f"## Content: {label}\n\n{text[:5000]}",
        output_schema=strategy.output_schema,
        max_tokens=512,
        cache_ttl_minutes=cache_ttl_minutes,
        trace_id=task_id,
        detail=label or None,  # readily-available one-liner → "Summarize: <label>"
    )

    if resp.is_error():
        logger.error("Summarization failed for %s: %s", label, resp.error)
        return _failed_page_summary(label, resp.error or "llm error")

    try:
        node = strategy.parse(resp.structured_output, resp.content)
    except Exception as exc:
        logger.error("Parse failed for %s: %s", label, exc)
        return _failed_page_summary(label, f"parse error: {exc}")

    return _node_to_page_summary(
        node,
        label,
        cached=resp.cached,
        tokens={"input": resp.input_tokens, "output": resp.output_tokens},
    )


def summarize_batch(
    items: list[dict[str, str]],
    *,
    cache_ttl_minutes: int = 0,
) -> list[PageSummary]:
    """Summarize multiple items in a single LLM call.

    Each item is a dict with ``text`` and ``label`` keys. More
    token-efficient than calling ``summarize()`` N times (shared system
    prompt, single API call).

    Args:
        items: List of ``{text: str, label: str}`` dicts.
        cache_ttl_minutes: Cache TTL for the batch via ``LLMRunner``'s cache.

    Returns:
        List of ``PageSummary`` aligned with input items.
    """
    if not items:
        return []

    strategy = FlatExtractionStrategy()
    batch_schema = strategy.batch_output_schema or strategy.output_schema

    parts: list[str] = []
    for i, item in enumerate(items):
        parts.append(f"## Item {i}: {item.get('label', '')}")
        parts.append(item.get("text", "")[:3000])
        parts.append("")

    # Scale max_tokens: ~500 per item (content_summary + entities + claims
    # + intent + posture). Cap at the tier's reasonable upper bound.
    max_tokens = min(max(1024, len(items) * 500 + 200), 4096)

    resp = LLMRunner().call(
        tier=ModelTier.FRONTIER_FAST,
        system=strategy.system_prompt,
        user="\n".join(parts),
        output_schema=batch_schema,
        max_tokens=max_tokens,
        cache_ttl_minutes=cache_ttl_minutes,
        trace_id="summarize:batch",
    )

    if resp.is_error():
        logger.error("Batch summarization failed: %s", resp.error)
        return [
            _failed_page_summary(item.get("label", ""), resp.error or "llm error")
            for item in items
        ]

    item_ids = [str(i) for i in range(len(items))]
    try:
        nodes = strategy.parse_batch(
            resp.structured_output, resp.content, item_ids,
        )
    except Exception as exc:
        logger.error("Batch parse failed: %s", exc)
        return [
            _failed_page_summary(item.get("label", ""), f"parse error: {exc}")
            for item in items
        ]

    per_item_tokens = {
        "input": resp.input_tokens // max(len(items), 1),
        "output": resp.output_tokens // max(len(items), 1),
    }

    out: list[PageSummary] = []
    for item, node in zip(items, nodes):
        label = item.get("label", "")
        if node is None:
            out.append(_empty_page_summary(label, per_item_tokens))
        else:
            out.append(_node_to_page_summary(
                node, label, cached=resp.cached, tokens=per_item_tokens,
            ))
    return out


# ---------------------------------------------------------------------------
# Adapters — SummaryNode -> PageSummary
# ---------------------------------------------------------------------------


def _node_to_page_summary(
    node: SummaryNode,
    label: str,
    *,
    cached: bool,
    tokens: dict[str, int],
) -> PageSummary:
    extra = node.extra or {}
    entities: list[TypedEntity] = []
    for e in extra.get("entities") or []:
        if isinstance(e, dict):
            entities.append(TypedEntity(
                name=str(e.get("name", "")),
                type=str(e.get("type", "other")),
                context=str(e.get("context", "")),
            ))
    return PageSummary(
        content_summary=node.summary,
        entities=entities,
        key_claims=list(extra.get("key_claims") or []),
        user_intent_speculation=str(extra.get("user_intent_speculation", "")),
        user_posture=str(extra.get("user_posture", "referencing")),
        source_label=label,
        cached=cached,
        tokens=tokens,
    )


def _failed_page_summary(label: str, error: str) -> PageSummary:
    return PageSummary(
        content_summary=f"Summarization failed: {error}",
        entities=[],
        key_claims=[],
        user_intent_speculation="",
        user_posture="referencing",
        source_label=label,
        tokens={"input": 0, "output": 0},
    )


def _empty_page_summary(label: str, tokens: dict[str, int]) -> PageSummary:
    return PageSummary(
        content_summary="",
        entities=[],
        key_claims=[],
        user_intent_speculation="",
        user_posture="referencing",
        source_label=label,
        tokens=tokens,
    )
