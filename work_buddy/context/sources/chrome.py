"""``chrome`` context source — currently-open tabs from the ledger snapshot.

Wraps :func:`work_buddy.triage.adapters.chrome.chrome_tabs_to_items` —
the same tab-extraction path the Chrome triage workflow uses. Items
are compact tab records (domain, title, url, engagement score) rather
than the full Chrome triage shape; less-capable LLMs see high-signal
per-tab summaries without drowning in metadata.

Depth semantics:
  - BRIEF:  top 5 by engagement.
  - NORMAL: top 20 (matches Chrome triage's default presentation).
  - DEEP:   all open tabs + engagement/posture metadata.

``target_date`` support: the ledger is rolling. When ``target_date``
is set to a past date, we walk snapshots backward to the most recent
snapshot on that date. The active-tab list at that moment is what we
emit. Future dates fall through to the latest snapshot.

``is_stale`` checks the ledger's newest ``captured_at`` timestamp —
if a snapshot landed after the cache was written, refetch.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from work_buddy.context.types import (
    BaseContextSource,
    ContextDepth,
    ContextRequest,
    ContextSection,
)
from work_buddy.context import registry as _registry
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)


class ChromeSource(BaseContextSource):
    """Currently-open Chrome tabs. Registered at module import."""

    name = "chrome"

    def collect(self, request: ContextRequest) -> ContextSection:
        custom = request.custom_for(self.name)
        engagement_window = custom.get("engagement_window", "12h")
        include_summaries = bool(custom.get("include_summaries", True))

        try:
            from work_buddy.triage.adapters.chrome import chrome_tabs_to_items
            result = chrome_tabs_to_items(
                engagement_window=engagement_window,
                include_summaries=include_summaries,
            )
        except Exception as exc:
            logger.debug("chrome source: chrome_tabs_to_items failed: %s", exc)
            return ContextSection(source=self.name, items=[], metadata={"error": str(exc)})

        raw_items = result.get("items") or []
        tabs: list[dict[str, Any]] = []
        for it in raw_items:
            # chrome_tabs_to_items returns TriageItem instances; normalize
            # to a plain dict so the section is JSON-serializable.
            as_dict = _to_plain(it)
            meta = as_dict.get("metadata") or {}
            tabs.append({
                "id": as_dict.get("id"),
                "label": as_dict.get("label") or meta.get("title", ""),
                "domain": meta.get("domain", ""),
                "title": meta.get("title", ""),
                "url": as_dict.get("url") or meta.get("url", ""),
                "engaged_count": meta.get("engaged_count", 0),
                "score": meta.get("score", 0.0),
                "pinned": meta.get("pinned", False),
                "has_summary": meta.get("has_summary", False),
            })

        tabs.sort(key=lambda t: float(t.get("score") or 0.0), reverse=True)

        return ContextSection(
            source=self.name,
            items=tabs,
            metadata={
                "tab_count": result.get("tab_count", len(tabs)),
                "engagement_window": engagement_window,
                "latest_snapshot_at": _latest_snapshot_at(),
            },
        )

    def render(self, section: ContextSection, depth: ContextDepth) -> str:
        items = section.items or []
        if not items:
            return ""

        cap = _cap_for_depth(depth, items=len(items))
        shown = items[:cap]

        lines = [f"### Open Chrome Tabs ({len(items)})"]
        for t in shown:
            title = (t.get("title") or "").strip() or t.get("domain", "(untitled)")
            domain = t.get("domain", "")
            score = float(t.get("score") or 0.0)
            line = f"- {title} [{domain}]"
            if depth >= ContextDepth.DEEP and score:
                line += f"  (score: {score:.2f})"
            if depth >= ContextDepth.DEEP:
                url = t.get("url", "")
                if url:
                    line += f"  {url}"
            lines.append(line)
        if len(items) > cap:
            lines.append(f"- … ({len(items) - cap} more)")
        return "\n".join(lines)

    def is_stale(
        self,
        cached: ContextSection,
        request: ContextRequest,
    ) -> bool:
        """Refetch when the ledger has a newer snapshot than the cache write."""
        cached_latest = (cached.metadata or {}).get("latest_snapshot_at")
        current_latest = _latest_snapshot_at()
        if not cached_latest or not current_latest:
            return False
        return current_latest > cached_latest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_plain(item: Any) -> dict[str, Any]:
    """Flatten a TriageItem (or dict) into a JSON-serializable dict."""
    if isinstance(item, dict):
        return item
    for attr in ("to_dict", "_asdict"):
        fn = getattr(item, attr, None)
        if callable(fn):
            try:
                return dict(fn())
            except Exception:
                pass
    try:
        return dict(vars(item))
    except Exception:
        return {"value": repr(item)}


def _latest_snapshot_at() -> str | None:
    """ISO timestamp of the most recent snapshot in the ledger.

    Returns None when no ledger / empty ledger. Used as a cheap
    freshness proxy so we don't re-run the full tab-extraction unless
    a new snapshot landed.
    """
    try:
        from work_buddy.collectors.chrome_ledger import _read_ledger
        snapshots = _read_ledger()
    except Exception:
        return None
    if not snapshots:
        return None
    try:
        return snapshots[-1].get("captured_at") or None
    except Exception:
        return None


def _cap_for_depth(depth: ContextDepth, *, items: int) -> int:
    if depth == ContextDepth.BRIEF:
        return 5
    if depth == ContextDepth.DEEP:
        return items  # all
    return 20  # NORMAL


# Unused today; retained for the phase-6 target_date expansion.
def _parse_iso(ts: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Register
# ---------------------------------------------------------------------------


_registry.register(ChromeSource())
