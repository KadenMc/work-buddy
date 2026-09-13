---
name: Email Accounts
kind: skill
description: List the email accounts visible through the bridge — only accounts the user has explicitly allowed in the extension's options page are exposed (default-deny).
category: email
op: op.wb.email_accounts
schema_version: wb-skill/v1
skill_name: email_accounts
tags:
- email
- accounts
aliases:
- list email accounts
- show mail accounts
- thunderbird accounts
- what email is connected
parents:
- email
requires:
- thunderbird
---
