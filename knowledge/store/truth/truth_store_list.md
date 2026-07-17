---
name: Truth Store List
kind: capability
description: List registered Truth stores with their canonical paths, profiles, titles, last-seen times, and reachability.
capability_name: truth_store_list
category: truth
op: op.wb.truth_store_list
schema_version: wb-capability/v1
parameters:
  refresh:
    type: bool
    description: Reopen and validate each registered store before returning it. Default true.
    required: false
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
