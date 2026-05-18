---
name: Local LLM With Tools
kind: concept
description: '[LEGACY — phase-8 deprecated] Local LM Studio-served models invoke a restricted whitelist of work-buddy MCP tools via /api/v1/chat. Gateway-enforced security via session_acl. Superseded for internal callers by architecture/llm-runner; retained here because the MCP-exposed `llm_with_tools` capability still uses this path.'
summary: LEGACY path. All internal Python callers migrated to LLMRunner (architecture/llm-runner) in phase 3 of the LLM + Context refactor. Retained for the MCP-exposed `llm_with_tools` capability only. Gateway-enforced per-session ACL via session_acl, tool presets in tool_presets.py, fail-closed bookend on unresolved sessions. Will be removed by task t-a373609f once LLMRunner grows native tool-call dispatch.
tags:
- llm_with_tools
- local-llm
- lm-studio
- mcp
- session_acl
- tool-presets
- ephemeral_mcp
- security
- whitelist
- acl-escape
aliases:
- llm with tools
- local model tool access
- lm studio native
- session acl
- tool preset
parents:
- architecture
- architecture
dev_notes: |-
  GOTCHAS FOR FUTURE MAINTAINERS:

  1. auto_retry=False is MANDATORY on llm_with_tools and llm_submit in
     registry.py. The gateway auto-enqueues transient failures of
     non-mutating capabilities for background retry by default. Without
     auto_retry=False, a hung model call gets replayed 5 times, each
     replay wasting tokens AND firing consent prompts. A real outage
     on 2026-04-16 left a retry loop attacking its own ACL for an hour
     before we caught it.

  2. wb_init is DELIBERATELY ABSENT from every preset in tool_presets.py.
     validate_presets() enforces this. If a future dev adds it back,
     the model can call wb_run(capability='wb_init', session_id='other')
     to re-register its MCP connection and escape the ACL. The gateway's
     wb_run handler also hard-blocks this at runtime, but defense-in-
     depth matters.

  3. The ORDER matters in gateway.py's wb_init special-case:
     `_auto_init_from_header(ctx)` MUST run BEFORE `_resolve_session(ctx)`.
     The first call from an LM Studio MCP connection arrives
     unregistered; without auto-init first, current_sid=None and the
     ACL check silently passes. We shipped this bug and fixed it on
     2026-04-17.

  4. The security boundary is gateway-enforced, not LM Studio-enforced.
     LM Studio's `integrations.allowed_tools` accepts only top-level
     MCP tool names (wb_run, wb_search). All work-buddy capabilities
     are dispatched THROUGH wb_run. Don't try to list capability names
     in allowed_tools — LM Studio will return HTTP 400 "invalid tools".

  5. wb_init is ALWAYS a special-case branch that short-circuits the
     normal wb_run flow. Any new ACL rule that expects to apply to
     wb_run must either be added inside the wb_init branch explicitly
     OR verified to apply after the branch. Easy to miss.

  6. LM Studio native /api/v1/chat is NOT OpenAI /v1/chat/completions.
     Different field names (input not messages, instructions not
     system, max_output_tokens not max_tokens), different response
     shape (output list of typed blocks not choices). The openai_compat
     backend and lmstudio_native backend are intentionally separate.

  7. LM Studio emits tool-call output as a JSON-serialized
     [{type, text}] envelope; inside `text` is another JSON. Three
     layers of encoding. _unwrap_mcp_output() peels them for clean
     artifact content. Re-wrapping to debug is tempting — don't; write
     to the artifact store and read via artifact_get.

  8. The ACL dict (set_session_acl / clear_session_acl in
     session_acl.py) is in-process state. It works because the gateway
     process IS the llm_with_tools process (both run in the MCP
     sidecar service). If llm_with_tools is ever moved out-of-process,
     replace the in-memory dict with something shared (file, SQLite).

  9. Reasoning tokens and tool-call outputs are STRIPPED from the
     response by default. persist_tool_results=True or any tool error
     auto-persists them to the scratch artifact store with 3-day TTL.
     Don't undo this hygiene — the original design had 324KB responses
     from a single sidecar_status call.

  10. Cross-platform process cleanup: os.kill(pid, SIGTERM) is
      UNRELIABLE on Windows for cross-process kills. It reports
      success and silently does nothing. compat.kill_process_on_port
      escalates to `taskkill /F /T /PID` and polls until the port is
      actually free; it returns a bool, and sidecar `_start_child`
      refuses to spawn a doomed child when cleanup returns False.
      Without this, a sidecar 'restart' can silently leave the old
      gateway listening on 5126 with pre-fix code.

  11. The consent system fires on low-level primitives (obsidian.eval_js).
      A capability in a readonly preset (like task_briefing) uses
      eval_js internally and triggers consent on every call — this is
      the call-stack-aware risk task (t-3629e1b1) still to be solved.
      Until fixed, presets containing obsidian-bridge-dependent
      capabilities cause consent spam. Workaround when live-testing:
      use capabilities that don't touch the bridge (sidecar_status,
      feature_status, knowledge).
