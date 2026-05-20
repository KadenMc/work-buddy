---
name: Blindspot Detection Directions
kind: directions
description: How to scan for active work-pattern blindspots — intervention levels, cascade checking, rewriting template, output format
summary: Load the user's documented patterns from personal knowledge (any accountability category they track — work patterns, habits, health signals, etc.), gather current context, name the 1–2 active patterns with evidence, apply the matching intervention level, check cascades, keep output to 4 lines.
trigger: user wants to check their current work for active cognitive or behavioral patterns
command: wb-meta-blindspots
capabilities:
- context/context_bundle
- tasks/task_briefing
- contracts/contract_health
tags:
- metacognition
- blindspots
- patterns
- intervention
- directions
aliases:
- check blindspots
- detect patterns
- metacognition scan
- work patterns
- self-regulation check
parents:
- metacognition
- metacognition
---

This framework helps the user spot patterns they have *chosen to be accountable for*. It applies to any self-accountability domain — research workflow, coding habits, health signals, focus, communication patterns — not just work patterns narrowly.

## Step 1 — Load the user's documented patterns

The specific patterns live in personal knowledge, not in this unit. Load all categories the user has documented:

```
mcp__work-buddy__wb_run("knowledge_personal", {"category": "work_pattern"})
mcp__work-buddy__wb_run("knowledge_personal", {"category": "self_regulation"})
// or any other category they track — see the `knowledge_personal` capability for the full category list
```

If no personal pattern units exist yet, tell the user: "No documented patterns yet — `knowledge_mint` one first, or we'll be guessing." Do not invent pattern names.

## Step 2 — Gather current context

```
mcp__work-buddy__wb_run("context_bundle", {"hours": 24})
mcp__work-buddy__wb_run("task_briefing")
mcp__work-buddy__wb_run("contract_health")
```

Or run the full workflow: `mcp__work-buddy__wb_run("detect-blindspots")`.

## Step 3 — Name what's active

Use only pattern names the user has documented. Be specific with evidence: "This looks like <pattern>. In the last session you <observable signal> — <concrete count or example>."

## Intervention levels

- **Level 1 (first detection):** Name and redirect — name the pattern, give one next action, park nonessential work.
- **Level 2 (pattern persists):** Force a decision — require the user to classify items, pick one path, or set an explicit stop rule.
- **Level 3 (repeated):** Tighten environment — reduce degrees of freedom. The exact form depends on the pattern domain (fewer branches for work patterns, fewer obligations for overcommitment, tighter routines for habit patterns).

## Cascade checking

Patterns can chain. If the user's personal pattern unit documents cascade relationships, check downstream patterns after detecting an upstream one, and address the upstream first.

## Rewriting template

For vague global complaints ("I'm bad at this"), rewrite:

> "When [trigger] happens, I tend to [habitual response], which causes [cost], so the best immediate intervention is [specific response]."

## Output format

1. Pattern detected (with evidence)
2. Intervention level
3. One next action
4. One thing to park or defer

## Don'ts

- Don't diagnose personality — name the pattern, not the person.
- Don't list every possible pattern — name the 1–2 actually active.
- Don't pile on interventions — one next action is enough.
- Don't invent patterns the user hasn't documented. If the signal is real but unnamed, suggest minting a new pattern unit rather than using ad-hoc labels.
