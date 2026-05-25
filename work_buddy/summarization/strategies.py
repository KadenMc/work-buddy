"""The two `SummaryStrategy` implementations.

- `LayeredDisclosureStrategy` — TL;DR + ordered child topics with source_refs.
  Used by `conversation_observability`. Prompt and schema moved verbatim from
  the previous in-tree implementation.
- `FlatExtractionStrategy` — one root carrying structured extra fields
  (entities, claims, intent, posture). Used by Chrome tab triage. Prompt and
  schema moved verbatim from the previous in-tree implementation. Declares
  `BATCHED` — one LLM call processes N items.
"""

from __future__ import annotations

from typing import Any

from work_buddy.summarization.protocol import (
    SummarizationError,
    SummaryCapability,
    SummaryNode,
)


# ===========================================================================
# Layered disclosure — sessions
# ===========================================================================


_LAYERED_SYSTEM_PROMPT = """\
You are an analyst producing compact, factual recaps of Claude Code
agent-user conversations. Each conversation is a sequence of turns
(user + assistant) interleaved with tool calls (Bash, Edit, Write, etc.)
and tool outputs.

Produce two things:
1. tldr: ONE sentence (≤25 words) capturing what was accomplished or
   attempted. No greetings, no commentary on tone. Concrete enough that
   the user can recognize the session a week from now.
2. topic_summary: an ordered list of distinct topics within the session.
   Each topic has a short title (≤8 words), a one-sentence summary, a
   span_range covering the spans it spans, and 2-5 keywords. Cap at 8
   topics; merge fine-grained sub-topics rather than emitting many
   nearly-identical entries.

Be operational. Prefer concrete nouns ("AFK build of conversation
observability subsystem") over abstract ones ("worked on a feature").
"""


_LAYERED_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["tldr", "topic_summary"],
    "properties": {
        "tldr": {"type": "string"},
        "topic_summary": {
            "type": "array",
            "maxItems": 8,
            "items": {
                "type": "object",
                "required": ["title", "summary", "span_range", "keywords"],
                "properties": {
                    "title": {"type": "string"},
                    "summary": {"type": "string"},
                    "span_range": {
                        "type": "array",
                        "minItems": 2,
                        "maxItems": 2,
                        "items": {"type": "integer"},
                    },
                    "keywords": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": 5,
                    },
                },
            },
        },
    },
}


class LayeredDisclosureStrategy:
    """Layered disclosure — produces tldr root + ordered topic children.

    Each child node carries a `source_ref` pointing back to the input span
    range, satisfying the "summaries are indexes, not truth" invariant the
    deferred PD phase relies on.
    """

    name = "layered_disclosure"
    prompt_version = 1
    schema_version = 1
    capabilities = frozenset({SummaryCapability.LAYERED})
    system_prompt = _LAYERED_SYSTEM_PROMPT
    output_schema = _LAYERED_OUTPUT_SCHEMA
    batch_output_schema: dict[str, Any] | None = None

    def parse(
        self,
        structured_output: dict[str, Any] | None,
        raw_content: str,
    ) -> SummaryNode:
        if not isinstance(structured_output, dict):
            raise SummarizationError(
                "layered_disclosure.parse: structured_output is not a dict "
                f"(got {type(structured_output).__name__})"
            )
        if "tldr" not in structured_output:
            raise SummarizationError(
                "layered_disclosure.parse: missing 'tldr' field"
            )

        tldr = str(structured_output["tldr"])
        topics = structured_output.get("topic_summary") or []

        children: list[SummaryNode] = []
        for i, topic in enumerate(topics):
            if not isinstance(topic, dict):
                continue
            span_range = topic.get("span_range") or [None, None]
            span_start = span_range[0] if len(span_range) >= 1 else None
            span_end = span_range[1] if len(span_range) >= 2 else None

            source_ref: dict[str, Any] | None = None
            if isinstance(span_start, int) and isinstance(span_end, int):
                source_ref = {
                    "span_start": span_start,
                    "span_end": span_end,
                }

            children.append(SummaryNode(
                summary=str(topic.get("summary", "")),
                source_ref=source_ref,
                children=[],
                extra={
                    "title": str(topic.get("title", "")),
                    "topic_index": i,
                    "keywords": list(topic.get("keywords") or []),
                    "span_start": span_start,
                    "span_end": span_end,
                },
            ))

        return SummaryNode(
            summary=tldr,
            source_ref=None,
            children=children,
            extra={},
        )


