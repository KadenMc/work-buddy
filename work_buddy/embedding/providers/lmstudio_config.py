"""Lightweight LM Studio connection configuration.

This module deliberately has no NumPy, SciPy, Torch, or ``httpx`` dependency.
Health checks and component discovery need only resolve the server URL; importing
the full embedding provider there would initialize OpenBLAS in processes that
have not performed any numerical work.
"""

from __future__ import annotations

from typing import Any

# Default base URL when ``lmstudio.base_url`` isn't set in config.
# Matches LM Studio's out-of-box server binding.
DEFAULT_BASE_URL = "http://localhost:1234"


def resolve_base_url(cfg: dict[str, Any] | None = None) -> str:
    """Return the configured LM Studio server root without a trailing slash."""
    if cfg is None:
        from work_buddy.config import load_config

        cfg = load_config()
    url = cfg.get("lmstudio", {}).get("base_url", DEFAULT_BASE_URL)
    if not isinstance(url, str) or not url:
        url = DEFAULT_BASE_URL
    return url.rstrip("/")


__all__ = ["DEFAULT_BASE_URL", "resolve_base_url"]
