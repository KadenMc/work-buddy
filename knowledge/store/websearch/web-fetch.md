---
name: Web Fetch
kind: capability
description: Fetch a URL and extract clean readable text (Jina r.jina.ai reader when a key is configured, else trafilatura). Best-effort — an unreachable page returns ok with empty text rather than an error. Use to pull the body of a specific page (e.g. one of web_search's hits).
capability_name: web_fetch
category: websearch
op: op.wb.web_fetch
schema_version: wb-capability/v1
parameters:
  url:
    type: str
    description: The page URL to fetch and extract.
    required: true
tags:
- websearch
- fetch
- extract
aliases:
- fetch a web page
- extract page text
- read this url
- get page content
- scrape a page
parents:
- websearch
requires: []
---
