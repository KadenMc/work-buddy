"""Local LLM call with MCP tool access, gated by a named preset.

Routes a query to a local LM Studio model via ``/api/v1/chat``, which
supports MCP tool-call loops server-side. The model invokes work-buddy
tools; the **gateway enforces the capability whitelist**, not LM
Studio. MCP clients only see 6 top-level tools (``wb_run`` et al.);
every domain capability is dispatched through ``wb_run``. Whitelisting
therefore has to live server-side — see ``session_acl``.

This is the tool-enabled companion to ``llm_call`` (bounded synchronous
text-only) and ``llm_submit`` (asynchronous background). All three
serve different jobs; this one addresses the "local model needs to
look something up" use case without treating local models as fully
agentic Claude replacements.

## How authentication + authorization work here

1. **Authentication.** We synthesize a per-call session id
   (``lms-<uuid>``) and pass it to LM Studio as an HTTP header
   (``X-Work-Buddy-Session``) on its MCP integration config. LM
   Studio forwards that header to the work-buddy gateway on every
   tool call. The gateway's ``_require_init`` auto-registers the
   session on first contact, so the model never has to call
   ``wb_init`` itself. Each ``llm_with_tools`` call therefore has a
   distinct, traceable session id.

2. **Authorization.** Before firing the request, we call
   ``session_acl.set_session_acl(session_id, allowed_capabilities)``.
   The gateway's ``wb_run`` path consults the ACL and rejects any
   capability not in the whitelist; ``wb_search`` filters its
   results to the allowed set so the model doesn't see tools it
   can't use. On completion (or error), the ACL is cleared in a
   ``finally``.

## What LM Studio sees in ``integrations.allowed_tools``

The 2-3 top-level MCP tools that matter for tool-calling:
``wb_run`` (always), ``wb_search`` (so the model can discover what
it's allowed to do), and optionally ``wb_status``. We deliberately
do NOT include ``wb_init`` — auto-init via the header makes it
unnecessary, and omitting it prevents the model from trying to
register unexpected sessions.
"""

from __future__ import annotations

import uuid
from typing import Any


# Default URL for the work-buddy MCP gateway HTTP transport; matches
# the sidecar config (``sidecar.services.mcp_gateway.port = 5126``).
_DEFAULT_MCP_ENDPOINT = "http://localhost:5126/mcp"


# Top-level MCP tools LM Studio needs direct access to. Everything
# else is dispatched *through* wb_run, gated by the server-side ACL.
_LM_STUDIO_ALLOWED_MCP_TOOLS = ["wb_run", "wb_search"]


