"""Conflict resolution for :class:`MarkdownDB`.

When the drift reconciler finds a field whose markdown value disagrees
with its store value, it asks a :data:`Resolver` to pick the winner.
Isolating that decision behind a single callable is the cheap
CRDT-smoothing move from the design discussion: swapping last-write-wins
for a richer policy (a manual-conflict UI, a text CRDT) is a one-symbol
change, not a rewrite of the reconcile loop.

The default :func:`lww_markdown_wins`:

1. If both candidates carry timestamps, the newer one wins.
2. On a tie — or when one/both timestamps are missing — the candidate
   read from the markdown surface wins.

Rule (2) makes the resolver degrade gracefully to pure
markdown-canonical behaviour when no LWW history exists yet (a
:class:`~work_buddy.markdown_db.lww.NullLwwLog`, or a legacy row not yet
backfilled). That is exactly the behaviour of the legacy
``obsidian/tasks/sync.py`` reconciler, so adopting the abstraction does
not change task-sync semantics until the LWW backend is wired in.
"""

from __future__ import annotations

from typing import Callable

from work_buddy.markdown_db.types import Candidate, FieldSpec, Surface

# A resolver takes the field being reconciled and the competing
# candidates, and returns the winning candidate. It must return one of
# the candidates it was given (not a fresh object) so the caller can
# rely on identity / ``source`` checks.
Resolver = Callable[[FieldSpec, list[Candidate]], Candidate]


def lww_markdown_wins(
    field: FieldSpec,
    candidates: list[Candidate],
    *,
    markdown_surface: Surface = "markdown",
) -> Candidate:
    """Last-write-wins resolver biased toward the markdown surface.

    See module docstring for the rule. ``candidates`` must be non-empty.

    The bias is deliberate and load-bearing: work-buddy's markdown is
    the *canonical* store, so when timestamps cannot break the tie the
    markdown value is the one to trust.
    """
    if not candidates:
        raise ValueError("lww_markdown_wins: candidates must be non-empty")
    if len(candidates) == 1:
        return candidates[0]

    timed = [c for c in candidates if c.ts is not None]
    if len(timed) == len(candidates):
        # Every candidate has a timestamp — compare them. On an exact
        # tie, fall through to the markdown-wins rule below.
        newest_ts = max(c.ts for c in timed)  # type: ignore[type-var]
        leaders = [c for c in timed if c.ts == newest_ts]
        if len(leaders) == 1:
            return leaders[0]
        candidates = leaders  # tie — resolve among the leaders

    # No timestamps, partial timestamps, or an exact tie: markdown wins.
    for c in candidates:
        if c.source == markdown_surface:
            return c
    # No markdown candidate present (store-only field?) — first wins.
    return candidates[0]


def make_default_resolver(markdown_surface: Surface = "markdown") -> Resolver:
    """Build the default resolver bound to a given markdown surface name."""

    def _resolver(field: FieldSpec, candidates: list[Candidate]) -> Candidate:
        return lww_markdown_wins(
            field, candidates, markdown_surface=markdown_surface,
        )

    return _resolver
