"""Smart Environment runtime access via eval_js.

All functions require Obsidian to be running with the work-buddy plugin active.
The Smart Connections plugin must be installed and loaded.
"""

import json
from pathlib import Path
from typing import Any

from work_buddy.obsidian import bridge
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)

_JS_DIR = Path(__file__).parent / "_js"


def _load_js(name: str) -> str:
    """Load a JS snippet from the _js directory."""
    path = _JS_DIR / name
    return path.read_text(encoding="utf-8")


def _run_js(
    snippet_name: str,
    replacements: dict[str, str] | None = None,
    timeout: int = 30,
) -> Any:
    """Load a JS snippet, apply replacements, execute via eval_js.

    Raises RuntimeError if the bridge is unavailable or the JS returns an error.
    """
    bridge.require_available()
    js = _load_js(snippet_name)
    for placeholder, value in (replacements or {}).items():
        js = js.replace(placeholder, value)
    result = bridge.eval_js(js, timeout=timeout)
    if isinstance(result, dict) and "error" in result:
        raise RuntimeError(f"Smart Environment error: {result['error']}")
    return result


# ── Readiness & Health ───────────────────────────────────────────


def check_ready() -> dict[str, Any]:
    """Check if SmartEnv is fully loaded and ready for use.

    Returns a dict with:
    - ready: bool — True when state='loaded' and collections are populated
    - state: str — 'loading' | 'loaded' | etc.
    - sources_count, blocks_count: int — number of indexed items
    - plugins_loaded/plugins_total: int — plugin loading progress
    - memory: dict — heap/RSS memory stats (if available)
    - performance_memory: dict — JS heap stats (if available)

    Use this before calling other Smart functions after Obsidian starts.
    SmartEnv loading can take 60-90+ seconds after Obsidian opens.
    """
    bridge.require_available()
    result = bridge.eval_js(_load_js("check_ready.js"), timeout=30)
    if result is None:
        return {"ready": False, "reason": "eval_js returned None (bridge may be overloaded)"}
    if result.get("ready"):
        from work_buddy.obsidian.plugin_versions import confirm_working
        # Smart Connections doesn't expose version via SmartEnv — read from manifest
        from work_buddy.obsidian.plugins import installed_plugins
        from work_buddy.config import load_config
        from pathlib import Path
        vault = Path(load_config()["vault_root"])
        info = installed_plugins(vault).get("smart-connections", {})
        if info.get("version"):
            confirm_working("smart-connections", info["version"])
    return result


def wait_until_ready(timeout_seconds: int = 180, poll_interval: int = 5) -> dict[str, Any]:
    """Block until SmartEnv is fully loaded, or timeout.

    Args:
        timeout_seconds: Maximum time to wait (default 3 minutes).
        poll_interval: Seconds between checks.

    Returns:
        The final check_ready() result.

    Raises:
        TimeoutError if SmartEnv doesn't load within the timeout.
    """
    import time

    deadline = time.time() + timeout_seconds
    last_result = None

    while time.time() < deadline:
        try:
            if not bridge.is_available():
                logger.debug("Bridge not available, waiting...")
                time.sleep(poll_interval)
                continue
            last_result = check_ready()
            if last_result.get("ready") and last_result.get("sources_count", 0) > 0:
                return last_result
            logger.debug(
                "SmartEnv loading: state=%s sources=%s blocks=%s",
                last_result.get("state"),
                last_result.get("sources_count"),
                last_result.get("blocks_count"),
            )
        except Exception as e:
            logger.debug("check_ready failed: %s", e)
        time.sleep(poll_interval)

    raise TimeoutError(
        f"SmartEnv did not load within {timeout_seconds}s. "
        f"Last state: {last_result}"
    )


# ── Diagnostics & Console ────────────────────────────────────────


def install_console_capture() -> str:
    """Install a console interception buffer inside Obsidian.

    Captures console.log/warn/error/debug from ALL plugins into a ring buffer
    (max 500 entries). Idempotent — safe to call multiple times.
    Original console output is preserved (still visible in dev tools).

    Returns 'installed' or 'already installed'.
    """
    return _run_js("install_console_capture.js")


