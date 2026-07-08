---
name: LLM Runner
kind: concept
description: Unified LLM call entry point (LLMRunner, llm_call) with semantic tier enum, normalized LLMResponse, and built-in tier escalation. Replaces the split between run_task (structured output) and llm_with_tools (tool calls).
summary: Single call site for every internal LLM request. Callers pick a semantic ModelTier (LOCAL_TOOL_CALLING / LOCAL_FAST / FRONTIER_FAST / FRONTIER_BALANCED / FRONTIER_BEST), get back a normalized LLMResponse, and opt into tier escalation via escalate_on + escalate_to. Tier names bind to concrete models/profiles in config.yaml, so model swaps are config-only.
tags:
- llm
- llm_runner
- llm_call
- model_tier
- error_kind
- escalation
- sonnet
- haiku
- opus
- local_tool_calling
- local_fast
- frontier_fast
- frontier_balanced
- frontier_best
- anthropic
- lmstudio
- structured_output
aliases:
- llm runner
- llm call
- unified llm
- model tier
- error kind
- escalation
- tier escalation
parents:
- architecture
- architecture
dev_notes: |-
  **Inference provenance.** ``run_task`` is wrapped by ``_with_call_id``, which binds an ambient ``call_id`` (+ start time) for the call and emits an ``error`` provenance row on failure. Successful / cached completions emit their provenance row from ``cost.log_call`` instead. The bound ``call_id`` is also reused by ``broker.slot`` as its ``SlotMetrics.id``, so a local call's scheduler-latency row joins its provenance row. See ``architecture/inference/provenance``.
---

# LLM Runner

One entry point (`work_buddy.llm.LLMRunner` or the module-level `llm_call` convenience) that accepts a semantic `ModelTier` and returns a normalized `LLMResponse`. Replaces the split between `run_task` (structured output, Anthropic-only) and `llm_with_tools` (tool calls, LM Studio-only) that predates the LLMRunner extraction.

## Why it exists

Before this: two incompatible LLM-calling paths with disjoint error shapes, no shared tier abstraction, and no escalation. Every caller had to pick a path and couldn't fall through to another tier on failure. `journal_triage_scan` dropped `group_intent` fields silently on local-LLM timeouts because there was no escalation and no normalized error surface to detect them.

After this: one call site, one response dataclass, one error taxonomy, opt-in tier escalation.

## ModelTier

```python
class ModelTier(str, Enum):
    LOCAL_TOOL_CALLING = "local_tool_calling"   # LM Studio, MCP tool-call loop
    LOCAL_FAST         = "local_fast"            # Local, structured output, no tools
    FRONTIER_FAST      = "frontier_fast"         # Haiku-class
    FRONTIER_BALANCED  = "frontier_balanced"     # Sonnet-class
    FRONTIER_BEST      = "frontier_best"         # Opus-class
```

Tiers bind to concrete models/profiles in `config.yaml` under `llm.tiers`:

```yaml
llm:
  tiers:
    frontier_balanced:
      backend: anthropic
      model: claude-sonnet-4-6
      defaults: {max_tokens: 4096}
    local_tool_calling:
      backend: lmstudio_native
      profile: local_agent
      defaults: {max_tokens: 4096}
```

When `claude-sonnet-5` ships, swap one config line. Call sites don't change.

