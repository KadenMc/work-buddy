---
name: Journal Sign In
kind: capability
description: 'Read sign-in state (sleep/energy/mood/check-in/motto) and wellness trends, optionally write fields. Composite: replaces separate extract_sign_in + interpret_wellness + write_sign_in calls.'
capability_name: journal_sign_in
category: journal
parameters:
  target:
    type: str
    description: 'Date target: ''today'', ''yesterday'', or YYYY-MM-DD. Default: today.'
    required: false
  write_fields:
    type: str
    description: 'JSON dict of fields to write (e.g. {"sleep": 7, "mood": 8}). Consent-gated. Omit for read-only.'
    required: false
mutates_state: true
retry_policy: manual
tags:
- journal
- sign
- in
aliases:
- sign in
- morning check in
- sleep energy mood
- wellness trends
- write sign in
parents:
- journal
- journal
requires:
- obsidian
---
