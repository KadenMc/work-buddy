"""Protocol contracts for the unified artifact lifecycle system.

This module defines the type-level skeleton: capability enum,
storage / lifecycle / provenance protocols, the ``Artifact`` composer
class that wraps them, and the exceptions raised when compositions are
incoherent or operations aren't supported.

Concrete backends, lifecycles, triggers, expiry actions, and provenance
flavors live in sibling subpackages and implement these protocols.

See ``architecture/artifact-system`` knowledge unit for the full design
discussion (composition over inheritance, capability declarations,
exposure-declaration trajectory toward a permissions model).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Iterable, Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# StorageTrait enum — declared by storage / lifecycle / provenance components
# ---------------------------------------------------------------------------


class StorageTrait(str, Enum):
    """What a backend declares it supports.

    Storage capabilities describe the on-disk shape and what
    operations make sense on it. Lifecycle capabilities describe how
    expiry is determined and what happens at expiry. Provenance
    capabilities describe identity/audit features.
    """

    # Storage shape
    RECORDS = "records"             # backend stores discrete records (rows, dict entries, JSONL lines, files)
    ATOMIC_BLOBS = "atomic_blobs"   # backend stores opaque file blobs (filesystem artifacts)
    TYPED_COLUMNS = "typed_columns" # records have a fixed typed schema (SQLite tables)
    APPEND_ONLY = "append_only"     # writes only ever append; never mutate in place

    # Storage operations supported
    LISTABLE = "listable"           # supports list(filters, limit) returning refs
    DELETABLE = "deletable"         # supports delete(record_id) for individual records
    BULK_PRUNEABLE = "bulk_pruneable"  # supports delete_where(predicate) on a record set

    # Lifecycle triggers
    PER_TYPE_TTL = "per_type_ttl"      # filesystem-style: TTL per type
    PER_RECORD_TTL = "per_record_ttl"  # each record carries its own expires_at
    TIME_WINDOW = "time_window"        # rolling window: drop records older than N
    MTIME_WINDOW = "mtime_window"      # filesystem mtime-based windowing

    # Lifecycle modifiers / actions
    CONDITIONAL_RETENTION = "conditional_retention"  # retention_predicate gates deletion
    TRANSFORM_ON_EXPIRY = "transform_on_expiry"      # rollup/archive instead of straight delete

    # Provenance
    SESSION_TAGGED = "session_tagged"  # records can be filtered by creating session


class Operation(str, Enum):
    """MCP-level operations that an Artifact can expose to agents.

    Distinct from :class:`ExpiryAction` (which is the lifecycle's
    internal "what happens at expiry"). These are the verbs an agent
    can invoke through MCP capabilities.

    Each registered Artifact declares which subset of operations it
    exposes. Today ``exposed_operations`` is a flat ``frozenset``;
    forward-compat to a per-principal map (``{Principal: frozenset}``)
    when the permissions model arrives.
    """

    SAVE = "save"
    GET = "get"
    LIST = "list"
    DELETE = "delete"
    CLEANUP = "cleanup"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class IncoherentComposition(ValueError):
    """Raised at ``Artifact`` construction when components don't fit together.

    Examples that should raise:
        - ``PerRecordTtl`` lifecycle paired with ``FilesystemStorage``
          (no records to attach per-record TTL to)
        - ``TransformAndDelete`` action paired with non-records storage
        - ``SessionTagged`` provenance on a backend with no record-level
          session field
    """


class UnsupportedOperation(RuntimeError):
    """Raised when calling a method whose required capability is absent.

    Carries the artifact name, the operation name, and the missing
    capability so diagnostics can pinpoint the misconfiguration.
    """

    def __init__(self, artifact_name: str, op: str, missing: StorageTrait) -> None:
        super().__init__(
            f"Artifact {artifact_name!r} does not support {op}() — "
            f"missing capability {missing.value!r}"
        )
        self.artifact_name = artifact_name
        self.op = op
        self.missing = missing


# ---------------------------------------------------------------------------
# Ref + SweepResult dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Ref:
    """Opaque reference to one record within a backend.

    Returned from ``list_expired`` and ``list``. Backends are free to
    interpret ``id`` however they want — filename stem, primary key,
    dict key, JSONL line offset, etc.

    The optional ``metadata`` dict carries fields useful for
    cross-backend display (e.g. ``size_bytes``, ``created_at``,
    ``expires_at``, ``session_id``) without forcing all backends to
    populate the same set.
    """

    id: str
    artifact_name: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class SweepResult:
    """Per-artifact result of a single ``prune()`` invocation.

    Returned by ``Artifact.prune()`` and aggregated by
    ``registry.sweep_all()``. Field shapes mirror the de facto contract
    of the existing ``prune_*`` callables in
    ``work_buddy.artifacts.meta_pruners`` so existing dashboard / job
    surfaces don't need to change.
    """

    artifact_name: str
    pruned: int = 0
    remaining: int = 0           # -1 means "not measured"
    bytes_before: int = 0
    bytes_after: int = 0
    transformed: int = 0         # for TRANSFORM_ON_EXPIRY actions; 0 otherwise
    error: str | None = None     # populated when prune failed; pruned/remaining are best-effort
    extra: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Storage / Lifecycle / Provenance protocols
# ---------------------------------------------------------------------------


@runtime_checkable
class Storage(Protocol):
    """Where the records live and how to enumerate / delete them.

    Concrete implementations live in ``backends/*.py``. Each declares
    its ``capabilities`` set; the ``Artifact`` composer uses these to
    validate coherence and gate operations.
    """

    capabilities: frozenset[StorageTrait]

    def iter_records(self) -> Iterable[dict[str, Any]]:
        """Yield each record as a dict.

        Each yielded dict is treated as opaque by the lifecycle —
        triggers and predicates pull whatever fields they need. Backends
        that store typed records (SQLite) yield rows-as-dicts; backends
        that store JSON values yield those values; filesystem yields
        meta-dicts (parsed from sidecar metadata).
        """
        ...

    def ref_for(self, record: dict[str, Any]) -> Ref:
        """Build a Ref for ``record``. Backend-specific id extraction."""
        ...

    def delete_record(self, ref: Ref) -> int:
        """Delete a single record. Returns bytes freed (best-effort)."""
        ...

    def delete_where(self, predicate: Callable[[dict[str, Any]], bool]) -> tuple[int, int]:
        """Bulk delete matching records. Returns (count_deleted, bytes_freed).

        Required only if ``BULK_PRUNEABLE`` is declared. Backends should
        prefer this over many ``delete_record`` calls for record-set
        storage (atomic rewrite of the underlying file).
        """
        ...

    def size_bytes(self) -> int:
        """Current on-disk size in bytes (0 if backend can't measure)."""
        ...


class Trigger(Protocol):
    """Determines which records are expired right now.

    Concrete triggers live in ``lifecycle/triggers/*.py``. Each takes
    an iterable of records and a ``now`` datetime, and yields
    booleans/predicates marking expired ones.
    """

    capabilities: frozenset[StorageTrait]

    def is_expired(self, record: dict[str, Any], now: datetime) -> bool:
        """Return True if ``record`` is past its expiry per this trigger."""
        ...


class ExpiryAction(Protocol):
    """What happens to a record once the trigger marks it expired.

    Default action is :class:`Delete`. Other actions
    (e.g. :class:`TransformAndDelete`) declare additional capabilities.
    """

    capabilities: frozenset[StorageTrait]

    def apply(
        self,
        storage: Storage,
        expired_refs: list[Ref],
        *,
        dry_run: bool,
    ) -> dict[str, int]:
        """Apply the action to the expired records.

        Returns a dict with at minimum ``{"pruned": int, "bytes_freed": int}``;
        transform-style actions add ``{"transformed": int}``.
        """
        ...


@runtime_checkable
class Provenance(Protocol):
    """Optional axis: identity / audit / session tagging.

    A backend may have no provenance (``provenance=None``) — that just
    means session-filtered queries aren't supported.
    """

    capabilities: frozenset[StorageTrait]

    def get_session(self, record: dict[str, Any]) -> str | None:
        """Extract the creating session ID from a record (or None)."""
        ...


# ---------------------------------------------------------------------------
# Lifecycle composer
# ---------------------------------------------------------------------------


@dataclass
class Lifecycle:
    """Composes a trigger × expiry action × optional retention predicate.

    The lifecycle's ``capabilities`` is the union of its components,
    plus ``CONDITIONAL_RETENTION`` if a retention predicate is set.

    The retention predicate, when provided, is consulted *after* the
    trigger marks a record as expired and decides whether to skip it
    anyway. Returning True means "keep this record despite the trigger"
    (e.g. messages with status ``pending`` are never deleted regardless
    of age).
    """

    trigger: Trigger
    action: ExpiryAction
    retention_predicate: Callable[[dict[str, Any]], bool] | None = None

    @property
    def capabilities(self) -> frozenset[StorageTrait]:
        caps = set(self.trigger.capabilities) | set(self.action.capabilities)
        if self.retention_predicate is not None:
            caps.add(StorageTrait.CONDITIONAL_RETENTION)
        return frozenset(caps)

    def find_expired(self, storage: Storage, now: datetime) -> list[Ref]:
        """Enumerate refs that should be acted on.

        Applies trigger.is_expired to every record, filters out those
        the retention predicate wants to keep, returns the surviving
        refs.
        """
        expired: list[Ref] = []
        for record in storage.iter_records():
            if not self.trigger.is_expired(record, now):
                continue
            if self.retention_predicate is not None and self.retention_predicate(record):
                continue
            expired.append(storage.ref_for(record))
        return expired


# ---------------------------------------------------------------------------
# Artifact composer
# ---------------------------------------------------------------------------


@dataclass
class Artifact:
    """One registered, lifecycle-managed entity.

    Each consumer (messaging, llm_cache, filesystem artifacts, etc.)
    constructs and registers exactly one ``Artifact`` describing how
    its data is stored, when records expire, and which agent-facing
    operations are exposed via MCP.

    Construction validates coherence: incompatible component
    combinations raise :class:`IncoherentComposition` immediately
    rather than failing at the next sweep tick.
    """

    name: str
    storage: Storage
    lifecycle: Lifecycle
    provenance: Provenance | None = None
    exposed_operations: frozenset[Operation] = frozenset()

    def __post_init__(self) -> None:
        self._validate_coherence()

    # ------------------------------------------------ public properties

    @property
    def capabilities(self) -> frozenset[StorageTrait]:
        """Union of storage, lifecycle, and provenance capabilities."""
        caps = set(self.storage.capabilities) | set(self.lifecycle.capabilities)
        if self.provenance is not None:
            caps |= set(self.provenance.capabilities)
        return frozenset(caps)

    # ------------------------------------------------ universal operations

    def prune(self, dry_run: bool = True) -> SweepResult:
        """Find expired records (per the lifecycle) and apply the expiry action.

        Universal — every Artifact supports this. The shape of work
        done depends on the composed components:

        * ``trigger.is_expired`` is consulted per record
        * ``retention_predicate`` (if any) skips records the lifecycle
          wants to preserve
        * ``action.apply`` (default ``Delete``) acts on the survivors

        Returns a :class:`SweepResult` summarizing the operation.
        """
        from datetime import timezone

        now = datetime.now(timezone.utc)
        bytes_before = self.storage.size_bytes()
        try:
            expired_refs = self.lifecycle.find_expired(self.storage, now)
            action_result = self.lifecycle.action.apply(
                self.storage, expired_refs, dry_run=dry_run
            )
        except Exception as exc:
            return SweepResult(
                artifact_name=self.name,
                bytes_before=bytes_before,
                bytes_after=bytes_before,
                error=str(exc),
            )
        bytes_after = self.storage.size_bytes() if not dry_run else bytes_before
        return SweepResult(
            artifact_name=self.name,
            pruned=action_result.get("pruned", 0),
            remaining=action_result.get("remaining", -1),
            bytes_before=bytes_before,
            bytes_after=bytes_after,
            transformed=action_result.get("transformed", 0),
            extra={k: v for k, v in action_result.items()
                   if k not in ("pruned", "remaining", "transformed")},
        )

    # ------------------------------------------------ capability-gated ops

    def delete(self, record_id: str) -> bool:
        """Delete a single record by id. Requires DELETABLE."""
        self._require(StorageTrait.DELETABLE, "delete")
        # Construct a minimal Ref; backend resolves it.
        ref = Ref(id=record_id, artifact_name=self.name)
        bytes_freed = self.storage.delete_record(ref)
        return bytes_freed >= 0

    def delete_where(self, predicate: Callable[[dict[str, Any]], bool]) -> int:
        """Bulk-delete matching records. Requires BULK_PRUNEABLE."""
        self._require(StorageTrait.BULK_PRUNEABLE, "delete_where")
        count, _bytes = self.storage.delete_where(predicate)
        return count

    def list_expired(self) -> list[Ref]:
        """Enumerate refs that the lifecycle considers expired right now."""
        from datetime import timezone

        return self.lifecycle.find_expired(self.storage, datetime.now(timezone.utc))

    def list_by_session(self, session_id: str) -> list[Ref]:
        """List refs whose creating session matches. Requires SESSION_TAGGED."""
        self._require(StorageTrait.SESSION_TAGGED, "list_by_session")
        assert self.provenance is not None  # guaranteed by capability check
        results: list[Ref] = []
        for record in self.storage.iter_records():
            if self.provenance.get_session(record) == session_id:
                results.append(self.storage.ref_for(record))
        return results

    # ------------------------------------------------ introspection

    def describe(self) -> dict[str, Any]:
        """Return a JSON-serializable snapshot of this artifact's shape.

        Used by ``artifact_registry()`` for cross-backend introspection.
        """
        return {
            "name": self.name,
            "storage_kind": type(self.storage).__name__,
            "lifecycle_kind": (
                f"{type(self.lifecycle.trigger).__name__}"
                f"+{type(self.lifecycle.action).__name__}"
                + ("+ConditionalRetention" if self.lifecycle.retention_predicate else "")
            ),
            "provenance_kind": (
                type(self.provenance).__name__ if self.provenance else None
            ),
            "capabilities": sorted(c.value for c in self.capabilities),
            "exposed_operations": sorted(o.value for o in self.exposed_operations),
        }

    # ------------------------------------------------ internals

    def _require(self, cap: StorageTrait, op_name: str) -> None:
        if cap not in self.capabilities:
            raise UnsupportedOperation(self.name, op_name, cap)

    def _validate_coherence(self) -> None:
        """Reject incompatible component combinations at construction time."""
        # Per-record TTL needs records, not blobs.
        if (
            StorageTrait.PER_RECORD_TTL in self.lifecycle.trigger.capabilities
            and StorageTrait.RECORDS not in self.storage.capabilities
        ):
            raise IncoherentComposition(
                f"Artifact {self.name!r}: PerRecordTtl trigger requires "
                f"a records-shaped storage; got {type(self.storage).__name__} "
                f"which declares {sorted(c.value for c in self.storage.capabilities)}"
            )
        # TransformAndDelete needs records (you can't rollup blobs).
        if (
            StorageTrait.TRANSFORM_ON_EXPIRY in self.lifecycle.action.capabilities
            and StorageTrait.RECORDS not in self.storage.capabilities
        ):
            raise IncoherentComposition(
                f"Artifact {self.name!r}: TransformAndDelete action requires "
                f"a records-shaped storage; got {type(self.storage).__name__}"
            )
        # delete_where capability requires BULK_PRUNEABLE storage
        # (no validation needed at construction — caught at call time
        # via UnsupportedOperation, since not all consumers need it).
