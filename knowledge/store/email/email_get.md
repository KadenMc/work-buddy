---
name: Email Get
kind: capability
description: Fetch one email message by its operational handle (provider_message_id + folder_path) — returns the body up to max_body_chars chars plus all summary fields.
capability_name: email_get
category: email
op: op.wb.email_get
schema_version: wb-capability/v1
parameters:
  provider_message_id:
    type: str
    description: RFC Message-ID (or backend handle) from a triage card
    required: true
  folder_path:
    type: str
    description: Backend folder URI (from the triage card)
    required: true
  max_body_chars:
    type: int
    description: Body truncation cap (default 8000)
    required: false
tags:
- email
- get
aliases:
- read email
- open email
- get email body
- show message content
- fetch mail
parents:
- email
requires:
- thunderbird
---
