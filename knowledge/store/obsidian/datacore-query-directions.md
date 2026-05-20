---
name: Datacore Query Directions
kind: directions
description: How to translate user intent into Datacore vault queries — schema-first, decomposition, plan vs raw query, validate-and-repair
summary: 'Always run datacore_schema first. Decompose the request: identify object type, map phrases to schema primitives, identify containment, surface ambiguities. Prefer datacore_run_plan over raw queries. Validate results, attempt at most one repair.'
trigger: user wants to search the vault structurally using Datacore (pages, tasks, sections, blocks, containment, tags, frontmatter)
command: wb-datacore-query
capabilities:
- context/datacore_schema
- context/datacore_run_plan
- context/datacore_query
- context/datacore_fullquery
- context/datacore_validate
- context/datacore_get_page
- context/datacore_evaluate
- context/datacore_compile_plan
tags:
- obsidian
- datacore
- vault
- query
- structural-search
- directions
aliases:
- datacore query
- vault query
- structural vault search
- query obsidian
- find vault pages
parents:
- obsidian
- obsidian
---

Query the Obsidian vault structurally using Datacore.

## Step 1: Discover the schema (mandatory)

Always fetch the vault schema first:
mcp__work-buddy__wb_run("datacore_schema")

This returns object type counts, top tags, frontmatter keys, and path prefixes. Do not skip this -- guessing at tag names or paths is the #1 cause of empty results.

For diagnostic-grade reconnaissance (frontmatter state machines, tag-family trees, cross-tabs by type/path/status), prefer `vault_recon` -- see `vault/recon-directions`.

## Step 2: Decompose the request

1. Identify the target object type (pages, tasks, sections, blocks)
2. Map the user's language to Datacore primitives using the schema
3. Identify containment relationships (childof, parentof)
4. Check for ambiguity -- surface both interpretations and ask before executing

## Step 3: Build and execute

Preferred: use datacore_run_plan with a structured plan JSON.
Direct: use datacore_query with raw Datacore syntax.

Plan keys: target, path, tags, tags_any, status, text_contains, frontmatter, child_of, parent, exists, expressions, negate.

## Step 4: Validate and repair

1. Error: fix syntax, retry once
2. Zero results: check tag names against schema, relax one condition, retry. State what changed.
3. >200 results: suggest tightening
4. Results look right: proceed to presentation

Do at most one repair attempt.

## Step 5: Present results

- Show the compiled query
- Format as concise table/list appropriate to object type
- Highlight anything surprising

## Object types and fields

@page: $path, $frontmatter, $tags, $links, $ctime, $mtime, $size, $sections
@task: $text, $status, $file, $tags, $parentLine, $symbol
@section: $title, $level, $ordinal, $tags
@block: $type, $ordinal, $tags, $infields
@list-item: $text, $symbol, $tags, $file
@codeblock: $type, $ordinal

## Serialization

Datacore result objects have circular parent/child references. The JS snippets handle this:
- Page objects: Use page.json() which produces clean JSON. Sections flattened to summaries.
- Non-page objects (tasks, list-items, blocks): Manual serialization — primitive fields kept, arrays truncated, objects stringified.
- Timestamps: $ctime/$mtime come as epoch ms from json() or Luxon DateTime strings from value().

## Known gotchas

- path() works on @page only, not @task -- use $file for tasks
- Timestamps are Luxon DateTimes -- use date("YYYY-MM-DD") for comparisons
- Result objects have circular references -- sections are flattened to summaries
- Bridge latency: Each call goes through HTTP bridge (~5s round-trip). Not suitable for high-frequency polling.
- tryQuery/tryFullQuery: The Result wrapper uses {value, successful} not {ok}.
- fields() method: Not available on query result objects. Use value('$fieldname') instead.
- Large result sets: Always use limit parameter. The full vault has 200k+ blocks.

## Don'ts
- Don't skip schema discovery
- Don't import work_buddy.* modules -- use MCP capabilities
- Don't dump raw JSON -- format results for readability
- Don't run queries without field selection on large result sets
- Don't silently pick one interpretation when ambiguous -- ask
- Don't retry more than once on failure
