---
name: Web Search Health
kind: capability
description: Report which web-search backend is active (first usable in the routing order — ddgs is keyless, Jina needs a key) and its readiness. Use to distinguish "no backend usable" from "Jina key missing, falling back to ddgs".
capability_name: web_search_health
category: websearch
op: op.wb.web_search_health
schema_version: wb-capability/v1
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
requires: []
---