---

## Status — legacy

This document describes the **legacy** local-LLM-with-tools path. All
internal Python callers migrated to :class:`work_buddy.llm.LLMRunner`
during phase 3 of the LLM + Context refactor. The `llm_with_tools`
function and its MCP capability are retained because external agents
(Claude Code sessions, slash commands, workflow steps) may still call
them over MCP. A CI sentinel test
(:file:`tests/unit/test_legacy_llm_api_guard.py`) blocks NEW internal
callers from adopting this path.

**For new code**, use :mod:`architecture/llm-runner` instead. Tool-call
dispatch on `LLMRunner.call(tools=[...])` currently raises
`NotImplementedError`; the deletion pass (task `t-a373609f`) wires it
through native Anthropic + LM Studio backend adapters and removes
`llm_with_tools` entirely.

The rest of this unit documents the legacy implementation for
reference — the gateway-enforced security model described below is
still correct because the MCP-exposed capability still operates.

---

## Why this exists

Claude calls consume cloud tokens per turn. For bounded, pattern-based work
(summarization, classification, structured extraction, context
pre-compression, low-priority background triage), local models on a compute
laptop are ~$0 per call and can run overnight without blocking. llm_with_tools
lets the user offload such work to a local model while keeping it
meaningfully useful by giving it access to a restricted set of work-buddy
capabilities.

The design explicitly does NOT try to make local models a Claude replacement.
They are slower, weaker at reasoning, and much more likely to make dangerous
mistakes if given broad tool access. Every layer below assumes adversarial or
confused-model behavior.

## How the call flow works

1. Agent calls wb_run("llm_with_tools", {profile, tool_preset, system, user,
   required_capabilities?, ...}).
2. **Pre-flight guard (if caller passed required_capabilities):** verify every
   name is in resolve_preset(tool_preset). On miss, return a specific error
   listing the missing names and the preset — zero LM Studio round-trip.
   Guards against goal-preset mismatch (e.g. reusing a readonly preset for
   a workflow that needs mutating capabilities).
3. llm_with_tools synthesizes a per-call session id (lms-<uuid>).
4. Calls session_acl.set_session_acl(session_id, preset_capabilities) —
   puts the whitelist in a process-local dict keyed by session id.
5. POSTs /api/v1/chat on LM Studio's localhost server with:
   - model from the profile
   - input (system + user text)
   - integrations: [{type: "ephemeral_mcp", server_url:
     "http://localhost:5126/mcp", allowed_tools: ["wb_run", "wb_search"],
     headers: {"X-Work-Buddy-Session": <session_id>}}]
6. LM Studio drives a tool-call loop server-side. On each call it hits
   work-buddy's MCP gateway with the X-Work-Buddy-Session header.
7. The gateway's _auto_init_from_header(ctx) reads the header and registers
   the MCP connection with that session id (so the model doesn't need to
   call wb_init). wb_run then consults session_acl for the session and
   rejects any capability not in the whitelist.
8. When wb_search is called, session_acl.filter_search_results trims the
   hit list to the ACL. If anything was trimmed, the response is wrapped
   as {results, _acl_filtered, _acl_hidden_count, _acl_notice} so the
   model sees WHY its list is short rather than silently reworking the
   query and burning tokens on a search loop.
9. When all tool calls finish, LM Studio returns the model's final message.
   llm_with_tools clears the ACL in a finally.
10. Raw tool-call outputs and reasoning tokens are trimmed from the response.
    persist_tool_results=True or any tool error triggers auto-persist to the
    scratch artifact store (3-day TTL).

## Security model: gateway-enforced, not LM Studio-enforced

LM Studio expects integrations.allowed_tools to contain top-level MCP tool
names. work-buddy's MCP surface exposes only six: wb_init, wb_run,
wb_search, wb_advance, wb_status, wb_step_result. Every domain capability
(task_briefing, project_get, etc.) is dispatched *through* wb_run.

We list ["wb_run", "wb_search"] in allowed_tools and enforce the
per-capability whitelist server-side via session_acl. LM Studio cannot be
trusted to enforce this — a malicious or confused preset could easily
over-advertise.

### Fail-closed bookend on unresolved sessions

The gateway relies on header-based auto-init to tie each tool call back to
the ACL set by llm_with_tools. If an MCP transport or client reconnect
loses the ctx→session mapping, _resolve_session returns None. Historically
is_capability_allowed(None, cap) returned True, which silently bypassed
the ACL. The fix (2026-04-17) adds session_acl.any_acl_registered() and
makes is_capability_allowed fail closed when session is None AND an ACL
is active anywhere in the process. wb_search's filter_search_results
helper applies the same fail-closed rule on the search path. The only
legitimate callers that resolve to None are normal agents in a process
with no ACL-scoped runs — they're unaffected.

