---
name: Project Create
kind: capability
description: Manually create a project. Accepts initial folders + aliases + provenance metadata. Consent-gated.
capability_name: project_create
category: projects
parameters:
  slug:
    type: str
    description: Unique identifier (lowercase, hyphens)
    required: true
  name:
    type: str
    description: Human-readable project name
    required: true
  status:
    type: str
    description: 'Status: active (default), paused, past, future'
    required: false
  description:
    type: str
    description: What is this project?
    required: false
  origin:
    type: str
    description: 'Origin: ''manual'' (default) or ''vault'' (auto-detected)'
    required: false
  folders:
    type: list
    description: List of {path, archived} dicts. Absolute system paths.
    required: false
  aliases:
    type: list
    description: Alternative slug strings (display casing preserved).
    required: false
  author:
    type: str
    description: 'Author of this create: ''user'' (default) or ''agent'''
    required: false
mutates_state: true
retry_policy: manual
tags:
- projects
- project
- create
aliases:
- new project
- create project
- add project
- manually create project
- register new project
- start tracking a project
- project registry new entry
parents:
- projects
- projects
---
