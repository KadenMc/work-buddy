---
name: Email Display
kind: skill
description: Open a message in Thunderbird's UI. Useful when the user wants to read it themselves — does not modify anything.
category: email
op: op.wb.email_display
schema_version: wb-skill/v1
parameters:
  provider_message_id:
    type: str
    description: RFC Message-ID from a triage card
    required: true
  folder_path:
    type: str
    description: Backend folder URI
    required: true
  mode:
    type: str
    description: '''3pane'' (default), ''tab'', or ''window'''
    required: false
skill_name: email_display
tags:
- email
- display
aliases:
- open in thunderbird
- show email in thunderbird
- display message
- open mail in client
parents:
- email
requires:
- thunderbird
---
