---
name: Project Confirm Description
kind: capability
description: Mark the latest revision as user-confirmed. Use this when a human reviews an LLM-authored description (or other agent edit) and signs off.
capability_name: project_confirm_description
category: projects
parameters:
  slug:
    type: str
    description: Project slug or alias
    required: true
mutates_state: true
retry_policy: manual
tags:
- projects
- project
- confirm
- description
aliases:
- confirm project description
- approve project edit
- sign off project
- user confirm project
parents:
- projects
- projects
---
