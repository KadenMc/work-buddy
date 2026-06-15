"""Core value types for the MarkdownDB abstraction.

These are the small, dependency-free dataclasses and type aliases shared
across the package. Kept in their own module so ``base.py`` (the ABC),
``resolver.py``, and ``lww.py`` can all import them without cycles.

See ``architecture/markdown-db`` for the subsystem reference and the
design rationale behind each axis of :class:`WriteProvenance`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

# ── Open vocabularies ───────────────────────────────────────────────
#
# ``actor``, ``process``, and ``surface`` are deliberately plain ``str``
# rather than ``Literal[...]``. They are *vocabularies that grow*: a new
# edit surface (a CLI, a Telegram bot), a new code path, or a new actor
# class must never require a schema migration or a type-stub edit. The
# values listed below are conventions — documented, not enforced.
#
#   actor   — 'user' | 'agent' | 'system'        (who originated the write)
#   process — 'mutation' | 'drift'               (which code path wrote it)
#             | 'materialize' | 'migration'
#   surface — 'markdown' | 'store' | 'dashboard' (where a value lives /
#             | 'external'                        came from / landed)
Actor = str
Process = str
Surface = str

# Conventional surface constants — import these instead of bare strings
# so a typo is a NameError rather than a silent mismatch.
SURFACE_MARKDOWN: Surface = "markdown"
SURFACE_STORE: Surface = "store"
SURFACE_DASHBOARD: Surface = "dashboard"
SURFACE_EXTERNAL: Surface = "external"

# Conventional process constants.
PROCESS_MUTATION: Process = "mutation"
PROCESS_DRIFT: Process = "drift"
PROCESS_MATERIALIZE: Process = "materialize"
PROCESS_MIGRATION: Process = "migration"

# Conventional actor constants.
ACTOR_USER: Actor = "user"
ACTOR_AGENT: Actor = "agent"
ACTOR_SYSTEM: Actor = "system"


@dataclass(frozen=True)
class WriteProvenance:
    """Why and how a write happened — the metadata stamped per write event.

    Three orthogonal axes describing a *logical* write. The fourth axis
    of the design discussion — ``to_surface`` — is intentionally NOT a
    field here: a single logical write (e.g. a dashboard mutation) lands
    on multiple surfaces and so produces multiple ``lww_meta`` rows that
    share this provenance but differ in which surface they record. That
    makes ``to_surface`` a property of the *row*, not of the provenance;
    it is passed alongside this object to :meth:`LwwLog.record`.

    Axes:

    - ``actor`` — whose intent the write encodes, as a *set of
      candidates with OR semantics*. This honestly encodes partial
      observability: a drift-detected change cannot be attributed to one
      actor with certainty.

        * ``frozenset({"user"})``          — observed exactly
        * ``frozenset({"user", "agent"})`` — narrowed to two candidates
        * ``frozenset()``                  — fully unknown; no narrowing

      The field name stays singular (``actor``) because the set encodes
      uncertainty about *which one*, not a claim that several actors
      collaborated.

    - ``process`` — which code path performed the write (``mutation``,
      ``drift``, ``materialize``, ``migration``, …). Describes *us*.

    - ``from_surface`` — where the written value originated (``markdown``,
      ``dashboard``, ``store``, …), or ``None`` when not meaningful
      (e.g. a migration backfill has no originating surface).
    """

    actor: frozenset[Actor]
    process: Process
    from_surface: Surface | None = None

    # ── Convenience constructors for the common cases ───────────────

    @classmethod
    def drift(cls, actor: frozenset[Actor] | None = None) -> "WriteProvenance":
        """Provenance for a drift-reconciler write.

        ``actor`` defaults to the empty set — the honest default, since
        drift detects an out-of-band edit whose author we did not
        observe. A subclass that can narrow the candidates (e.g. via
        agent-activity telemetry) passes a non-empty set.
        """
        return cls(
            actor=actor if actor is not None else frozenset(),
            process=PROCESS_DRIFT,
            from_surface=SURFACE_MARKDOWN,
        )

    @classmethod
    def mutation(
        cls, actor: frozenset[Actor], from_surface: Surface,
    ) -> "WriteProvenance":
        """Provenance for an ``apply_mutation`` write (agent / dashboard)."""
        return cls(actor=actor, process=PROCESS_MUTATION, from_surface=from_surface)

    @classmethod
    def materialize(cls) -> "WriteProvenance":
        """Provenance for a first-run materialization write (store → markdown)."""
        return cls(
            actor=frozenset({ACTOR_SYSTEM}),
            process=PROCESS_MATERIALIZE,
            from_surface=SURFACE_STORE,
        )

    @classmethod
    def migration(cls) -> "WriteProvenance":
        """Provenance for a schema-migration / backfill write."""
        return cls(
            actor=frozenset({ACTOR_SYSTEM}),
            process=PROCESS_MIGRATION,
            from_surface=None,
        )


@dataclass(frozen=True)
class FieldSpec:
    """Declares one reconcilable field for a :class:`MarkdownDB` subclass.

    The generic drift loop reads the field from the parsed file via
    ``file_key``, runs it through ``parse_value`` to get a *store-shaped*
    value, compares that against ``store_col`` on the store row, and on a
    mismatch resolves a winner.

    Fields:

    - ``parse_value`` — converts the raw parsed-file value into the store
      representation. Identity by default. The drift loop operates
      entirely in store representation *after* this conversion, so
      ``file_key`` may carry a different shape than ``store_col`` (the
      task checkbox is a ``bool`` in the file but a ``state`` string in
      the store).

    - ``serialize_value`` — the inverse, for rendering a store value back
      into a markdown fragment. Used on the store-wins write-back path.

    - ``propagate_on_falsy`` — when ``False`` (default) an empty / ``None``
      file value never overwrites a populated store value (a line can
      lose an emoji without the user meaning to clear the field). Set
      ``True`` only where "absent in file" genuinely means "cleared"
      (e.g. a checkbox, always present).

    - ``equivalent`` — optional custom "are these in sync?" predicate,
      ``(file_store_value, store_value) -> bool``. Needed when the file
      representation is a *lossy projection* of the store column: the
      task checkbox encodes only done-vs-not-done, so an unchecked box
      is consistent with store states ``inbox``/``mit``/``focused``/
      ``snoozed`` alike. Defaults to ``None`` → ``MarkdownDB._values_equal``.

    - ``extra_store_fields`` — optional ``(new_value) -> dict`` producing
      additional store columns to write in lockstep with this field.
      The task ``deadline_date`` field uses it to keep the boolean
      ``has_deadline`` column consistent.
    """

    name: str
    file_key: str
    store_col: str
    parse_value: Callable[[Any], Any] = lambda v: v
    serialize_value: Callable[[Any], str] = lambda v: "" if v is None else str(v)
    propagate_on_falsy: bool = False
    equivalent: Callable[[Any, Any], bool] | None = None
    extra_store_fields: Callable[[Any], dict[str, Any]] | None = None


@dataclass
class ParsedFileRow:
    """One entity as parsed out of the markdown surface.

    ``fields`` is keyed by ``FieldSpec.file_key``. ``line_number`` is
    populated for line-oriented layouts (the task master list) and left
    ``None`` for file-per-entity layouts (project notes).
    """

    pk: str
    fields: dict[str, Any]
    line_number: int | None = None


@dataclass
class Candidate:
    """A competing value for one field, handed to :meth:`MarkdownDB.resolve`.

    ``ts`` is when the value was written (``None`` when no LWW history
    exists yet — e.g. a legacy row before the ``lww_meta`` backfill).
    ``source`` is the surface the reconciler *read* this value from; it
    is computed at reconcile time and is not itself stored.
    """

    value: Any
    source: Surface
    ts: datetime | None = None
    provenance: WriteProvenance | None = None


@dataclass
class ReconcileReport:
    """Outcome of one :meth:`MarkdownDB.reconcile_drift` pass."""

    created: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    # field name → list of {pk, old, new}
    drift: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    # Non-fatal conditions that need human attention but did NOT change
    # state — e.g. a store row whose markdown file is present but failed
    # to parse (left intact, not deleted).
    warnings: list[str] = field(default_factory=list)
    # Set by the mass-delete circuit-breaker when reconcile_drift refused an
    # implausibly large orphan-delete batch (degraded read, not a real bulk
    # deletion): (would_delete_count, live_store_count). None = breaker did
    # not trip.
    aborted_bulk_delete: tuple[int, int] | None = None

    @property
    def changed(self) -> bool:
        """True if the pass altered any state. Warnings do not count —
        a warning means something was *left alone* pending attention."""
        return bool(
            self.created
            or self.deleted
            or any(self.drift.values())
        )

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly summary for capability output / logging."""
        return {
            "created": list(self.created),
            "deleted": list(self.deleted),
            "drift": {k: list(v) for k, v in self.drift.items()},
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "aborted_bulk_delete": self.aborted_bulk_delete,
            "changed": self.changed,
        }
