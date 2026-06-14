"""Config for the consolidated index — the master feature flag + per-partition tuning.

The whole consolidated index is **inert until ``index.enabled`` is true** (default
False). Per-partition config carries the single-sourced RRF ``k`` (fork F-RRFK), the
hybrid weights, the candidate-pool sizing, and the recency knobs.

Config shape (``config.yaml`` / ``config.local.yaml``):

```yaml
index:
  enabled: false                 # master flag — OFF by default
  db_path: null                  # null → paths.resolve("db/index-consolidated")
  partitions:
    knowledge:
      rrf_k: 20                  # smaller default per the A/B finding (was hardcoded 60)
      recency: false
    conversation:
      rrf_k: 60
      recency: true
```
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Default RRF k. The hardcoded ×7 value was 60; the A/B (session 9dda8859) showed
# k=60 vs k=15 give identical top-8/10 ranking, so the default is a legibility knob.
# Per-partition overrides live in config (fork F-RRFK).
DEFAULT_RRF_K = 20


@dataclass(frozen=True)
class PartitionConfig:
    """Per-partition tuning. All fields have safe defaults; config overrides any."""

    name: str
    enabled: bool = True
    rrf_k: int = DEFAULT_RRF_K
    meta_weight: float = 0.3
    content_weight: float = 0.7
    pool_multiplier: int = 5      # candidate pool = max(top_k * mult, floor)
    pool_floor: int = 50
    recency: bool = False
    recency_half_life_days: float = 14.0
    recency_floor: float = 0.15
    # Corpus COVERAGE this partition indexes (generic, source-interpreted). "active" =
    # the source's working set (the safe default — preserves each source's own default,
    # e.g. task_note excludes archived). "all" = full history incl. archived/closed/
    # superseded items, so retrospective queries can find them; selection is then a
    # query-time concern (Query.filters), not a build-time policy. A source that doesn't
    # understand `coverage` simply ignores it. See HISTORY-PARTITION-COVERAGE.md.
    coverage: str = "active"
    # RETENTION — what STAYS after the source DROPS an item (orthogonal to `coverage`,
    # which controls what ENTERS). The build's prune step honors this:
    #   "track_source" (default) — delete the doc when its source item disappears (today's
    #       behavior; the index mirrors the live source).
    #   "retain" — keep it, stamped ``lifecycle_state="orphaned"`` (and forget the change
    #       ledger entry), so search RECALL survives source deletion. A frozen snapshot —
    #       it can't refresh (source gone); if the source later restores the item it
    #       re-indexes fresh (ledger forgot it). Use when a hit's value is in-hit content,
    #       not a drill-through pointer (e.g. conversation spans after Claude Code prunes
    #       the JSONL).
    #   "ttl" — like "retain", but a per-build sweep prunes orphans whose newest doc
    #       timestamp is older than ``retention_ttl_days``, to bound storage growth.
    # Config accepts ``retention: track_source|retain`` or ``retention: {ttl_days: N}``.
    # See RETENTION-POLICY-DESIGN. Defaults preserve current behavior on every partition.
    retention: str = "track_source"
    retention_ttl_days: float | None = None

    @classmethod
    def from_dict(cls, name: str, raw: dict[str, Any] | None) -> "PartitionConfig":
        raw = raw or {}
        defaults = cls(name=name)
        retention, retention_ttl_days = cls._parse_retention(raw, defaults)
        return cls(
            name=name,
            enabled=bool(raw.get("enabled", defaults.enabled)),
            rrf_k=int(raw.get("rrf_k", defaults.rrf_k)),
            meta_weight=float(raw.get("meta_weight", defaults.meta_weight)),
            content_weight=float(raw.get("content_weight", defaults.content_weight)),
            pool_multiplier=int(raw.get("pool_multiplier", defaults.pool_multiplier)),
            pool_floor=int(raw.get("pool_floor", defaults.pool_floor)),
            recency=bool(raw.get("recency", defaults.recency)),
            recency_half_life_days=float(
                raw.get("recency_half_life_days", defaults.recency_half_life_days)
            ),
            recency_floor=float(raw.get("recency_floor", defaults.recency_floor)),
            coverage=str(raw.get("coverage", defaults.coverage)),
            retention=retention,
            retention_ttl_days=retention_ttl_days,
        )

    @staticmethod
    def _parse_retention(
        raw: dict[str, Any], defaults: "PartitionConfig"
    ) -> tuple[str, float | None]:
        """Normalize ``retention`` (a mode string, or ``{ttl_days: N}``) → (mode, ttl).

        An unknown mode or a ttl without a positive ``ttl_days`` degrades to a SAFE
        default — track_source if nothing was set, else retain (keep, never prune-all).
        """
        ret = raw.get("retention", defaults.retention)
        ttl: float | None = None
        if isinstance(ret, dict):
            ttl_raw = ret.get("ttl_days")
            ttl = float(ttl_raw) if ttl_raw is not None else None
            mode = "ttl"
        else:
            mode = str(ret)
            ttl_raw = raw.get("retention_ttl_days")
            ttl = float(ttl_raw) if ttl_raw is not None else None
        if mode == "ttl" and not (ttl and ttl > 0):
            mode = "retain"  # ttl with no positive window → keep (don't prune everything)
            ttl = None
        if mode not in ("track_source", "retain", "ttl"):
            mode = "track_source"  # unknown → today's behavior, never a surprise prune
            ttl = None
        return mode, ttl


@dataclass(frozen=True)
class IndexConfig:
    """Top-level consolidated-index config. ``enabled`` is the master kill-switch."""

    enabled: bool = False
    db_path: Path | None = None
    partitions: dict[str, PartitionConfig] = field(default_factory=dict)
    # Per-CONSUMER routing gates (``index.consumers.<name>``). A consumer (e.g. the
    # agent_docs knowledge search) routes to the consolidated index only when BOTH
    # ``enabled`` AND its gate are true — so each consumer can be staged live
    # independently even while ``enabled`` is already on for another. Default off.
    consumers: dict[str, bool] = field(default_factory=dict)
    # Kill-switch for the cold-start warming signal. When true (default), a search that
    # finds a partition's dense matrix not-yet-resident serves lexical-only for it,
    # triggers a background warm, and returns a ``warming`` marker so the client can wait
    # once and retry against the now-warm matrix. False reverts to the inline blocking
    # load (the matrix loads within the request, which can exceed the request timeout).
    warming_signal: bool = True

    def partition(self, name: str) -> PartitionConfig:
        """Config for ``name`` — falls back to defaults for an unlisted partition."""
        return self.partitions.get(name) or PartitionConfig(name=name)

    def consumer_enabled(self, name: str) -> bool:
        """True iff the consolidated index is enabled AND consumer ``name``'s gate is on.
        Unlisted consumer → False (ships inert)."""
        return self.enabled and bool(self.consumers.get(name, False))

    def resolved_db_path(self) -> Path:
        if self.db_path is not None:
            return self.db_path
        from work_buddy.paths import resolve
        return resolve("db/index-consolidated")


def load_index_config(cfg: dict[str, Any] | None = None) -> IndexConfig:
    """Load the ``index:`` config block, defensively. Returns defaults if absent.

    Never raises — a malformed block degrades to the OFF default so the consolidated
    index can't accidentally activate or crash a caller.
    """
    try:
        if cfg is None:
            from work_buddy.config import load_config
            cfg = load_config()
        raw = (cfg or {}).get("index", {}) or {}
    except Exception:
        raw = {}
    if not isinstance(raw, dict):
        raw = {}

    db_path_raw = raw.get("db_path")
    db_path = Path(db_path_raw) if isinstance(db_path_raw, str) and db_path_raw else None

    parts_raw = raw.get("partitions", {}) or {}
    partitions = {
        name: PartitionConfig.from_dict(name, pc)
        for name, pc in parts_raw.items()
        if isinstance(pc, dict) or pc is None
    }

    consumers_raw = raw.get("consumers", {}) or {}
    consumers = (
        {str(k): bool(v) for k, v in consumers_raw.items()}
        if isinstance(consumers_raw, dict) else {}
    )

    return IndexConfig(
        enabled=bool(raw.get("enabled", False)),
        db_path=db_path,
        partitions=partitions,
        consumers=consumers,
        warming_signal=bool(raw.get("warming_signal", True)),
    )
