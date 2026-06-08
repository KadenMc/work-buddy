"""ddgs metasearch backend — the no-key ``$0`` fallback, hardened.

ddgs (the maintained successor to duckduckgo_search) fans a query across ~10
engines with no API key. It is fragile and has a documented sync-hang failure
mode, so this adapter hardens it three ways:

1. **Wall-clock timeout** — every call runs in a one-shot ``ThreadPoolExecutor``
   and is awaited with ``future.result(timeout=…)``. On timeout we raise
   :class:`WebSearchTimeout` and *do not* block on the worker (the hung thread
   is abandoned and the shared client is rebuilt next call) — using a ``with``
   block here would defeat the timeout by joining the hung thread on exit.
2. **Request spacing** — a per-instance lock enforces ``min_interval_s`` between
   outbound calls so we don't trip DuckDuckGo's rate limiter.
3. **Backoff + retry** — ``RatelimitException`` retries with exponential backoff
   up to ``max_retries`` before surfacing :class:`WebSearchRateLimited`.

One ``DDGS()`` instance is reused across calls (cheaper, keeps a warm client);
it is discarded and rebuilt after a timeout in case the hung worker left it in a
bad state. No key, snippet-only results, so ``supports("full_text") → False`` —
the router/extractor fetch full text via trafilatura when a card needs it.
"""

from __future__ import annotations

import concurrent.futures as _futures
import logging
import threading
import time
from typing import Any

from work_buddy.websearch.errors import (
    WebSearchRateLimited,
    WebSearchTimeout,
    WebSearchUnavailable,
)
from work_buddy.websearch.models import SearchHit

log = logging.getLogger(__name__)


class DdgsSearchProvider:
    """Hardened ddgs adapter. Config keys (under ``websearch.ddgs``): ``backend``
    (default ``"auto"``), ``timeout_s`` (10), ``min_interval_s`` (2),
    ``max_retries`` (3), ``region`` (``"us-en"``), ``safesearch``
    (``"moderate"``)."""

    name = "ddgs"

    def __init__(self, cfg: dict | None = None) -> None:
        cfg = cfg or {}
        self.backend: str = str(cfg.get("backend", "auto"))
        self.timeout_s: float = float(cfg.get("timeout_s", 10))
        self.min_interval_s: float = float(cfg.get("min_interval_s", 2))
        self.max_retries: int = int(cfg.get("max_retries", 3))
        self.region: str = str(cfg.get("region", "us-en"))
        self.safesearch: str = str(cfg.get("safesearch", "moderate"))
        self._ddgs: Any = None
        self._lock = threading.Lock()
        self._last_call_ts: float = 0.0

    # --- protocol ----------------------------------------------------------

    def health(self) -> dict:
        """ddgs needs no key and no service — it is always probeable. We report
        readiness without spending a live query (the component's Diagnose probe
        does the live round-trip)."""
        return {"ok": True, "provider": "ddgs", "backend": self.backend, "needs_key": False}

    def search(
        self,
        query: str,
        *,
        max_results: int = 10,
        topic: str | None = None,
        time_range: str | None = None,
        since: str | None = None,
    ) -> list[SearchHit]:
        kwargs: dict[str, Any] = {
            "region": self.region,
            "safesearch": self.safesearch,
            "max_results": max(1, int(max_results)),
            "backend": self.backend,
        }
        if time_range:
            kwargs["timelimit"] = time_range  # d|w|m|y or a custom range
        rows = self._search_with_retries(query, kwargs)
        return [self._to_hit(r) for r in rows if isinstance(r, dict)]

    def supports(self, feature: str) -> bool:
        # Snippet-only backend: no inline full text. Time filtering + news work.
        return feature in {"time_filter", "news"}

    # --- internals ---------------------------------------------------------

    def _client(self) -> Any:
        if self._ddgs is None:
            from ddgs import DDGS  # lazy: only when ddgs is the active backend
            self._ddgs = DDGS()
        return self._ddgs

    def _respect_interval(self) -> None:
        with self._lock:
            now = time.monotonic()
            wait = self.min_interval_s - (now - self._last_call_ts)
            if wait > 0:
                time.sleep(wait)
            self._last_call_ts = time.monotonic()

    def _search_with_retries(self, query: str, kwargs: dict) -> list[dict]:
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._respect_interval()
            try:
                return self._call_with_timeout(query, kwargs)
            except WebSearchRateLimited as exc:
                last_exc = exc
                if attempt >= self.max_retries:
                    raise
                backoff = self.min_interval_s * (2 ** attempt)
                log.warning("ddgs rate-limited (attempt %d), backing off %.1fs", attempt + 1, backoff)
                time.sleep(backoff)
        # Defensive — loop either returns or raises above.
        raise WebSearchUnavailable("ddgs exhausted retries") from last_exc

    def _call_with_timeout(self, query: str, kwargs: dict) -> list[dict]:
        executor = _futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="ddgs")
        future = executor.submit(self._raw_search, query, kwargs)
        try:
            return future.result(timeout=self.timeout_s)
        except _futures.TimeoutError as exc:
            # Abandon the (possibly hung) worker; rebuild the client next call.
            self._ddgs = None
            raise WebSearchTimeout(
                f"ddgs.text exceeded {self.timeout_s}s wall-clock budget"
            ) from exc
        finally:
            executor.shutdown(wait=False)

    def _raw_search(self, query: str, kwargs: dict) -> list[dict]:
        from ddgs.exceptions import (
            DDGSException,
            RatelimitException,
            TimeoutException,
        )
        try:
            return self._client().text(query, **kwargs) or []
        except RatelimitException as exc:
            raise WebSearchRateLimited(f"ddgs rate-limited: {exc}") from exc
        except TimeoutException as exc:
            raise WebSearchTimeout(f"ddgs internal timeout: {exc}") from exc
        except DDGSException as exc:
            raise WebSearchUnavailable(f"ddgs error: {exc}") from exc

    @staticmethod
    def _to_hit(row: dict) -> SearchHit:
        # ddgs text rows are {"title","href","body"}; map defensively in case an
        # engine returns url/link/snippet variants.
        return SearchHit(
            title=str(row.get("title") or ""),
            url=str(row.get("href") or row.get("url") or row.get("link") or ""),
            snippet=str(row.get("body") or row.get("snippet") or row.get("description") or ""),
            provider="ddgs",
            published=row.get("date") or row.get("published") or None,
            score=None,
            raw_text=None,
        )
