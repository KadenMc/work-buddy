"""Obsidian tag operations via Tag Wrangler plugin + metadataCache.

Read operations (no plugin dependency):
    get_all_tags        — all vault tags with counts, hierarchy, optional file lists
    get_tag_hierarchy   — hierarchical tree of all tags
    get_file_tags       — all tags in a specific file
    search_by_tag       — find files by tag (exact or prefix)

Mutation operations (requires Tag Wrangler):
    rename_tag          — vault-wide tag rename (consent-gated)
    merge_tags          — merge one tag into another (consent-gated)
    get_tag_page        — check if a tag page exists
    create_tag_page     — create a tag page note (consent-gated)

Readiness:
    check_ready         — verify Tag Wrangler is loaded
"""

from work_buddy.obsidian.tags.env import (
    check_ready,
    create_tag_page,
    get_all_tags,
    get_file_tags,
    get_tag_hierarchy,
    get_tag_page,
    merge_tags,
    rename_tag,
    search_by_tag,
)

__all__ = [
    "check_ready",
    "create_tag_page",
    "get_all_tags",
    "get_file_tags",
    "get_tag_hierarchy",
    "get_tag_page",
    "merge_tags",
    "rename_tag",
    "search_by_tag",
]
