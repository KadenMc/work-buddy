---
name: Project Get
kind: skill
description: Get a single project (resolved via slug or alias) with its folders, aliases, and recent Hindsight memory recall
category: projects
op: op.wb.project_get
schema_version: wb-skill/v1
parameters:
  slug:
    type: str
    description: Project slug or alias (e.g. 'ecg-inquiry' or 'electricrag')
    required: true
skill_name: project_get
tags:
- projects
- project
- get
aliases:
- project details
- project info
- project state
- project observations
parents:
- projects
---
