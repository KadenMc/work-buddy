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

    @classmethod
    def from_dict(cls, name: str, raw: dict[str, Any] | None) -> "PartitionConfig":
        raw = raw or {}
        defaults = cls(name=name)
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
        )


@dataclass(frozen=True)
class IndexConfig:
    """Top-level consolidated-index config. ``enabled`` is the master kill-switch."""

    enabled: bool = False
    db_path: Path | None = None
    partitions: dict[str, PartitionConfig] = field(default_factory=dict)

    def partition(self, name: str) -> PartitionConfig:
        """Config for ``name`` — falls back to defaults for an unlisted partition."""
        return self.partitions.get(name) or PartitionConfig(name=name)

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

    return IndexConfig(
        enabled=bool(raw.get("enabled", False)),
        db_path=db_path,
        partitions=partitions,
    )
