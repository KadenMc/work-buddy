---
name: Email Accounts
kind: capability
description: List the email accounts visible through the bridge — only accounts the user has explicitly allowed in the extension's options page are exposed (default-deny).
capability_name: email_accounts
category: email
op: op.wb.email_accounts
schema_version: wb-capability/v1
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
