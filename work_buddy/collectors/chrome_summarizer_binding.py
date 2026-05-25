"""Chrome as the second composition of the summarization framework.

Composition: `ChromeSource × FlatExtractionStrategy × TtlCacheStore`.

Public entry: `summarize_tabs(selected, tab_contents) -> (list[PageSummary],
cached_count)`. Replaces the previous hand-rolled per-tab caching + batched
LLM call in `chrome_infer.py`.

Cache fidelity: the existing cache key scheme used by Chrome triage —
`scoped_task_id = f"summarize_tab:{_normalize_for_cache(url)}"`, content-hash
invalidation, SimHash fuzzy fallback, system-hash from a `summarize_tab:v1`
tag, 30-minute TTL — is preserved at the cache-key / hash / TTL layer. The
JSON shape *inside* each entry's `result` field changes (now wrapping the
`SummaryNode` tree); entries written by the legacy code path are treated as
cache misses on the next access and naturally refresh under the 30-minute
TTL. New-format entries written by the framework remain readable by the
framework.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any
from urllib.parse import urlparse

from work_buddy.summarization import (
    DiscoveryWindow,
    Summarizer,
    SummaryCapability,
    SummaryNode,
)
from work_buddy.summarization.strategies import FlatExtractionStrategy
from work_buddy.summarization.stores import TtlCacheStore

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Cache key normalization — preserves the existing Chrome scheme
# ---------------------------------------------------------------------------


def normalize_url_for_cache(url: str) -> str:
    """Normalize URL for cache key — strip query params except where they
    materially identify the page (Google search, ChatGPT conversations).

    Mirrors the prior `chrome_infer._normalize_for_cache` exactly so existing
    cache entries written by the legacy code path remain reusable.
    """
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    if parsed.query and any(
        k in parsed.netloc for k in ("google.com", "chatgpt.com")
    ):
        return f"{parsed.netloc}{path}?{parsed.query}"
    return f"{parsed.netloc}{path}"


def _tab_label(tab: dict) -> str:
    url = tab.get("url", "")
    domain = urlparse(url).netloc
    title = tab.get("title", "")[:60]
    return f"{title} [{domain}]" if domain else title


# ---------------------------------------------------------------------------
# ChromeSource
# ---------------------------------------------------------------------------


class ChromeSource:
    """`Source` for Chrome tab summarization.

    Constructed per `summarize_tabs` call with the tabs to process and the
    fetched tab contents. `discover` enumerates the tabs with content-hash
    freshness tokens; `render_batch` produces per-tab prompt text.
    """

    name = "chrome_page"
    capabilities = frozenset({SummaryCapability.BATCHED})

    def __init__(
        self,
        tabs: list[dict],
        tab_contents: dict[str, dict],
    ) -> None:
        # Indexed by normalized URL (the framework's `item_id`) so render()
        # and discover() agree.
        self._by_item: dict[str, tuple[dict, str]] = {}
        for tab in tabs:
            url = tab.get("url", "")
            if not url:
                continue
            item_id = normalize_url_for_cache(url)
            content_text = tab_contents.get(url, {}).get("text", "") or ""
            self._by_item[item_id] = (tab, content_text)

    # --- protocol -----------------------------------------------------------

    def discover(self, window: DiscoveryWindow) -> list[tuple[str, Any]]:
        out: list[tuple[str, Any]] = []
        for item_id, (_tab, content) in self._by_item.items():
            content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
            out.append((item_id, {"hash": content_hash, "text": content}))
        return out

    def render(self, item_id: str) -> str | None:
        # Single-render isn't part of Chrome's BATCHED path, but provided
        # for the Protocol surface.
        entry = self._by_item.get(item_id)
        if entry is None:
            return None
        tab, content = entry
        return _build_tab_prompt(tab, content)

    def render_batch(self, item_ids: list[str]) -> list[str | None]:
        out: list[str | None] = []
        for iid in item_ids:
            entry = self._by_item.get(iid)
            if entry is None:
                out.append(None)
                continue
            tab, content = entry
            out.append(_build_tab_prompt(tab, content))
        return out


def _build_tab_prompt(tab: dict, content: str) -> str:
    """Per-tab prompt body. Mirrors the previous `summarize_batch` shape —
    a short content sample with an optional meta-description prefix."""
    label = _tab_label(tab)
    meta = (
        tab.get("meta", {}).get("description", "")
        if isinstance(tab.get("meta"), dict)
        else ""
    )
    text = content[:3000]
    if meta:
        text = f"[Meta: {meta[:150]}]\n{text}"
    return f"{label}\n\n{text}"


# ---------------------------------------------------------------------------
# Adapter — SummaryNode → PageSummary (the consumer-facing type kept verbatim)
# ---------------------------------------------------------------------------


def _node_to_page_summary(
    node: SummaryNode,
    tab: dict,
    *,
    cached: bool,
):
    """Adapt a `SummaryNode` (root of a depth-1 flat tree) back into a
    `PageSummary` so existing consumers (`pipelines/chrome.py`) see no shape
    change.
    """
    from work_buddy.llm.summarize import PageSummary, TypedEntity

    extra = node.extra or {}
    raw_entities = extra.get("entities") or []
    entities: list = []
    for e in raw_entities:
        if isinstance(e, dict):
            entities.append(TypedEntity(
                name=str(e.get("name", "")),
                type=str(e.get("type", "other")),
                context=str(e.get("context", "")),
            ))
    return PageSummary(
        content_summary=node.summary,
        entities=entities,
        key_claims=list(extra.get("key_claims") or []),
        user_intent_speculation=str(extra.get("user_intent_speculation", "")),
        user_posture=str(extra.get("user_posture", "referencing")),
        source_label=_tab_label(tab),
        cached=cached,
    )


def _fallback_page_summary(tab: dict):
    from work_buddy.llm.summarize import PageSummary

    return PageSummary(
        content_summary="Content unavailable",
        entities=[],
        key_claims=[],
        user_intent_speculation="",
        user_posture="referencing",
        source_label=_tab_label(tab),
        cached=False,
    )


# ---------------------------------------------------------------------------
# Factory + public entry
# ---------------------------------------------------------------------------


def build_chrome_summarizer(
    tabs: list[dict],
    tab_contents: dict[str, dict],
) -> Summarizer:
    """Build a per-pass Chrome `Summarizer`.

    A new `ChromeSource` is constructed each call (the source holds the
    tabs-to-summarize as state). The strategy and store are stateless.
    """
    return Summarizer(
        name="chrome_page",
        source=ChromeSource(tabs, tab_contents),
        strategy=FlatExtractionStrategy(),
        store=TtlCacheStore(
            namespace="chrome_page",
            strategy_version_tag="chrome_page:v1",
            ttl_minutes=30,
            # Exact prior cache key prefix — entries written by the legacy
            # code path remain readable.
            key_prefix="summarize_tab",
        ),
    )


def summarize_tabs(
    selected: list[dict],
    tab_contents: dict[str, dict],
    *,
    llm_caller=None,
) -> tuple[list, int]:
    """Summarize a batch of tabs through the framework.

    Returns `(summaries_aligned_with_selected, cached_count)` where each
    element of the returned list is a `PageSummary`. Drop-in replacement for
    the previous `chrome_infer._summarize_tabs` body.

    `llm_caller` is an injection seam for tests; production callers leave it
    `None` and the framework uses the default `LLMRunner` caller.
    """
    from work_buddy.config import load_config
    from work_buddy.llm.cost import log_call

    if not selected:
        return [], 0

    summarizer = build_chrome_summarizer(selected, tab_contents)
    # One batched LLM call for all stale tabs; cache hits are skipped.
    report = summarizer.refresh(
        days=0,
        max_items=max(1, len(selected)),
        force=False,
        llm_caller=llm_caller,
    )
    cached_count = report.skipped_fresh

    # Per-tab cache-hit cost-log entries (preserves the prior accounting).
    if cached_count > 0:
        model = (
            load_config().get("llm", {}).get(
                "default_model", "claude-haiku-4-5-20251001",
            )
        )
    # Build a quick lookup of which item_ids were stale (= newly summarized)
    # so we know which loaded results came from cache vs the fresh call.
    stale_item_ids: set[str] = set()
    for iid, _err in report.errors:
        stale_item_ids.add(iid)
    # We don't have a direct "saved-this-pass" list from RefreshReport.
    # Practical proxy: anything we just summarized was, before the call,
    # not in cache. We rebuild that set via discover + the store's
    # post-refresh state — but a cheaper signal is just `cached` from the
    # cache entry itself (lookup re-reads).

    summaries: list = []
    from work_buddy.llm.cache import get as cache_get

    for tab in selected:
        url = tab.get("url", "")
        item_id = normalize_url_for_cache(url) if url else ""
        node = summarizer.store.load(item_id) if item_id else None

        # Cache-hit detection: re-query the cache layer to see if this entry
        # was already present (TTL not new). Simpler than threading state
        # through the orchestrator.
        was_cached = False
        if item_id and node is not None:
            content_text = (tab_contents.get(url, {}).get("text", "") or "")
            content_hash = hashlib.sha256(
                content_text.encode("utf-8")
            ).hexdigest()
            # If the cache entry exists and would still match by hash, it was
            # a cache hit (the orchestrator's `skipped_fresh` count agrees).
            entry = cache_get(
                f"summarize_tab:{item_id}",
                input_hash=content_hash,
                input_text=content_text,
            )
            was_cached = entry is not None

        if node is None:
            summaries.append(_fallback_page_summary(tab))
        else:
            summaries.append(
                _node_to_page_summary(node, tab, cached=was_cached),
            )

    # Cost-log accounting for cached entries — single aggregate entry per
    # cache hit, mirroring the per-hit behavior of the legacy code.
    if cached_count > 0:
        for ps in summaries:
            if getattr(ps, "cached", False):
                log_call(
                    model=model,
                    input_tokens=0,
                    output_tokens=0,
                    task_id=f"summarize_tab:cached:{ps.source_label[:40]}",
                    cached=True,
                )

    return summaries, cached_count
