"""Capture and persist Anthropic rate-limit headers per model.

Anthropic's Messages API attaches a family of ``anthropic-ratelimit-*``
headers to every successful response — they describe the current
token-bucket state for your API key against that model's family
(requests/min, input-tokens/min, output-tokens/min, plus a combined
all-tokens cap on some tiers). We extract them on every call work-buddy
makes and persist the most-recent observation per model to a single
JSON file. The Costs tab's "rate-limit headroom" chip reads from there.

Shape of the persisted file (``data/runtime/rate_limits.json``)::

    {
      "claude-sonnet-4-6": {
        "observed_at": "2026-04-25T22:10:00+00:00",
        "requests":         {"limit": 50,    "remaining": 47,    "reset": "..."},
        "input_tokens":     {"limit": 50000, "remaining": 49000, "reset": "..."},
        "output_tokens":    {"limit": 8000,  "remaining": 7800,  "reset": "..."},
        "tokens_combined":  {"limit": null,  "remaining": null,  "reset": null}
      },
      ...
    }

Caveats baked into the design (surfaced in the UI as well):

* **Stale by nature.** Headers reflect the bucket state at response
  time. If work-buddy hasn't called a model in N minutes, the saved
  values are N minutes old — Anthropic's bucket has likely refilled
  since. The UI greys out observations older than 5 minutes.
* **Coverage gap.** We only see headers on calls that go through this
  runner. Claude Code's calls drain the same buckets but their
  responses aren't visible to us. The popover help text says so.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

logger = logging.getLogger(__name__)

_lock = threading.Lock()


def _path() -> Path:
    from work_buddy.paths import resolve
    return resolve("runtime/rate-limits")


def _h_int(headers: Mapping[str, str], key: str) -> int | None:
    """Read an integer header value; return None on absence / parse error."""
    v = headers.get(key) if headers else None
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (ValueError, TypeError):
        return None


def _h_str(headers: Mapping[str, str], key: str) -> str | None:
    v = headers.get(key) if headers else None
    return v if v else None


def _extract(headers: Mapping[str, str]) -> dict[str, Any]:
    """Pull all anthropic-ratelimit-* headers into the persisted shape."""
    return {
        "observed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "requests": {
            "limit":     _h_int(headers, "anthropic-ratelimit-requests-limit"),
            "remaining": _h_int(headers, "anthropic-ratelimit-requests-remaining"),
            "reset":     _h_str(headers, "anthropic-ratelimit-requests-reset"),
        },
        "input_tokens": {
            "limit":     _h_int(headers, "anthropic-ratelimit-input-tokens-limit"),
            "remaining": _h_int(headers, "anthropic-ratelimit-input-tokens-remaining"),
            "reset":     _h_str(headers, "anthropic-ratelimit-input-tokens-reset"),
        },
        "output_tokens": {
            "limit":     _h_int(headers, "anthropic-ratelimit-output-tokens-limit"),
            "remaining": _h_int(headers, "anthropic-ratelimit-output-tokens-remaining"),
            "reset":     _h_str(headers, "anthropic-ratelimit-output-tokens-reset"),
        },
        "tokens_combined": {
            "limit":     _h_int(headers, "anthropic-ratelimit-tokens-limit"),
            "remaining": _h_int(headers, "anthropic-ratelimit-tokens-remaining"),
            "reset":     _h_str(headers, "anthropic-ratelimit-tokens-reset"),
        },
    }


def _has_any_rate_limit_data(obs: dict[str, Any]) -> bool:
    """True if at least one rate-limit dimension was populated."""
    for dim in ("requests", "input_tokens", "output_tokens", "tokens_combined"):
        if obs.get(dim, {}).get("limit") is not None:
            return True
    return False


def record_observation(model: str, headers: Mapping[str, str] | None) -> None:
    """Persist the most-recent rate-limit observation for ``model``.

    Best-effort. Silent on any header-parsing or write failure — we are
    on the LLM hot path and a logging-side bug should never break a
    real call.

    Skips entirely when ``headers`` doesn't carry any anthropic-ratelimit-*
    keys (e.g., a non-Anthropic backend, or the SDK fell back to a
    raw-response-less call path).
    """
    if not model or headers is None:
        return
    try:
        obs = _extract(headers)
        if not _has_any_rate_limit_data(obs):
            return
        path = _path()
        with _lock:
            existing = read_observations()
            existing[model] = obs
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(existing, ensure_ascii=False, indent=2),
                            encoding="utf-8")
            tmp.replace(path)
    except (OSError, ValueError) as exc:
        logger.debug("rate_limits: write skipped: %s", exc)


def read_observations() -> dict[str, Any]:
    """Read the on-disk observations dict. Empty dict on any failure."""
    try:
        path = _path()
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
