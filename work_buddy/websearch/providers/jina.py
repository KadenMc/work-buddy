"""Jina ``s.jina.ai`` search backend — the reliable, LLM-native default.

Jina's Search API returns full-page Markdown inline, so a hit carries
``raw_text`` and downstream extraction can short-circuit (one credential covers
both search via ``s.jina.ai`` and fetch via ``r.jina.ai`` — the reader is used
by :mod:`work_buddy.websearch.extract`). ``supports("full_text") → True``.

Auth is a bearer key resolved via :func:`work_buddy.secret_env.read_secret_env`
(env-or-``.env``) under the name in ``websearch.jina.api_key_env`` (default
``JINA_API_KEY``). A missing key raises :class:`WebSearchBadKey`, which the
router treats as "skip Jina, fall through to ddgs" — so a keyless install works.

Request contract (per Jina docs; the exact result-count param + JSON envelope
are confirmed by a live round-trip at build time): ``GET https://s.jina.ai/``
with ``?q=<query>``, ``Authorization: Bearer <key>``, ``Accept:
application/json`` → ``{"data": [{title,url,description,content,...}]}``. Parsing
is defensive about list-vs-envelope and field-name variants so a minor API drift
degrades to fewer fields rather than an exception.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from work_buddy.secret_env import read_secret_env
from work_buddy.websearch.errors import (
    WebSearchBadKey,
    WebSearchRateLimited,
    WebSearchTimeout,
    WebSearchUnavailable,
)
from work_buddy.websearch.models import SearchHit

log = logging.getLogger(__name__)

_DEFAULT_SEARCH_URL = "https://s.jina.ai/"
_DEFAULT_READER_URL = "https://r.jina.ai/"


class JinaSearchProvider:
    """Jina s.jina.ai adapter. Config keys (under ``websearch.jina``):
    ``api_key_env`` (default ``"JINA_API_KEY"``), ``search_url``, ``reader_url``,
    ``timeout_s`` (default 30 — Jina fetches + returns page content, so it is
    slower than a snippet API)."""

    name = "jina"

    def __init__(self, cfg: dict | None = None) -> None:
        cfg = cfg or {}
        self.api_key_env: str = str(cfg.get("api_key_env", "JINA_API_KEY"))
        self.search_url: str = str(cfg.get("search_url", _DEFAULT_SEARCH_URL))
        self.reader_url: str = str(cfg.get("reader_url", _DEFAULT_READER_URL))
        self.timeout_s: float = float(cfg.get("timeout_s", 30))

    # --- protocol ----------------------------------------------------------

    def health(self) -> dict:
        """Cheap readiness check — key presence only, no network (a live Jina
        call costs tokens; the component's Diagnose can opt into a live probe)."""
        present = bool(read_secret_env(self.api_key_env))
        return {
            "ok": present,
            "provider": "jina",
            "needs_key": True,
            "key_present": present,
            "key_env": self.api_key_env,
            "detail": "key present" if present else f"${self.api_key_env} not set",
        }

    def search(
        self,
        query: str,
        *,
        max_results: int = 10,
        topic: str | None = None,
        time_range: str | None = None,
        since: str | None = None,
    ) -> list[SearchHit]:
        key = self._require_key()
        headers = {
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
        }
        params: dict[str, Any] = {"q": query}
        try:
            with httpx.Client(timeout=self.timeout_s) as client:
                resp = client.get(self.search_url, params=params, headers=headers)
        except httpx.TimeoutException as exc:
            raise WebSearchTimeout(f"jina search timed out after {self.timeout_s}s") from exc
        except httpx.HTTPError as exc:
            raise WebSearchUnavailable(f"jina transport error: {exc}") from exc

        self._raise_for_status(resp)

        try:
            payload = resp.json()
        except ValueError as exc:
            raise WebSearchUnavailable(f"jina returned non-JSON body: {exc}") from exc

        rows = self._extract_rows(payload)
        hits = [self._to_hit(r) for r in rows if isinstance(r, dict)]
        return hits[: max(1, int(max_results))]

    def supports(self, feature: str) -> bool:
        return feature in {"full_text"}

    # --- internals ---------------------------------------------------------

    def _require_key(self) -> str:
        key = read_secret_env(self.api_key_env)
        if not key:
            raise WebSearchBadKey(
                f"${self.api_key_env} is not set; Jina backend unavailable "
                "(router falls through to ddgs)."
            )
        return key

    @staticmethod
    def _raise_for_status(resp: httpx.Response) -> None:
        code = resp.status_code
        if code in (401, 403):
            raise WebSearchBadKey(f"jina rejected the key (HTTP {code})")
        if code in (402, 429):
            raise WebSearchRateLimited(f"jina rate/quota limit (HTTP {code})")
        if code >= 400:
            raise WebSearchUnavailable(f"jina HTTP {code}: {resp.text[:200]}")

    @staticmethod
    def _extract_rows(payload: Any) -> list[dict]:
        # Envelope {"data": [...]}, or {"data": {...}}, or a bare list.
        if isinstance(payload, dict):
            data = payload.get("data", payload.get("results"))
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                return [data]
            return []
        if isinstance(payload, list):
            return payload
        return []

    @staticmethod
    def _to_hit(row: dict) -> SearchHit:
        content = row.get("content") or row.get("text") or None
        return SearchHit(
            title=str(row.get("title") or ""),
            url=str(row.get("url") or row.get("href") or row.get("link") or ""),
            snippet=str(row.get("description") or row.get("snippet") or row.get("body") or ""),
            provider="jina",
            published=row.get("date") or row.get("published") or None,
            score=row.get("score") if isinstance(row.get("score"), (int, float)) else None,
            raw_text=str(content) if content else None,
        )
