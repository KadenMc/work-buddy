---
name: Chrome Content
kind: capability
description: Extract full page text from currently-open Chrome tabs. Filter by domain or title substring, or get top-engagement tabs. Free — no LLM calls. Use for single-tab inspection or reading specific page content.
capability_name: chrome_content
category: context
op: op.wb.chrome_content
schema_version: wb-capability/v1
parameters:
  tab_filter:
    type: str
    description: Domain or title substring to match (e.g., 'github', 'obsidian'). If not set, returns top-engagement tabs.
    required: false
  tab_limit:
    type: int
    description: Max tabs to extract (default 5)
    required: false
  max_chars:
    type: int
    description: Max characters per tab (default 5000)
    required: false
tags:
- context
- chrome
- content
aliases:
- page text
- tab content
- read tab
- extract tab text
- what's on this tab
- show tab content
parents:
- context
requires:
- chrome_extension
---
