---
name: Setup Wizard
kind: capability
description: 'Comprehensive setup wizard for work-buddy. Validates bootstrap requirements, checks feature health, manages user preferences (wanted/unwanted features), and provides guided first-time setup. Modes: ''status'' (quick overview), ''guided'' (interactive walkthrough), ''diagnose'' (deep diagnostic for one component), ''preferences'' (view/edit).'
capability_name: setup_wizard
category: status
parameters:
  mode:
    type: str
    description: 'Wizard mode: ''status'' (default), ''guided'', ''diagnose'', ''preferences'''
    required: false
  component:
    type: str
    description: Component ID for 'diagnose' mode
    required: false
  updates:
    type: dict
    description: 'Preference updates for ''preferences'' mode. Dict of component_id -> {wanted: bool, reason: str}'
    required: false
mutates_state: true
retry_policy: manual
tags:
- status
- setup
- wizard
aliases:
- setup
- wizard
- configure
- preferences
- onboarding
- first time
- requirements
- bootstrap
- wanted
- unwanted
parents:
- status
- status
---
