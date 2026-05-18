---
name: Context Review Directions
kind: directions
description: How to review an existing context bundle — freshness check, same synthesis rules as context-collect, no re-collection
summary: 'Follow review-latest-bundle workflow to find and check freshness of the most recent bundle. Synthesize using same rules as context-collect: priority order, extract signal, cross-reference contracts, suggest one next action, max 10-15 lines.'
trigger: user wants to orient from an existing context bundle without re-collecting
command: wb-context-review
workflow: context/review-latest-bundle
capabilities:
- context/context_bundle
- contracts/active_contracts
tags:
- context
- bundle
- review
- orientation
- synthesis
- directions
aliases:
- review context
- review bundle
- check context
- latest bundle
- orient from existing bundle
parents:
- context
---

Follow the review-latest-bundle workflow to find the latest bundle and check freshness. Then synthesize using the same rules as /wb-context-collect -- the behavioral rules are identical; only the data source differs (existing bundle vs fresh collection).

<<wb:context/collect-directions>>

## When to prefer this over collect-and-orient
- The user just ran the collector manually and wants the results interpreted
- You're mid-conversation and need a quick check -- re-collecting would break flow
- The bundle is less than a few hours old and conditions haven't changed much
