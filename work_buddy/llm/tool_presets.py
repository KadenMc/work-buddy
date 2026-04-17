"""MCP tool presets for local-model tool access.

Each preset is a frozen whitelist of work-buddy capability names a
local model is permitted to call during an ``llm_with_tools`` session.
The whitelist lives in CODE (not config) so adding or expanding a
preset is a reviewed PR-level change, not something mis-edited config
can silently relax.

## Security posture

* **Every preset must include** ``wb_init`` — the work-buddy MCP
  gateway requires a session registration before any other tool call,
  and the model needs to be able to make that call. The system prompt
  injected by ``llm_with_tools`` tells the model to call ``wb_init``
  first with a synthesized session id. This is a known limitation:
  brittle in the sense that it relies on the model following the
  instruction. Follow-up: modify the gateway to auto-register from a
  request header so we don't have to trust the model.
* **Read-only presets must contain zero mutating capabilities.** Any
  capability whose name starts with ``task_`` (other than reads like
  ``task_briefing``, ``task_stale_check``, ``task_review_inbox``,
  ``task_scattered``) or touches vault writes, messaging, memory
  writes, consent grants, or service_restart is disallowed.
* **No "allow all" escape hatch.** ``llm_with_tools`` takes a preset
  NAME; it cannot accept an arbitrary allowed_tools list at call time.
* **Unknown preset names fail fast** with a helpful error listing
  what's available.

## Adding a new preset

1. Add the frozen set below with a careful audit of each tool name.
2. Run the validator test — it confirms every name exists in the MCP
   registry and that read-only presets have no mutating tools.
3. Register it in ``PRESETS``.
4. Submit as a PR with a specific justification for why this preset
   exists and what workflow needs it.
"""

from __future__ import annotations

# Note on wb_init: it is DELIBERATELY EXCLUDED from every preset.
# Header-based auto-init in the gateway (`_auto_init_from_header`)
# registers the local model's MCP session from the
# ``X-Work-Buddy-Session`` header on first contact — the model never
# needs to call wb_init itself. Allowing wb_init inside an ACL-scoped
# session was confirmed (2026-04-17 live test) to be an ACL-escape
# vector: a small model could call wb_run(capability="wb_init",
# session_id="other") to swap its session and drop the ACL. The
# gateway now hard-rejects wb_init from ACL-scoped sessions at the
# `wb_run` dispatch path (belt), and the preset omission is the
# suspenders.


# ---------------------------------------------------------------------------
# readonly_safe — minimum-footprint read access
# ---------------------------------------------------------------------------
# Only the tools needed to understand the user's ongoing work:
# tasks, contracts, projects, journal reads, day planner, sidecar
# status. Zero vault/task/messaging/memory writes. Zero execution of
# long-running context-bundle collectors.
_READONLY_SAFE = frozenset({
    # Tasks (reads only — no toggle/delete/create/assign)
    "task_briefing",
    "task_stale_check",
    "task_review_inbox",
    "task_scattered",
    "weekly_review_data",
    # Contracts (reads only — no create/update)
    "active_contracts",
    "contracts_summary",
    "contract_health",
    "contract_constraints",
    "contract_wip_check",
    "overdue_contracts",
    "stale_contracts",
    # Projects (reads only — no observe/create/update/delete)
    "project_list",
    "project_get",
    # Journal reads
    "journal_state",
    "running_notes",
    "day_planner",
    "hot_files",
    "activity_timeline",
    # Sidecar / system status
    "sidecar_status",
    "sidecar_jobs",
    "service_health",
    "feature_status",
    "tailscale_status",
    # Knowledge reads
    "knowledge",
    "knowledge_docs",
    "knowledge_personal",
    "agent_docs",
    # Messaging / thread reads (no sends)
    "query_messages",
    "read_message",
    "get_thread",
    "thread_list",
})


