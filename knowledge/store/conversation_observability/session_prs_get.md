---
name: Session PRs Get
kind: capability
description: List the GitHub pull-request events (created / merged / closed / reviewed) attributed to one session, detected structurally from `gh pr` Bash invocations in its JSONL. Read-only.
capability_name: session_prs_get
category: conversation_observability
op: op.wb.session_prs_get
schema_version: wb-capability/v1
parameters:
  session_id:
    type: str
    description: Full or 8-char prefix session UUID.
    required: true
tags:
- conversation_observability
- conversation
- session
- pr
- pull-request
- provenance
- get
aliases:
- prs for session
- pull requests by session
- which prs did this session author
- session pr activity
parents:
- conversation_observability
---

Return the GitHub pull-request activity attributed to one session. The PR-side counterpart to `session_commits` — it answers "which PRs did this session author / merge / close / review."

Attribution is **structural**: the observability sweep detects `gh pr create|merge|close|review` Bash invocations in the session JSONL and records the canonical PR URL parsed from the command-or-output. This avoids the failure modes of commit-message `Closes #NNN` regex (which misses agent-created PRs whose commits predate the PR number, false-positives human PRs linked from agent commits, and loses the create timestamp).

Read-only; reads cached `session_prs` rows without scanning JSONL (the sweep keeps them fresh).

Returns:

```
{
  "prs": [
    {
      "pr_number": int,
      "pr_url": str,            # https://github.com/<owner>/<repo>/pull/<n>
      "repo": str,              # 'owner/name'
      "action": str,            # created | merged | closed | reviewed
      "ts": str | None,         # ISO-8601 of the invocation
      "message_index": int | None
    },
    ...
  ]
}
```

Sorted newest-first by `ts`. An empty list means the session ran no detected `gh pr` activity (not a failure).

## Limitations

A `merge`/`close`/`review` invocation that references the PR by bare number (e.g. `gh pr merge 92`) with no GitHub PR URL anywhere in the command or output is skipped — the row needs a real URL/repo. A `created` row for the same PR (whose `gh pr create` always prints the URL) still links the session to it.
