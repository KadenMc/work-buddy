"""Background triage infrastructure — producer and pending-review pool.

The ``BackgroundTriageProducer`` orchestrates a single hourly-style
triage pass:

  1. An **adapter** yields :class:`TriageItem` objects from some source
     (journal, Chrome snapshot, etc.).
  2. :func:`work_buddy.triage.enrich.enrich_with_ir_context` attaches
     hybrid-IR supporting context to each item.
  3. A local-LLM **agent loop** (``llm_with_tools``) reasons about each
     item with read-only tools + one designated submission capability
     (``triage_submit``). The agent's verdict is captured through that
     capability — if it never calls ``triage_submit``, the run is
     discarded as ``unsubmitted``.
  4. Submitted verdicts accumulate in the :class:`TriagePool` — a
     persistent, artifact-backed store that the on-demand review
     entrypoint reads later.

This layer is **source-agnostic**. Journal is the first consumer but
Chrome's existing generate path could migrate here in the future.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import unicodedata
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from work_buddy.logging_config import get_logger
from work_buddy.paths import data_dir
from work_buddy.triage.items import TRIAGE_ACTIONS, TriageItem

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Pool entry lifecycle states (Slice 1)
# ---------------------------------------------------------------------------

# Lifecycle states a :class:`PoolEntry` may be in.
#
# - ``pending``     — created by the producer, awaiting human review.
# - ``stale``       — past its ``expires_at`` per the source descriptor's TTL.
#                     Soft signal; not destructive. Hidden from the active
#                     Review tab but still inspectable.
# - ``quarantined`` — the source no longer resolves (file deleted, paragraph
#                     edited beyond the match threshold, capture-tag removed,
#                     etc. — per the source-descriptor's quarantine triggers).
#                     Hidden from the Review tab; preserved on disk.
# - ``reviewed``    — human reviewed it (terminal). Equivalent of
#                     ``reviewed_at`` being non-null.
# - ``dropped``     — explicitly removed (rare; reserved for future use).
STATE_PENDING = "pending"
STATE_STALE = "stale"
STATE_QUARANTINED = "quarantined"
STATE_REVIEWED = "reviewed"
STATE_DROPPED = "dropped"

POOL_ENTRY_STATES: frozenset[str] = frozenset({
    STATE_PENDING, STATE_STALE, STATE_QUARANTINED,
    STATE_REVIEWED, STATE_DROPPED,
})


# ---------------------------------------------------------------------------
# Pool storage
# ---------------------------------------------------------------------------

# Pool state lives at a stable, known path so the on-demand review
# entrypoint can read it without guessing artifact IDs. A timestamped
# snapshot is ALSO saved as an artifact on every mutation for audit /
# TTL purposes.
_POOL_DIR_NAME = "triage_pool"
_POOL_INDEX_FILENAME = "pool.json"

_pool_lock = threading.Lock()


@dataclass
class PoolEntry:
    """A single pending-review verdict in the pool.

    ``state`` is the canonical lifecycle marker (Slice 1). For legacy
    entries that predate the field, :meth:`from_dict` infers it from
    ``reviewed_at`` so they keep behaving the way callers expect.

    ``expires_at`` is set at create time from the source descriptor's
    TTL (see :mod:`work_buddy.triage.sources`). ``None`` means no
    auto-expiry — the daily sweep skips TTL transitions for the entry.

    ``quarantine_reason`` records WHY a quarantine fired
    (``source_removed``, ``source_edited_beyond_match``, ``tag_removed``,
    …) so the user can inspect after the fact.
    """

    run_id: str
    adapter: str
    source: str
    item_id: str
    item: dict[str, Any]               # full TriageItem.to_dict()
    verdict: dict[str, Any]            # submission payload
    created_at: str                    # ISO8601
    reviewed_at: str | None = None
    review_outcome: str | None = None  # e.g. "approved", "rejected", "deferred"
    # Deterministic hash of (source, normalized item text) used for
    # per-item dedup across runs. Producer consults the pool's
    # pending set of hashes and skips items already queued, so
    # repeated cron cycles over unchanged content don't stack
    # duplicate cards. Optional for backwards compat with entries
    # created before this field existed.
    item_content_hash: str | None = None
    # Slice 1 lifecycle additions ------------------------------------
    # State enum from POOL_ENTRY_STATES. Default ``pending``; legacy
    # entries get the right value via ``from_dict``'s inference.
    state: str = STATE_PENDING
    # ISO8601. ``None`` for sources with no TTL (e.g. inline).
    expires_at: str | None = None
    # Set when state transitions to ``quarantined``. One of the
    # quarantine_trigger names from the source descriptor.
    quarantine_reason: str | None = None
    # ISO8601. Stamped whenever ``state`` changes. Useful for audit
    # and for the sweep's "skip if checked recently" optimization.
    state_changed_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PoolEntry:
        # Drop unknown fields so older code reading newer pool files
        # doesn't crash. Forward compat: newer fields just get
        # ignored.
        known = {f for f in cls.__dataclass_fields__}
        kwargs = {k: v for k, v in d.items() if k in known}
        # Backwards compat: if ``state`` is missing, infer it from
        # ``reviewed_at``. Legacy entries (pre-Slice-1) had only the
        # reviewed-or-not signal; preserve their meaning.
        if "state" not in kwargs:
            kwargs["state"] = (
                STATE_REVIEWED if kwargs.get("reviewed_at") else STATE_PENDING
            )
        return cls(**kwargs)


class TriagePool:
    """Pending-review pool, persisted as a stable JSON index + artifact snapshots.

    The pool is a single source of truth for "verdicts that haven't
    been reviewed yet." It supports:

    - adding a verdict for an active run (:meth:`submit`)
    - listing pending entries, optionally filtered
    - marking entries reviewed after the user has acted

    The index file lives at ``<data_root>/triage_pool/pool.json`` — a
    known path. Every mutation also writes a timestamped snapshot
    through the normal artifact store, so the TTL-sweep still caps
    storage growth.
    """

    def __init__(self, pool_dir: Path | None = None) -> None:
        self._pool_dir = pool_dir or data_dir(_POOL_DIR_NAME)
        self._pool_dir.mkdir(parents=True, exist_ok=True)
        self._index_path = self._pool_dir / _POOL_INDEX_FILENAME

    # -- Run registration ---------------------------------------------------

    def register_run(
        self,
        *,
        run_id: str,
        adapter: str,
        source: str,
        items: list[TriageItem],
    ) -> None:
        """Register an active run so ``submit`` calls can be validated.

        The run metadata is persisted next to the pool (``runs.json``)
        so ``triage_submit`` calls made from a separate process (i.e.
        from inside ``llm_with_tools`` → LM Studio → MCP gateway) can
        look up the legal item_ids. Stale runs age out with the
        artifact cleanup.
        """
        with _pool_lock:
            runs = self._load_runs()
            runs[run_id] = {
                "run_id": run_id,
                "adapter": adapter,
                "source": source,
                "created_at": _now_iso(),
                "item_ids": [item.id for item in items],
                "items": {item.id: item.to_dict() for item in items},
                "status": "open",
            }
            self._save_runs(runs)

    def close_run(self, run_id: str, *, status: str = "done") -> None:
        """Mark a run closed so late ``triage_submit`` calls are rejected."""
        with _pool_lock:
            runs = self._load_runs()
            if run_id in runs:
                runs[run_id]["status"] = status
                runs[run_id]["closed_at"] = _now_iso()
                self._save_runs(runs)

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        runs = self._load_runs()
        return runs.get(run_id)

    # -- Verdict submission -------------------------------------------------

    def submit(
        self,
        *,
        run_id: str,
        item_id: str,
        verdict: dict[str, Any],
    ) -> dict[str, Any]:
        """Record a verdict for a live run's item.

        Returns a status dict suitable for returning to the caller
        (including the LLM, when this is exposed as a tool). Never
        raises for ordinary rejection paths — an agent that supplies
        bad args must see a structured error, not a tool-call crash.
        """
        run = self.get_run(run_id)
        if run is None:
            return {
                "status": "error",
                "error": f"Unknown run_id: {run_id!r}",
                "hint": "Only active triage runs accept submissions.",
            }
        if run.get("status") != "open":
            return {
                "status": "error",
                "error": f"Run {run_id!r} is {run.get('status')!r}, not open.",
            }
        if item_id not in run.get("item_ids", []):
            return {
                "status": "error",
                "error": (
                    f"Item {item_id!r} does not belong to run {run_id!r}."
                ),
                "valid_item_ids": run.get("item_ids", []),
            }

        action = verdict.get("recommended_action", "")
        if action not in TRIAGE_ACTIONS:
            return {
                "status": "error",
                "error": (
                    f"recommended_action={action!r} is not one of "
                    f"{list(TRIAGE_ACTIONS)}."
                ),
            }

        with _pool_lock:
            index = self._load_index()
            # Reject duplicate submission for the same (run_id, item_id)
            for entry in index.get("entries", []):
                if entry["run_id"] == run_id and entry["item_id"] == item_id:
                    return {
                        "status": "error",
                        "error": (
                            f"Duplicate submission for item {item_id!r} "
                            f"in run {run_id!r}."
                        ),
                    }

            item_dict = run["items"].get(item_id, {})
            now = _now_iso()
            entry_source = run.get("source", "")
            pe = PoolEntry(
                run_id=run_id,
                adapter=run.get("adapter", ""),
                source=entry_source,
                item_id=item_id,
                item=item_dict,
                verdict=_shape_verdict(verdict),
                created_at=now,
                item_content_hash=item_content_hash(
                    item_dict.get("source", entry_source),
                    item_dict.get("text", ""),
                ),
                state=STATE_PENDING,
                expires_at=_compute_expires_at(entry_source, now),
                state_changed_at=now,
            )
            index.setdefault("entries", []).append(pe.to_dict())
            self._save_index(index)

        return {
            "status": "ok",
            "run_id": run_id,
            "item_id": item_id,
            "recommended_action": action,
        }

    def submit_raw(
        self,
        *,
        run_id: str,
        item_id: str,
    ) -> dict[str, Any]:
        """Write a raw (un-verdicted) entry to the pool (Slice 1).

        Used when the unattended LLM verdict pass is gated off — the
        producer still segments and registers items but skips the
        agent invocation, calling this method to write a placeholder
        entry. Slice 3 brings GTD-shaped verdicts back; entries with
        ``verdict == {"raw": True}`` are the ones Slice 3's migration
        targets.

        Validation mirrors :meth:`submit` (unknown run / wrong item /
        duplicate are rejected with structured errors).
        """
        run = self.get_run(run_id)
        if run is None:
            return {
                "status": "error",
                "error": f"Unknown run_id: {run_id!r}",
                "hint": "Only active triage runs accept submissions.",
            }
        if run.get("status") != "open":
            return {
                "status": "error",
                "error": f"Run {run_id!r} is {run.get('status')!r}, not open.",
            }
        if item_id not in run.get("item_ids", []):
            return {
                "status": "error",
                "error": (
                    f"Item {item_id!r} does not belong to run {run_id!r}."
                ),
                "valid_item_ids": run.get("item_ids", []),
            }

        with _pool_lock:
            index = self._load_index()
            for entry in index.get("entries", []):
                if entry["run_id"] == run_id and entry["item_id"] == item_id:
                    return {
                        "status": "error",
                        "error": (
                            f"Duplicate submission for item {item_id!r} "
                            f"in run {run_id!r}."
                        ),
                    }

            item_dict = run["items"].get(item_id, {})
            now = _now_iso()
            entry_source = run.get("source", "")
            pe = PoolEntry(
                run_id=run_id,
                adapter=run.get("adapter", ""),
                source=entry_source,
                item_id=item_id,
                item=item_dict,
                verdict={"raw": True},
                created_at=now,
                item_content_hash=item_content_hash(
                    item_dict.get("source", entry_source),
                    item_dict.get("text", ""),
                ),
                state=STATE_PENDING,
                expires_at=_compute_expires_at(entry_source, now),
                state_changed_at=now,
            )
            index.setdefault("entries", []).append(pe.to_dict())
            self._save_index(index)

        return {
            "status": "ok",
            "run_id": run_id,
            "item_id": item_id,
            "raw": True,
        }

    # -- Read / review ------------------------------------------------------

    def pending_content_hashes(
        self,
        *,
        source: str | None = None,
        adapter: str | None = None,
    ) -> set[str]:
        """Return the set of item_content_hash values for pending entries.

        Used by the producer to dedup items across runs: if an item's
        content hash is already in this set, don't create another
        pool entry for it. Missing or None hashes are skipped silently
        (older entries predating the field can't be deduped).
        """
        index = self._load_index()
        hashes: set[str] = set()
        for raw in index.get("entries", []):
            if not _is_active_pending(raw):
                continue
            if source and raw.get("source") != source:
                continue
            if adapter and raw.get("adapter") != adapter:
                continue
            h = raw.get("item_content_hash")
            if not h:
                # Legacy entry without a stored hash — recompute on
                # read from the preserved item text so the entry
                # still participates in dedup. No file migration
                # needed; new entries persist the hash going forward.
                item_dict = raw.get("item") or {}
                text = item_dict.get("text", "")
                src = item_dict.get("source", raw.get("source", ""))
                h = item_content_hash(src, text)
            hashes.add(h)
        return hashes

    def pending(
        self,
        *,
        source: str | None = None,
        adapter: str | None = None,
        since: str | None = None,
        max_items: int | None = None,
    ) -> list[PoolEntry]:
        """Return entries currently active for human review.

        After Slice 1 this means ``state == "pending"``. Legacy
        entries that predate the ``state`` field are treated as
        pending iff they have no ``reviewed_at`` (handled by
        :func:`_is_active_pending`).
        """
        index = self._load_index()
        out: list[PoolEntry] = []
        for raw in index.get("entries", []):
            if not _is_active_pending(raw):
                continue
            if source and raw.get("source") != source:
                continue
            if adapter and raw.get("adapter") != adapter:
                continue
            if since and raw.get("created_at", "") < since:
                continue
            out.append(PoolEntry.from_dict(raw))
            if max_items and len(out) >= max_items:
                break
        return out

    def pending_count(
        self,
        *,
        source: str | None = None,
        adapter: str | None = None,
    ) -> int:
        """Cheap count of currently-pending entries (no PoolEntry build).

        Used by the daily sweep for stats and (in future) by anyone
        wanting to gate work on pool size without paying for the
        full :meth:`pending` materialization.
        """
        index = self._load_index()
        n = 0
        for raw in index.get("entries", []):
            if not _is_active_pending(raw):
                continue
            if source and raw.get("source") != source:
                continue
            if adapter and raw.get("adapter") != adapter:
                continue
            n += 1
        return n

    def entries_in_state(
        self,
        state: str,
        *,
        source: str | None = None,
        adapter: str | None = None,
    ) -> list[PoolEntry]:
        """Explicit-state filter (Slice 1).

        Distinct from :meth:`pending`, which collapses the legacy
        no-``state`` case into ``pending``. This method only returns
        entries whose ``state`` field exactly matches ``state``.
        """
        if state not in POOL_ENTRY_STATES:
            raise ValueError(
                f"Unknown state {state!r}; valid: {sorted(POOL_ENTRY_STATES)}"
            )
        index = self._load_index()
        out: list[PoolEntry] = []
        for raw in index.get("entries", []):
            if raw.get("state") != state:
                continue
            if source and raw.get("source") != source:
                continue
            if adapter and raw.get("adapter") != adapter:
                continue
            out.append(PoolEntry.from_dict(raw))
        return out

    def all_entries(self) -> list[PoolEntry]:
        """Every entry on disk, regardless of state. For audit/migration."""
        index = self._load_index()
        return [PoolEntry.from_dict(raw) for raw in index.get("entries", [])]

    def mark_reviewed(
        self,
        entry_keys: list[tuple[str, str]],
        *,
        outcome: str,
    ) -> int:
        """Stamp ``reviewed_at`` + ``state=reviewed`` on entries.

        Returns the number of entries stamped.
        """
        stamped = 0
        now = _now_iso()
        key_set = set(entry_keys)
        with _pool_lock:
            index = self._load_index()
            for raw in index.get("entries", []):
                key = (raw.get("run_id"), raw.get("item_id"))
                if key in key_set and not raw.get("reviewed_at"):
                    raw["reviewed_at"] = now
                    raw["review_outcome"] = outcome
                    raw["state"] = STATE_REVIEWED
                    raw["state_changed_at"] = now
                    stamped += 1
            if stamped:
                self._save_index(index)
        return stamped

    def mark_state(
        self,
        entry_keys: list[tuple[str, str]],
        *,
        state: str,
        reason: str | None = None,
    ) -> int:
        """Generic state-change for the daily sweep (Slice 1).

        Stamps ``state``, ``state_changed_at``, and (when applicable)
        ``quarantine_reason``. Does NOT touch ``reviewed_at`` —
        ``reviewed`` transitions go through :meth:`mark_reviewed`.

        ``state`` must be one of :data:`POOL_ENTRY_STATES`. Entries
        already in the target state are no-ops (won't double-stamp).
        """
        if state not in POOL_ENTRY_STATES:
            raise ValueError(
                f"Unknown state {state!r}; valid: {sorted(POOL_ENTRY_STATES)}"
            )
        if state == STATE_REVIEWED:
            raise ValueError(
                "Use mark_reviewed() for the reviewed transition; it stamps "
                "reviewed_at + review_outcome too."
            )
        stamped = 0
        now = _now_iso()
        key_set = set(entry_keys)
        with _pool_lock:
            index = self._load_index()
            for raw in index.get("entries", []):
                key = (raw.get("run_id"), raw.get("item_id"))
                if key not in key_set:
                    continue
                if raw.get("state") == state:
                    continue
                raw["state"] = state
                raw["state_changed_at"] = now
                if state == STATE_QUARANTINED and reason:
                    raw["quarantine_reason"] = reason
                stamped += 1
            if stamped:
                self._save_index(index)
        return stamped

    def quarantine(
        self,
        entry_keys: list[tuple[str, str]],
        *,
        reason: str,
    ) -> int:
        """Convenience wrapper around :meth:`mark_state` for quarantine."""
        return self.mark_state(
            entry_keys, state=STATE_QUARANTINED, reason=reason,
        )

    def mark_stale(self, entry_keys: list[tuple[str, str]]) -> int:
        """Convenience wrapper around :meth:`mark_state` for TTL expiry."""
        return self.mark_state(entry_keys, state=STATE_STALE)

    def apply_reviewer(
        self,
        reviewer: Callable[[list[PoolEntry]], list[dict[str, Any]]],
    ) -> dict[str, Any]:
        """Pluggable seam for an optional pre-human review pass.

        The reviewer callable (e.g. a local-Claude sweep) receives
        the list of pending :class:`PoolEntry` objects and returns
        a list of decision dicts of the form::

            {"run_id": str, "item_id": str,
             "decision": "agree"|"disagree"|"needs_clarification",
             "note": str}

        Decisions are merged into the corresponding pool entries
        under ``verdict["reviewer"]``. This does NOT stamp
        ``reviewed_at`` — that's reserved for the human. This is the
        v2 seam mentioned in the plan; v1 callers can ignore it.
        """
        pending = self.pending()
        if not pending:
            return {"reviewed": 0}
        try:
            decisions = reviewer(pending)
        except Exception as exc:
            logger.warning("Reviewer callback failed: %s", exc)
            return {"reviewed": 0, "error": str(exc)}
        dec_map: dict[tuple[str, str], dict[str, Any]] = {
            (d["run_id"], d["item_id"]): d for d in decisions
        }
        with _pool_lock:
            index = self._load_index()
            n = 0
            for raw in index.get("entries", []):
                key = (raw.get("run_id"), raw.get("item_id"))
                if key in dec_map:
                    raw["verdict"].setdefault("reviewer", dec_map[key])
                    n += 1
            self._save_index(index)
        return {"reviewed": n}

    # -- Persistence internals ---------------------------------------------

    def _load_index(self) -> dict[str, Any]:
        if not self._index_path.exists():
            return {"entries": []}
        try:
            return json.loads(self._index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Triage pool index unreadable (%s); starting fresh", exc)
            return {"entries": []}

    def _save_index(self, index: dict[str, Any]) -> None:
        tmp = self._index_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(index, indent=2), encoding="utf-8")
        tmp.replace(self._index_path)
        # Mirror to the artifact store for audit / TTL sweep
        try:
            from work_buddy import artifacts as _artifacts
            _artifacts.save(
                json.dumps(index, indent=2),
                type="report",
                slug="triage-pool",
                ext="json",
                tags=["triage", "pool", "snapshot"],
                description="Pending-review triage pool snapshot",
            )
        except Exception as exc:  # non-fatal
            logger.debug("Pool snapshot save failed: %s", exc)

    def _runs_path(self) -> Path:
        return self._pool_dir / "runs.json"

    def _load_runs(self) -> dict[str, Any]:
        p = self._runs_path()
        if not p.exists():
            return {}
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_runs(self, runs: dict[str, Any]) -> None:
        p = self._runs_path()
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(runs, indent=2), encoding="utf-8")
        tmp.replace(p)


# ---------------------------------------------------------------------------
# Module-level singleton — resolves paths lazily so tests can override.
# ---------------------------------------------------------------------------

_default_pool: TriagePool | None = None


def get_pool() -> TriagePool:
    global _default_pool
    if _default_pool is None:
        _default_pool = TriagePool()
    return _default_pool


def set_pool_for_tests(pool: TriagePool | None) -> None:
    """Test hook: override the module-level pool singleton."""
    global _default_pool
    _default_pool = pool


# ---------------------------------------------------------------------------
# Producer
# ---------------------------------------------------------------------------


@dataclass
class ProducerResult:
    """Summary returned by :meth:`BackgroundTriageProducer.run`."""

    status: str                              # "ok" | "skipped" | "error"
    run_id: str | None = None
    adapter: str = ""
    source: str = ""
    item_count: int = 0
    submitted: int = 0
    unsubmitted: list[str] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    content_hash: str | None = None
    reason: str | None = None                # populated when skipped

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class BackgroundTriageProducer:
    """Orchestrates a single background-triage pass.

    Adapters supply candidate items; the producer handles idempotence,
    enrichment, agent invocation, and pool bookkeeping. Adapter-facing
    contract:

    - ``adapter_name``: stable string identifier used for idempotence
      keys and pool filtering (e.g. ``"journal_triage"``).
    - ``collect()``: callable returning ``(items, content_hash)``
      where ``items`` is a ``list[TriageItem]`` and ``content_hash``
      is a short stable hash of the input the adapter considered.
      Return ``([], None)`` to signal "nothing to do, don't even write
      a run marker."

    The agent callable is pluggable so tests can stub it without
    hitting LM Studio.
    """

    def __init__(
        self,
        *,
        adapter_name: str,
        source: str,
        collect: Callable[[], tuple[list[TriageItem], str | None]],
        agent: Callable[[TriageItem, str], dict[str, Any]],
        pool: TriagePool | None = None,
        enrich: bool = True,
        ir_top_k: int = 5,
        verdict_pass_enabled: bool = True,
    ) -> None:
        self.adapter_name = adapter_name
        self.source = source
        self._collect = collect
        self._agent = agent
        self._pool = pool or get_pool()
        self._enrich = enrich
        self._ir_top_k = ir_top_k
        # Slice 1: when False, the producer skips the agent invocation
        # and writes raw entries (verdict={"raw": True}) directly into
        # the pool. The capability sets this from the triage config's
        # ``verdict_pass.enabled`` flag (default off).
        self._verdict_pass_enabled = verdict_pass_enabled

    # ---- Idempotence --------------------------------------------------

    def _last_hash_path(self) -> Path:
        return self._pool._pool_dir / f"last_hash_{self.adapter_name}.txt"

    def _load_last_hash(self) -> str | None:
        p = self._last_hash_path()
        if not p.exists():
            return None
        try:
            return p.read_text(encoding="utf-8").strip() or None
        except OSError:
            return None

    def _save_last_hash(self, h: str) -> None:
        p = self._last_hash_path()
        tmp = p.with_suffix(".tmp")
        tmp.write_text(h, encoding="utf-8")
        tmp.replace(p)

    # ---- Run ----------------------------------------------------------

    def run(self, *, force: bool = False) -> ProducerResult:
        """Execute one producer pass. See :class:`ProducerResult`."""
        try:
            items, content_hash = self._collect()
        except Exception as exc:
            logger.exception("%s: collect() failed", self.adapter_name)
            return ProducerResult(
                status="error",
                adapter=self.adapter_name,
                source=self.source,
                reason=f"collect failed: {type(exc).__name__}: {exc}",
            )

        if not items:
            return ProducerResult(
                status="skipped",
                adapter=self.adapter_name,
                source=self.source,
                content_hash=content_hash,
                reason="no_items",
            )

        if not force and content_hash and self._load_last_hash() == content_hash:
            return ProducerResult(
                status="skipped",
                adapter=self.adapter_name,
                source=self.source,
                content_hash=content_hash,
                item_count=len(items),
                reason="unchanged",
            )

        # Per-item dedup: if an item's content hash is already in
        # the pending pool for this adapter+source, skip it. Prevents
        # repeated cron cycles over the same journal content from
        # stacking duplicate cards (the per-run content_hash gate
        # above handles the "nothing changed at all" case; this one
        # handles "one new bullet was added, but the other 5 are
        # already queued").
        #
        # ``force=True`` bypasses this filter — the semantic meaning
        # of force is "ignore all dedup gates, process everything."
        original_count = len(items)
        if not force:
            pending_hashes = self._pool.pending_content_hashes(
                source=self.source, adapter=self.adapter_name,
            )
            filtered: list[TriageItem] = []
            for item in items:
                h = item_content_hash(item.source or self.source, item.text or "")
                if h in pending_hashes:
                    continue
                filtered.append(item)
            if len(filtered) < original_count:
                logger.info(
                    "%s: deduped %d/%d items already pending in pool",
                    self.adapter_name,
                    original_count - len(filtered), original_count,
                )
            items = filtered

            if not items:
                # Content hash changed from last run (new bullet
                # somewhere) but every candidate is already in the
                # pool. Nothing to do.
                if content_hash:
                    self._save_last_hash(content_hash)
                return ProducerResult(
                    status="skipped",
                    adapter=self.adapter_name,
                    source=self.source,
                    content_hash=content_hash,
                    item_count=original_count,
                    reason="all_items_already_pending",
                )

        # IR context enrichment is opt-in but default-on. Keep it
        # before run registration so we don't register a run that
        # then blows up on enrichment.
        if self._enrich:
            try:
                from work_buddy.triage.enrich import enrich_with_ir_context
                enrich_with_ir_context(items, top_k=self._ir_top_k)
            except Exception as exc:
                logger.warning(
                    "%s: IR enrichment failed globally: %s",
                    self.adapter_name, exc,
                )

        run_id = f"bgt_{uuid.uuid4().hex[:10]}"
        self._pool.register_run(
            run_id=run_id,
            adapter=self.adapter_name,
            source=self.source,
            items=items,
        )

        submitted = 0
        unsubmitted: list[str] = []
        errors: list[dict[str, Any]] = []

        for item in items:
            # Slice 1 verdict-pass gate: when off, skip the agent
            # entirely and write a raw entry. No LLM tokens spent;
            # the capture still lands in the pool so the user sees
            # it in the Review tab. Slice 3 fills these in with the
            # new GTD-shaped verdict schema.
            if not self._verdict_pass_enabled:
                raw_result = self._pool.submit_raw(
                    run_id=run_id, item_id=item.id,
                )
                if raw_result.get("status") == "ok":
                    submitted += 1
                else:
                    unsubmitted.append(item.id)
                    errors.append({
                        "item_id": item.id,
                        "error": raw_result.get("error", "raw_submit_failed"),
                        "error_kind": "raw_submit_rejected",
                    })
                    logger.warning(
                        "%s: item %s — raw submit rejected: %s",
                        self.adapter_name, item.id, raw_result.get("error"),
                    )
                continue

            try:
                result = self._agent(item, run_id)
            except Exception as exc:
                logger.warning(
                    "%s: agent raised for item %s: %s",
                    self.adapter_name, item.id, exc,
                )
                errors.append({"item_id": item.id, "error": str(exc)})
                unsubmitted.append(item.id)
                continue

            # Did the agent actually submit via triage_submit?
            # The registered capability returns {"status":"ok", ...}
            # on success. We detect submission by checking the pool,
            # not by trusting the agent's own report.
            #
            # The agent loop runs in-process; by the time it returns
            # the pool has been written. See triage_submit.
            if _item_submitted(self._pool, run_id, item.id):
                submitted += 1
            else:
                unsubmitted.append(item.id)
                # Surface why the agent didn't submit. The LLM result dict
                # carries a structured ``error`` + ``error_kind`` (e.g.
                # ``timeout``, ``context_exceeded``, ``mcp_gateway_timeout``);
                # a bare ``content_len=0`` log is misleading because the
                # caller can't tell "model returned empty" apart from
                # "transport failed".
                err = (result or {}).get("error") or ""
                err_kind = (result or {}).get("error_kind") or ""
                content_len = len((result or {}).get("content", "") or "")
                if err:
                    logger.warning(
                        "%s: item %s — agent did not call triage_submit "
                        "(error_kind=%s): %s",
                        self.adapter_name, item.id, err_kind or "unknown", err,
                    )
                    errors.append({
                        "item_id": item.id,
                        "error": err,
                        "error_kind": err_kind,
                    })
                else:
                    logger.info(
                        "%s: item %s — agent did not call triage_submit "
                        "(content_len=%d)",
                        self.adapter_name, item.id, content_len,
                    )

        self._pool.close_run(run_id)
        if content_hash:
            self._save_last_hash(content_hash)

        return ProducerResult(
            status="ok",
            run_id=run_id,
            adapter=self.adapter_name,
            source=self.source,
            item_count=len(items),
            submitted=submitted,
            unsubmitted=unsubmitted,
            errors=errors,
            content_hash=content_hash,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_active_pending(raw: dict[str, Any]) -> bool:
    """Decide whether a raw index entry counts as 'active for review'.

    Slice 1 made ``state`` the canonical lifecycle marker. Legacy
    entries (pre-Slice-1) may not have a ``state`` field; for those
    we fall back to the old ``reviewed_at`` signal.

    The rule:
      - if ``state`` is present → active iff ``state == "pending"``
      - else (legacy) → active iff ``reviewed_at`` is None
    """
    state = raw.get("state")
    if state is not None:
        return state == STATE_PENDING
    return not raw.get("reviewed_at")


def _compute_expires_at(source: str, created_at_iso: str) -> str | None:
    """Look up the source descriptor and compute ``expires_at``.

    Returns ``None`` when:
      - the source has no registered descriptor (unknown source — leave
        TTL unset rather than guess)
      - the descriptor's ``ttl_days`` is ``None`` (e.g. inline)

    Soft-imports :mod:`work_buddy.triage.sources` to avoid an import
    cycle (sources imports background for type hints).
    """
    try:
        from work_buddy.triage.sources import get_descriptor
    except Exception:
        return None
    descriptor = get_descriptor(source)
    if descriptor is None or descriptor.ttl_days is None:
        return None
    try:
        created = datetime.fromisoformat(created_at_iso)
    except ValueError:
        return None
    return (created + timedelta(days=descriptor.ttl_days)).isoformat()


def _shape_verdict(verdict: dict[str, Any]) -> dict[str, Any]:
    """Keep only known verdict fields to avoid the agent smuggling
    arbitrary payloads into the pool.

    ``raw`` is allowed (Slice 1) so :meth:`TriagePool.submit_raw` can
    flag entries that landed without a verdict pass. Slice 3's
    migration filters on ``verdict.get("raw") is True``.
    """
    allowed = {
        "recommended_action",
        "rationale",
        "group_intent",
        "confidence",
        "target_task_id",
        "suggested_task_text",
        "related_item_ids",
        "raw",
    }
    return {k: v for k, v in verdict.items() if k in allowed}


def _item_submitted(pool: TriagePool, run_id: str, item_id: str) -> bool:
    index = pool._load_index()
    for raw in index.get("entries", []):
        if raw.get("run_id") == run_id and raw.get("item_id") == item_id:
            return True
    return False


def content_hash(parts: list[str]) -> str:
    """Stable hash for idempotence keys. Public for adapters to use."""
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()[:16]


# Pattern for stripping per-line markdown bullet noise. Catches:
#   "- foo", "* foo", "+ foo", "1. foo", "  > foo", combinations with
#   nested indentation.
_LEADING_LIST_RE = re.compile(r"^[\s>]*(?:[-*+]|\d+[.)])\s+", re.MULTILINE)


def item_content_hash(source: str, text: str) -> str:
    """Stable per-item hash for cross-run dedup.

    Hardened in Slice 1 to catch byte-different / semantically-identical
    re-emissions that the original whitespace-only normalization missed.
    Pipeline:

      1. NFKC Unicode normalization (collapses precomposed vs combining
         forms — "é" written two different ways now hashes the same).
      2. Lowercase (case drift across re-segmentations doesn't matter).
      3. Strip leading markdown bullets per line (``- foo`` and
         ``* foo`` and ``1. foo`` all reduce to ``foo``).
      4. Collapse runs of whitespace within and across lines.

    Scoped by ``source`` so a conversation and a journal thread that
    happen to share text still count as distinct items.

    NOTE: changes here invalidate cross-run dedup for entries whose
    hashes were computed under the old normalization. The Slice 1
    migration script recomputes ``item_content_hash`` for all existing
    entries to bring them back into alignment.
    """
    if not text:
        return content_hash([source or "", ""])
    normalized = unicodedata.normalize("NFKC", text)
    normalized = normalized.lower()
    normalized = _LEADING_LIST_RE.sub("", normalized)
    # Whitespace collapse: split (any whitespace, including newlines) +
    # rejoin on single spaces. Preserves word boundaries; kills any
    # multi-line / multi-space artifacts.
    normalized = " ".join(normalized.split()).strip()
    return content_hash([source or "", normalized])