def llm_with_tools(
    *,
    system: str,
    user: str,
    profile: str,
    tool_preset: str,
    required_capabilities: list[str] | None = None,
    previous_response_id: str | None = None,
    max_tokens: int = 4096,
    temperature: float = 0.0,
    store: bool = False,
    persist_tool_results: bool = False,
    mcp_endpoint: str = _DEFAULT_MCP_ENDPOINT,
) -> dict[str, Any]:
    """Invoke a local model with restricted work-buddy MCP tool access.

    Args:
        system: System prompt — this becomes the ``instructions`` field
            on the native chat request. An instruction to call
            ``wb_init`` first is prepended automatically.
        user: User query — sent as ``input``.
        profile: Named local profile (e.g. ``"local_general"``). Must
            resolve to an LM Studio-backed server; the ``/v1`` suffix
            is stripped to build the ``/api/v1/chat`` URL.
        tool_preset: Name of a whitelist defined in
            ``work_buddy/llm/tool_presets.py`` (e.g. ``"readonly_safe"``,
            ``"readonly_context"``). Required; no arbitrary tool list
            accepted at call time.
        required_capabilities: Optional list of capability names the
            model MUST be able to call to complete its task (e.g.
            ``["update-journal", "journal_write"]``). Pre-flight
            checked against ``resolve_preset(tool_preset)``; if any
            required name isn't in the preset, the call fails fast
            with an explicit error instead of silently launching a
            doomed run. This is the sanctioned guard against the
            goal-preset mismatch where a caller reuses a preset
            from a prior call without checking whether it covers
            the new task.
        previous_response_id: Continue a prior stateful-chat turn.
        max_tokens: Output budget. Default 4096 — tool-using models
            burn budget on reasoning and tool args.
        temperature: Sampling temperature.
        store: Whether LM Studio should retain this turn server-side.
        persist_tool_results: When False (default), raw tool outputs
            are stripped from the response — only metadata (tool
            name, arguments, status, size, provider_info) is returned.
            The calling agent delegated to the local model precisely
            so it wouldn't have to look at raw tool output; keeping
            it in the response defeats that purpose and blows up
            payload size. When True, each tool call's output is
            saved to the artifact store and the artifact id is
            embedded in the response for later inspection. When ANY
            tool call errored, outputs are auto-persisted regardless
            of this flag so the caller can audit a failing run
            without re-executing.
        mcp_endpoint: URL of the work-buddy MCP gateway. Override only
            when testing against a non-standard endpoint.

    Returns:
        ``{content, tool_calls, response_id, model, input_tokens,
        output_tokens, tool_preset, allowed_tools, session_id, error}``.
    """
    if not profile:
        return _error("'profile' is required")
    if not tool_preset:
        return _error(
            "'tool_preset' is required. Presets are defined in "
            "work_buddy/llm/tool_presets.py — currently: "
            "'readonly_safe', 'readonly_context'."
        )

    from work_buddy.llm.profiles import resolve_profile
    from work_buddy.llm.tool_presets import resolve_preset

    try:
        allowed_tools = resolve_preset(tool_preset)
    except KeyError as exc:
        # KeyError.__str__ returns repr of its arg (adds outer quotes),
        # so extract the raw message instead for a clean error string.
        msg = exc.args[0] if exc.args else str(exc)
        return _error(str(msg))

    # Pre-flight goal-preset mismatch check. Catches the "I reused the
    # preset from last call without checking whether it covers this
    # task" failure mode — explicit required_capabilities forces the
    # caller to name what the model must reach, and we verify every
    # one is in the preset before firing.
    if required_capabilities:
        missing = [c for c in required_capabilities if c not in allowed_tools]
        if missing:
            return _error(
                f"required_capabilities not in preset {tool_preset!r}: "
                f"{missing}. Either pick a preset that covers them, "
                f"or add a new preset in work_buddy/llm/tool_presets.py "
                f"(reviewed PR).",
                tool_preset=tool_preset,
                allowed_tools=allowed_tools,
            )

    try:
        profile_info = resolve_profile(profile)
    except KeyError as exc:
        # KeyError.__str__ returns repr of its arg (adds outer quotes),
        # so extract the raw message instead for a clean error string.
        msg = exc.args[0] if exc.args else str(exc)
        return _error(str(msg))

    # Strip the ``/v1`` suffix from the openai_compat base_url to build
    # the native-endpoint base. LM Studio serves both from the same host.
    native_base = profile_info["base_url"].rstrip("/")
    if native_base.endswith("/v1"):
        native_base = native_base[:-3]
    native_base = native_base.rstrip("/")

    # Synthesize a one-shot session id for this call's MCP connection.
    # LM Studio forwards this back to work-buddy's gateway via the
    # ``X-Work-Buddy-Session`` header on every tool call, and the
    # gateway's auto-init picks it up without requiring the model to
    # call wb_init itself.
    session_id = f"lms-{uuid.uuid4().hex[:8]}"

    integrations = [
        {
            "type": "ephemeral_mcp",
            "server_label": "work-buddy",
            "server_url": mcp_endpoint,
            # What LM Studio advertises to the model: the 2 top-level
            # MCP tools it needs to do its job. The real per-call
            # whitelist is enforced on the gateway side via the ACL
            # set below. Domain capabilities (task_briefing, etc.)
            # are NOT in this list — they're dispatched through
            # wb_run, which the gateway gates.
            "allowed_tools": list(_LM_STUDIO_ALLOWED_MCP_TOOLS),
            "headers": {"X-Work-Buddy-Session": session_id},
        }
    ]

    from work_buddy.llm.backends.lmstudio_native import call_lmstudio_native
    from work_buddy.llm.backends._errors import LocalInferenceError
    from work_buddy.mcp_server.session_acl import (
        set_session_acl, clear_session_acl,
    )

    # Register the per-session ACL BEFORE firing the request. The
    # gateway's wb_run and wb_search paths consult it for each tool
    # call LM Studio makes on behalf of the model.
    set_session_acl(session_id, allowed_tools)
    try:
        result = call_lmstudio_native(
            base_url=native_base,
            model=profile_info["model"],
            system=system,
            user=user,
            integrations=integrations,
            previous_response_id=previous_response_id,
            store=store,
            max_tokens=max_tokens,
            temperature=temperature,
            api_key_env=profile_info["api_key_env"],
        )
    except LocalInferenceError as exc:
        return _error(
            str(exc),
            model=profile_info["model"],
            tool_preset=tool_preset,
            allowed_tools=allowed_tools,
            session_id=session_id,
            error_kind=exc.kind,
            hint=exc.hint,
        )
    except Exception as exc:
        return _error(
            f"{type(exc).__name__}: {exc}",
            model=profile_info["model"],
            tool_preset=tool_preset,
            allowed_tools=allowed_tools,
            session_id=session_id,
        )
    finally:
        # Always clear the ACL so a later call through the same
        # process doesn't inherit it. Safe even when the call failed
        # before the ACL was consulted.
        clear_session_acl(session_id)

    # Log cost (local mode → $0.00, per existing v1 convention)
    from work_buddy.llm.cost import log_call
    log_call(
        model=result.get("model", profile_info["model"]),
        input_tokens=result.get("input_tokens", 0),
        output_tokens=result.get("output_tokens", 0),
        task_id=f"llm_with_tools:{tool_preset}",
        execution_mode="local",
        backend=profile_info["backend_id"],
    )

    # Trim raw tool-call outputs for the response. The calling agent
    # delegated to the local model so it wouldn't have to look at raw
    # outputs; surfacing them bloats the response. When any call
    # errored we auto-persist everything so the caller can debug
    # without re-running.
    from work_buddy.llm._tool_call_trim import trim_tool_calls
    raw_tool_calls = result.get("tool_calls", []) or []
    trimmed_tool_calls = trim_tool_calls(
        raw_tool_calls,
        persist_tool_results=persist_tool_results,
        session_id=session_id,
        tool_preset=tool_preset,
    )
    any_tool_errored = any(
        entry.get("status") == "error" for entry in trimmed_tool_calls
    )
    should_persist = bool(persist_tool_results or any_tool_errored)

    # Reasoning is the local model's chain-of-thought — often
    # hundreds of tokens that the CALLING agent has no need to see
    # (it delegated precisely to avoid reading raw intermediates).
    # By default we strip it from the response and, when persistence
    # is active, save it as a separate artifact for debugging.
    reasoning_text = result.get("reasoning", "") or ""
    reasoning_artifact_id: str | None = None
    if should_persist and reasoning_text:
        try:
            from work_buddy.artifacts import save as _artifact_save
            rec = _artifact_save(
                reasoning_text,
                type="scratch",
                slug="llm_reasoning",
                ext="md",
                tags=[
                    "llm_with_tools",
                    f"preset:{tool_preset}",
                    f"session:{session_id}",
                    "reasoning",
                ],
                description=(
                    f"Local-model reasoning trace from llm_with_tools "
                    f"(session={session_id}, preset={tool_preset})"
                ),
                session_id=session_id,
            )
            reasoning_artifact_id = rec.id
        except Exception:
            # Non-fatal — reasoning is a nice-to-have, not critical
            reasoning_artifact_id = None

    return {
        "content": result.get("content", ""),
        "tool_calls": trimmed_tool_calls,
        "tool_calls_persisted": should_persist,
        "any_tool_errored": any_tool_errored,
        "reasoning_tokens": result.get("reasoning_tokens", 0),
        "reasoning_artifact_id": reasoning_artifact_id,
        "response_id": result.get("response_id"),
        "model": result.get("model", profile_info["model"]),
        "input_tokens": result.get("input_tokens", 0),
        "output_tokens": result.get("output_tokens", 0),
        "tool_preset": tool_preset,
        "allowed_tools": allowed_tools,
        "session_id": session_id,
        "error": None,
    }


def _error(message: str, **extra: Any) -> dict[str, Any]:
    """Build an error response with consistent shape.

    ``error_kind`` (one of the ``LocalInferenceError`` kinds) and
    ``hint`` are surfaced when present so agents can branch on the
    failure category and relay concrete next-steps to the user.
    """
    return {
        "content": "",
        "tool_calls": [],
        "response_id": None,
        "model": extra.get("model", ""),
        "input_tokens": 0,
        "output_tokens": 0,
        "tool_preset": extra.get("tool_preset"),
        "allowed_tools": extra.get("allowed_tools", []),
        "session_id": extra.get("session_id"),
        "error": message,
        "error_kind": extra.get("error_kind"),
        "hint": extra.get("hint", ""),
    }
