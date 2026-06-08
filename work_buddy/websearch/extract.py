"""Fetch + extract clean page text — a single shared service (not a vendor seam).

``extract_text(url, raw_text=...)`` returns a :class:`FetchResult`:

1. **Short-circuit** — if the caller already has full text (a Jina ``SearchHit``
   carries ``raw_text``), use it; no fetch. ``extractor="jina_reader"``.
2. **Jina reader** — when a ``JINA_API_KEY`` is configured, fetch via
   ``r.jina.ai/<url>`` (one credential covers search + fetch, returns clean
   Markdown). ``extractor="jina_reader"``.
3. **trafilatura** — otherwise ``httpx.get`` the page and run
   ``trafilatura.extract``. ``extractor="trafilatura"``.

Extraction is best-effort enrichment: a fetch/parse failure degrades to empty
text (``extractor="none"``) rather than raising, so a single dead link never
sinks a whole evidence pass. Timeouts are the one exception worth surfacing
upstream, but even those are swallowed to empty here — callers treat thin
evidence as "not enough," which is the correct behavior.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx

from work_buddy.secret_env import read_secret_env
from work_buddy.websearch.models import FetchResult

log = logging.getLogger(__name__)

_DEFAULT_READER_URL = "https://r.jina.ai/"
_DEFAULT_TIMEOUT_S = 20.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _websearch_cfg() -> dict:
    from work_buddy.config import load_config
    return (load_config() or {}).get("websearch", {}) or {}


def extract_text(url: str, *, raw_text: str | None = None, timeout_s: float | None = None) -> FetchResult:
    """Return clean text for ``url``. ``raw_text`` (e.g. ``SearchHit.raw_text``)
    short-circuits the fetch. Never raises on fetch/parse failure — returns a
    ``FetchResult`` with empty ``text`` and ``extractor="none"`` instead."""
    now = _now_iso()
    if raw_text:
        return FetchResult(url=url, canonical_url=url, text=raw_text, fetched_at=now, extractor="jina_reader")

    timeout = timeout_s if timeout_s is not None else _DEFAULT_TIMEOUT_S
    cfg = _websearch_cfg()
    jina_cfg = cfg.get("jina", {}) or {}
    key = read_secret_env(jina_cfg.get("api_key_env", "JINA_API_KEY"))

    if key:
        text = _jina_reader(url, key, jina_cfg, timeout)
        if text:
            return FetchResult(url=url, canonical_url=url, text=text, fetched_at=now, extractor="jina_reader")
        # fall through to trafilatura if the reader came back empty

    text = _trafilatura_extract(url, timeout)
    if text:
        return FetchResult(url=url, canonical_url=url, text=text, fetched_at=now, extractor="trafilatura")

    log.info("extract_text: no text extracted for %s", url)
    return FetchResult(url=url, canonical_url=url, text="", fetched_at=now, extractor="none")


def _jina_reader(url: str, key: str, jina_cfg: dict, timeout: float) -> str:
    reader = str(jina_cfg.get("reader_url", _DEFAULT_READER_URL)).rstrip("/") + "/"
    headers = {"Authorization": f"Bearer {key}", "Accept": "text/plain"}
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            resp = client.get(reader + url, headers=headers)
        if resp.status_code >= 400:
            log.info("jina reader HTTP %s for %s", resp.status_code, url)
            return ""
        return resp.text or ""
    except httpx.HTTPError as exc:
        log.info("jina reader transport error for %s: %s", url, exc)
        return ""


def _trafilatura_extract(url: str, timeout: float) -> str:
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True,
                          headers={"User-Agent": "work-buddy-websearch/1.0"}) as client:
            resp = client.get(url)
        if resp.status_code >= 400:
            return ""
        html = resp.text
    except httpx.HTTPError as exc:
        log.info("fetch failed for %s: %s", url, exc)
        return ""
    try:
        import trafilatura  # lazy: keeps package import light
        extracted = trafilatura.extract(html, url=url, favor_recall=True)
        return extracted or ""
    except Exception as exc:  # noqa: BLE001 — trafilatura can raise assorted parse errors
        log.info("trafilatura.extract failed for %s: %s", url, exc)
        return ""
