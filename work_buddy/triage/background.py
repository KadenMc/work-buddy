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
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from work_buddy.logging_config import get_logger
from work_buddy.paths import data_dir
from work_buddy.triage.items import TRIAGE_ACTIONS, TriageItem

logger = get_logger(__name__)


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
    """A single pending-review verdict in the pool."""

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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PoolEntry:
        # Drop unknown fields so older code reading newer pool files
        # doesn't crash. Forward compat: newer fields just get
        # ignored.
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


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
            pe = PoolEntry(
                run_id=run_id,
                adapter=run.get("adapter", ""),
                source=run.get("source", ""),
                item_id=item_id,
                item=item_dict,
                verdict=_shape_verdict(verdict),
                created_at=_now_iso(),
                item_content_hash=item_content_hash(
                    item_dict.get("source", run.get("source", "")),
                    item_dict.get("text", ""),
                ),
            )
            index.setdefault("entries", []).append(pe.to_dict())
            self._save_index(index)

        return {
            "status": "ok",
            "run_id": run_id,
            "item_id": item_id,
            "recommended_action": action,
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
            if raw.get("reviewed_at"):
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
        """Return unreviewed entries, optionally filtered."""
        index = self._load_index()
        out: list[PoolEntry] = []
        for raw in index.get("entries", []):
            if raw.get("reviewed_at"):
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

    def mark_reviewed(
        self,
        entry_keys: list[tuple[str, str]],
        *,
        outcome: str,
    ) -> int:
        """Stamp ``reviewed_at`` on a set of ``(run_id, item_id)`` entries.

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
                    stamped += 1
            if stamped:
                self._save_index(index)
        return stamped

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
    ) -> None:
        self.adapter_name = adapter_name
        self.source = source
        self._collect = collect
        self._agent = agent
        self._pool = pool or get_pool()
        self._enrich = enrich
        self._ir_top_k = ir_top_k

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


def _shape_verdict(verdict: dict[str, Any]) -> dict[str, Any]:
    """Keep only known verdict fields to avoid the agent smuggling
    arbitrary payloads into the pool."""
    allowed = {
        "recommended_action",
        "rationale",
        "group_intent",
        "confidence",
        "target_task_id",
        "suggested_task_text",
        "related_item_ids",
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


def item_content_hash(source: str, text: str) -> str:
    """Stable per-item hash for cross-run dedup.

    Normalizes whitespace so trivial reformatting doesn't defeat the
    dedup, and scopes by source so a conversation and a journal
    thread that happen to share text still count as distinct items.
    Keeping it short (same length as ``content_hash``) keeps the
    pool index readable in the filesystem.
    """
    normalized = " ".join((text or "").split()).strip()
    return content_hash([source or "", normalized])
