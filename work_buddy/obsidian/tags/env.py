"""Tag Wrangler plugin (v0.6.4) + Obsidian metadataCache tag access via eval_js.

Read operations use Obsidian's metadataCache directly (no plugin dependency).
Mutation operations (rename, merge, tag pages) delegate to Tag Wrangler's
runtime, which handles vault-wide find-and-replace across both inline tags
and frontmatter.

Discovered runtime surface:
  Plugin prototype methods:
    - rename(oldTag, newTag)  — vault-wide tag rename via internal sr()
    - tagPage(tag)            — get the file associated with a tag page
    - createTagPage(tag)      — create a new note aliased to a tag
    - openTagPage(file)       — open a tag page in the editor
    - updatePage(file, meta)  — sync tag page aliases with pageAliases map

  Obsidian metadataCache:
    - getTags()               — all tags with occurrence counts
    - getFileCache(file)      — per-file metadata (inline tags, frontmatter)

  Note: Tag Wrangler has no public API object. All access is through
  prototype methods on the plugin instance.
"""

import json
from pathlib import Path
from typing import Any

from work_buddy.obsidian import bridge
from work_buddy.consent import requires_consent
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)

_JS_DIR = Path(__file__).parent / "_js"


def _load_js(name: str) -> str:
    """Load a JS snippet from the _js directory."""
    return (_JS_DIR / name).read_text(encoding="utf-8")


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
        raise RuntimeError(f"Tag Wrangler error: {result['error']}")
    return result


def _escape_js(text: str) -> str:
    """Escape a string for safe insertion into JS template placeholders."""
    return text.replace("\\", "\\\\").replace("'", "\\'").replace('"', '\\"').replace("\n", "\\n")


# ── Readiness ───────────────────────────────────────────────────


def check_ready() -> dict[str, Any]:
    """Check if Tag Wrangler plugin is loaded and functional.

    Returns a dict with:
    - ready: bool — True when the plugin is loaded
    - version: str — plugin version
    - has_rename: bool — rename capability available
    - tag_count: int — total tags in vault
    - reason: str — explanation if not ready
    """
    bridge.require_available()
    result = bridge.eval_js(_load_js("check_ready.js"), timeout=15)
    if result is None:
        return {"ready": False, "reason": "eval_js returned None"}
    if result.get("ready") and result.get("version"):
        from work_buddy.obsidian.plugin_versions import confirm_working
        confirm_working("tag-wrangler", result["version"])
    return result


# ── Read Operations (metadataCache — no Tag Wrangler dependency) ─


def get_all_tags(include_files: bool = False, file_limit: int = 10) -> list[dict]:
    """Get all vault tags with counts and hierarchy info.

    Args:
        include_files: If True, include a sample of file paths per tag.
        file_limit: Max files to include per tag (when include_files=True).

    Returns a list of dicts, each with:
    - tag: str — e.g. "#projects/ecg-cred"
    - count: int — total occurrences across vault
    - depth: int — nesting depth (1 = top-level)
    - parent: str | None — parent tag for nested tags
    - files: list[str] — file paths (only when include_files=True)
    """
    return _run_js("get_all_tags.js", {
        "__INCLUDE_FILES__": "true" if include_files else "false",
        "__LIMIT__": str(file_limit),
    }, timeout=30)


def get_tag_hierarchy() -> list[dict]:
    """Build a hierarchical tree of all vault tags.

    Returns a nested list where each node has:
    - name: str — tag segment name (e.g. "ecg-cred")
    - tag: str — full tag (e.g. "#projects/ecg-cred")
    - count: int — occurrences
    - depth: int — nesting level (0 = root)
    - child_count: int — number of direct children
    - children: list — nested child nodes (same structure)

    Sorted by count (descending) at each level.
    """
    return _run_js("get_tag_hierarchy.js", timeout=20)


def get_file_tags(file_path: str) -> dict:
    """Get all tags for a specific file.

    Args:
        file_path: Vault-relative path (e.g. "Journal/2026-04-04.md").

    Returns a dict with:
    - path: str — the file path
    - tags: list of dicts, each with:
      - tag: str — e.g. "#todo"
      - source: str — "inline" or "frontmatter"
      - line: int | None — line number (inline only)
      - col: int | None — column (inline only)
    """
    return _run_js("get_file_tags.js", {
        "__FILE_PATH__": _escape_js(file_path),
    })


