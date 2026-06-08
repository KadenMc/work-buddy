"""Health probe for the ``websearch`` component (resolved on Diagnose only).

``check_websearch`` reports whether the *active* backend (the first usable one
in the routing order) is ready. ddgs is keyless and always probeable, so a
keyless install is healthy on the ddgs fallback; Jina contributes only when a
key is present. The probe is intentionally cheap — it calls each backend's
``health()`` (key-presence / readiness), **not** a live search — so Diagnose
doesn't spend tokens or hammer the engines.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


def check_websearch() -> dict[str, Any]:
    """Return ``{"ok": bool, "detail": str}`` for the websearch component."""
    try:
        from work_buddy.websearch.provider import get_search_provider
        from work_buddy.websearch.router import active_backend

        name = active_backend()
        if not name:
            return {
                "ok": False,
                "detail": "no usable web-search backend (check network and that "
                          "websearch.enabled is not false)",
            }
        provider = get_search_provider(name)
        health = provider.health()
        ok = bool(health.get("ok"))
        if ok:
            return {"ok": True, "detail": f"active backend: {name}"}
        return {"ok": False, "detail": f"active backend {name} not ready: {health.get('detail', 'unknown')}"}
    except Exception as exc:  # noqa: BLE001 — a probe must never raise
        log.info("check_websearch failed: %s", exc)
        return {"ok": False, "detail": f"websearch probe error: {exc}"}