## Tool presets

Defined in code at work_buddy/llm/tool_presets.py. PRESETS is a frozen dict
of preset name → frozenset of capability names. Validated by
validate_presets():
- readonly_* presets must contain zero mutating capabilities (checked
  against _MUTATING_CAPABILITIES)
- every preset name must match a real registered capability
- NO preset may contain wb_init (ACL-escape vector)

Adding or expanding a preset is a reviewed PR. Config-level overrides are
not supported — presets live in code.

### required_capabilities pre-flight guard

Callers who know which capabilities the model will need can pass
`required_capabilities: [...]` alongside `tool_preset`. llm_with_tools
verifies every name is in the resolved preset BEFORE opening an LM Studio
session. On miss, returns an explicit error naming the missing
capabilities and the preset. This catches the common failure mode of
reusing a preset from a prior call without checking whether it covers
the new task.

## Response hygiene

- content (model's final text answer): always surfaced
- reasoning (chain of thought): stripped from response by default; saved
  as .md artifact on persist/error
- tool_calls[].output (raw MCP tool result): stripped by default; replaced
  with output_size_chars + output_omitted=true; saved as .json artifact
  (cleanly unwrapped, no triple-JSON escape) on persist/error
- error_preview (capped at 500 chars): always included when a tool call
  errored, so the caller has signal even without the full artifact
- Any tool error in the batch auto-escalates to persist everything, so
  the caller can audit without re-running

## Auto-retry opt-out

llm_with_tools and llm_submit declare auto_retry=False on their Capability
registration. The gateway honors this by forcing retry_policy="manual".
Without this, a local-LLM failure gets replayed 5 times — each replay
wastes tokens, spams consent prompts on bridge-dependent capabilities, and
almost never succeeds on the next tick.

## Disabled-vs-ACL-hidden distinction

Two very different conditions used to share the single `unavailable: true`
flag in wb_search results:

1. Capability is in the knowledge store but NOT in the live registry —
   typically because a tool dependency (e.g. Obsidian bridge) is unmet.
2. Capability is registered but filtered out by this session's ACL.

As of 2026-04-17 these are explicit:
- Case 1 → result carries `disabled: true` + `disabled_reason:
  "Dependency unavailable: obsidian"` (or the equivalent for whatever
  dep is missing). The old `unavailable: true` key is kept as a
  back-compat alias through 2026-Q3. This branch now applies to
  workflows too — previously workflow hits without a live registry
  entry came back with no flag at all.
- Case 2 → results are filtered out entirely; if any were hidden,
  wb_search returns the _acl_notice wrapper described above.

Reasoning models were conflating the two conditions ("this capability
is unavailable" = "I don't have permission") and drawing the wrong
conclusion. The distinct signals + explicit reason strings fix that.

## Known limitations

- Consent-gated primitives like obsidian.eval_js fire on every call,
  because gating is per-primitive, not per-caller-context. A readonly
  preset that includes task_briefing still triggers eval_js high-risk
  consent even though task_briefing is read-only. Parked in task
  t-3629e1b1 as the call-stack-aware consent risk work.
- The native /api/v1/chat endpoint is LM Studio-specific. Other
  OpenAI-compat servers (vLLM, Ollama) don't support it. Use llm_call
  with a profile for plain text generation on those.
- LM Studio routes remote models via LM Link through its OpenAI-compat
  endpoints; /api/v1/chat behavior with LM Link is less extensively
  documented. Keep the compute laptop loaded and linked for reliability.

## Key files

- work_buddy/llm/with_tools.py — llm_with_tools capability
- work_buddy/llm/tool_presets.py — PRESETS, resolve_preset, validate_presets
- work_buddy/llm/backends/lmstudio_native.py — /api/v1/chat client
- work_buddy/llm/_tool_call_trim.py — response hygiene, artifact persist
- work_buddy/mcp_server/session_acl.py — per-session capability ACL,
  any_acl_registered, filter_search_results, fail-closed semantics
- work_buddy/mcp_server/tools/gateway.py — _auto_init_from_header,
  wb_init escape block, wb_run ACL enforcement, wb_search ACL filter
- tests/unit/test_llm_with_tools.py, test_llm_with_tools_hygiene.py,
  test_llm_tool_call_trim.py, test_session_acl_escape_block.py,
  test_search_disabled_flag.py, test_compat_port_cleanup.py

## Related

- `architecture/llm-runner` — the unified replacement. New code should
  go there.
- Task `t-a373609f` — the deletion pass that retires this path.
