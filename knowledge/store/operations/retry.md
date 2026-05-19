---
name: Retry
kind: capability
description: Retry a previously recorded operation by its ID. Use wb_status() to discover recent/pending operations after a timeout. Operations with retry_policy='manual' cannot be auto-retried. Operations with an active execution lease will be refused to prevent double-dispatch.
capability_name: retry
category: operations
op: op.wb.retry
schema_version: wb-capability/v1
parameters:
  operation_id:
    type: str
    required: true
    description: The operation ID from a wb_run response or wb_status output
mutates_state: true
retry_policy: manual
tags:
- operations
- retry
aliases:
- retry
- replay
- re-run
- retry operation
parents:
- operations
---
