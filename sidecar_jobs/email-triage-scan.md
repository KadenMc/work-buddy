---
schedule: "23 * * * *"  # hourly at :23, off the top-of-hour pile-up
recurring: true
type: capability
capability: email_triage_run
params:
  days_back: 2
  max_messages: 50
  unread_only: true
  include_body_chars: 0
enabled: false
---
Background triage of recent email.

Hits the configured email provider (Thunderbird bridge by default),
collects unread messages within `days_back` days, dedupes against the
existing pool, and drops new captures into the Review pool with
`source="email_message"`.

The job is **disabled by default**. Enable it by:

1. Installing the thunderbird-work-buddy companion extension and
   ticking at least one account in its options page.
2. Setting `tools.thunderbird.enabled: true` in `config.local.yaml`.
3. Flipping `enabled: true` in this file's frontmatter.

Slice 1 has the LLM verdict pass disabled — entries land as raw captures
that the user reviews in the dashboard. The verdict pass over emails
is a follow-up.

Never sends, replies, archives, or otherwise mutates mail. The bridge
exposes only read-and-display operations in v1.
