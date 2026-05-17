"""``MarkdownDB`` — two-way markdown ⇄ SQLite synchronisation.

This is the abstraction extracted from the bespoke reconciler in
``work_buddy/obsidian/tasks/sync.py``. It captures the *shape-agnostic*
half of two-way sync — orphan handling, the per-field drift loop,
conflict resolution, dual-surface mutation — and leaves the
*shape-specific* half (how a markdown file is parsed and rendered) to
subclasses.

## The model

Markdown is **canonical**. The SQLite store is a queryable projection.
Writes that originate in our own code (agent, dashboard) go through
:meth:`MarkdownDB.apply_mutation`, which writes *both* surfaces. Writes
that originate outside our code (a human editing in Obsidian) are caught
later by :meth:`MarkdownDB.reconcile_drift`, the periodic safety net.

## What a subclass provides

- ``FIELDS`` — a list of :class:`~work_buddy.markdown_db.types.FieldSpec`
  declaring each reconcilable field. This single declaration drives the
  generic drift loop; the 8 hand-written loops in the legacy tasks
  reconciler collapse to one loop over this list.
- ``table_name`` / ``pk_column`` — store identity.
- :meth:`parse_all_from_markdown` — read + parse every entity.
- :meth:`write_entity_to_markdown` — persist one entity's markdown.
- :meth:`markdown_path_for` — where an entity's markdown lives.
- The store-adapter hooks (``_store_*``) default to a plain CRUD module
  API and are overridable when a store's signature differs.

## What the base class provides

- :meth:`apply_mutation` — atomic dual-surface write, LWW-stamped.
- :meth:`reconcile_drift` — the generic drift reconciler.
- :meth:`resolve` — conflict resolution (pluggable; LWW by default).
- :meth:`materialize_from_store` — bulk store → markdown (first-run).

See ``architecture/markdown-db`` for the subsystem reference.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from work_buddy.logging_config import get_logger
from work_buddy.markdown_db.lww import LwwLog, NullLwwLog
from work_buddy.markdown_db.resolver import Resolver, make_default_resolver
from work_buddy.markdown_db.storage_helpers import file_lock, mtime_utc
from work_buddy.markdown_db.types import (
    SURFACE_MARKDOWN,
    SURFACE_STORE,
    Candidate,
    FieldSpec,
    ParsedFileRow,
    ReconcileReport,
    Surface,
    WriteProvenance,
)

logger = get_logger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class MarkdownDB(ABC):
    """Abstract two-way markdown ⇄ store synchroniser. Subclass per entity.

    Construct with the entity's store module and, optionally, an
    :class:`LwwLog` and a :data:`Resolver`. The defaults
    (:class:`~work_buddy.markdown_db.lww.NullLwwLog` +
    markdown-wins LWW) make the abstraction behave exactly like the
    legacy markdown-canonical tasks reconciler — adopting it changes no
    behaviour until a real LWW backend is wired in.
    """

    # ── Subclass-declared class attributes ──────────────────────────
    FIELDS: list[FieldSpec] = []
    table_name: str = ""
    pk_column: str = ""
    markdown_surface: Surface = SURFACE_MARKDOWN
    store_surface: Surface = SURFACE_STORE

    # When True (default), reconcile_drift soft-deletes a store row whose
    # entity is absent from markdown ("orphan in store" — markdown is
    # canonical, so gone-from-markdown means gone). A subclass MUST set
    # this False until its markdown surface is fully materialized:
    # otherwise the very first reconcile pass — when no markdown notes
    # exist yet — would soft-delete every store row. See ProjectMarkdownDB.
    delete_orphans_in_store: bool = True

    def __init__(
        self,
        store: Any,
        *,
        lww: LwwLog | None = None,
        resolver: Resolver | None = None,
    ) -> None:
        if not self.FIELDS:
            raise TypeError(
                f"{type(self).__name__} must declare a non-empty FIELDS list"
            )
        if not self.table_name or not self.pk_column:
            raise TypeError(
                f"{type(self).__name__} must set table_name and pk_column"
            )
        self.store = store
        self.lww: LwwLog = lww if lww is not None else NullLwwLog()
        self._resolver: Resolver = (
            resolver
            if resolver is not None
            else make_default_resolver(self.markdown_surface)
        )
        # field name → spec, and file_key / store_col indexes
        self._by_name: dict[str, FieldSpec] = {f.name: f for f in self.FIELDS}

    # ════════════════════════════════════════════════════════════════
    # Abstract surface — subclasses MUST implement these.
    # ════════════════════════════════════════════════════════════════

    @abstractmethod
    def parse_all_from_markdown(self) -> dict[str, ParsedFileRow]:
        """Read and parse every entity from the markdown surface.

        Returns ``{pk: ParsedFileRow}``. Each row's ``fields`` dict is
        keyed by :attr:`FieldSpec.file_key`. For a line-oriented layout
        (the task master list) this parses one file; for a
        file-per-entity layout (project notes) it globs and parses many.
        """

    @abstractmethod
    def write_entity_to_markdown(self, pk: str, fields: dict[str, Any]) -> None:
        """Persist one entity's ``fields`` to its markdown representation.

        ``fields`` is keyed by :attr:`FieldSpec.name` (logical names).
        Implementations rewrite a single line (tasks) or a whole note
        file (projects). Use
        :func:`~work_buddy.markdown_db.storage_helpers.atomic_write_text`
        for the actual write. Callers hold the file lock already.
        """

    @abstractmethod
    def markdown_path_for(self, pk: str) -> Path:
        """Filesystem path whose lock guards writes for ``pk``.

        For a single-master-file layout, return that one file regardless
        of ``pk``. For file-per-entity, return the entity's own file.
        """

    # ════════════════════════════════════════════════════════════════
    # Overridable hooks — sensible defaults, override when a store or
    # layout needs something different.
    # ════════════════════════════════════════════════════════════════

    def _store_query(self) -> list[dict[str, Any]]:
        """Return every live store row as a dict. Override per store API."""
        return list(self.store.query())

    def _store_create(self, pk: str, fields: dict[str, Any]) -> None:
        """Create a store row. ``fields`` keyed by store column. Override
        per store API (positional PK arg, ``upsert_*`` name, …)."""
        self.store.create(pk, **fields)

    def _store_update(self, pk: str, fields: dict[str, Any]) -> None:
        """Update a store row. ``fields`` keyed by store column."""
        self.store.update(pk, **fields)

    def _store_delete(self, pk: str) -> None:
        """Soft-delete a store row."""
        self.store.delete(pk)

    def build_create_kwargs(self, parsed: ParsedFileRow) -> dict[str, Any]:
        """Map a :class:`ParsedFileRow` to store-create kwargs.

        Default: run each declared field's parsed value through
        ``parse_value`` into its ``store_col`` (plus any
        ``extra_store_fields``). Subclasses override to inject required
        defaults the markdown doesn't carry (e.g. a task's ``urgency``
        when the line has no priority emoji).
        """
        out: dict[str, Any] = {}
        for spec in self.FIELDS:
            if spec.file_key not in parsed.fields:
                continue
            value = spec.parse_value(parsed.fields[spec.file_key])
            out[spec.store_col] = value
            if spec.extra_store_fields is not None:
                out.update(spec.extra_store_fields(value))
        return out

    def markdown_exists(self, pk: str) -> bool:
        """Whether ``pk`` already has a markdown representation.

        Default checks :meth:`markdown_path_for` existence — correct for
        file-per-entity layouts. Single-master-file subclasses should
        override (the master file always exists; existence is
        per-line)."""
        return self.markdown_path_for(pk).exists()

    def _markdown_mtime(self, pk: str) -> datetime | None:
        """Best-effort timestamp for when ``pk``'s markdown was last
        touched — used as the LWW ts proxy for out-of-band edits.

        Default: mtime of :meth:`markdown_path_for`. For a single master
        file this is coarse (shared across all entities) but still a
        valid lower bound. File-per-entity layouts get a per-entity
        mtime for free.
        """
        return mtime_utc(self.markdown_path_for(pk))

    def _infer_drift_actor(self, pk: str) -> frozenset[str]:
        """Best-effort actor attribution for a drift-detected change.

        Default is the empty set — the honest answer, since drift
        detects an out-of-band edit whose author we did not observe.
        Subclasses may override to narrow the candidates when signals
        are available (e.g. "the agent has been idle for an hour, so
        this was almost certainly the user").
        """
        return frozenset()

    # ════════════════════════════════════════════════════════════════
    # Conflict resolution.
    # ════════════════════════════════════════════════════════════════

    def resolve(self, field: FieldSpec, candidates: list[Candidate]) -> Candidate:
        """Pick the winning value for a conflicted field.

        Delegates to the injected :data:`Resolver` (LWW-markdown-wins by
        default). Exposed as a method so subclasses can override per
        field if they ever need to.
        """
        return self._resolver(field, candidates)

    # ════════════════════════════════════════════════════════════════
    # Mutation — the in-code write path (agent / dashboard).
    # ════════════════════════════════════════════════════════════════

    def apply_mutation(
        self,
        pk: str,
        fields: dict[str, Any],
        *,
        provenance: WriteProvenance,
    ) -> None:
        """Atomically write ``fields`` to BOTH the markdown and the store.

        ``fields`` is keyed by :attr:`FieldSpec.name`. Markdown is
        written first (it is canonical — markdown-ahead is the safe
        failure direction); the store update follows. Each written field
        is stamped in the :class:`LwwLog` once per surface.

        Creates the entity if it does not exist yet.
        """
        unknown = set(fields) - set(self._by_name)
        if unknown:
            raise ValueError(
                f"apply_mutation: unknown field(s) {sorted(unknown)}; "
                f"declared: {sorted(self._by_name)}"
            )

        ts = _utcnow()
        with file_lock(self.markdown_path_for(pk)):
            # 1. Merge the change over current state so the markdown
            #    write has the complete entity to render.
            current = self._current_logical_fields(pk)
            merged = {**current, **fields}

            # 2. Markdown first (canonical).
            self.write_entity_to_markdown(pk, merged)

            # 3. Store second.
            store_fields = {
                self._by_name[name].store_col: value
                for name, value in fields.items()
            }
            if current:
                self._store_update(pk, store_fields)
            else:
                self._store_create(pk, store_fields)

            # 4. Stamp the LWW log — one event per field per surface.
            for name in fields:
                for surface in (self.markdown_surface, self.store_surface):
                    self.lww.record(
                        table=self.table_name,
                        pk=pk,
                        field=name,
                        ts=ts,
                        provenance=provenance,
                        to_surface=surface,
                    )

        logger.info(
            "markdown_db[%s]: apply_mutation pk=%s fields=%s process=%s",
            self.table_name, pk, sorted(fields), provenance.process,
        )

    # ════════════════════════════════════════════════════════════════
    # Drift reconciliation — the periodic safety net.
    # ════════════════════════════════════════════════════════════════

    def reconcile_drift(self) -> ReconcileReport:
        """Reconcile the markdown surface against the store.

        The generic form of the 8-loop pattern in the legacy tasks
        reconciler:

        1. Parse every entity from markdown; query every store row.
        2. **Orphan in markdown** (parsed, not in store) → create the
           store row.
        3. **Orphan in store** (store row, not parsed) → soft-delete it
           (markdown is canonical: gone from markdown means gone).
        4. **Field drift** — for every declared field of every entity in
           both, compare the markdown value to the store value; on a
           mismatch :meth:`resolve` picks the winner. Markdown winning
           updates the store; the store winning writes the value back
           into the markdown (keeping both surfaces consistent).

        Returns a :class:`ReconcileReport`.
        """
        report = ReconcileReport()
        try:
            parsed = self.parse_all_from_markdown()
        except Exception as exc:  # parsing the whole surface failed
            logger.exception("markdown_db[%s]: parse failed", self.table_name)
            report.errors.append(f"parse_all_from_markdown: {exc}")
            return report

        store_rows = {
            row[self.pk_column]: row for row in self._store_query()
        }
        parsed_ids = set(parsed)
        store_ids = set(store_rows)

        # 2. Orphan in markdown → create store row.
        for pk in sorted(parsed_ids - store_ids):
            try:
                kwargs = self.build_create_kwargs(parsed[pk])
                self._store_create(pk, kwargs)
                report.created.append(pk)
                self._stamp_drift_fields(pk, parsed[pk].fields.keys())
                logger.info(
                    "markdown_db[%s]: created store row for orphan-in-"
                    "markdown pk=%s", self.table_name, pk,
                )
            except Exception as exc:
                logger.warning(
                    "markdown_db[%s]: failed to create %s: %s",
                    self.table_name, pk, exc,
                )
                report.errors.append(f"create {pk}: {exc}")

        # 3. Orphan in store → soft-delete (only when the subclass has
        #    opted in via delete_orphans_in_store; otherwise skipped, so
        #    an un-materialized markdown surface cannot wipe the store).
        orphans_in_store = sorted(store_ids - parsed_ids)
        if orphans_in_store and not self.delete_orphans_in_store:
            logger.info(
                "markdown_db[%s]: %d orphan-in-store row(s) left intact "
                "(delete_orphans_in_store is False): %s",
                self.table_name, len(orphans_in_store), orphans_in_store,
            )
        elif self.delete_orphans_in_store:
            for pk in orphans_in_store:
                try:
                    self._store_delete(pk)
                    report.deleted.append(pk)
                    logger.info(
                        "markdown_db[%s]: soft-deleted orphan-in-store pk=%s",
                        self.table_name, pk,
                    )
                except Exception as exc:
                    logger.warning(
                        "markdown_db[%s]: failed to delete %s: %s",
                        self.table_name, pk, exc,
                    )
                    report.errors.append(f"delete {pk}: {exc}")

        # 4. Field drift over the intersection.
        for spec in self.FIELDS:
            report.drift[spec.name] = []
        for pk in sorted(parsed_ids & store_ids):
            self._reconcile_one_entity(
                pk, parsed[pk], store_rows[pk], report,
            )

        return report

    def _reconcile_one_entity(
        self,
        pk: str,
        parsed: ParsedFileRow,
        store_row: dict[str, Any],
        report: ReconcileReport,
    ) -> None:
        """Reconcile every declared field of one entity present on both
        surfaces. Markdown write-backs are batched into one call."""
        markdown_writeback: dict[str, Any] = {}
        ts = _utcnow()

        for spec in self.FIELDS:
            # Convert the raw parsed value into the store representation
            # FIRST — the whole loop operates in store shape after this.
            file_val = spec.parse_value(parsed.fields.get(spec.file_key))
            store_val = store_row.get(spec.store_col)

            # Falsy-file-value discipline: an empty markdown value never
            # clears a populated store value unless the field opts in.
            if not spec.propagate_on_falsy and not file_val:
                continue
            in_sync = spec.equivalent or self._values_equal
            if in_sync(file_val, store_val):
                continue

            winner = self.resolve(spec, [
                Candidate(
                    value=file_val,
                    source=self.markdown_surface,
                    ts=self._lww_ts(pk, spec.name, self.markdown_surface)
                    or self._markdown_mtime(pk),
                    provenance=self._lww_prov(pk, spec.name, self.markdown_surface),
                ),
                Candidate(
                    value=store_val,
                    source=self.store_surface,
                    ts=self._lww_ts(pk, spec.name, self.store_surface),
                    provenance=self._lww_prov(pk, spec.name, self.store_surface),
                ),
            ])

            if winner.source == self.markdown_surface:
                # Markdown wins → push the markdown value into the store.
                update_fields: dict[str, Any] = {spec.store_col: file_val}
                if spec.extra_store_fields is not None:
                    update_fields.update(spec.extra_store_fields(file_val))
                try:
                    self._store_update(pk, update_fields)
                except Exception as exc:
                    logger.warning(
                        "markdown_db[%s]: drift update %s.%s failed: %s",
                        self.table_name, pk, spec.name, exc,
                    )
                    report.errors.append(f"drift {pk}.{spec.name}: {exc}")
                    continue
                self._record_drift(pk, spec.name, ts, self.store_surface)
                # Also record the markdown-side observation so future
                # passes have a timestamp to compare against.
                md_ts = self._markdown_mtime(pk) or ts
                self.lww.record(
                    table=self.table_name, pk=pk, field=spec.name,
                    ts=md_ts, provenance=WriteProvenance.drift(),
                    to_surface=self.markdown_surface,
                )
                report.drift[spec.name].append(
                    {"pk": pk, "old": store_val, "new": file_val,
                     "winner": "markdown"}
                )
            else:
                # Store wins → write the store value back into markdown.
                markdown_writeback[spec.name] = store_val
                self._record_drift(pk, spec.name, ts, self.markdown_surface)
                report.drift[spec.name].append(
                    {"pk": pk, "old": file_val, "new": store_val,
                     "winner": "store"}
                )

        if markdown_writeback:
            try:
                merged = self._current_logical_fields(pk) or {}
                merged.update(markdown_writeback)
                with file_lock(self.markdown_path_for(pk)):
                    self.write_entity_to_markdown(pk, merged)
            except Exception as exc:
                logger.warning(
                    "markdown_db[%s]: drift writeback to markdown for %s "
                    "failed: %s", self.table_name, pk, exc,
                )
                report.errors.append(f"drift writeback {pk}: {exc}")

    # ════════════════════════════════════════════════════════════════
    # Materialization — bulk store → markdown (first-run, Phase 4).
    # ════════════════════════════════════════════════════════════════

    def materialize_from_store(self, *, dry_run: bool = True) -> dict[str, Any]:
        """Write a markdown representation for every store row lacking one.

        The one-time direction flip for adopting markdown-canonical on an
        entity whose data currently lives only in the store. Never
        overwrites an existing markdown file — only fills gaps.

        ``dry_run`` (the default) reports what *would* be written without
        touching the filesystem. Pass ``dry_run=False`` to apply.
        """
        planned: list[str] = []
        skipped: list[str] = []
        written: list[str] = []
        errors: list[str] = []

        for row in self._store_query():
            pk = row[self.pk_column]
            if self.markdown_exists(pk):
                skipped.append(pk)
                continue
            planned.append(pk)
            if dry_run:
                continue
            try:
                logical = self._store_row_to_logical(row)
                with file_lock(self.markdown_path_for(pk)):
                    self.write_entity_to_markdown(pk, logical)
                written.append(pk)
                for spec in self.FIELDS:
                    self.lww.record(
                        table=self.table_name, pk=pk, field=spec.name,
                        ts=_utcnow(), provenance=WriteProvenance.materialize(),
                        to_surface=self.markdown_surface,
                    )
            except Exception as exc:
                logger.warning(
                    "markdown_db[%s]: materialize %s failed: %s",
                    self.table_name, pk, exc,
                )
                errors.append(f"{pk}: {exc}")

        return {
            "dry_run": dry_run,
            "planned": planned,
            "written": written,
            "skipped": skipped,
            "errors": errors,
        }

    # ════════════════════════════════════════════════════════════════
    # Internal helpers.
    # ════════════════════════════════════════════════════════════════

    def _current_logical_fields(self, pk: str) -> dict[str, Any]:
        """Current store row for ``pk`` mapped to logical field names.

        Empty dict when the entity has no store row yet.
        """
        for row in self._store_query():
            if row[self.pk_column] == pk:
                return self._store_row_to_logical(row)
        return {}

    def _store_row_to_logical(self, row: dict[str, Any]) -> dict[str, Any]:
        """Map a store row (store-column keyed) to logical field names."""
        return {
            spec.name: row.get(spec.store_col)
            for spec in self.FIELDS
        }

    @staticmethod
    def _values_equal(a: Any, b: Any) -> bool:
        """Compare two field values, treating ``None`` and ``""`` alike.

        Markdown round-trips can turn an absent value into ``""`` while
        the store holds ``None``; that is not a real drift.
        """
        if a in (None, "") and b in (None, ""):
            return True
        return a == b

    def _stamp_drift_fields(self, pk: str, file_keys: Any) -> None:
        """Record drift-provenance LWW events for a freshly-created row."""
        ts = _utcnow()
        present = {
            spec.name for spec in self.FIELDS if spec.file_key in set(file_keys)
        }
        for name in present:
            for surface in (self.markdown_surface, self.store_surface):
                self.lww.record(
                    table=self.table_name, pk=pk, field=name, ts=ts,
                    provenance=WriteProvenance.drift(), to_surface=surface,
                )

    def _record_drift(
        self, pk: str, field: str, ts: datetime, to_surface: Surface,
    ) -> None:
        """Record one drift-provenance LWW event."""
        self.lww.record(
            table=self.table_name, pk=pk, field=field, ts=ts,
            provenance=WriteProvenance.drift(actor=self._infer_drift_actor(pk)),
            to_surface=to_surface,
        )

    def _lww_ts(self, pk: str, field: str, surface: Surface) -> datetime | None:
        entry = self.lww.latest(
            table=self.table_name, pk=pk, field=field, surface=surface,
        )
        return entry.ts if entry else None

    def _lww_prov(
        self, pk: str, field: str, surface: Surface,
    ) -> WriteProvenance | None:
        entry = self.lww.latest(
            table=self.table_name, pk=pk, field=field, surface=surface,
        )
        return entry.provenance if entry else None
