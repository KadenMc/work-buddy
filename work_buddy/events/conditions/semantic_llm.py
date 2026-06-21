"""Tier-3 semantic-LLM condition — gate firing on a local-model relevance verdict.

The most expensive condition tier, so it runs **last** in the cascade (only after
the cheap CEL gate passes) and is guarded three ways:

- a **post-fire cooldown** — within the window of a firing, suppress entirely (no
  network, no LLM) so an ongoing story doesn't re-notify;
- a **results content-hash prefilter** — if the searched evidence is identical to
  the last evaluation, skip the expensive classify call and reuse the verdict;
- an optional **N-of-M debounce** — fire only on N of the last M positive verdicts.

Every path is **fail-closed**: any error (websearch disabled, network, parse)
yields ``False`` — a watcher never fires on an inconclusive verdict (mirrors
`classify_evidence`, which itself never raises).

State lives in its own ``<state>/<name>.semantic.json`` — written only by the
reaction consumer, never the poller (the same separate-writer reasoning as the
rate-limit fire-log).

NB: the prefilter hashes the **search results** (what `classify` actually sees),
not the polled value — the CEL tier already gated on the polled value changing, so
a poll-value hash would never match between evaluations.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from work_buddy.events.envelope import Event
from work_buddy.events.protocol import ConditionContext
from work_buddy.events.sources.definition import parse_debounce, parse_interval
from work_buddy.events.sources.extract import content_hash
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _do_search(query: str, max_results: int):
    """Indirection seam (monkeypatched in tests) → the websearch router."""
    from work_buddy.websearch import search

    return search(query, max_results=max_results)


def _do_classify(question: str, hits, *, watch_label: str):
    """Indirection seam (monkeypatched in tests) → to_evidence_cards + classify.

    `classify_evidence` already broker-admits at BACKGROUND priority on a local
    model — do **not** wrap it in another broker slot.
    """
    from work_buddy.websearch import classify_evidence, to_evidence_cards

    cards = to_evidence_cards(hits, watch_label=watch_label)
    return classify_evidence(question, cards)


def _results_hash(hits) -> str:
    """Stable hash of a hit set's identity (membership, order-independent)."""
    urls = sorted(str(getattr(h, "url", h)) for h in (hits or []))
    return content_hash(urls)


# --- state I/O (its own file; never the cursor or the fire-log) ---------------


def _semantic_path(name: str, directory: Path | None = None) -> Path:
    from work_buddy.events.sources.state import state_dir

    d = Path(directory) if directory is not None else state_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{name}.semantic.json"


def load_semantic_state(name: str, directory: Path | None = None) -> dict[str, Any]:
    p = _semantic_path(name, directory)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def save_semantic_state(name: str, state: dict[str, Any], directory: Path | None = None) -> None:
    try:
        _semantic_path(name, directory).write_text(
            json.dumps(state, default=str), encoding="utf-8"
        )
    except OSError:  # pragma: no cover — defensive
        logger.warning("semantic: could not persist state for %s", name)


class SemanticLlmCondition:
    """A guarded local-LLM relevance gate bound to one source's ``semantic`` block."""

    def __init__(self, source, *, state_directory: Path | None = None) -> None:
        cfg = source.semantic or {}
        self.name = source.name
        self.question = str(cfg.get("question") or "").strip()
        self.query = str(cfg.get("query") or "").strip() or self.question
        self.cooldown_s = parse_interval(cfg.get("cooldown")) if cfg.get("cooldown") else None
        self.debounce = parse_debounce(cfg.get("debounce")) or (1, 1)
        self.min_confidence = float(cfg.get("min_confidence") or 0.0)
        self.max_results = int(cfg.get("max_results") or 8)
        self._dir = state_directory

    def evaluate(self, event: Event, prev: Event | None, ctx: ConditionContext) -> bool:
        if not self.question:
            return False  # malformed — fail-closed (validation should have caught it)

        state = load_semantic_state(self.name, self._dir)
        now = _now()

        # 1. post-fire cooldown — suppress entirely (no network, no LLM).
        if self.cooldown_s and self._in_cooldown(state.get("last_fire_at"), now):
            return False

        # 2. search (the cheap gate). Fail-closed; transient → don't persist.
        try:
            hits = _do_search(self.query, self.max_results)
        except Exception as exc:  # noqa: BLE001
            logger.info("semantic %s: search failed (%s) — not-met", self.name, exc)
            return False

        # 3. results content-hash prefilter — skip the classify LLM when the
        #    evidence is unchanged since the last evaluation; reuse the verdict.
        rhash = _results_hash(hits)
        if rhash == state.get("last_results_hash") and "last_verdict" in state:
            reused = bool(state.get("last_verdict"))
            upd = {**state, "last_eval_at": now.isoformat()}
            if reused:
                upd["last_fire_at"] = now.isoformat()
            save_semantic_state(self.name, upd, self._dir)
            return reused

        # 4. classify (the expensive step). Fail-closed.
        try:
            verdict = _do_classify(self.question, hits, watch_label=self.name)
            pos = bool(verdict.relevant) and float(
                getattr(verdict, "confidence", 0.0) or 0.0
            ) >= self.min_confidence
        except Exception as exc:  # noqa: BLE001
            logger.info("semantic %s: classify failed (%s) — not-met", self.name, exc)
            return False

        # 5. N-of-M debounce.
        n, m = self.debounce
        votes = (list(state.get("votes") or []) + [pos])[-m:]
        fire = sum(1 for v in votes if v) >= n

        # 6. persist + return.
        save_semantic_state(
            self.name,
            {
                "last_eval_at": now.isoformat(),
                "last_results_hash": rhash,
                "last_verdict": fire,
                "votes": votes,
                "last_fire_at": now.isoformat() if fire else state.get("last_fire_at"),
            },
            self._dir,
        )
        return fire

    def _in_cooldown(self, last_fire_at, now: datetime) -> bool:
        if not last_fire_at:
            return False
        try:
            t = datetime.fromisoformat(str(last_fire_at))
        except (ValueError, TypeError):
            return False
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return (now - t).total_seconds() < (self.cooldown_s or 0)
