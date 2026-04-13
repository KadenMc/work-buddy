"""Intent classification types for multi-label and multi-class classification.

Pure type definitions — no logic, no LLM calls. Used by ``classify.py``
for the classify() function and by consumers that inspect results.

Two classification modes:
- **Multi-label**: each item can match 0..N intents simultaneously.
  Use for activity inference where a tab/chat/commit relates to
  multiple concurrent work streams.
- **Multi-class**: each item matches exactly one intent (or 'other').
  Use for routing/triage where an item must go to exactly one bucket.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class DataItem:
    """A piece of data to classify. Source-agnostic.

    The same type works for Chrome tabs, chat sessions, git commits,
    vault notes — anything that has text content and a label.
    """

    text: str  # the content to analyze (page text, chat transcript, diff, etc.)
    label: str  # short display label (e.g., "ChatGPT - Work Buddy [chatgpt.com]")
    source: str  # data source type (e.g., "chrome_tab", "chat_session", "git_commit")
    metadata: dict[str, Any] = field(default_factory=dict)  # source-specific signals


@dataclass
class IntentMatch:
    """One label in a classification of a data item against an intent.

    In multi-label mode, multiple IntentMatches per item can have
    ``relevant=True``. In multi-class mode, exactly one will.
    """

    intent: str  # the hypothesis being evaluated
    relevant: bool  # does this item relate to this intent?
    confidence: float  # 0.0–1.0
    evidence: str  # one sentence citing specific content from the item
    strength: str  # "strong", "moderate", or "weak"


@dataclass
class ItemClassification:
    """Intent classification result for one data item.

    Shared by both multi-label and multi-class modes. The difference
    is in the constraints on ``intent_matches``:
    - Multi-label: 0..N can be relevant
    - Multi-class: exactly 1 is relevant

    This is a pure classification result — no summarization fields.
    For content summaries, use ``PageSummary`` from ``summarize.py``.
    """

    item_index: int
    label: str
    intent_matches: list[IntentMatch]


@dataclass
class MultilabelClassification:
    """Result of multi-label classification: N items against M intents.

    Each item can match 0..N intents simultaneously. Use for activity
    inference where a tab/chat/commit relates to multiple concurrent
    work streams.
    """

    items: list[ItemClassification]
    overall_narrative: str  # 1-2 sentence synthesis across all items
    activity_domains: list[str]  # distinct work areas identified
    confidence: float  # 0.0–1.0 overall confidence
    tokens: dict[str, int] = field(default_factory=dict)  # {input, output}
    cached: bool = False


@dataclass
class MulticlassClassification:
    """Result of multi-class classification: N items, each into exactly 1 intent.

    Each item's ``intent_matches`` will have exactly one entry with
    ``relevant=True`` (or an 'other' fallback). Use for routing/triage
    where an item must go to exactly one bucket.

    Not yet implemented in classify() — defined for future use.
    """

    items: list[ItemClassification]
    overall_narrative: str
    activity_domains: list[str]
    confidence: float
    tokens: dict[str, int] = field(default_factory=dict)
    cached: bool = False
