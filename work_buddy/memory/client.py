"""Hindsight client wrapper — connection management and tag conventions.

All memory operations flow through ``get_client()`` which returns a
cached ``Hindsight`` instance pointed at the local server.
"""

from __future__ import annotations

from typing import Any
from urllib.error import URLError
from urllib.request import urlopen

from hindsight_client import Hindsight

from work_buddy.config import load_config
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)

_CLIENT: Hindsight | None = None

# Tags applied to every retain call for multi-tenant safety.
# Populated from config.yaml hindsight.user_tag (default: "user:default")
BASE_TAGS: list[str] = []  # set by _init_base_tags() on first use


def _cfg() -> dict[str, Any]:
    return load_config().get("hindsight", {})


def _init_base_tags() -> None:
    """Populate BASE_TAGS from config on first use."""
    if not BASE_TAGS:
        user_tag = _cfg().get("user_tag", "user:default")
        BASE_TAGS.append(user_tag)


def get_client() -> Hindsight:
    """Return a cached Hindsight client.  Creates one on first call."""
    global _CLIENT
    if _CLIENT is None:
        cfg = _cfg()
        base_url = cfg.get("base_url", "http://localhost:8888")
        _CLIENT = Hindsight(base_url=base_url)
        logger.info("Hindsight client created: %s", base_url)
    return _CLIENT


def get_bank_id() -> str:
    """Return the configured personal bank ID."""
    return _cfg().get("bank_id", "default")


def get_project_bank_id() -> str:
    """Return the configured project memory bank ID."""
    cfg = load_config().get("hindsight_projects", {})
    return cfg.get("bank_id", "project-memory")


def build_tags(*extra: str) -> list[str]:
    """Merge BASE_TAGS with caller-supplied tags, deduplicating."""
    _init_base_tags()
    return list(dict.fromkeys(BASE_TAGS + list(extra)))


def health_check() -> dict[str, Any]:
    """Check whether the Hindsight server is reachable.

    Returns a dict with ``ok`` (bool) and ``detail`` (str).
    """
    cfg = _cfg()
    url = cfg.get("base_url", "http://localhost:8888") + "/health"
    try:
        with urlopen(url, timeout=5) as resp:
            return {"ok": resp.status == 200, "detail": resp.read().decode()}
    except (URLError, OSError) as exc:
        return {"ok": False, "detail": str(exc)}
