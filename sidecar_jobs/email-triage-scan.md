---
schedule: "23 * * * *"  # hourly at :23, off the top-of-hour pile-up
recurring: true
type: capability
capability: run_source_pipeline
params:
  source: email_triage
  days_back: 2
  max_messages: 50
  unread_only: true
enabled: false
---
Background triage of recent email through the unified source pipeline.

Pipeline stages:
1. Collect recent unread mail via the configured email provider
   (Thunderbird bridge by default).
2. Annotate each item with synthesised tags (sender domain, folder
   type, flagged/read/labels). No per-message LLM call here.
3. Algorithmic precluster on subject + sender + tag overlap.
4. LLM cluster refinement (local-first tier_chain) — names clusters
   and proposes per-cluster actions from the email action library
   (`email_close`, `email_create_tasks`, `email_create_umbrella_task`).
5. Spawn a group-relationship umbrella thread + N children with the
   emails as ContextItems. The user reviews + approves on the
   dashboard's Threads tab.

The job is **disabled by default**. Enable it by:

1. Installing the thunderbird-work-buddy companion extension and
   ticking at least one account in its options page (see the `email/`
   integration knowledge unit for the click trail).
2. Setting `tools.thunderbird.enabled: true` in `config.local.yaml`.
3. Flipping `enabled: true` in this file's frontmatter.

Never sends, replies, archives, or otherwise mutates mail. The
Thunderbird bridge is read-first in v1; `email_close` is advisory
(dismisses the Thread without touching the underlying mailbox).
