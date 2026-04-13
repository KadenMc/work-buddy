"""Structured page/content summarization via LLM.

Extracts a ``PageSummary`` from raw text content (webpage, chat transcript,
document, etc.). Pure fact extraction — no intent classification, no
hypothesis testing. Results are cached per content hash.

Separate from ``classify.py`` which handles intent classification.
These are independent tasks that happen to share data sources.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from work_buddy.llm.runner import ModelTier, run_task
from work_buddy.logging_config import get_logger
from work_buddy.prompts import get_prompt

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@dataclass
class TypedEntity:
    """A named entity extracted from content with type and context."""

    name: str  # e.g., "Max 5x", "Anthropic", "v0.89.0"
    type: str  # e.g., "product", "organization", "version", "price", "tool", "person", "concept"
    context: str  # 1 phrase: how this entity appears ("$100/month tier", "deprecated in v3")


@dataclass
class PageSummary:
    """Structured summary of a piece of content.

    Extracted by Haiku from raw text. Cached per URL + content hash.
    Consumed by agents for activity reconstruction, or fed into
    classify() for intent matching.
    """

    content_summary: str  # 80-word max factual summary
    entities: list[TypedEntity]  # concrete things mentioned (3-6 items)
    key_claims: list[str]  # 2-4 specific quotable facts
    user_intent_speculation: str  # "Speculate as to why the user visited..."
    user_posture: str  # enum: researching, referencing, evaluating, operating, troubleshooting, contributing, monitoring

    # Metadata (not from LLM)
    source_label: str = ""  # display label of the source (e.g., "Claude [claude.ai]")
    cached: bool = False
    tokens: dict[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = get_prompt("summarize_system")

_SUMMARY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "content_summary": {
            "type": "string",
            "description": "80-word max factual summary of what the content contains",
        },
        "entities": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "type": {
                        "type": "string",
                        "description": "Entity type: product, tool, library, service, person, organization, version, price, concept, project, file_or_path, other",
                    },
                    "context": {
                        "type": "string",
                        "description": "1 phrase explaining how this entity appears in the content",
                    },
                },
                "required": ["name", "type", "context"],
                "additionalProperties": False,
            },
            "description": "3-6 named entities mentioned in the content",
        },
        "key_claims": {
            "type": "array",
            "items": {"type": "string"},
            "description": "2-4 specific, quotable facts from the content worth remembering",
        },
        "user_intent_speculation": {
            "type": "string",
            "description": "Speculate as to why the user visited this page and what they might do with this information. This is a best guess, not a known fact.",
        },
        "user_posture": {
            "type": "string",
            "enum": [
                "researching",
                "referencing",
                "evaluating",
                "operating",
                "troubleshooting",
                "contributing",
                "monitoring",
            ],
            "description": "The user's role relative to this content",
        },
    },
    "required": [
        "content_summary",
        "entities",
        "key_claims",
        "user_intent_speculation",
        "user_posture",
    ],
    "additionalProperties": False,
}

# Schema for batch summarization (multiple items in one call)
_BATCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summaries": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "item_index": {"type": "integer"},
                    **_SUMMARY_SCHEMA["properties"],
                },
                "required": ["item_index", *_SUMMARY_SCHEMA["required"]],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summaries"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def summarize(
    text: str,
    label: str = "",
    *,
    cache_ttl_minutes: int = 30,
    content_hash: str | None = None,
    content_sample: str | None = None,
) -> PageSummary:
    """Summarize a single piece of content into a structured PageSummary.

    Args:
        text: Raw content to summarize (up to ~3KB recommended).
        label: Display label for the content (e.g., "Claude [claude.ai]").
        cache_ttl_minutes: Cache TTL. Cached by task_id + content hash.
        content_hash: Hash for cache invalidation (e.g., MD5 of full text).
        content_sample: ~500 char sample for fuzzy SimHash matching.

    Returns:
        PageSummary with structured fields extracted by Haiku.
    """
    task_id = f"summarize:{label}" if label else "summarize:unknown"

    result = run_task(
        task_id=task_id,
        system=_SYSTEM_PROMPT,
        user=f"## Content: {label}\n\n{text[:5000]}",
        output_schema=_SUMMARY_SCHEMA,
        max_tokens=512,
        cache_ttl_minutes=cache_ttl_minutes,
        content_hash=content_hash,
        content_sample=content_sample,
        tier=ModelTier.HAIKU,
        allowed_tiers=[ModelTier.HAIKU],
    )

    if result.error:
        logger.error("Summarization failed for %s: %s", label, result.error)
        return PageSummary(
            content_summary=f"Summarization failed: {result.error}",
            entities=[], key_claims=[],
            user_intent_speculation="", user_posture="referencing",
            source_label=label,
            tokens={"input": 0, "output": 0},
        )

    return _parse_single(result, label)


def summarize_batch(
    items: list[dict[str, str]],
    *,
    cache_ttl_minutes: int = 0,
) -> list[PageSummary]:
    """Summarize multiple items in a single LLM call.

    Each item is a dict with ``text`` and ``label`` keys.
    More token-efficient than calling summarize() N times (shared
    system prompt, single API call).

    Args:
        items: List of {text: str, label: str} dicts.
        cache_ttl_minutes: Cache TTL for the batch. Default 0 (individual
            item caching should be handled by the caller).

    Returns:
        List of PageSummary in the same order as input items.
    """
    if not items:
        return []

    # Build prompt
    lines = []
    for i, item in enumerate(items):
        lines.append(f"## Item {i}: {item.get('label', '')}")
        lines.append(item.get("text", "")[:3000])
        lines.append("")

    # Scale max_tokens: ~500 per item (content_summary + entities + claims + intent + posture)
    # Empirically: single items produce ~270 tokens, but batch overhead is higher
    max_tokens = max(1024, len(items) * 500 + 200)

    result = run_task(
        task_id="summarize:batch",
        system=_SYSTEM_PROMPT,
        user="\n".join(lines),
        output_schema=_BATCH_SCHEMA,
        max_tokens=min(max_tokens, 4096),
        cache_ttl_minutes=cache_ttl_minutes,
        tier=ModelTier.HAIKU,
        allowed_tiers=[ModelTier.HAIKU],
    )

    if result.error:
        logger.error("Batch summarization failed: %s", result.error)
        return [
            PageSummary(
                content_summary=f"Summarization failed: {result.error}",
                entities=[], key_claims=[],
                user_intent_speculation="", user_posture="referencing",
                source_label=item.get("label", ""),
                tokens={"input": 0, "output": 0},
            )
            for item in items
        ]

    return _parse_batch(result, items)


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _parse_entities(raw: list[dict]) -> list[TypedEntity]:
    return [
        TypedEntity(
            name=e.get("name", ""),
            type=e.get("type", "other"),
            context=e.get("context", ""),
        )
        for e in raw
    ]


def _parse_single(result: Any, label: str) -> PageSummary:
    parsed = result.parsed or {}
    return PageSummary(
        content_summary=parsed.get("content_summary", ""),
        entities=_parse_entities(parsed.get("entities", [])),
        key_claims=parsed.get("key_claims", []),
        user_intent_speculation=parsed.get("user_intent_speculation", ""),
        user_posture=parsed.get("user_posture", "referencing"),
        source_label=label,
        cached=result.cached,
        tokens={"input": result.input_tokens, "output": result.output_tokens},
    )


def _parse_batch(result: Any, items: list[dict]) -> list[PageSummary]:
    parsed = result.parsed or {}
    raw_summaries = parsed.get("summaries", [])

    # Index by item_index for safe lookup
    by_index: dict[int, dict] = {}
    for s in raw_summaries:
        idx = s.get("item_index", -1)
        by_index[idx] = s

    summaries = []
    per_item_tokens = {
        "input": result.input_tokens // max(len(items), 1),
        "output": result.output_tokens // max(len(items), 1),
    }

    for i, item in enumerate(items):
        raw = by_index.get(i, {})
        summaries.append(PageSummary(
            content_summary=raw.get("content_summary", ""),
            entities=_parse_entities(raw.get("entities", [])),
            key_claims=raw.get("key_claims", []),
            user_intent_speculation=raw.get("user_intent_speculation", ""),
            user_posture=raw.get("user_posture", "referencing"),
            source_label=item.get("label", ""),
            cached=result.cached,
            tokens=per_item_tokens,
        ))

    return summaries
