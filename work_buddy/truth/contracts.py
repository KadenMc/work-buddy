"""Shared truth-kernel contracts used across independently built modules."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


class TruthError(Exception):
    """Base class for truth-kernel failures."""


class InvariantViolation(TruthError):
    """A requested operation would violate a ledger invariant."""


class TransitionError(InvariantViolation):
    """A claim lifecycle transition is not permitted."""


class GestureError(InvariantViolation):
    """A gesture is absent, stale, mismatched, consumed, or unauthorized."""


class ProfileError(InvariantViolation):
    """A store profile or profile-constrained write is invalid."""


class AnchorError(InvariantViolation):
    """An evidence selector cannot be validated or re-anchored safely."""


class StoreVersionError(TruthError):
    """A store schema cannot be opened safely by this engine."""


VALID_ACTOR_KINDS = frozenset({"human", "agent_run", "system"})
VALID_STATUSES = frozenset(
    {
        "proposed",
        "confirmed",
        "rejected",
        "expired",
        "challenged",
        "needs_review",
        "superseded",
        "retracted",
    }
)
TERMINAL_STATUSES = frozenset({"rejected", "expired", "superseded", "retracted"})


@dataclass(frozen=True, slots=True)
class Actor:
    """The durable actor identity supplied to every write operation."""

    kind: str
    ref: str | None = None
    meta: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in VALID_ACTOR_KINDS:
            raise ValueError(
                f"actor kind must be one of {sorted(VALID_ACTOR_KINDS)}"
            )
        if self.kind == "agent_run" and not self.ref:
            raise ValueError("agent_run actor requires ref")


@dataclass(frozen=True, slots=True)
class StorePaths:
    """Canonical paths belonging to one targeted truth store."""

    root: Path
    sidecar: Path
    db: Path
    config: Path
    blobs: Path
    export_dir: Path
    claims_export: Path

    @classmethod
    def from_root(cls, root: str | Path) -> "StorePaths":
        root_path = Path(root).expanduser().resolve()
        sidecar = (
            root_path
            if root_path.name == ".wb-truth"
            else root_path / ".wb-truth"
        )
        scope_root = sidecar.parent
        export_dir = sidecar / "export"
        return cls(
            root=scope_root,
            sidecar=sidecar,
            db=sidecar / "store.db",
            config=sidecar / "store.yaml",
            blobs=sidecar / "blobs",
            export_dir=export_dir,
            claims_export=export_dir / "claims.jsonl",
        )


AGENT_PRODUCER_META_KEYS = frozenset(
    {"model", "harness", "surface", "session_id"}
)


def validate_agent_producer_meta(meta: Mapping[str, Any]) -> None:
    """Require the durable producer identity for an agent-authored write."""
    missing = sorted(
        key for key in AGENT_PRODUCER_META_KEYS if not str(meta.get(key, "")).strip()
    )
    if missing:
        raise InvariantViolation(
            "agent write is missing producer identity fields: "
            + ", ".join(missing)
        )
