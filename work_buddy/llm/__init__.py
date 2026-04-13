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
from work_buddy.llm.runner import ModelTier, TaskResult, run_task
from work_buddy.llm.summarize import PageSummary, summarize, summarize_batch

__all__ = [
    "run_task",
    "TaskResult",
    "ModelTier",
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
