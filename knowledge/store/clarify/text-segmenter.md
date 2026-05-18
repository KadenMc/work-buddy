---
name: Text-segmenter SubCall
kind: system
description: Generic text-segmentation SubCall. Splits captured prose into distinct *matters*. Used by inline-capture (right-click 'Send to agent') to detect multi-matter selections. Reusable for any future singular-input pipeline (per-message email triage, etc.).
tags:
- clarify
- segmentation
- subcall
- inline-capture
- singular
- text-splitting
aliases:
- matter detection
- text segmenter
- split into matters
- segment_into_matters
parents:
- architecture/llm-runner
dev_notes: 'Built 2026-05-09 as Stage 2 of the singular-pattern fix. The user''s specific ask: ''segmenter is very valuable and shouldn''t be tied into just one pipeline''. Hence the SubCall framing — any pipeline can call segment_into_matters(text) and consume the resulting per-matter list, dispatching each matter through its own per-matter spawn primitive. The first consumer is inline-capture; future per-message email triage drops in for free.'
---

Generic text-segmentation SubCall built on the decomposed-judgment framework. Splits captured prose into distinct *matters* — coherent subjects the user might think of as one thing each.

## Where it lives

- Module: `work_buddy/clarify/text_segmenter.py`
- SubCall declaration: `TEXT_SEGMENTER_SUBCALL`
- Public entry: `segment_into_matters(text, *, hint='', item_id='', short_text_bypass_chars=120, runner=None)`
- Helper: `_significant_newline_count(text)` — counts newlines NOT immediately followed by a bullet marker (`-`, `*`, `+`, or `1.`-style numbered, with optional leading whitespace).
- Config: `triage.text_segmenter.{tier_chain,max_tokens,temperature,cache_ttl_minutes,max_segments}` in `clarify/config.py:TRIAGE_DEFAULTS`.

## Output shape

A list of dicts, each carrying:
- `start_char`: integer offset (inclusive) into the input text
- `end_char`: integer offset (exclusive)
- `label`: short noun-phrase summarising what the matter is about
- `text`: the input slice for this matter (`text[start:end]`)

ALWAYS returns at least one segment when the input has any non-whitespace content (passthrough on segmenter failure, never fragment the user's input).

## Bypass rules

The LLM call is skipped when ALL of these hold; otherwise the segmenter runs.

- Empty / whitespace-only text → `[]` (caller treats as no work).
- **Short single-block bypass**: when `len(text) < short_text_bypass_chars` AND the text contains ≤1 significant newline, return a single-segment passthrough labeled `(short capture)`. Both clauses matter independently:
  - Char-count alone misses short multi-matter captures: `"Email Bob about the report.\n\nRenew car insurance Friday."` is 56 chars but is two distinct matters that the user expects to land as two separate threads.
  - Newline-count alone over-segments short bullet lists: `"Read research paper\n- Paper A\n- Paper B"` has multiple newlines but is one matter; the bullet-aware count of significant newlines is 0, so it stays bypassed.
- SubCall soft-fail (every tier exhausts) → single-segment passthrough labeled `(unsegmented)`.

## Bias-toward-cohesion

The system prompt biases strongly toward 'one matter' to avoid false-splits. False-splits create user-visible thread fragmentation; false-merges absorb cleanly into the multi-action pattern downstream (the singular umbrella's render hoist). Mirrors `project_picker`'s 'lean toward null when uncertain'.

Multiple ACTIONS on the same matter is NOT a split signal — `Buy gift for Sarah's birthday on May 12` is ONE matter (the birthday) yielding two records (task + calendar event), but those records are about the same subject.

Split signals (require strong evidence): different topics with no semantic bridge, separating conjunctions (`Also,` `Separately,`), paragraph breaks introducing a new subject, distinct imperatives addressing unrelated work.

## Post-parse validation

After the LLM returns a structured-output dict, `_validate_and_normalize_segments`:

1. Drops entries with non-integer offsets, inverted ranges, or out-of-text offsets.
2. Sorts by `start_char` ascending.
3. Drops segments overlapping with an earlier segment.
4. Caps at `max_segments` (default 6).
5. Coverage check: if the union of all segments covers less than 85% of the non-whitespace input characters, distrusts the segmentation entirely and returns `[]` (caller treats as passthrough). The model probably hallucinated boundaries.
6. Attaches `text` slices.

## What this segmenter does NOT subsume

The journal pipeline has its own segmenter (`clarify/adapters/journal.py:_segment_with_escalation`) with line-range output and post-parse semantic-validation-driven escalation. That escalation pattern doesn't fit SubCall today (SubCall escalates on backend errors / schema violations, not on caller-defined post-parse semantic checks). Migrating journal to this generic segmenter is future work after SubCall grows a `validate_post_parse` hook.

## Relationship to the singular pattern

This SubCall is the upstream filter for `pipelines/inline.py:inline_capture`. Multi-matter captures route as N independent root threads (one per detected matter); single-matter captures whose verdict produces 2+ records spawn a singular umbrella with hoisted actions on the umbrella card. See `threads/grouping` for the singular pattern's render-side semantics.
