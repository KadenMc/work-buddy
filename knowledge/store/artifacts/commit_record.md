---
name: Commit Record
kind: capability
description: Record structured commit metadata (hash, files, test results, knowledge units updated) as an artifact. Called after a successful git commit to enable enriched commit cards in the dashboard.
capability_name: commit_record
category: artifacts
op: op.wb.commit_record
schema_version: wb-capability/v1
parameters:
  commit_hash:
    type: str
    description: Git commit hash (7+ chars)
    required: true
  message:
    type: str
    description: Commit message
    required: true
  branch:
    type: str
    description: Branch name
    required: false
  files_changed:
    type: str
    description: Comma-separated file paths
    required: false
  tests_run:
    type: str
    description: Comma-separated test file names
    required: false
  tests_passed:
    type: int
    description: Number of tests passed
    required: false
  tests_failed:
    type: int
    description: Number of tests failed
    required: false
  knowledge_units_updated:
    type: str
    description: Comma-separated knowledge store paths updated
    required: false
  summary:
    type: str
    description: 1-2 sentence summary of the commit
    required: false
  agent_session_id:
    type: str
    description: Session ID (auto-injected by gateway)
    required: false
mutates_state: true
retry_policy: replay
tags:
- artifacts
- commit
- record
aliases:
- record commit
- commit metadata
- save commit info
- log commit
- commit artifact
parents:
- artifacts
---
