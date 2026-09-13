---
name: Mode Toggle
kind: skill
description: Toggle a session mode (e.g. dev, knowledge) on or off. Pass active=true to enable, active=false to disable, or omit to flip the current state. Returns the full set of active modes after the change. Activating a mode is refused when its activation constraint is unmet. Modes gate which skills and workflows are discoverable (wb_search) and callable (wb_run) via their available_when declarations.
category: modes
op: op.wb.mode_toggle
schema_version: wb-skill/v1
parameters:
  mode_id:
    type: str
    description: The mode to toggle (e.g. 'dev', 'knowledge').
    required: true
  active:
    type: bool
    description: True=enable, False=disable, omit=flip current state.
    required: false
skill_name: mode_toggle
tags:
- modes
- mode
- toggle
- dev
aliases:
- toggle mode
- enable mode
- disable mode
- enter mode
- exit mode
- switch mode
- dev mode
- knowledge mode
parents:
- modes
---