**Tier binding is authoritative for endpoint dispatch.** For local tiers, the tier's `backend` field (`lmstudio_native` vs `openai_compat`) determines which endpoint the runner hits — not the legacy `provider:` field on `llm.backends.<id>` in config. A `provider:` value that disagrees with the tier binding is ignored with a warning prompting the user to drop it. This invariant exists because non-tool-calling tiers (`LOCAL_FAST`) need the `openai_compat` endpoint for LM Studio's JIT auto-load to work; tool-calling tiers (`LOCAL_TOOL_CALLING`) need `lmstudio_native` for server-side MCP tool loops. Mixing them produces silent failures (e.g., 500 "Model unloaded" because the native endpoint doesn't JIT-load).

## LLMResponse

Frozen dataclass at `work_buddy.llm.response.LLMResponse`. Always safe to read:
- `content: str`  ("" on error or tool-only turn)
- `structured_output: dict | None`  (parsed when output_schema supplied)
- `tool_calls: tuple[ToolCall, ...]`  (normalized across backends)
- `reasoning: str | None`  (LM Studio reasoning trace; None for Anthropic)
- `tier_used: str`  (may differ from requested tier if escalated)
- `tier_attempts: tuple[TierAttempt, ...]`  (audit trail)
- `model: str`, `backend: str`
- `input_tokens`, `output_tokens`, `reasoning_tokens`, `cost_usd`, `cached`
- `error: str | None`, `error_kind: ErrorKind | None`, `hint: str | None`

Check `resp.is_error()` before reading output fields on failure-sensitive paths.

## Structured-output schema normalization

`run_task` — and therefore every `LLMRunner` call with `output_schema` — passes the schema through `_normalize_structured_output_schema` before any backend sees it. The constrained-decoding APIs accept only a subset of JSON Schema, so the normalizer:

- sets `additionalProperties: false` on every object node (the API requires it explicitly and rejects any other value);
- strips validation-constraint keywords the API rejects — `maxItems`, `minimum` / `maximum`, `multipleOf`, `minLength` / `maxLength`, `pattern`, `uniqueItems`, and object-size bounds;
- keeps `minItems` only when it is 0 or 1 (the only values the API supports).

Consequence for schema authors: a hard bound expressed only in the schema (e.g. `maxItems: 8`) is no longer enforced by constrained decoding — state such limits in the prompt as well.

## ErrorKind taxonomy

```
TIMEOUT | CONTEXT_EXCEEDED | EMPTY_CONTENT | SCHEMA_VIOLATION |
BACKEND_UNAVAILABLE | AUTH | RATE_LIMITED | TOOL_EXECUTION |
MODEL_NOT_AVAILABLE | MODEL_UNSUPPORTED | BAD_REQUEST |
MALFORMED_RESPONSE | VALIDATION_FAILED | UNKNOWN
```

Mirrors `LocalInferenceError.kind` for LM Studio and extends for Anthropic-side failures. Heuristic fallback classifies legacy bare-string errors (`run_task.error`) when no `error_kind` is provided.

`MODEL_NOT_AVAILABLE` (renamed from `MODEL_NOT_LOADED`) covers the broader "the model the caller asked for isn't reachable right now" state: model not downloaded, no LM Link device surfaces it, JIT loading disabled, or the linked device just disconnected. The old name implied "loadable but not in memory," which is a narrower (and JIT-handles-this-automatically) condition than what this kind actually represents.

`VALIDATION_FAILED` is **adapter-side** — the runner itself never emits it. Backends produce well-formed responses; an adapter (e.g. journal segmenter, `call_for_verdict`) checks the parsed structured output, finds a required field missing or a content-shape rule violated, and synthesizes a `VALIDATION_FAILED` error onto the response so the caller can decide whether to retry at a higher tier. Distinct from `SCHEMA_VIOLATION` (JSON-shape failures at the backend) — `VALIDATION_FAILED` means the JSON parsed but doesn't satisfy semantic constraints the caller cares about.

## Tier escalation

```python
resp = LLMRunner().call(
    tier=ModelTier.FRONTIER_BALANCED,
    system=..., user=...,
    output_schema=VERDICT_SCHEMA,
    escalate_on=[ErrorKind.TIMEOUT, ErrorKind.CONTEXT_EXCEEDED, ErrorKind.EMPTY_CONTENT],
    escalate_to=[ModelTier.FRONTIER_BEST],
)
```

If the first tier returns a matching error, the runner retries on the next tier in `escalate_to`. Every attempt is recorded in `resp.tier_attempts`. Empty-content detection is built-in — any call returning no content, no structured output, and no tool calls triggers `EMPTY_CONTENT` even when the backend returned 200 OK (catches the LM Studio "zero tokens" failure mode).

For adapter-side validation escalation (re-call on `VALIDATION_FAILED`), see `work_buddy.triage.verdict_call.call_for_verdict` (verdict-shape callers) and `work_buddy.triage.adapters.journal._segment_with_escalation` (journal segmentation). These manage their own tier-iteration loop on top of the runner because the runner's built-in escalation only fires on `LLMResponse` errors, not on post-parse semantic-validation failures.

## Content-aware caching

Results are cached at `work_buddy.llm.cache` keyed by `{backend}:{model}:{system_hash[:12]}:{task_id}`. The cache requires an `input_hash` (SHA-256 of the user prompt) on every `get` / `put` — there is no "skip the hash" path. Lookup is exact-match on input_hash, with a SimHash fuzzy-match fallback (Hamming distance ≤ 3) on the full user prompt for tolerating trivial noise (timestamp rotation, ad changes).

Fingerprinting happens automatically inside `run_task` — callers don't compute hashes themselves. System-prompt edits cleanly invalidate the cache by changing the scoped key; user-prompt changes correctly miss without polluting unrelated callers' slots. Each entry stores `system_hash` and `system_preview` (first 500 chars) for provenance — operators tracing a stale result can identify which prompt revision produced it.

Legacy on-disk entries (pre-content-aware refactor) lack `input_hash` and never satisfy a lookup; they age out via TTL and `cache.prune()` evicts them on next call.

**Segmenter-specific cache.** The journal segmenter does NOT use the LLM-prompt cache (`cache_ttl_minutes=0` on its calls). The prompt cache's SimHash fuzzy-match fallback is wrong for line-number-output callers — a small content edit shifts line numbers but stays within the Hamming threshold, serving stale partitions. Instead, the segmenter uses a domain-specific content-addressable cache at `work_buddy.journal_backlog.segmentation_cache` that keys on the *content set* of input lines (per-line content hashes, not the prompt text). Robust to line reordering, blank-line edits, and whitespace-only changes; misses on any meaningful content change.

## Broker integration (per-call priority + metrics)

Both LM-Studio-backed backends run inside ``LocalInferenceBroker.slot(...)`` before the HTTP call. See ``architecture/inference/broker`` for the admission-control contract.

- ``work_buddy.llm.backends.lmstudio_native.call_lmstudio_native`` — profile ``lmstudio_native:<model>``.
- ``work_buddy.llm.backends.openai_compat.call_openai_compat`` — profile ``openai_compat:<model>``.

Priority is threaded from the runner: ``LLMRunner.call(priority=...)`` forwards it through ``run_task`` and ``_run_profile`` to the backend's ``broker.slot(...)``. Both backend functions accept a ``priority`` kwarg (defaults to ``WORKFLOW``) and ``queue_wait_s`` (default 30s); the runner forwards ``priority`` and leaves ``queue_wait_s`` at the backend/config default. Callers on user-facing paths pass ``priority=Priority.INTERACTIVE``; batch classifiers / summarizers pass ``Priority.BACKGROUND`` to yield to interactive work. ``priority=None`` (the default) leaves the backend's ``WORKFLOW`` default in place.

The MCP-exposed ``llm_call`` / ``llm_submit`` capabilities take the priority as a string (``"interactive"`` / ``"workflow"`` / ``"background"``), mapped onto the enum by ``work_buddy.inference.parse_priority``. ``llm_submit`` validates it at submit time and carries the canonical name across the queue boundary so the sidecar-replayed ``llm_call`` admits at the requested priority.

The Anthropic backend is NOT broker-wrapped — Anthropic is a cloud service, its own rate-limiting handles the admission layer, and the priority/slot vocabulary doesn't apply (the runner attaches ``priority`` only to the local-profile dispatch).

## Current internal callers

- `work_buddy.triage.capabilities.inline_triage_scan` — FRONTIER_BALANCED → FRONTIER_BEST on timeout/context/empty/rate-limited (via `verdict_call`)
- `work_buddy.triage.capabilities.journal_triage_scan` — same escalation policy (via `verdict_call`)
- `work_buddy.triage.recommend.group_intents` (Chrome intent grouping) — FRONTIER_BALANCED
- `work_buddy.llm.classify`, `work_buddy.llm.summarize` — FRONTIER_FAST (Haiku)
- `work_buddy.triage.adapters.journal._call_segmenter` — LOCAL_FAST → FRONTIER_FAST escalation chain (configurable via `triage.segment.tier_chain`) for running-notes thread segmentation
- `work_buddy.journal_backlog.manifest.build_thread_manifest` — FRONTIER_FAST per-thread tag/summary generation for the backlog pipeline

## Current limitations (tracked in task t-a373609f)

- `LLMRunner._call_one` still delegates to legacy `run_task` for actual HTTP dispatch. Native Anthropic + local backend adapters replace this in the deletion pass.
- `tools=` parameter raises `NotImplementedError`. Tool-call dispatch (Anthropic + LM Studio unified) is on the roadmap.
- No streaming. No prompt-caching knob (Anthropic caching is implicit via the underlying `run_task` cache).

## Guard rail

`tests/unit/test_legacy_llm_api_guard.py` blocks new imports of `llm_with_tools`, `run_task`, and `work_buddy.llm.call.llm_call` from anywhere in `work_buddy/`. Add to `_ALLOWED_EXCEPTIONS` only with a strong rationale.

## Related

- `architecture/inference/broker` — the slot / priority / metrics layer both LLM backends run inside.
- `architecture/llm-with-tools` — documents the legacy tool-call path (now deprecated).
- `architecture/context-pipeline` — the other half of the refactor (context collection/curation that feeds LLMRunner prompts).
