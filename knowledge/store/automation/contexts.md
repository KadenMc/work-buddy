---
name: Action contexts
kind: reference
description: resolve_who_can_act answers "who can act on this task now?" by consulting the CONTEXT_REGISTRY against the live tool-status cache. Tasks declare agent_required_contexts + user_required_contexts; the resolver returns a WhoCanActDecision with per-side unmet tokens and a handoff-eligible flag.
entry_points:
- work_buddy.automation.contexts.resolve_who_can_act
- work_buddy.automation.contexts.CONTEXT_REGISTRY
- work_buddy.automation.contexts.WhoCanActDecision
- work_buddy.automation.contexts.user_satisfies_against
- work_buddy.automation.contexts.context_tokens_blocked_by_tool
tags:
- automation
- contexts
- action-contexts
- lazy-resolution
aliases:
- who can act
- resolve_who_can_act
- context registry
- agent required contexts
- user required contexts
- handoff eligibility
- action context resolver
parents:
- automation
- automation
dev_notes: Pure function -- only I/O is the in-memory _TOOL_STATUS lookup via is_tool_available. Tests inject tool_status= to bypass the global cache. Empty list ([]) vs None sentinel discipline is load-bearing -- get this wrong and all tasks become user-only OR all become universal. The user-side resolver trusts declared contexts (always satisfies for known + universal tokens); the engage-view filter user_satisfies_against answers the orthogonal "is the user CURRENTLY in this context?" question.
---

# automation/contexts

Module: work_buddy/automation/contexts.py.

## What

Two concepts:

1. CONTEXT_REGISTRY -- dict[str, list[str] | None] mapping context tokens (e.g. @filesystem, @email_send) to the tool IDs that satisfy them for the agent.
2. resolve_who_can_act(agent_required, user_required, *, tool_status=None) -- pure function returning a frozen WhoCanActDecision.

## Sentinel discipline (registry values)

- None  -> user-only context (@physical, @user_creds, @cluster). Agent never satisfies regardless of tool state.
- []    -> universally available (@filesystem, @web_public, @llm, @github). Both actors satisfy without a probe.
- [...] -> probe-gated. Agent satisfies iff ALL listed tool IDs are available (cf. work_buddy.tools.is_tool_available).

## Starter token set (14)

User-only: @physical, @in_person, @phone_voice, @user_creds, @user_workstation, @cluster.
Universal: @filesystem, @web_public, @llm, @github.
Probe-gated: @vault -> obsidian, @email_send -> thunderbird, @email_read -> thunderbird, @chrome_active -> chrome_extension.

Unknown tokens (Clarify may invent new ones for forward-compat) are treated as user-only AND reported in unknown_tokens so the dashboard can warn.

## WhoCanActDecision

Frozen dataclass: agent (bool), user (bool), blocked (bool), agent_unmet (tuple), user_unmet (tuple), agent_handoff_eligible (bool), unknown_tokens (tuple).

## Handoff framing (ROADMAP section 3.2)

When agent=False AND user=True -> agent_handoff_eligible=True. The right surface is a HANDOFF card ("agent prepared what it can; you take from here"), NOT a "task waiting" badge. The Today tab renders this badge.

## Risk integration

automation.risk.resolve_achievable_tier consults this module via the tool_status kwarg. When the agent cannot satisfy its required contexts, the achievable ceiling drops to 1 (suggest only). resolve_operating_tier then emits agent_context_unmet / user_context_unmet blockers per ROADMAP section 3.3.

## Schema (task_metadata)

Three nullable columns:
- agent_required_contexts (TEXT, JSON array)
- user_required_contexts (TEXT, JSON array)
- required_contexts_source ('agent_inferred' | 'user_authored' | NULL)

Clarify populates the lists at task creation; the dashboard flips required_contexts_source to 'user_authored' once the user edits the inferred set.
