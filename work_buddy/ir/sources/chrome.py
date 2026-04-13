"""Chrome tab source adapter — ledger snapshots + cached content to IR documents.

Indexes Chrome tabs that are "worth remembering":
- Persisted across 2+ consecutive snapshots (open 10+ minutes)
- Title doesn't already tell the whole story (configurable skip rules)
- Content enriched from LLM cache when available

Skip rules support domain patterns, title patterns, or both (AND logic).
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from work_buddy.ir.sources.base import Document
from work_buddy.logging_config import get_logger

from work_buddy.paths import resolve

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Skip rule matching
# ---------------------------------------------------------------------------

def _load_skip_rules(cfg: dict) -> list[dict[str, re.Pattern]]:
    """Load and compile skip rules from config."""
    raw = cfg.get("ir", {}).get("sources", {}).get("chrome", {}).get("skip_rules", [])
    compiled = []
    for rule in raw:
        entry = {}
        if "domain" in rule:
            entry["domain"] = re.compile(rule["domain"], re.IGNORECASE)
        if "title" in rule:
            entry["title"] = re.compile(rule["title"], re.IGNORECASE)
        if entry:
            compiled.append(entry)
    return compiled


def _should_skip(url: str, title: str, rules: list[dict[str, re.Pattern]]) -> bool:
    """Check if a tab matches any skip rule.

    Each rule can have 'domain', 'title', or both:
    - domain only: skip if domain matches
    - title only: skip if title matches
    - both: skip only if BOTH match (AND logic)
    """
    parsed = urlparse(url)
    domain = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme else parsed.netloc

    for rule in rules:
        domain_pat = rule.get("domain")
        title_pat = rule.get("title")

        if domain_pat and title_pat:
            # AND: both must match
            if domain_pat.search(domain) and title_pat.search(title):
                return True
        elif domain_pat:
            if domain_pat.search(domain):
                return True
        elif title_pat:
            if title_pat.search(title):
                return True

    return False


# ---------------------------------------------------------------------------
# Ledger parsing
# ---------------------------------------------------------------------------

def _load_ledger() -> list[dict]:
    """Load the Chrome tab ledger (list of snapshots)."""
    ledger_path = resolve("chrome/ledger")
    if not ledger_path.exists():
        return []
    try:
        with open(ledger_path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _normalize_url(url: str) -> str:
    """Normalize URL for comparison (strip fragments, trailing slashes)."""
    parsed = urlparse(url)
    if parsed.scheme in ("chrome", "chrome-extension", "about", "devtools"):
        return ""
    path = parsed.path.rstrip("/")
    if parsed.query:
        return f"{parsed.netloc}{path}?{parsed.query}"
    return f"{parsed.netloc}{path}"


def _extract_domain(url: str) -> str:
    """Extract clean domain from URL."""
    parsed = urlparse(url)
    return parsed.netloc or ""


def _find_persistent_tabs(
    snapshots: list[dict],
    min_consecutive: int = 2,
) -> dict[str, dict[str, Any]]:
    """Find URLs that appeared in min_consecutive consecutive snapshots.

    Returns {normalized_url: {url, title, first_seen, last_seen, snapshot_count,
    best_title}} for each persistent tab.
    """
    if len(snapshots) < min_consecutive:
        return {}

    # Track consecutive runs per URL
    # For each URL, track the current consecutive count and max info
    persistent: dict[str, dict[str, Any]] = {}
    prev_urls: set[str] = set()

    # Track current consecutive run per URL
    consecutive: dict[str, int] = {}
    url_info: dict[str, dict[str, Any]] = {}  # latest info per URL

    for snap in snapshots:
        captured_at = snap.get("captured_at", "")
        current_urls: set[str] = set()

        for tab in snap.get("tabs", []):
            raw_url = tab.get("url", "")
            norm = _normalize_url(raw_url)
            if not norm:
                continue

            current_urls.add(norm)
            title = tab.get("title", "")

            # Update info (keep longest/best title)
            tab_id = tab.get("tabId")

            if norm not in url_info:
                url_info[norm] = {
                    "url": raw_url,
                    "title": title,
                    "domain": _extract_domain(raw_url),
                    "first_seen": captured_at,
                    "last_seen": captured_at,
                    "snapshot_count": 0,
                    "tab_id": tab_id,
                }

            info = url_info[norm]
            info["last_seen"] = captured_at
            info["snapshot_count"] += 1
            info["tab_id"] = tab_id  # Always keep the latest tab ID
            # Keep the longer title (pages often have short titles while loading)
            if len(title) > len(info["title"]):
                info["title"] = title

        # Update consecutive counts
        for norm in current_urls:
            if norm in prev_urls:
                consecutive[norm] = consecutive.get(norm, 1) + 1
            else:
                consecutive[norm] = 1

            # Check if this URL has reached the threshold
            if consecutive[norm] >= min_consecutive and norm not in persistent:
                persistent[norm] = url_info[norm]

        # URLs no longer present reset their consecutive count
        for norm in list(consecutive.keys()):
            if norm not in current_urls:
                del consecutive[norm]

        prev_urls = current_urls

    return persistent


# ---------------------------------------------------------------------------
# Content from LLM cache
# ---------------------------------------------------------------------------

def _load_cached_content() -> dict[str, dict[str, Any]]:
    """Load any cached tab content from the LLM cache.

    Returns {normalized_url: {text, meta_description}} for tabs that
    had content previously extracted and summarized.
    """
    cache_path = resolve("cache/llm")
    if not cache_path.exists():
        return {}

    try:
        with open(cache_path, encoding="utf-8") as f:
            cache = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}

    content_by_url: dict[str, dict[str, Any]] = {}

    for key, entry in cache.items():
        if not key.startswith("summarize_tab:"):
            continue

        # Extract URL from cache key
        url_part = key[len("summarize_tab:"):]

        result = entry.get("result", {})
        if not isinstance(result, dict):
            continue

        content = result.get("content", "")
        # The content_sample field has the raw text before summarization
        raw_sample = entry.get("content_sample", "")

        if content or raw_sample:
            content_by_url[url_part] = {
                "text": raw_sample or content,
                "meta_description": "",  # Meta is in the extraction, not cache
            }

    return content_by_url


# ---------------------------------------------------------------------------
# Source adapter
# ---------------------------------------------------------------------------

class ChromeSource:
    """IR source adapter for Chrome tab snapshots."""

    @property
    def name(self) -> str:
        return "chrome"

    def default_field_weights(self) -> dict[str, float]:
        return {
            "title": 1.75,
            "url": 1.0,
            "domain": 1.25,
            "content": 1.25,
            "meta_description": 1.0,
        }

    def discover(self, days: int = 7) -> list[tuple[str, float]]:
        """Return (ledger_path, mtime) if the ledger has been modified.

        Chrome has a single ledger file (unlike conversations which have
        many JSONL files). We return the ledger itself as the single item.
        """
        ledger_path = resolve("chrome/ledger")
        if not ledger_path.exists():
            return []

        try:
            stat = ledger_path.stat()
        except OSError:
            return []

        return [(str(ledger_path), stat.st_mtime)]

    def parse(self, item_id: str) -> list[Document]:
        """Parse the Chrome ledger into documents for persistent tabs.

        Extracts page content for tabs that meet indexing criteria:
        1. Checks LLM cache for previously extracted content
        2. For tabs still open without cached content, calls request_content()
           to extract page text via Chrome extension content script injection
        """
        from work_buddy.config import load_config
        cfg = load_config()

        chrome_cfg = cfg.get("ir", {}).get("sources", {}).get("chrome", {})
        min_consecutive = chrome_cfg.get("min_consecutive_snapshots", 2)
        max_dense = cfg.get("ir", {}).get("dense_text_max_chars", 1500)

        # Load and filter
        snapshots = _load_ledger()
        if not snapshots:
            return []

        skip_rules = _load_skip_rules(cfg)
        persistent = _find_persistent_tabs(snapshots, min_consecutive)
        cached_content = _load_cached_content()

        # Find tabs that need content extraction (persistent, not skipped,
        # no cached content, and still open in the latest snapshot)
        latest_tabs = {}
        if snapshots:
            for tab in snapshots[-1].get("tabs", []):
                norm = _normalize_url(tab.get("url", ""))
                if norm:
                    latest_tabs[norm] = tab.get("tabId")

        needs_extraction: list[tuple[str, int]] = []  # (norm_url, tab_id)
        for norm_url, info in persistent.items():
            if _should_skip(info["url"], info["title"], skip_rules):
                continue
            if norm_url in cached_content:
                continue  # Already have content
            tab_id = latest_tabs.get(norm_url)
            if tab_id is not None:
                needs_extraction.append((norm_url, tab_id))

        # Extract content for tabs that need it
        if needs_extraction:
            logger.info("Extracting content from %d tabs...", len(needs_extraction))
            from work_buddy.collectors.chrome_collector import request_content

            tab_ids = [tid for _, tid in needs_extraction]
            extracted = request_content(tab_ids, max_chars=10000, timeout_seconds=45)

            if extracted:
                for entry in extracted:
                    text = (entry.get("text") or "").strip()
                    if not text:
                        continue
                    url = entry.get("url", "")
                    norm = _normalize_url(url)
                    meta = entry.get("meta") or {}
                    cached_content[norm] = {
                        "text": text,
                        "meta_description": (meta.get("description") or ""),
                    }
                logger.info("Extracted content for %d tabs", len(extracted))
            else:
                logger.warning("Content extraction failed (Chrome not responding?)")

        # Build documents
        docs: list[Document] = []
        for norm_url, info in persistent.items():
            url = info["url"]
            title = info["title"]

            if _should_skip(url, title, skip_rules):
                continue

            domain = info["domain"]

            # Get content (from cache or freshly extracted)
            content = ""
            meta_desc = ""
            cached = cached_content.get(norm_url)
            if cached:
                content = cached.get("text", "")
                meta_desc = cached.get("meta_description", "")

            # Build dense text: title + meta + content start
            dense_parts = [title]
            if meta_desc:
                dense_parts.append(meta_desc)
            if content:
                dense_parts.append(content[:1200])
            dense_text = " ".join(dense_parts)[:max_dense]

            # Display text
            display = f"{title} ({domain})" if domain else title

            # Use normalized URL as stable doc_id
            doc_id = norm_url.replace("/", "_").replace("?", "_").replace("&", "_")[:200]

            docs.append(Document(
                doc_id=doc_id,
                source="chrome",
                fields={
                    "title": title,
                    "url": url,
                    "domain": domain,
                    "content": content[:3000],
                    "meta_description": meta_desc,
                },
                dense_text=dense_text,
                display_text=display,
                metadata={
                    "url": url,
                    "domain": domain,
                    "first_seen": info["first_seen"],
                    "last_seen": info["last_seen"],
                    "snapshot_count": info["snapshot_count"],
                    "has_content": bool(content),
                },
            ))

        with_content = sum(1 for d in docs if d.metadata.get("has_content"))
        logger.info(
            "Chrome source: %d persistent tabs, %d after skip rules, %d with content",
            len(persistent),
            len(docs),
            with_content,
        )
        return docs
