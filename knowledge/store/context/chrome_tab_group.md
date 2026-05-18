---
name: Chrome Tab Group
kind: capability
description: Create a Chrome tab group or add tabs to an existing group. Returns the group ID.
capability_name: chrome_tab_group
category: context
parameters:
  tab_ids:
    type: list
    description: List of Chrome tab IDs to group
    required: true
  title:
    type: str
    description: Group title displayed in Chrome
    required: false
  color:
    type: str
    description: 'Group color: grey, blue, red, yellow, green, pink, purple, cyan, orange'
    required: false
  group_id:
    type: int
    description: Existing group ID to add to (omit to create new group)
    required: false
mutates_state: true
retry_policy: manual
tags:
- context
- chrome
- tab
- group
aliases:
- group tab
- tab group
- organize tabs
- bundle tabs together
- create tab group
- add to tab group
- organize browser
parents:
- context
- context
requires:
- chrome_extension
---
