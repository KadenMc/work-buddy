"""WorkItem — the thin universal base for system-managed work units.

The universal base of the Thread/Task hierarchy: ``Thread`` and ``Task`` are
its two sibling subtypes (neither subclasses the other).
WorkItem carries only the *universal* slots — identity, lineage, attached
context, lifecycle timestamps, and governance metadata (autonomy policy,
risk profile) — and crucially **no resolution FSM**. Its two subtypes are:

* ``Thread(WorkItem)`` — the FSM-resolution subtype (``threads/models.py``);
* ``Task(WorkItem)``   — the master-list-contract subtype.

Design rules this module must honour:

* **No subtype branching.** The base never inspects ``self.subtype`` to
  change behaviour (no ``if subtype == ...``). Subtype-specific behaviour
  lives on the subtype, via overridden methods or injected data (e.g. the
  FSM transition table). ``is_task`` is a plain accessor, not a branch.
* **No storage assumption.** WorkItem is a pure in-memory dataclass.
  ``Thread`` persists in the ``threads`` table; ``Task`` persists in the
  task_metadata store. The base imposes neither.

The id field is named ``thread_id`` (not ``work_item_id``) deliberately:
~19 modules already import and use ``thread_id``; renaming is churn for a
later cleanup, not part of the foundation. Subtypes override its default
factory so the id prefix stays per-subtype (``th-`` / ``t-``); the base's
own default is the generic ``wi-``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:  # pragma: no cover - typing only
    from work_buddy.threads.models import AutonomyPolicy, ContextItem


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_work_item_id() -> str:
    """Generic base id. Subtypes override (Thread -> ``th-``, Task -> ``t-``)."""
    return f"wi-{uuid.uuid4().hex[:8]}"


def _default_autonomy_policy() -> "AutonomyPolicy":
    # Lazy import: ``models.py`` imports this module at load time, so we
    # cannot import ``AutonomyPolicy`` at module top (would cycle). The
    # factory is only invoked at instance-construction time, by which
    # point ``models`` is fully loaded.
    from work_buddy.threads.models import AutonomyPolicy

    return AutonomyPolicy()


@dataclass
class WorkItem:
    """Thin universal base for any system-managed unit of work. No FSM.

    See the module docstring for the design rules. Subtypes inherit these
    universal fields and add their own (Thread: the resolution-FSM fields;
    Task: nothing structural — it delegates persistence to the task store).
    """

    thread_id: str = field(default_factory=_new_work_item_id)
    parent_id: Optional[str] = None
    subtype: Optional[str] = None  # 'task' | None; never mutated

    autonomy_policy: "AutonomyPolicy" = field(
        default_factory=_default_autonomy_policy,
    )

    # Attached ContextItems (live in their source; this is just a
    # reference list).
    context_items: tuple["ContextItem", ...] = ()

    # Contextual risk dimensions (DESIGN.md §10.4). Intrinsic amplifiers
    # live on the action template and are composed at execution time.
    risk_profile: dict[str, Any] = field(default_factory=dict)

    # Inciting-event metadata: what brought this work item into being.
    inciting_event_summary: dict[str, Any] = field(default_factory=dict)

    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)
    archived_at: Optional[str] = None

    # Resurfacing / linearization / search (universal surfacing metadata).
    resurface_at: Optional[str] = None        # Later mechanic
    order_index: int = 0                       # write-time linearization
    search_blob: str = ""                      # denormalized search

    # NOTE(sovereignty): a per-WorkItem ``resurfacing_policy`` slot will
    # land here when the attention/sovereignty governor is built (design
    # 08 §6). It is deliberately NOT added now — an unused, unpersisted
    # field would change serialization for zero current benefit and dent
    # the "pure no-op extraction" guarantee. Add the field + its
    # persistence + serialization together at that time.

    @property
    def is_task(self) -> bool:
        """True iff this work item is a Task. A plain accessor — callers
        branch on it; the base itself never does."""
        return self.subtype == "task"

    def to_dict(self) -> dict[str, Any]:
        """Default serialization = the universal projection. Subtypes with
        extra fields (e.g. ``Thread``) override this and merge their keys
        on top of ``_universal_dict()``."""
        return self._universal_dict()

    def _universal_dict(self) -> dict[str, Any]:
        """The universal-field projection shared by every subtype's
        ``to_dict``. Subtypes call this and merge their own keys, so the
        base owns the serialization of the fields it owns."""
        return {
            "thread_id": self.thread_id,
            "parent_id": self.parent_id,
            "subtype": self.subtype,
            "autonomy_policy": self.autonomy_policy.to_dict(),
            "context_items": [c.to_dict() for c in self.context_items],
            "risk_profile": self.risk_profile,
            "inciting_event_summary": self.inciting_event_summary,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "archived_at": self.archived_at,
            "resurface_at": self.resurface_at,
            "order_index": self.order_index,
            "search_blob": self.search_blob,
        }
