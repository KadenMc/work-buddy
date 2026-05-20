---
name: Chrome Infer
kind: capability
description: Infer what the user is working on by reading page content from engaged Chrome tabs and analyzing with Haiku. Evaluates provided theories against actual page evidence. Caches results per tab to avoid redundant API calls. ~$0.001/call.
capability_name: chrome_infer
category: context
op: op.wb.chrome_infer
schema_version: wb-capability/v1
parameters:
  since:
    type: str
    description: 'Lookback window. Relative (''1h'', ''30m'') or ISO datetime. Default: 1h.'
    required: false
  theories:
    type: str
    description: Comma-separated theories to evaluate (e.g., 'researching pricing, writing code')
    required: false
  tab_limit:
    type: int
    description: Max tabs to analyze (default 5)
    required: false
tags:
- context
- chrome
- infer
aliases:
- what am I working on
- browsing analysis
- page content analysis
- infer activity from tabs
- chrome page content
- what is the user doing
parents:
- context
requires:
- chrome_extension
---
