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
from work_buddy.consent import reduces_risk_for
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


@reduces_risk_for("obsidian.eval_js", "low")
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


@reduces_risk_for("obsidian.eval_js", "low")
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


@reduces_risk_for("obsidian.eval_js", "low")
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


@reduces_risk_for("obsidian.eval_js", "low")
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


@reduces_risk_for("obsidian.eval_js", "low")
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


@reduces_risk_for("obsidian.eval_js", "low")
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


@reduces_risk_for("obsidian.eval_js", "low")
def schema_summary(sample_limit: int = 200) -> dict[str, Any]:
    """Summarize the vault's Datacore schema.

    Walks all pages to count tags, frontmatter keys, and path prefixes.

    Args:
        sample_limit: Retained for API compatibility; ignored. Earlier versions
            stride-sampled pages, which undercounted by ~36x on a 6k-page vault.

    Returns:
        Dict with 'object_types', 'top_tags', 'frontmatter_keys',
        'path_prefixes', 'task_statuses', 'pages_sampled', 'pages_total'.
        ``pages_sampled`` equals ``pages_total`` (full walk).
    """
    return _run_js(
        "schema_summary.js",
        {"__SAMPLE_LIMIT__": str(sample_limit)},
        timeout=60,
    )


@reduces_risk_for("obsidian.eval_js", "low")
def vault_recon(
    path_prefix: str | None = None,
    activity_days: int = 30,
    timeout: int = 90,
) -> dict[str, Any]:
    """Diagnostic-grade vault reconnaissance.

    Single page walk that produces cross-tabs an agent can reason over to
    spot recurring conventions: frontmatter state machines (type x status),
    tag families (depth-3 tree), path-by-type distribution, recent activity
    by region. Cardinality caps prevent UUID-style frontmatter and timestamp
    leaves from drowning the result.

    Args:
        path_prefix: Optional vault-relative path prefix (e.g. "repos/electricrag/")
            to scope the walk. ``None`` walks the full vault.
        activity_days: Lookback window for ``recent_activity_by_path`` (default 30).
        timeout: Bridge timeout. 90s safety margin against bridge spikes; the
            actual JS work over a 6k-page vault runs in <1s.

    Returns:
        Dict with snapshot_ts, object_types, pages_total, pages_walked,
        top_tags, frontmatter_keys, frontmatter_values (capped), path_prefixes,
        tag_tree (depth 3), type_by_status, path_by_type, recent_activity_by_path,
        high_cardinality_keys, task_statuses, tasks_total.
    """
    return _run_js(
        "vault_recon.js",
        {
            "__PATH_PREFIX__": _escape_js(path_prefix) if path_prefix else "",
            "__ACTIVITY_DAYS__": str(activity_days),
        },
        timeout=timeout,
    )
