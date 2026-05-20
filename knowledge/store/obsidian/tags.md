---
name: Tag Wrangler Integration
kind: integration
description: Tag operations via Tag Wrangler plugin (v0.6.4) + metadataCache -- read, rename, merge, tag pages
tags:
- obsidian
- tags
- tag-wrangler
- metadataCache
- eval_js
aliases:
- tag wrangler
- tags plugin
- tag operations
- tag rename
- tag merge
parents:
- obsidian
- obsidian
---

Tag operations via Tag Wrangler plugin (v0.6.4) + Obsidian metadataCache.

## Two Paths

- Read operations: use app.metadataCache directly -- no plugin dependency. Work even if Tag Wrangler is disabled.
- Mutation operations: delegate to Tag Wrangler prototype methods for vault-wide find-and-replace across inline tags and frontmatter.

## MetadataCache Methods

getTags() -> all tags with occurrence counts (dict). getFileCache(file) -> per-file inline tags (with positions), frontmatter, links. Note: getTags() counts occurrences not unique files.

## Tag Wrangler Methods

rename(oldTag, newTag?) -> vault-wide tag rename. tagPage(tag) -> get TFile for tag page. createTagPage(tag, newLeaf?) -> create note aliased to tag. Events: tag-page:will-create, tag-page:did-create, tag-wrangler:contextmenu.

## Python API

Read: check_ready, get_all_tags, get_tag_hierarchy, get_file_tags, search_by_tag.
Mutations (consent-gated): rename_tag, merge_tags, get_tag_page, create_tag_page.

## Relationship to bridge.py

bridge.get_tags() and bridge.get_tag_files() use Local REST API plugin /tags endpoint. Simpler (no eval_js), read-only, slightly different normalization. Both paths maintained.

## Stale Warnings

- Tag Wrangler v0.6.4 minified source, no public API. rename delegates to closure-captured sr() function.
- metadataCache is an Obsidian internal API. Field names stable but not formally guaranteed.

<<wb:obsidian/bridge>>
