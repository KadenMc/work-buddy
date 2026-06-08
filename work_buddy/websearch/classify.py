"""Broker-admitted LOCAL_FAST relevance classify over evidence cards.

Given a question and a set of :class:`EvidenceCard`s, return a structured
:class:`ClassifyResult` verdict. The call runs at ``ModelTier.LOCAL_FAST``
(cheap local structured output) and at ``Priority.BACKGROUND`` so a poll-watcher
classify yields to interactive inference on the same LM Studio profile.

**Admission:** the local backend acquires the broker slot itself —
``LLMRunner.call(priority=...)`` threads the priority down through ``run_task``
to ``call_openai_compat``'s ``broker.slot(profile="openai_compat:<model>")``
(pinned by ``tests/unit/test_llm_backends_broker_wiring.py``). So this classify
passes ``priority=Priority.BACKGROUND`` and must NOT wrap its own
``get_broker().slot(...)``: a second slot on a separate profile would be
redundant and would not actually yield to interactive work on the contended
profile. Admission/timeout is governed by the broker profile under
``inference.profiles.<key>``, not a websearch-local config block.

The caller does any cheap prefilter (content-hash / threshold / CEL) *before*
this — the classify is the expensive tier. On any error the verdict defaults to
``relevant=False`` (a watcher must not fire on an inconclusive judgment).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from work_buddy.websearch.models import ClassifyResult, EvidenceCard

log = logging.getLogger(__name__)

_VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "relevant": {"type": "boolean"},
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
        "evidence_urls": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["relevant", "confidence", "reason"],
    "additionalProperties": False,
}

_SYS = (
    "You are a precise relevance classifier. Given a QUESTION and a list of "
    "retrieved EVIDENCE cards (title, source, url, snippet), decide whether the "
    "evidence, taken together, is relevant and material to the question. Judge "
    "ONLY the evidence shown — do not use outside knowledge. Return strict JSON: "
    "`relevant` (bool), `confidence` (0..1), `reason` (one sentence citing the "
    "evidence), `evidence_urls` (the urls that support a positive verdict). When "
    "the evidence is thin, off-topic, or contradictory, return relevant=false."
)


def _render(question: str, cards: Sequence[EvidenceCard]) -> str:
    lines = [f"QUESTION: {question}", "", "EVIDENCE:"]
    for i, c in enumerate(cards, 1):
        published = f" ({c.published})" if c.published else ""
        lines.append(f"[{i}] {c.title} — {c.source}{published}\n    {c.url}\n    {c.snippet}")
    return "\n".join(lines)


def classify_evidence(question: str, cards: Sequence[EvidenceCard]) -> ClassifyResult:
    """Return a structured relevance verdict for ``question`` over ``cards``.

    No-evidence and error cases both return ``relevant=False`` (never raises)."""
    if not cards:
        return ClassifyResult(relevant=False, confidence=0.0,
                              reason="no evidence provided", evidence_urls=[])

    from work_buddy.inference import Priority
    from work_buddy.llm.runner_v2 import LLMRunner
    from work_buddy.llm.tiers import ModelTier

    try:
        resp = LLMRunner().call(
            tier=ModelTier.LOCAL_FAST,
            system=_SYS,
            user=_render(question, cards),
            output_schema=_VERDICT_SCHEMA,
            max_tokens=512,
            priority=Priority.BACKGROUND,
            trace_id="websearch:classify",
        )
    except Exception as exc:  # noqa: BLE001 — admission/backend errors must not propagate
        log.warning("websearch classify call raised: %s", exc)
        return ClassifyResult(relevant=False, confidence=0.0,
                              reason=f"classify call failed: {exc}", evidence_urls=[])

    if resp.is_error():
        log.warning("websearch classify error: %s", resp.error)
        return ClassifyResult(relevant=False, confidence=0.0,
                              reason=f"classify error: {resp.error}", evidence_urls=[])

    out = resp.structured_output or {}
    try:
        return ClassifyResult(
            relevant=bool(out.get("relevant", False)),
            confidence=float(out.get("confidence", 0.0) or 0.0),
            reason=str(out.get("reason", "")),
            evidence_urls=[str(u) for u in (out.get("evidence_urls") or [])],
        )
    except (TypeError, ValueError) as exc:
        log.warning("websearch classify: malformed structured_output %r (%s)", out, exc)
        return ClassifyResult(relevant=False, confidence=0.0,
                              reason="malformed classifier output", evidence_urls=[])
