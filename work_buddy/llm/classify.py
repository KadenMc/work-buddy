"""General-purpose multi-label/multi-class intent classification via LLM.

Classifies a list of DataItems against N intent hypotheses. Each item
gets per-intent relevance judgments with evidence and strength ratings.

This is a pure classification task — no summarization. For structured
content extraction, use ``summarize.py`` instead. The two are independent
tasks that can be composed: summarize first, then classify the summaries.

Built on ``runner.run_task()`` for API calls, caching, and cost tracking.
Model tier is locked to Haiku by default — agents cannot escalate.
"""

from __future__ import annotations

from typing import Any, Literal

from work_buddy.llm.intent import (
    DataItem,
    IntentMatch,
    ItemClassification,
    MulticlassClassification,
    MultilabelClassification,
)
from work_buddy.logging_config import get_logger
from work_buddy.prompts import get_prompt

logger = get_logger(__name__)

_MULTILABEL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "item_index": {"type": "integer"},
                    "intent_matches": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "intent": {"type": "string"},
                                "relevant": {"type": "boolean"},
                                "confidence": {"type": "number"},
                                "evidence": {
                                    "type": "string",
                                    "description": "One sentence citing specific content from the item",
                                },
                                "strength": {
                                    "type": "string",
                                    "enum": ["strong", "moderate", "weak"],
                                },
                            },
                            "required": ["intent", "relevant", "confidence", "evidence", "strength"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["item_index", "intent_matches"],
                "additionalProperties": False,
            },
        },
        "overall_narrative": {
            "type": "string",
            "description": "1-2 sentence synthesis of what the user is doing across all items",
        },
        "activity_domains": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Distinct work areas identified",
        },
        "confidence": {
            "type": "number",
            "description": "0.0-1.0 overall confidence",
        },
    },
    "required": ["items", "overall_narrative", "activity_domains", "confidence"],
    "additionalProperties": False,
}


def classify(
    items: list[DataItem],
    intents: list[str],
    context: str | None = None,
    cache_ttl_minutes: int = 0,
    mode: Literal["multilabel", "multiclass"] = "multilabel",
) -> MultilabelClassification | MulticlassClassification:
    """Classify data items against intent hypotheses.

    Args:
        items: Data items to classify (any source).
        intents: Intent hypotheses to evaluate against. Required — for
            open-ended summarization without intents, use ``summarize()``.
        context: Optional background context for the classification.
        cache_ttl_minutes: Cache TTL. Default 0 (intents change between calls).
        mode: "multilabel" (0..N intents per item) or "multiclass" (exactly 1).

    Returns:
        MultilabelClassification or MulticlassClassification.
    """
    if mode == "multiclass":
        raise NotImplementedError("Multi-class classification not yet implemented.")

    if not items:
        return MultilabelClassification(
            items=[], overall_narrative="No data items provided.",
            activity_domains=[], confidence=0.0,
        )

    if not intents:
        return MultilabelClassification(
            items=[
                ItemClassification(
                    item_index=i, label=item.label,
                    intent_matches=[],
                )
                for i, item in enumerate(items)
            ],
            overall_narrative="No intents provided for classification.",
            activity_domains=[], confidence=0.0,
        )

    from work_buddy.llm.runner import ModelTier, run_task

    prompt = _build_prompt(items, intents, context)

    # Scale max_tokens: ~80 per intent per item + overhead
    estimated_tokens = len(items) * len(intents) * 80 + 200
    max_tokens = max(512, min(4096, estimated_tokens))

    result = run_task(
        task_id="classify:multilabel",
        system=get_prompt("classify_system"),
        user=prompt,
        output_schema=_MULTILABEL_SCHEMA,
        max_tokens=max_tokens,
        cache_ttl_minutes=cache_ttl_minutes,
        tier=ModelTier.HAIKU,
        allowed_tiers=[ModelTier.HAIKU],
    )

    if result.error:
        logger.error("Classification failed: %s", result.error)
        return MultilabelClassification(
            items=[], overall_narrative=f"Classification failed: {result.error}",
            activity_domains=[], confidence=0.0,
            tokens={"input": 0, "output": 0},
        )

    return _parse_result(result, items)


def _build_prompt(
    items: list[DataItem],
    intents: list[str],
    context: str | None,
) -> str:
    """Build the user prompt for classification."""
    lines: list[str] = []

    if context:
        lines.append("## Context")
        lines.append(context)
        lines.append("")

    lines.append("## Items")
    for i, item in enumerate(items):
        lines.append(f"\n### Item {i}: {item.label} [source: {item.source}]")
        if item.metadata:
            meta_parts = [f"{k}: {v}" for k, v in item.metadata.items() if v]
            if meta_parts:
                lines.append(f"Metadata: {', '.join(meta_parts[:5])}")
        if item.text:
            lines.append(f"Content:\n{item.text[:3000]}")
        else:
            lines.append("[No content available]")

    lines.append("\n## Intents to classify against")
    for j, intent in enumerate(intents, 1):
        lines.append(f'{j}. "{intent}"')

    return "\n".join(lines)


def _parse_result(
    result: Any,
    items: list[DataItem],
) -> MultilabelClassification:
    """Parse run_task result into typed MultilabelClassification."""
    parsed = result.parsed or {}

    classified_items: list[ItemClassification] = []
    for raw_item in parsed.get("items", []):
        matches = [
            IntentMatch(
                intent=m.get("intent", ""),
                relevant=m.get("relevant", False),
                confidence=m.get("confidence", 0.0),
                evidence=m.get("evidence", ""),
                strength=m.get("strength", "weak"),
            )
            for m in raw_item.get("intent_matches", [])
        ]

        idx = raw_item.get("item_index", 0)
        label = items[idx].label if idx < len(items) else ""

        classified_items.append(ItemClassification(
            item_index=idx,
            label=label,
            intent_matches=matches,
        ))

    return MultilabelClassification(
        items=classified_items,
        overall_narrative=parsed.get("overall_narrative", ""),
        activity_domains=parsed.get("activity_domains", []),
        confidence=parsed.get("confidence", 0.5),
        tokens={"input": result.input_tokens, "output": result.output_tokens},
        cached=result.cached,
    )