def drain_console(
    since_ts: int = 0,
    level: str = "all",
) -> list[dict[str, Any]]:
    """Drain buffered console messages from Obsidian.

    Requires install_console_capture() to have been called first.

    Args:
        since_ts: Only return messages after this epoch timestamp (ms). 0 = all.
        level: 'all', 'error', 'warn', 'log', or 'debug'.

    Returns:
        List of {ts, level, msg} dicts.
    """
    return _run_js(
        "drain_console.js",
        {"__SINCE_TS__": str(since_ts), "__LEVEL_FILTER__": level},
    )


# ── Tier 1: Core ─────────────────────────────────────────────────


def embed_text(text: str) -> list[float]:
    """Embed arbitrary text using Smart Connections' live Transformers.js model.

    Returns a 384-dimensional vector compatible with the vault embedding index.
    Uses the same model Smart Connections uses, so vectors are always compatible.

    Args:
        text: The text to embed.

    Returns:
        List of 384 floats.
    """
    # Escape for JS string literal
    escaped = text.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")
    result = _run_js("embed.js", {"__TEXT__": escaped}, timeout=45)
    return result["vec"]


def embed_batch(texts: list[str]) -> list[dict[str, Any]]:
    """Embed multiple texts in a single batch call.

    More efficient than calling embed_text() in a loop.

    Args:
        texts: List of strings to embed.

    Returns:
        List of dicts with 'vec' (list[float]) and 'tokens' (int) per input.
    """
    inputs_json = json.dumps(texts)
    result = _run_js("embed_batch.js", {"__INPUTS_JSON__": inputs_json}, timeout=60)
    return result


def semantic_search(
    query: str,
    limit: int = 10,
    collection: str = "smart_blocks",
) -> list[dict[str, Any]]:
    """Semantic search over the vault using Smart Connections' embeddings.

    Embeds the query text using their model, then finds nearest items
    by cosine similarity across all indexed vault content.

    Args:
        query: Natural language search query.
        limit: Maximum number of results (default 10).
        collection: 'smart_blocks' (heading-level, default) or 'smart_sources' (file-level).

    Returns:
        List of dicts with 'key' (vault path + heading) and 'score' (cosine similarity).
    """
    escaped = query.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")
    result = _run_js(
        "semantic_search.js",
        {"__TEXT__": escaped, "__LIMIT__": str(limit), "__COLLECTION__": collection},
        timeout=60,
    )
    return result["results"]


def lookup_context(
    queries: list[str],
    limit: int = 10,
) -> dict[str, Any]:
    """High-level semantic context retrieval via Smart Chat's lookup_context action.

    This is Smart Chat's own context gathering pipeline. It:
    1. Embeds each query as a "hypothetical document"
    2. Searches for nearest items with score merging across queries
    3. Creates a SmartContext pack with the results

    More sophisticated than raw semantic_search — supports multi-query fusion,
    filtering, and LookupLists.

    Args:
        queries: List of query/hypothetical strings.
        limit: Maximum number of results.

    Returns:
        Dict with 'context_key' (SmartContext ID) and 'items' (list of {key, score}).
    """
    hypo_json = json.dumps(queries)
    return _run_js(
        "lookup_context.js",
        {"__HYPOTHETICALS_JSON__": hypo_json, "__LIMIT__": str(limit)},
        timeout=120,
    )


# ── Tier 2: Context & Navigation ─────────────────────────────────


def create_context_pack(item_keys: list[str]) -> dict[str, Any]:
    """Create a SmartContext pack from a list of vault item keys.

    The created context appears in Smart Context's UI and can be reused.

    Args:
        item_keys: List of SmartSource/SmartBlock keys (e.g. 'journal/2026-04-03.md').

    Returns:
        Dict with 'context_key' and 'item_count'.
    """
    keys_json = json.dumps(item_keys)
    return _run_js("create_context_pack.js", {"__ITEM_KEYS_JSON__": keys_json})


