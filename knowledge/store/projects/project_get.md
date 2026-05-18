---
name: Project Get
kind: capability
description: Get a single project (resolved via slug or alias) with its folders, aliases, and recent Hindsight memory recall
capability_name: project_get
category: projects
parameters:
  slug:
    type: str
    description: Project slug or alias (e.g. 'ecg-inquiry' or 'electricrag')
    required: true
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
- projects
---
