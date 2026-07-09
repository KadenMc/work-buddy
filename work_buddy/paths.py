"""Centralized path resolution for work-buddy.

Single source of truth for the repo root and the ``<data_root>/`` directory
tree. All persistent files — runtime state, caches, databases, agent
artifacts — are registered here by hierarchical ID and resolved through
:func:`resolve`.

Modules should never compute paths locally via ``_REPO_ROOT / "some_file"``.
Instead::

    from work_buddy.paths import resolve
    PID_FILE = resolve("runtime/sidecar-pid")

The data root is set by ``paths.data_root`` in ``config.yaml`` (with optional
``config.local.yaml`` overlay), interpreted as either an absolute path or as
relative to the repo root. The shipped default is ``.data`` so the runtime
tree is dot-prefixed (Obsidian treats dot-prefixed dirs as system folders
and skips them, which matters when work-buddy is installed inside a vault).

The hierarchical ID doubles as the directory structure: ``runtime/sidecar-pid``
resolves to ``<data_root>/runtime/sidecar.pid``, keeping files organized in
folders matching the ID prefix.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Singleton resource registry
# ---------------------------------------------------------------------------
# Maps hierarchical ID → relative path under data_root.
# The ID prefix (before the last /) determines the subdirectory.

RESOURCES: dict[str, str] = {
    # Runtime state — ephemeral, regenerated on each sidecar start
    "runtime/sidecar-pid":       "runtime/sidecar.pid",
    "runtime/sidecar-state":     "runtime/sidecar_state.json",
    "runtime/tool-status":       "runtime/tool_status.json",
    "runtime/agent-registry":    "runtime/agent_registry.json",
    "runtime/tray-pid":          "runtime/tray.pid",

    # Caches — safe to delete anytime
    "cache/llm":                 "cache/llm_cache.json",
    "cache/websearch":           "cache/websearch_cache.json",
    "cache/segmentation":        "cache/segmentation_cache.json",
    "cache/chrome-tabs":         "cache/chrome_tabs.json",
    "cache/chrome-request":      "cache/chrome_tabs_request",
    "cache/knowledge-content":   "cache/knowledge_index/content.npz",
    "cache/knowledge-aliases":   "cache/knowledge_index/aliases.npz",
    "cache/claude-code-usage":   "cache/claude_code_usage.db",
    # Stage 5 grouping: content-hash → embedding cache for the
    # journal-similarity merge layer + the cross-group suggestions
    # endpoint. ``.npz`` suffix is appended at use time by
    # ``Path.with_suffix(".npz")``.
    "cache/journal-similarity-embeddings": "cache/journal_similarity/embeddings",

    # Chrome integration — rolling data
    "chrome/ledger":             "chrome/tab_ledger.json",

    # Vault recon — periodic reconnaissance ledger
    "vault_recon":               "vault_recon",
    "user_jobs":                 "user_jobs",
    "event_sources":             "event_sources",  # user-authored event-source .md files

    # Databases — persistent stores
    "db/messages":               "db/messages.db",
    "db/tasks":                  "db/task_metadata.db",
    "db/projects":               "db/projects.db",
    "db/entities":               "db/entities.db",
    "db/threads":                "db/threads.db",  # Thread + thread_events
    "db/llm_queue":              "db/llm_call_queue.db",  # LLM-call priority queue
    "db/work_item_events":       "db/work_item_events.db",  # WorkItem audit/provenance log
    "db/events":                 "db/events.db",  # Events backbone — durable spine (log + offsets + DLQ)
    "db/vault-index":            "db/vault-index.db",  # Vault semantic-index chunk store
    "db/index-consolidated":     "db/index-consolidated.db",  # Consolidated index (flag-gated; index.enabled)
    "db/broker-metrics":         "db/broker_metrics.db",  # Persisted LocalInferenceBroker call metrics

    # Logs
    "logs/gateway-debug":        "logs/gateway_debug.log",
    "logs/search-debug":         "logs/search_debug.log",
    "logs/escalations":          "logs/escalations.log",

    # Anthropic rate-limit observations (latest snapshot per model).
    "runtime/rate-limits":       "runtime/rate_limits.json",

    # Telegram
    "runtime/telegram-chat-id":  "runtime/telegram_chat_id",

    # Credentials — persistent secrets (gitignored under the data root)
    "credentials/google-oauth":         "credentials/google_oauth_token.json",
    "credentials/google-client-secret": "credentials/google_client_secret.json",
}


# ---------------------------------------------------------------------------
# Entry-level pruner registry — DEPRECATED
# ---------------------------------------------------------------------------
# As of the artifact-system unification (t-aade2f16), every entry that
# used to live here has been migrated to a registered :class:`Artifact`
# in :mod:`work_buddy.artifacts`. The cleanup tick now drives off
# :func:`work_buddy.artifacts.registry.sweep_all` instead of iterating
# this dict.
#
# The dict is kept (empty) for backwards compatibility with any external
# code that imports it. New consumers should NOT add entries here —
# register an Artifact in their own module instead. See
# ``architecture/artifact-system`` for the registration pattern.
#
# The standalone ``prune_*`` callables in
# :mod:`work_buddy.artifacts.meta_pruners` (re-exported from
# :mod:`work_buddy.artifacts`) remain importable so existing tests that
# exercise them with custom paths keep working.

PRUNERS: dict[str, tuple[str, dict[str, Any]]] = {}


# ---------------------------------------------------------------------------
# Core path resolution
# ---------------------------------------------------------------------------


def repo_root() -> Path:
    """Return the repository root (parent of the ``work_buddy`` package).

    This is the code/install anchor. In a clone it is the checkout root; the
    config dir, shipped-asset root, and data root all default to it (so a
    clone behaves as one undivided directory). Each of those can be pointed
    elsewhere independently — see :func:`config_dir`, :func:`asset_root`, and
    the ``WORK_BUDDY_DATA_DIR`` override in :func:`_data_base` — which is what
    lets work-buddy run when it is not a clone.
    """
    return Path(__file__).resolve().parent.parent


def install_root() -> Path:
    """Directory of the installed ``work_buddy`` package (where the code lives).

    Distinct from :func:`repo_root`, which is its parent. Under a wheel
    install this is ``site-packages/work_buddy``; it is the basis for
    resolving assets shipped *inside* the package.
    """
    from importlib.resources import files

    return Path(str(files("work_buddy")))


def config_dir() -> Path:
    """Directory holding ``config.yaml`` / ``config.local.yaml`` / ``.env``.

    Defaults to :func:`repo_root` (the clone layout, where config sits beside
    the package). Override with ``WORK_BUDDY_CONFIG_DIR`` to point at a
    per-user config directory in a packaged install. It cannot itself be read
    from config (that would be circular), so the only override is the env var.
    """
    env = os.environ.get("WORK_BUDDY_CONFIG_DIR")
    return Path(env).expanduser() if env else repo_root()


def asset_root() -> Path:
    """Root of work-buddy's shipped assets.

    These trees (``knowledge/store`` seed, ``prompts/``, ``sidecar_jobs/``,
    ``.claude/commands``, ``config.example*``, ``docs/``) live beside the
    ``work_buddy`` package in a clone. Resolution precedence: the
    ``WORK_BUDDY_ASSET_ROOT`` env var, then a ``paths.asset_root`` config
    value, then :func:`repo_root`.
    """
    env = os.environ.get("WORK_BUDDY_ASSET_ROOT")
    if env:
        return Path(env).expanduser()
    val = _load_paths_section().get("asset_root")
    if val:
        p = Path(val).expanduser()
        return p if p.is_absolute() else repo_root() / val
    return repo_root()


# Memoized (cache_key, paths-section) for the merged ``paths:`` config
# block. The parse is keyed on the config files' (config_dir, mtime)
# identity so a runtime edit (or a test that rewrites config under a
# monkeypatched root) still invalidates, while the common case skips the
# YAML parse entirely. This matters because ``resolve()`` / ``data_dir()``
# run on every DB connection open across the app; an uncached parse put
# ~60ms of YAML on every query (about 10s across one dashboard endpoint).
_paths_section_cache: tuple[tuple[str, ...], dict[str, Any]] | None = None


def _load_paths_section() -> dict[str, Any]:
    """Read the merged ``paths:`` config section without importing config.py.

    Reads ``config.yaml`` then overlays ``config.local.yaml`` from
    :func:`config_dir` so user-local overrides win. This mirrors how
    ``config.load_config()`` merges, but avoids the import: ``config.py``
    does not import ``paths``, so reading config here directly keeps that
    dependency one-way and cannot create a cycle.

    Returns an empty dict when nothing is set or PyYAML is missing. The
    result is memoized on the config files' mtimes (see
    ``_paths_section_cache``), so repeated calls on a hot path stat two
    files instead of re-parsing YAML.
    """
    global _paths_section_cache
    try:
        import yaml
    except ImportError:
        return {}

    root = config_dir()
    files: list[tuple[Path, int | None]] = []
    key_parts: list[str] = [str(root)]
    for name in ("config.yaml", "config.local.yaml"):
        path = root / name
        try:
            mtime: int | None = path.stat().st_mtime_ns
        except OSError:
            mtime = None
        files.append((path, mtime))
        key_parts.append(f"{name}:{mtime}")
    cache_key = tuple(key_parts)

    cached = _paths_section_cache
    if cached is not None and cached[0] == cache_key:
        return cached[1]

    paths_section: dict[str, Any] = {}
    for path, mtime in files:
        if mtime is None:
            continue
        try:
            with open(path) as f:
                cfg = yaml.safe_load(f) or {}
        except Exception:
            continue
        local_paths = cfg.get("paths") or {}
        if isinstance(local_paths, dict):
            paths_section.update(local_paths)
    _paths_section_cache = (cache_key, paths_section)
    return paths_section


def _load_data_root_from_config() -> str:
    """Return the configured ``paths.data_root`` (default ``"data"``)."""
    return _load_paths_section().get("data_root", "data")


def _data_base() -> Path:
    """Resolve the data-root directory, honoring overrides.

    Precedence: the ``WORK_BUDDY_DATA_DIR`` env var (an absolute per-user
    data dir for a packaged install), then the configured ``paths.data_root``
    (used as-is when absolute, else relative to :func:`repo_root`).
    """
    env = os.environ.get("WORK_BUDDY_DATA_DIR")
    if env:
        return Path(env).expanduser()
    raw = _load_data_root_from_config()
    return Path(raw) if Path(raw).is_absolute() else repo_root() / raw


def data_dir(category: str = "") -> Path:
    """Return ``<data_root>/[category]/``, creating it if needed.

    Parameters
    ----------
    category:
        Optional subdirectory under the data root (e.g. ``"context"``,
        ``"runtime"``).  Pass ``""`` to get the data root itself.
    """
    root = _data_base()
    target = root / category if category else root
    target.mkdir(parents=True, exist_ok=True)
    return target


def resolve(resource_id: str) -> Path:
    """Resolve a hierarchical resource ID to its file path.

    The resource must be registered in :data:`RESOURCES`.  The parent
    directory is created automatically.

    Examples::

        resolve("runtime/sidecar-pid")   # → <data_root>/runtime/sidecar.pid
        resolve("chrome/ledger")         # → <data_root>/chrome/tab_ledger.json
        resolve("db/messages")           # → <data_root>/db/messages.db

    Raises ``KeyError`` for unregistered IDs.
    """
    if resource_id not in RESOURCES:
        raise KeyError(
            f"Unknown resource ID: {resource_id!r}. "
            f"Register it in work_buddy.paths.RESOURCES first."
        )
    rel = RESOURCES[resource_id]
    root = _data_base()
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    return target
