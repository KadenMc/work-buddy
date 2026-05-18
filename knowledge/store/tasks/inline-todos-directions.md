---
name: Inline TODOs Directions
kind: directions
description: 'How to triage and execute #wb/TODO vault markers — batch presentation, execution rules, tag cleanup'
summary: 'Present items in batches of 5. For each: file, full line, parsed instruction (bold), 1-2 lines context. Actions: Handle, Skip, Delete tag. Confirm ambiguous instructions before executing. Never delete lines — only replace #wb/TODO with #wb/DONE.'
trigger: 'user wants to process #wb/TODO markers from across the vault'
command: wb-inline-todos
workflow: tasks/inline-todos
capabilities:
- tasks/task_create
tags:
- tasks
- inline
- todos
- triage
- directions
aliases:
- process vault todos
- inline todos
- wb todo markers
- handle todos
parents:
- tasks
---

Start via mcp__work-buddy__wb_run("inline-todos"), then advance with wb_advance.

## Triage rules

Present items in batches of 5. For each item show:
1. File: vault path
2. Line: full line with #wb/TODO highlighted
3. Instruction: parsed instruction text (bold)
4. Context: 1-2 lines before/after

Actions:
- Handle -- execute the instruction
- Skip -- leave for next time
- Delete tag -- remove tag without executing

## Executing instructions

For Handle items:
1. Read the instruction text (freeform natural language)
2. Use surrounding context to interpret
3. If clear, execute directly
4. If ambiguous, ask the user first

Report what was done for each handled item.

## Don'ts
- Don't execute without showing the user what you'll do
- Don't modify files outside the cleanup step
- Don't delete lines -- only replace #wb/TODO with #wb/DONE
- Don't assume what ambiguous instructions mean -- ask