def get_item_content(key: str) -> dict[str, Any]:
    """Read the content of a vault item by its SmartSource/SmartBlock key.

    Tries multiple methods: get_as_context(), read(), data.content, vault read.

    Args:
        key: Item key (e.g. 'journal/2026-04-03.md' or 'file.md#Heading').

    Returns:
        Dict with 'key', 'path', 'content' (str or None), 'collection', 'has_content'.
    """
    escaped = key.replace("\\", "\\\\").replace("'", "\\'")
    return _run_js("get_item_content.js", {"__KEY__": escaped}, timeout=30)


def get_workspace_context(semantic_limit: int = 5) -> dict[str, Any]:
    """Get the current workspace state plus semantic neighbors of the active file.

    Combines the bridge's workspace endpoint with semantic search on the active note.

    Returns:
        Dict with 'workspace' (active file, open files) and 'related' (semantic neighbors).
    """
    workspace = bridge.get_workspace()
    if not workspace:
        raise RuntimeError("Workspace unavailable — is Obsidian running?")

    active = workspace.get("active_file")
    related = []
    if active:
        try:
            related = find_related(active, limit=semantic_limit, collection="smart_blocks")
        except RuntimeError:
            logger.debug("Could not find related items for active file: %s", active)

    return {"workspace": workspace, "related": related}


# ── Tier 3: Advanced ─────────────────────────────────────────────


def hybrid_search(query: str, limit: int = 10) -> list[dict[str, Any]]:
    """Combined semantic + lexical search with reciprocal rank fusion.

    Runs Smart Connections (semantic) and Omnisearch (lexical) in parallel,
    then fuses results using RRF scoring to get the best of both.

    Args:
        query: Search query.
        limit: Maximum number of results.

    Returns:
        List of dicts with 'path', 'rrf' (fused score), and optionally
        'semantic_score', 'lexical_score', 'excerpt', 'block_key'.
    """
    escaped = query.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")
    result = _run_js(
        "hybrid_search.js",
        {"__TEXT__": escaped, "__LIMIT__": str(limit)},
        timeout=90,
    )
    return result["results"]


def search_with_filter(
    query: str,
    limit: int = 10,
    collection: str = "smart_blocks",
    folders: list[str] | None = None,
    tags: list[str] | None = None,
    exclude_folders: list[str] | None = None,
    exclude_keys: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Semantic search with folder/tag/exclusion filters.

    Args:
        query: Search query.
        limit: Maximum results.
        collection: 'smart_blocks' or 'smart_sources'.
        folders: Only include items under these folder prefixes.
        tags: Only include items with these tags.
        exclude_folders: Exclude items under these folder prefixes.
        exclude_keys: Exclude specific item keys.

    Returns:
        List of dicts with 'key' and 'score'.
    """
    escaped = query.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")
    filter_obj = {
        "folders": folders or [],
        "tags": tags or [],
        "exclude_folders": exclude_folders or [],
        "exclude_keys": exclude_keys or [],
    }
    result = _run_js(
        "search_with_filter.js",
        {
            "__TEXT__": escaped,
            "__LIMIT__": str(limit),
            "__COLLECTION__": collection,
            "__FILTER_JSON__": json.dumps(filter_obj),
        },
        timeout=60,
    )
    return result["results"]


def find_related(
    vault_path: str,
    limit: int = 10,
    collection: str = "smart_blocks",
) -> list[dict[str, Any]]:
    """Find items semantically similar to a specific vault file.

    Uses the file's existing embedding — no new embedding call needed.

    Args:
        vault_path: Vault-relative file path (e.g. 'journal/2026-04-03.md').
        limit: Maximum results.
        collection: 'smart_blocks' or 'smart_sources'.

    Returns:
        List of dicts with 'key' and 'score'.
    """
    escaped = vault_path.replace("\\", "\\\\").replace("'", "\\'")
    result = _run_js(
        "find_related.js",
        {"__KEY__": escaped, "__LIMIT__": str(limit), "__COLLECTION__": collection},
        timeout=30,
    )
    return result["results"]


def monitor_model_config() -> dict[str, Any]:
    """Report the current embedding model configuration and index stats.

    Use to detect model changes, check index health, and verify Pro status.

    Returns:
        Dict with model_key, dims, provider_key, adapter_type, counts, is_pro, etc.
    """
    return _run_js("monitor_model_config.js")
