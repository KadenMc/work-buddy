"""Slice 7: pickup-time readiness evaluation.

Pure function that decides whether a task picked up by the engage
flow is **ready to execute** as-is or needs to **develop first** —
the develop-at-pickup-default principle from ROADMAP §3.6.

Inputs are everything cheap to read off the task row + a small
``world_state`` blob the caller assembles:

* ``creation_effort`` — how informed was the agent that wrote this
  task (sparse / medium / developed).  Sparse → almost always not-ready.
* ``user_involvement`` — did the user actively engage at capture
  (low / medium / high).  Low → leans toward develop-first.
* ``creation_provenance`` — what flow produced the task (manual,
  agent_inferred_from_journal, …).  Manual + medium-high involvement
  → trust the user.
* staleness — derived from ``created_at``.  Older sparse tasks
  haven't aged into clarity; if they had, the user would have
  developed them by now.
* ``has_deadline`` / ``has_dependency`` — when these are populated,
  the task carries enough at-capture signal that pickup-time has
  something to anchor against.
* ``has_action_items`` — Slice 7 signal — once the task has been
  decomposed into approved action items, pickup-readiness flips to
  True (the develop step has happened).

The function returns a frozen :class:`PickupReadiness` so the engage
flow can render *why* it's surfacing develop-first.

This is the v0 heuristic; Slice 8's relevance scan + attraction
signal feed in later.  Today it's pure thresholds — keep them
visible (no magic numbers buried in if-statements) so the tuning
discussion is a one-file PR.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping


# ---------------------------------------------------------------------------
# Tunables — exposed as module constants so they're greppable.
# ---------------------------------------------------------------------------

# Days after which a sparse task is "stale" — sparse tasks older than
# this without development have probably aged out of relevance, but
# we don't auto-archive (Slice 8's job).  We just bump the "needs
# develop" signal weight.
SPARSE_STALENESS_DAYS = 7

# Provenances that count as "user actively engaged" — high signal
# of intent.  Manual capture trumps everything; the agent-inferred
# flows are weaker signals.
HIGH_INVOLVEMENT_PROVENANCES = frozenset({"manual"})


@dataclass(frozen=True)
class PickupReadiness:
    """Result of :func:`compute_pickup_readiness`.

    ``ready`` is True when the engage flow can execute this task
    without first invoking the develop-at-pickup decomposition.
    ``reason`` is a short human-readable string for the surface;
    ``signals`` carries the full per-signal breakdown for debugging
    and for the "why develop first?" tooltip.
    """

    ready: bool
    reason: str
    signals: tuple[str, ...] = field(default_factory=tuple)


def compute_pickup_readiness(
    task: Mapping[str, Any] | None,
    world_state: Mapping[str, Any] | None = None,
    *,
    now_iso: str | None = None,
) -> PickupReadiness:
    """Decide whether a task is ready to execute or should develop first.

    Default precedence (highest signal first):

    1. **Action items already exist** → ready.  The decomposition has
       happened (manually or via develop-at-pickup); execute against
       the current step.
    2. **Density='developed' or 'dense'** → ready.  The user (or
       Clarify) explicitly pre-developed; trust it.
    3. **Sparse + manual + high involvement + has_deadline** → ready.
       The user knew enough at capture to add a deadline; the sparse
       text is intentional brevity, not under-specification.
    4. **Sparse + agent-inferred + low involvement + stale** →
       NOT ready (develop first).  Old, weakly-attested, hasn't been
       touched — develop step earns the user's attention.
    5. **Otherwise** → develop first as the safe default per
       develop-at-pickup-default doctrine.

    ``world_state`` is a forward-compat hook for Slice 8 attraction
    + relevance signals; today it accepts ``has_action_items`` (set
    by the caller after a count of approved action items).
    """
    if not isinstance(task, Mapping):
        task = {}
    world_state = dict(world_state or {})

    signals: list[str] = []

    # 1. Action items already approved → execute against them.
    if world_state.get("has_action_items"):
        signals.append("action items already exist")
        return PickupReadiness(
            ready=True,
            reason="action items present — execute against current step",
            signals=tuple(signals),
        )

    # 2. Pre-developed density wins.
    density = task.get("density") or "sparse"
    if density in {"developed", "dense"}:
        signals.append(f"density={density}")
        return PickupReadiness(
            ready=True,
            reason=f"task pre-developed (density={density})",
            signals=tuple(signals),
        )

    # 3. Sparse-but-fresh + user-authored + has_deadline → ready.
    creation_effort = task.get("creation_effort") or "sparse"
    user_involvement = task.get("user_involvement") or "high"
    creation_provenance = task.get("creation_provenance") or "manual"
    has_deadline = bool(task.get("has_deadline"))
    days_old = _days_old(task.get("created_at"), now_iso=now_iso)

    # 3a. Slice-7 Addendum 1: consult the density-promotion heuristic
    # when the task is sparse.  If the heuristic finds parenthetical
    # sublists or dense bullet sections in the note body, the agent
    # has a structural prior that decomposition is worth proposing
    # (NOT ready -- prefer develop-at-pickup).  If the heuristic
    # finds nothing, the user's "sparse" intent is reinforced and we
    # let the rest of the ladder decide.  The density signal is
    # carried in world_state.density_flag (caller-loaded so this
    # function stays pure).
    density_flag = world_state.get("density_flag")
    if (
        density_flag
        and creation_effort == "sparse"
        and density != "developed"
    ):
        signals.append(
            f"density signals fired: {','.join(density_flag.get('signals', []))}"
        )
        return PickupReadiness(
            ready=False,
            reason=(
                "density-promotion heuristic detects structural sub-list "
                "in this sparse task -- decompose first"
            ),
            signals=tuple(signals),
        )

    if (
        creation_effort == "sparse"
        and creation_provenance in HIGH_INVOLVEMENT_PROVENANCES
        and user_involvement in {"medium", "high"}
        and has_deadline
    ):
        signals.append("sparse-but-intentional: manual + has_deadline")
        return PickupReadiness(
            ready=True,
            reason=(
                "sparse capture but manually authored with a deadline — "
                "trust the user's brevity"
            ),
            signals=tuple(signals),
        )

    # 4. Stale + agent-inferred + low involvement → develop first.
    is_agent_inferred = (
        creation_provenance != "manual"
        and not creation_provenance.startswith("user")
    )
    if (
        creation_effort == "sparse"
        and is_agent_inferred
        and user_involvement == "low"
        and days_old is not None
        and days_old >= SPARSE_STALENESS_DAYS
    ):
        signals.append(
            f"agent-inferred + low-involvement + sparse + {days_old}d old"
        )
        return PickupReadiness(
            ready=False,
            reason=(
                "agent-inferred sparse capture aged "
                f"{days_old} days without user touch — develop first"
            ),
            signals=tuple(signals),
        )

    # 5. Default to develop-first per ROADMAP §3.6.
    signals.append(
        f"default develop-first (effort={creation_effort}, "
        f"involvement={user_involvement}, provenance={creation_provenance})"
    )
    return PickupReadiness(
        ready=False,
        reason="develop-at-pickup default — sparse task without strong signal",
        signals=tuple(signals),
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _days_old(created_at: Any, *, now_iso: str | None) -> int | None:
    """Return whole-days-since for an ISO timestamp, or None."""
    if not isinstance(created_at, str) or not created_at.strip():
        return None
    try:
        created = datetime.fromisoformat(created_at)
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
    except ValueError:
        return None

    if now_iso is None:
        now = datetime.now(timezone.utc)
    else:
        try:
            now = datetime.fromisoformat(now_iso)
            if now.tzinfo is None:
                now = now.replace(tzinfo=timezone.utc)
        except ValueError:
            now = datetime.now(timezone.utc)

    delta = now - created
    return int(delta.total_seconds() // 86400)
