"""SourceActionConsumer — the reaction half of a pull source.

The poller (the *producer*) publishes ``ai.workbuddy.source.<name>.changed`` when
a watched value changes. This durable consumer reacts: resolve the source def,
evaluate its CEL condition (fail-closed), then run its action — gated by
``allowed_actions`` (the reserved policy ``deny`` path) and per-action consent.

Putting the reaction on the drain (rather than inline in the poller) means it
inherits the spine's at-least-once delivery + bounded-retry + DLQ for free; the
poller stays a pure fetch→diff→publish loop.
"""

from __future__ import annotations

from work_buddy.events.conditions.cel import CelCondition
from work_buddy.events.dispatcher import DurableConsumer, register_consumer
from work_buddy.events.policy import policy_check
from work_buddy.events.processors.registry import get_action
from work_buddy.events.protocol import (
    ConditionContext,
    ProcessorManifest,
    ProcessorResult,
)
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)

CONSUMER_ID = "events.source-action"
SOURCE_TYPE_PREFIX = "ai.workbuddy.source."


class SourceActionProcessor:
    """React to a ``source.changed`` event: condition gate → scoped action."""

    manifest = ProcessorManifest(
        name="source-action",
        description="Evaluate a source's condition and run its action",
        consent_action=None,  # the per-action consent is checked below, per-fire
        consent_weight="low",
    )

    def __init__(self) -> None:
        # CEL programs are reusable + the drain is single-threaded; cache by expr.
        self._cel_cache: dict[str, CelCondition] = {}

    def run(self, event, ctx) -> ProcessorResult:
        data = event.data or {}
        name = data.get("source_name")
        if not name:
            return ProcessorResult(text="no source_name on event", is_error=True)

        source = self._resolve(name)
        if source is None:
            logger.warning("source-action: no enabled def for source %r", name)
            return ProcessorResult(text=f"source {name!r} not found")

        # Condition gate (fail-closed): never fire on an inconclusive/bad condition.
        if source.condition and not self._condition_passes(source, event):
            return ProcessorResult(text="condition not met")

        # allowed_actions scope — the reserved policy `deny` path, made concrete.
        if source.action_name not in source.allowed_actions:
            logger.warning(
                "source-action: %s action %r not in allowed_actions %s — denied",
                name,
                source.action_name,
                source.allowed_actions,
            )
            return ProcessorResult(
                text=f"action {source.action_name!r} denied (allowed_actions)"
            )

        action = get_action(source.action_name)
        if action is None:
            return ProcessorResult(
                text=f"unknown action {source.action_name!r}", is_error=True
            )

        # Per-action consent. `notify` declares no gate (consent_action=None →
        # allow); a future high-weight action would prompt here, holding the event.
        decision = policy_check(
            action.consent_action, ctx, consent_weight=action.consent_weight
        )
        if decision != "allow":
            logger.info(
                "source-action: %s action %s gated: %s", name, source.action_name, decision
            )
            return ProcessorResult(text=f"action gated: {decision}")

        # Rate-limit + auto-suspend: a flapping watcher is notification spam.
        now = None
        if source.max_per_hour is not None:
            from datetime import datetime, timezone

            from work_buddy.events.sources.ratelimit import fires_last_hour

            now = datetime.now(timezone.utc)
            if fires_last_hour(source.name, now) >= source.max_per_hour:
                self._auto_suspend(source)
                return ProcessorResult(
                    text=f"rate-limited (>= {source.max_per_hour}/h) — {source.name} auto-suspended"
                )

        # Tier-3 semantic-LLM gate (expensive — runs last, after every cheap gate,
        # so a search + local-model call never happens for a fire a cheaper gate
        # already closed).
        if source.semantic and not self._semantic_passes(source, event):
            return ProcessorResult(text="semantic condition not met")

        result = action.run(event, source, ctx)

        if source.max_per_hour is not None:
            from work_buddy.events.sources.ratelimit import record_fire

            record_fire(source.name, now)
        return result

    def _auto_suspend(self, source) -> None:
        """Disable a flapping source (rewrite its ``.md`` with ``enabled: false``)
        and notify the user once. The cursor is preserved, so a later re-enable
        resumes cleanly."""
        from work_buddy.events.sources.loader import sources_dir, write_event_source

        fm = dict(source.raw)
        fm["enabled"] = False
        write_event_source(sources_dir(), source.name, fm, overwrite=True)
        logger.warning(
            "source-action: %s auto-suspended (rate limit %s/h)",
            source.name,
            source.max_per_hour,
        )
        try:
            from work_buddy.notifications.dispatcher import SurfaceDispatcher
            from work_buddy.notifications.models import Notification, SourceType
            from work_buddy.notifications.store import create_notification, mark_delivered

            notif = create_notification(
                Notification(
                    title=f"Watcher paused: {source.name}",
                    body=(
                        f"{source.name} fired more than {source.max_per_hour} times in "
                        "an hour and was auto-suspended. Re-enable it once the source "
                        "settles."
                    ),
                    source=f"events:source:{source.name}",
                    source_type=SourceType.PROGRAMMATIC.value,
                    tags=["events", "source", source.name, "auto-suspend"],
                )
            )
            SurfaceDispatcher.from_config().deliver(notif, mark_delivered_fn=mark_delivered)
        except Exception:  # pragma: no cover — defensive
            logger.exception("source-action: auto-suspend notify failed for %s", source.name)

    def _resolve(self, name: str):
        from work_buddy.events.sources.loader import load_event_sources

        defs, _ = load_event_sources()
        return next((d for d in defs if d.name == name and d.enabled), None)

    def _condition_passes(self, source, event) -> bool:
        cond = self._cel_cache.get(source.condition)
        if cond is None:
            try:
                cond = CelCondition(source.condition)
            except Exception:  # noqa: BLE001 — invalid CEL → fail-closed
                logger.warning(
                    "source-action: %s has invalid CEL %r — treating as not-met",
                    source.name,
                    source.condition,
                )
                return False
            self._cel_cache[source.condition] = cond
        return cond.evaluate(event, None, ConditionContext())

    def _semantic_passes(self, source, event) -> bool:
        # Fresh per fire: construction is just config parsing, so this always
        # honours the current `semantic` block (no stale-config cache).
        from work_buddy.events.conditions.semantic_llm import SemanticLlmCondition

        try:
            cond = SemanticLlmCondition(source)
        except Exception:  # noqa: BLE001 — malformed semantic block → fail-closed
            logger.warning(
                "source-action: %s has an invalid semantic block — treating as not-met",
                source.name,
            )
            return False
        return cond.evaluate(event, None, ConditionContext())


def register_source_action() -> None:
    """Register the source-action consumer with the dispatcher (sidecar boot)."""
    register_consumer(
        DurableConsumer(
            id=CONSUMER_ID,
            processor=SourceActionProcessor(),
            consent_action=None,
            type_prefix=SOURCE_TYPE_PREFIX,
        )
    )
    logger.info("events: registered source-action consumer (%s*)", SOURCE_TYPE_PREFIX)
