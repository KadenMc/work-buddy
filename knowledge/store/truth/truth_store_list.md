---
name: Truth Store List
kind: skill
description: List registered Truth stores with their canonical paths, profiles, titles, last-seen times, and reachability.
category: truth
op: op.wb.truth_store_list
schema_version: wb-skill/v1
parameters:
  refresh:
    type: bool
    description: Reopen and validate each registered store before returning it. Default true.
    required: false
skill_name: truth_store_list
tags:
- truth
- store
- list
- registry
aliases:
- list truth stores
- find truth ledgers
- truth store registry
- known evidence stores
- show claim stores
parents:
- truth
---
