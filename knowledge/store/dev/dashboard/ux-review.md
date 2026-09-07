---
name: Dashboard UX Review
kind: workflow
description: Record scenario coverage and verification evidence for the affected dashboard change.
workflow_name: dashboard-ux-review
execution: main
allow_override: false
parents:
- dev/dashboard
tags:
- dashboard
- ux
- verification
steps:
- id: scope
  name: Establish affected scenarios
  step_type: reasoning
  depends_on: []
  result_schema:
    required_keys: [change_scope, scenarios]
    key_types:
      change_scope: str
      scenarios: list
    min_items:
      scenarios: 1
- id: review
  name: Record outcomes and evidence
  step_type: reasoning
  depends_on: [scope]
  result_schema:
    required_keys: [scenarios, changes, verification, remaining, blocked_outcomes]
    key_types:
      scenarios: list
      changes: list
      verification: list
      remaining: list
      blocked_outcomes: list
---

Keep a review scoped to the current change. Its workflow run id is the `ux_review_ref` used by `dev-pr`. An artifact path is also valid when it contains the same record. Reuse evidence only while its scope and implementation still match.

## scope

Load `dev/dashboard/ux-directions` and apply its proportional depth. Record `change_scope` and the retained `scenarios`, each with situation, starting point, outcome, success condition, exceptional conditions, and classification. Candidate needs do not expand the authorized scope automatically.

## review

Follow `dev/dashboard/verification-directions`. Record each scenario's classification, what changed, and the observable outcome. Every `verification` entry names the environment, executed action, observed result, and evidence label: live-verified, source-inferred, design-hypothesis, or untested. Put inferred and untested gaps in `remaining`, and unresolved required outcomes in `blocked_outcomes`. Describe the implementation tested so subsequent changes cannot silently inherit stale evidence. Return the record and this workflow run id for the commit workflow.
