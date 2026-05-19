---
name: Chrome Tab Close
kind: capability
description: Close specified Chrome tabs by tab ID. Returns count of closed/missing tabs.
capability_name: chrome_tab_close
category: context
op: op.wb.chrome_tab_close
schema_version: wb-capability/v1
parameters:
  tab_ids:
    type: list
    description: List of Chrome tab IDs (integers) to close
    required: true
mutates_state: true
retry_policy: manual
is_action: true
intrinsic_amplifiers:
  irreversibility: moderate
  regret_potential: moderate
tags:
- context
- chrome
- tab
- close
aliases:
- close tab
- remove tab
- close chrome
- kill tabs
- close browser tabs
- dismiss tabs
- close tab by id
parents:
- context
requires:
- chrome_extension
---