def search_by_tag(
    tag: str,
    mode: str = "exact",
    limit: int = 100,
) -> dict:
    """Find all files containing a specific tag.

    Args:
        tag: Tag to search for (with or without #).
        mode: "exact" for exact match, "prefix" to match tag and all children
              (e.g. "#projects" matches "#projects/ecg-cred").
        limit: Maximum number of files to return.

    Returns a dict with:
    - query: str — the tag searched for
    - mode: str — "exact" or "prefix"
    - count: int — number of matching files
    - files: list of dicts with path and matched_tags
    """
    clean_tag = tag if tag.startswith("#") else "#" + tag
    return _run_js("search_by_tag.js", {
        "__TAG__": _escape_js(clean_tag),
        "__MODE__": mode,
        "__LIMIT__": str(limit),
    }, timeout=30)


# ── Mutation Operations (Tag Wrangler required) ─────────────────


@requires_consent(
    "tags.rename",
    reason="Rename a tag across all vault files (modifies file contents vault-wide)",
    risk="high",
    default_ttl=30,
)
def rename_tag(old_tag: str, new_tag: str) -> dict:
    """Rename a tag across the entire vault.

    Uses Tag Wrangler's rename method, which handles both inline tags
    and frontmatter occurrences in all files.

    Args:
        old_tag: Current tag name (with or without #).
        new_tag: New tag name (with or without #).

    Returns a dict with:
    - success: bool
    - old_tag: str — the original tag
    - new_tag: str — the new tag

    Raises RuntimeError if the tag doesn't exist or rename fails.
    """
    old_clean = old_tag.lstrip("#")
    new_clean = new_tag.lstrip("#")
    logger.info("Renaming tag #%s -> #%s", old_clean, new_clean)
    return _run_js("rename_tag.js", {
        "__OLD_TAG__": _escape_js(old_clean),
        "__NEW_TAG__": _escape_js(new_clean),
    }, timeout=30)


@requires_consent(
    "tags.merge",
    reason="Merge one tag into another (all occurrences of the source tag will be replaced vault-wide)",
    risk="high",
    default_ttl=30,
)
def merge_tags(source_tag: str, target_tag: str) -> dict:
    """Merge one tag into another across the entire vault.

    All occurrences of source_tag will be replaced with target_tag.
    This is a vault-wide operation that modifies file contents.

    Args:
        source_tag: Tag to merge FROM (will be removed). With or without #.
        target_tag: Tag to merge INTO (will remain). With or without #.

    Returns a dict with:
    - success: bool
    - source_tag: str
    - target_tag: str
    - source_occurrences_merged: int — how many occurrences were rewritten
    - target_original_count: int — original count of target tag
    """
    src = source_tag.lstrip("#")
    tgt = target_tag.lstrip("#")
    logger.info("Merging tag #%s -> #%s", src, tgt)
    return _run_js("merge_tags.js", {
        "__SOURCE_TAG__": _escape_js(src),
        "__TARGET_TAG__": _escape_js(tgt),
    }, timeout=30)


def get_tag_page(tag: str) -> dict:
    """Get the tag page associated with a tag, if one exists.

    Tag pages are Obsidian notes that have a tag as a frontmatter alias,
    managed by Tag Wrangler.

    Args:
        tag: Tag name (with or without #).

    Returns a dict with:
    - exists: bool
    - path: str | None — file path if exists
    - tag: str
    """
    clean = tag.lstrip("#")
    return _run_js("tag_page.js", {
        "__TAG__": _escape_js(clean),
        "__ACTION__": "get",
    })


@requires_consent(
    "tags.create_page",
    reason="Create a new tag page in the vault (creates a new markdown file)",
    risk="moderate",
    default_ttl=30,
)
def create_tag_page(tag: str) -> dict:
    """Create a tag page for a tag.

    Creates an Obsidian note with the tag as a frontmatter alias.
    If a tag page already exists, returns its info without creating.

    Args:
        tag: Tag name (with or without #).

    Returns a dict with:
    - exists: bool — True (always, after creation)
    - created: bool — True if newly created
    - path: str — file path of the tag page
    - tag: str
    """
    clean = tag.lstrip("#")
    logger.info("Creating tag page for #%s", clean)
    return _run_js("tag_page.js", {
        "__TAG__": _escape_js(clean),
        "__ACTION__": "create",
    }, timeout=15)
