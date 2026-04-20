"""Lightweight LLM task runner with caching and cost tracking.

Provides a thin wrapper around the Anthropic API for programmatic LLM
calls from work-buddy modules. Designed for cheap, selective tasks
(Haiku summarization, classification) — not full conversations.

Usage::

    from work_buddy.llm import run_task

    result = run_task(
        task_id="chrome_infer:github.com",
        system="You are a browsing analyst.",
        user="What is this page about?",
        json_mode=True,
    )
    print(result.parsed)  # dict from JSON response
"""

from work_buddy.llm.classify import classify
from work_buddy.llm.intent import (
    DataItem,
    IntentMatch,
    ItemClassification,
    MulticlassClassification,
    MultilabelClassification,
)
from work_buddy.llm.response import ErrorKind, LLMResponse, TierAttempt, ToolCall
from work_buddy.llm.runner import ModelTier as ModelTierLegacy
from work_buddy.llm.runner import TaskResult, run_task
from work_buddy.llm.runner_v2 import LLMRunner, llm_call
from work_buddy.llm.summarize import PageSummary, summarize, summarize_batch
from work_buddy.llm.tiers import ModelTier, TierBinding, resolve_tier

__all__ = [
    # Unified runner (phase 1 of LLM + Context refactor)
    "LLMRunner",
    "llm_call",
    "LLMResponse",
    "ErrorKind",
    "ModelTier",
    "TierBinding",
    "resolve_tier",
    "ToolCall",
    "TierAttempt",
    # Legacy runner (kept during phases 2–3 migration, removed in phase 8)
    "run_task",
    "TaskResult",
    "ModelTierLegacy",
    # Higher-level helpers (unchanged)
    "classify",
    "summarize",
    "summarize_batch",
    "PageSummary",
    "DataItem",
    "IntentMatch",
    "ItemClassification",
    "MultilabelClassification",
    "MulticlassClassification",
]
