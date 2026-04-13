"""Source-agnostic data types for the triage pipeline.

These types flow through the entire pipeline:
  adapter → cluster → task_match → recommend → workflow

All types support JSON serialization for auto_run subprocess transport.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class TriageItem:
    """A single unit of information to be triaged.

    Source-agnostic — Chrome tabs, journal entries, conversations, etc.
    all become TriageItems via their respective adapters.
    """

    id: str
    text: str               # content for embedding (title + summary)
    label: str              # human-readable display label
    source: str             # "chrome_tab", "journal_thread", "conversation", etc.
    url: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    embedding: list[float] | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "text": self.text,
            "label": self.label,
            "source": self.source,
            "metadata": self.metadata,
        }
        if self.url:
            d["url"] = self.url
        if self.embedding is not None:
            d["embedding"] = self.embedding
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TriageItem:
        return cls(
            id=d["id"],
            text=d["text"],
            label=d["label"],
            source=d["source"],
            url=d.get("url"),
            metadata=d.get("metadata", {}),
            embedding=d.get("embedding"),
        )


@dataclass
class TaskMatch:
    """A potential match between a cluster and an existing task."""

    task_id: str
    task_text: str
    project: str | None
    score: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "task_text": self.task_text,
            "project": self.project,
            "score": round(self.score, 4),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TaskMatch:
        return cls(
            task_id=d["task_id"],
            task_text=d["task_text"],
            project=d.get("project"),
            score=d["score"],
        )


# Valid triage actions
TRIAGE_ACTIONS = ("close", "group", "create_task", "record_into_task", "leave")


@dataclass
class TriageCluster:
    """A group of related TriageItems with recommended action."""

    cluster_id: int
    items: list[TriageItem]
    label: str
    cohesion: float = 0.0
    centroid: list[float] | None = None
    task_matches: list[TaskMatch] = field(default_factory=list)
    recommended_action: str | None = None   # one of TRIAGE_ACTIONS
    action_rationale: str | None = None
    suggested_task_text: str | None = None  # for create_task action
    target_task_id: str | None = None       # for record_into_task action
    cross_cluster_edges: list[dict[str, Any]] = field(default_factory=list)

    @property
    def size(self) -> int:
        return len(self.items)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "cluster_id": self.cluster_id,
            "items": [item.to_dict() for item in self.items],
            "label": self.label,
            "cohesion": round(self.cohesion, 3),
            "task_matches": [m.to_dict() for m in self.task_matches],
            "cross_cluster_edges": self.cross_cluster_edges,
        }
        if self.centroid is not None:
            d["centroid"] = self.centroid
        if self.recommended_action:
            d["recommended_action"] = self.recommended_action
        if self.action_rationale:
            d["action_rationale"] = self.action_rationale
        if self.suggested_task_text:
            d["suggested_task_text"] = self.suggested_task_text
        if self.target_task_id:
            d["target_task_id"] = self.target_task_id
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TriageCluster:
        return cls(
            cluster_id=d["cluster_id"],
            items=[TriageItem.from_dict(i) for i in d["items"]],
            label=d["label"],
            cohesion=d.get("cohesion", 0.0),
            centroid=d.get("centroid"),
            task_matches=[TaskMatch.from_dict(m) for m in d.get("task_matches", [])],
            recommended_action=d.get("recommended_action"),
            action_rationale=d.get("action_rationale"),
            suggested_task_text=d.get("suggested_task_text"),
            target_task_id=d.get("target_task_id"),
            cross_cluster_edges=d.get("cross_cluster_edges", []),
        )


@dataclass
class TriageResult:
    """Complete output of the triage pipeline."""

    clusters: list[TriageCluster]
    singletons: list[TriageCluster]
    item_count: int
    embedding_model: str = ""
    task_match_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "clusters": [c.to_dict() for c in self.clusters],
            "singletons": [c.to_dict() for c in self.singletons],
            "item_count": self.item_count,
            "embedding_model": self.embedding_model,
            "task_match_count": self.task_match_count,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TriageResult:
        return cls(
            clusters=[TriageCluster.from_dict(c) for c in d["clusters"]],
            singletons=[TriageCluster.from_dict(c) for c in d["singletons"]],
            item_count=d["item_count"],
            embedding_model=d.get("embedding_model", ""),
            task_match_count=d.get("task_match_count", 0),
        )
