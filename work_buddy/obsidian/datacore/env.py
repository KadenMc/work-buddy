"""Datacore plugin (v0.1.29) runtime access via eval_js.

All functions require Obsidian to be running with the work-buddy plugin active.
Datacore must be installed and its index initialized.

Discovered runtime surface (probed 2026-04-09):
- plugin.api / window.datacore — public API instance
- api.query(str) — returns array of result objects
- api.fullquery(str) — returns {query, results, duration, revision}
- api.tryQuery(str) — returns Result with {value, successful}
- api.tryParseQuery(str) — validates query syntax
- api.page(path) — returns page object with json() method
- api.evaluate(expr) — evaluates Datacore expressions

Object types: @page, @section, @block, @codeblock, @list-item, @task
Result objects have circular parent refs — must serialize via json() or manual flattening.
Page.json() works cleanly; non-page objects need manual serialization.
"""

from pathlib import Path
from typing import Any

from work_buddy.obsidian import bridge
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)

_JS_DIR = Path(__file__).parent / "_js"


def _load_js(name: str) -> str:
    """Load a JS snippet from the _js directory."""
    return (_JS_DIR / name).read_text(encoding="utf-8")


def _escape_js(text: str) -> str:
    """Escape text for safe insertion into a JS template literal."""
    return (
        text.replace("\\", "\\\\")
        .replace("`", "\\`")
        .replace("${", "\\${")
    )


def _run_js(
    snippet_name: str,
    replacements: dict[str, str] | None = None,
    timeout: int = 15,
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
        raise RuntimeError(f"Datacore error: {result['error']}")
    return result


# ── Readiness ───────────────────────────────────────────────────


def check_ready() -> dict[str, Any]:
    """Check if Datacore is installed, initialized, and queryable.

    Returns a dict with:
    - ready: bool
    - version: str — plugin version
    - initialized: bool — whether the index is built
    - revision: int — current index revision
    - counts: dict — sampled object counts (@page, @section, @task)
    """
    bridge.require_available()
    result = bridge.eval_js(_load_js("check_ready.js"), timeout=15)
    if result is None:
        return {"ready": False, "reason": "eval_js returned None"}
    if result.get("ready") and result.get("version"):
        from work_buddy.obsidian.plugin_versions import confirm_working

        confirm_working("datacore", result["version"])
    return result


# ── Query ───────────────────────────────────────────────────────


def validate_query(query: str) -> dict[str, Any]:
    """Validate a Datacore query string without executing it.

    Returns:
        Dict with 'valid' (bool) and either 'parsed' or 'error'.
    """
    return _run_js(
        "validate_query.js",
        {"__QUERY__": _escape_js(query)},
        timeout=10,
    )


def query(
    query_str: str,
    fields: list[str] | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Execute a Datacore query and return serialized results.

    Args:
        query_str: Datacore query (e.g. '@page and path("journal")').
        fields: Optional list of fields to include. None = all default fields.
        limit: Maximum results to return.

    Returns:
        Dict with 'total', 'returned', and 'results' (list of dicts).
    """
    return _run_js(
        "query.js",
        {
            "__QUERY__": _escape_js(query_str),
            "__FIELDS__": ",".join(fields) if fields else "",
            "__LIMIT__": str(limit),
        },
        timeout=15,
    )


def fullquery(
    query_str: str,
    fields: list[str] | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Execute a Datacore fullquery with timing and revision metadata.

    Args:
        query_str: Datacore query string.
        fields: Optional list of fields to include.
        limit: Maximum results to return.

    Returns:
        Dict with 'total', 'returned', 'duration_s', 'revision', 'results'.
    """
    return _run_js(
        "fullquery.js",
        {
            "__QUERY__": _escape_js(query_str),
            "__FIELDS__": ",".join(fields) if fields else "",
            "__LIMIT__": str(limit),
        },
        timeout=15,
    )


def get_page(
    path: str,
    fields: list[str] | None = None,
) -> dict[str, Any]:
    """Get a single page by vault-relative path.

    Args:
        path: Vault-relative path (e.g. 'journal/2026-04-09.md').
        fields: Optional list of fields to include.

    Returns:
        Serialized page dict with sections, frontmatter, tags, links, etc.
    """
    return _run_js(
        "get_page.js",
        {
            "__PATH__": _escape_js(path),
            "__FIELDS__": ",".join(fields) if fields else "",
        },
        timeout=10,
    )


def evaluate(
    expression: str,
    source_path: str | None = None,
) -> dict[str, Any]:
    """Evaluate a Datacore expression.

    Args:
        expression: Datacore expression (e.g. '1 + 2' or 'this.$tags').
        source_path: Optional vault path to use as 'this' context.

    Returns:
        Dict with 'result' key containing the evaluated value.
    """
    return _run_js(
        "evaluate.js",
        {
            "__EXPRESSION__": _escape_js(expression),
            "__SOURCE_PATH__": _escape_js(source_path) if source_path else "",
        },
        timeout=10,
    )


# ── Schema introspection ───────────────────────────────────────


def schema_summary(sample_limit: int = 200) -> dict[str, Any]:
    """Summarize the vault's Datacore schema.

    Samples pages to discover common tags, frontmatter keys, path prefixes,
    and object type counts.

    Args:
        sample_limit: Maximum pages to sample for tag/field discovery.

    Returns:
        Dict with 'object_types', 'top_tags', 'frontmatter_keys',
        'path_prefixes', 'task_statuses'.
    """
    return _run_js(
        "schema_summary.js",
        {"__SAMPLE_LIMIT__": str(sample_limit)},
        timeout=20,
    )