# ---------------------------------------------------------------------------
# readonly_context — readonly_safe + richer context collection
# ---------------------------------------------------------------------------
# Adds the context collectors that fetch richer but more expensive
# context (git diffs, Obsidian summaries, chat history, Chrome tabs,
# calendar, smart/semantic search). All reads — no mutations.
_READONLY_CONTEXT = _READONLY_SAFE | frozenset({
    # Context collectors
    "context_bundle",
    "context_git",
    "context_obsidian",
    "context_chat",
    "context_tasks",
    "context_projects",
    "context_messages",
    "context_chrome",
    "context_calendar",
    "context_smart",
    "context_wellness",
    "context_search",
    "ir_index",
    # Datacore (structured vault queries — read only)
    "datacore_status",
    "datacore_query",
    "datacore_fullquery",
    "datacore_validate",
    "datacore_get_page",
    "datacore_evaluate",
    "datacore_schema",
    "datacore_compile_plan",
    "datacore_run_plan",
    # Chrome reads (no tab close/move/group)
    "chrome_activity",
    "chrome_cluster",
    "chrome_content",
    "chrome_infer",
    "triage_item_detail",
    # Session reads
    "list_sessions",
    "session_get",
    "session_search",
    "session_expand",
    "session_locate",
    "session_commits",
    "session_uncommitted",
    "session_activity",
    "session_summary",
    # Artifacts (reads)
    "artifact_list",
    "artifact_get",
})


PRESETS: dict[str, frozenset[str]] = {
    "readonly_safe": _READONLY_SAFE,
    "readonly_context": _READONLY_CONTEXT,
}

# Capability names that are considered mutating — any preset whose
# name starts with ``readonly_`` must contain none of these.
_MUTATING_CAPABILITIES: frozenset[str] = frozenset({
    # Tasks
    "task_create", "task_assign", "task_change_state", "task_toggle",
    "task_delete", "task_sync", "task_archive",
    # Contracts
    "create_contract",
    # Projects
    "project_observe", "project_update", "project_create", "project_delete",
    "project_discover",
    # Journal writes
    "journal_write", "journal_sign_in", "vault_write_at_location",
    # Memory writes
    "memory_write", "memory_reflect", "memory_prune",
    # Messaging / notifications
    "send_message", "reply_to_message", "update_message_status",
    "notification_send", "request_send", "consent_request",
    "consent_request_resolve", "consent_grant", "consent_revoke",
    # Threads (writes)
    "thread_create", "thread_send", "thread_ask", "thread_close",
    # Chrome mutations
    "chrome_tab_close", "chrome_tab_group", "chrome_tab_move", "triage_execute",
    # Workflow control
    "dev_mode_toggle",
    # Admin
    "service_restart", "mcp_registry_reload", "obsidian_retry",
    # Docs edits
    "docs_create", "docs_update", "docs_delete", "docs_move",
    "agent_docs_rebuild", "knowledge_mint", "knowledge_index_rebuild",
    # Artifacts
    "artifact_save", "artifact_delete", "artifact_cleanup",
    "commit_record",
    # Setup
    "setup_help", "setup_wizard",
    # Remote sessions
    "remote_session_begin", "remote_session_list",
    # Retry
    "retry",
    # LLM dispatch (no nested LLM calls from a local model)
    "llm_call", "llm_submit", "llm_with_tools",
})


def resolve_preset(name: str) -> list[str]:
    """Return the sorted allowed-tool list for a named preset.

    Raises:
        KeyError with a helpful message (including available preset
        names) when ``name`` is unknown.
    """
    if name not in PRESETS:
        available = ", ".join(sorted(PRESETS.keys()))
        raise KeyError(
            f"Unknown tool preset {name!r}. Available: {available}. "
            f"Presets are defined in work_buddy/llm/tool_presets.py and "
            f"adding one requires a PR for security review."
        )
    return sorted(PRESETS[name])


def validate_presets(registry_names: set[str] | None = None) -> list[str]:
    """Structural validation for every preset.

    Checks:
    - No preset contains ``wb_init`` (security: would allow ACL escape
      via session re-init; header-based auto-init makes it unnecessary).
    - Each ``readonly_*`` preset contains zero mutating capabilities.
    - When ``registry_names`` is provided, every preset entry is a
      real registered capability (no typos, no drift from deletions).

    Returns a list of problems; empty when all presets are clean.
    """
    problems: list[str] = []

    for preset_name, tools in PRESETS.items():
        if "wb_init" in tools:
            problems.append(
                f"Preset {preset_name!r} contains 'wb_init' — this is an "
                f"ACL-escape vector and must be excluded. Header-based "
                f"auto-init handles session registration for local models."
            )
        if preset_name.startswith("readonly_"):
            mutating = tools & _MUTATING_CAPABILITIES
            if mutating:
                problems.append(
                    f"Preset {preset_name!r} contains mutating capabilities "
                    f"but its name claims readonly: {sorted(mutating)}"
                )
        if registry_names is not None:
            unknown = tools - registry_names
            if unknown:
                problems.append(
                    f"Preset {preset_name!r} references names not in the "
                    f"capability registry: {sorted(unknown)}"
                )
    return problems