# ===========================================================================
# Flat extraction — web pages / Chrome tabs
# ===========================================================================


_FLAT_SYSTEM_PROMPT = """\
You extract structured facts from web pages, chats, and similar content.
Produce a compact factual summary plus typed entities, key claims, an
intent guess, and the user's posture toward the content.

Be operational and factual. No greetings, no opinions. Quote concrete
nouns and numbers where they appear.
"""


_FLAT_SUMMARY_PROPERTIES: dict[str, Any] = {
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
                    "description": (
                        "Entity type: product, tool, library, service, person, "
                        "organization, version, price, concept, project, "
                        "file_or_path, other"
                    ),
                },
                "context": {
                    "type": "string",
                    "description": (
                        "1 phrase explaining how this entity appears in the "
                        "content"
                    ),
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
        "description": (
            "Speculate as to why the user visited this page and what they "
            "might do with this information. This is a best guess, not a "
            "known fact."
        ),
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
}

_FLAT_REQUIRED: list[str] = [
    "content_summary",
    "entities",
    "key_claims",
    "user_intent_speculation",
    "user_posture",
]


_FLAT_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": _FLAT_SUMMARY_PROPERTIES,
    "required": _FLAT_REQUIRED,
    "additionalProperties": False,
}


_FLAT_BATCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summaries": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "item_index": {"type": "integer"},
                    **_FLAT_SUMMARY_PROPERTIES,
                },
                "required": ["item_index", *_FLAT_REQUIRED],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summaries"],
    "additionalProperties": False,
}


class FlatExtractionStrategy:
    """Flat extraction — one root node carrying structured fields.

    Produces a depth-1 `SummaryNode`: `summary=content_summary`, empty
    `children`, null `source_ref`, and an `extra` dict with `entities`,
    `key_claims`, `user_intent_speculation`, `user_posture`. The `PageSummary`
    consumer-facing dataclass is rebuilt by an adapter near the Chrome
    binding.

    Declares `BATCHED` — the orchestrator's batch path issues one LLM call
    per N items via `parse_batch`.
    """

    name = "flat_extraction"
    prompt_version = 1
    schema_version = 1
    capabilities = frozenset({SummaryCapability.FLAT, SummaryCapability.BATCHED})
    system_prompt = _FLAT_SYSTEM_PROMPT
    output_schema = _FLAT_OUTPUT_SCHEMA
    batch_output_schema = _FLAT_BATCH_SCHEMA

    def parse(
        self,
        structured_output: dict[str, Any] | None,
        raw_content: str,
    ) -> SummaryNode:
        if not isinstance(structured_output, dict):
            raise SummarizationError(
                "flat_extraction.parse: structured_output is not a dict "
                f"(got {type(structured_output).__name__})"
            )
        if "content_summary" not in structured_output:
            raise SummarizationError(
                "flat_extraction.parse: missing 'content_summary' field"
            )

        return SummaryNode(
            summary=str(structured_output.get("content_summary", "")),
            source_ref=None,
            children=[],
            extra={
                "entities": list(structured_output.get("entities") or []),
                "key_claims": list(structured_output.get("key_claims") or []),
                "user_intent_speculation": str(
                    structured_output.get("user_intent_speculation", "")
                ),
                "user_posture": str(
                    structured_output.get("user_posture", "referencing")
                ),
            },
        )

    def parse_batch(
        self,
        structured_output: dict[str, Any] | None,
        raw_content: str,
        item_ids: list[str],
    ) -> list[SummaryNode | None]:
        if not isinstance(structured_output, dict):
            raise SummarizationError(
                "flat_extraction.parse_batch: structured_output is not a dict"
            )
        raw = structured_output.get("summaries") or []
        by_index: dict[int, dict[str, Any]] = {}
        for entry in raw:
            if isinstance(entry, dict):
                idx = entry.get("item_index")
                if isinstance(idx, int):
                    by_index[idx] = entry

        results: list[SummaryNode | None] = []
        for i, _item_id in enumerate(item_ids):
            entry = by_index.get(i)
            if entry is None:
                results.append(None)
                continue
            try:
                results.append(self.parse(entry, ""))
            except SummarizationError:
                results.append(None)
        return results
