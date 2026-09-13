---
name: Email Health
kind: skill
description: Liveness probe for the email bridge. Returns the bridge's /health payload (port, version, allowed-account count). Use this when the user reports email features are missing — it distinguishes 'bridge down' from 'no accounts allowed'.
category: email
op: op.wb.email_health
schema_version: wb-skill/v1
skill_name: email_health
tags:
- email
- health
aliases:
- email health
- email bridge status
- thunderbird bridge running
- is email working
- email provider check
parents:
- email
requires:
- thunderbird
---
