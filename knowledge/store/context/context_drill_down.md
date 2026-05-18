---
name: Context Drill Down
kind: capability
description: 'Expand one item from a context source. Works on structured wave-1 sources that implement drill_down — tasks (field: ''note'' / ''line''), git (field: ''full_message'' / ''diff_stats''), projects (field: ''description'' / ''full''). Wave-2/3 markdown wrappers don''t implement drill-down — the prompt already holds their full body at DEEP depth.'
capability_name: context_drill_down
category: context
parameters:
  source:
    type: str
    description: Source name (tasks / git / projects).
    required: true
  item_id:
    type: str
    description: Item identifier within the source (task_id / commit sha / project slug).
    required: true
  field:
    type: str
    description: Which expansion to return. See source docs for valid fields.
    required: true
tags:
- context
- drill
- down
aliases:
- show full commit message
- show full task note
- expand project description
- drill down on item
- get more detail
parents:
- context
- context
---
