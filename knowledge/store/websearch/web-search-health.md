---
name: Web Search Health
kind: skill
description: Report which web-search backend is active (first usable in the routing order — ddgs is keyless, Jina needs a key) and its readiness. Use to distinguish "no backend usable" from "Jina key missing, falling back to ddgs".
category: websearch
op: op.wb.web_search_health
schema_version: wb-skill/v1
skill_name: web_search_health
tags:
- websearch
- health
aliases:
- web search health
- is web search working
- websearch backend status
- which search backend is active
parents:
- websearch
---
