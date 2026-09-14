---
name: Project Confirm Description
kind: skill
description: Mark the latest revision as user-confirmed. Use this when a human reviews an LLM-authored description (or other agent edit) and signs off.
category: projects
op: op.wb.project_confirm_description
schema_version: wb-skill/v1
parameters:
  slug:
    type: str
    description: Project slug or alias
    required: true
mutates_state: true
retry_policy: manual
skill_name: project_confirm_description
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
---
