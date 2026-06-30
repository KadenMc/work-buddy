"""Process-agnostic secret resolution: ``os.environ`` first, then a line-scan
of the config-dir ``.env``.

work-buddy does **not** auto-load ``.env`` in most processes (only the Telegram
subprocess calls ``load_dotenv`` at its own startup). Existing consumers cope by
hand-scanning ``.env`` as a fallback — that exact pattern is duplicated in
``llm/runner.py`` and several ``health/requirement_checks.py`` functions. This is
the one shared implementation new code should use so a key written to ``.env``
(by the Settings fixer, which runs in the dashboard process) is still visible to
a *different* process (e.g. the MCP server, where ``web_search`` runs) without a
restart or a ``load_dotenv`` call.
"""

from __future__ import annotations

import os

from work_buddy import paths


def _scan_env_file(name: str) -> str | None:
    env_file = paths.config_dir() / ".env"
    if not env_file.exists():
        return None
    try:
        for raw in env_file.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            if key.strip() == name:
                val = val.strip().strip('"').strip("'")
                return val or None
    except OSError:
        return None
    return None


def read_secret_env(name: str) -> str | None:
    """Return the secret named ``name`` from the environment, falling back to a
    config-dir ``.env`` line-scan. Returns ``None`` if absent/empty in both."""
    v = os.environ.get(name)
    if v:
        return v
    return _scan_env_file(name)


def has_secret_env(name: str) -> bool:
    """True iff :func:`read_secret_env` would return a non-empty value."""
    return bool(read_secret_env(name))
