"""Summarization framework — `Summarizer = Source × Strategy × Store`.

Protocol-based composition for content summarization, modeled on the artifact
system (`Artifact = Storage × Lifecycle × Provenance`). Three pluggable axes
plus a written-once shared core that handles bounded refresh, staleness
detection, error isolation, and provenance stamping.

Two consumers today:
- `work_buddy.conversation_observability` — sessions, layered disclosure,
  durable version-stamped store.
- `work_buddy.collectors.chrome_summarizer_binding` — web pages, flat
  extraction, TTL content-hash cache.

The stored summary is always a `SummaryNode` tree — flat extraction is the
depth-1 case. This invariant keeps the deferred progressive-disclosure phase
additive (it adds consumers of the tree, never reshapes it).
"""

from work_buddy.summarization.artifacts import (
    register_summarization_artifact,
)
from work_buddy.summarization.protocol import (
    DiscoveryWindow,
    IncoherentComposition,
    LLMCallResult,
    LLMCaller,
    Provenance,
    Source,
    Store,
    SummarizationError,
    SummaryCapability,
    SummaryNode,
    SummaryStrategy,
)
from work_buddy.summarization.summarizer import RefreshReport, Summarizer
from work_buddy.summarization.orchestrator import as_caller, run_refresh

# Importing this package registers the artifact so it appears in
# `artifact_registry_dump` and the cleanup tick (no-op under
# INFINITE_LIFECYCLE; provides registry visibility).
register_summarization_artifact()

__all__ = [
    "DiscoveryWindow",
    "IncoherentComposition",
    "LLMCallResult",
    "LLMCaller",
    "Provenance",
    "RefreshReport",
    "Source",
    "Store",
    "SummarizationError",
    "SummaryCapability",
    "SummaryNode",
    "SummaryStrategy",
    "Summarizer",
    "as_caller",
    "run_refresh",
]
