"""Post-process raw tool_calls from a local-model MCP tool-call loop.

By default we return only the tool call metadata (name, arguments,
status, size marker) to the calling agent — the whole point of
``llm_with_tools`` is that the agent delegates looking at raw tool
output to the local model. Embedding full outputs in the response
defeats that purpose and can balloon payloads past MCP token caps
(observed: 324 KB from a single ``sidecar_status`` call).

Policies:

* **default (persist_tool_results=False, no errors):** strip every
  output, surface metadata only.
* **explicit (persist_tool_results=True):** save each output to the
  artifact store, embed ``output_artifact_id`` in the trimmed entry.
* **auto-escalate on error:** when any tool call in the batch
  returned an error, persist ALL outputs for this run regardless of
  ``persist_tool_results``. The calling agent can then audit the
  full run without re-executing.

Errors are detected by unwrapping LM Studio's ``output`` field (a
JSON-encoded MCP result envelope) and checking for ``error`` /
``success: false`` inside. A short ``error_preview`` is always
included when a call errored so the agent gets signal without
needing the full artifact.
"""

from __future__ import annotations

import json
from typing import Any


_ERROR_PREVIEW_MAX_CHARS = 500


def _unwrap_mcp_output(output: Any) -> Any:
    """Unwrap LM Studio's MCP ``output`` into the inner result dict.

    Shape observed in practice::

        "[{\\"type\\":\\"text\\",\\"text\\":\\"{\\\\\\"error\\\\\\":\\\\\\"...\\\\\\"}\\"}]"

    That's a JSON string of a list of ``{type, text}`` items; each
    ``text`` is itself a JSON string of the actual MCP result dict.
    Returns the first parsed inner dict, or None when nothing usable.
    """
    if output is None:
        return None
    if isinstance(output, dict):
        return output
    if not isinstance(output, str):
        return None
    try:
        outer = json.loads(output)
    except (json.JSONDecodeError, TypeError):
        return None
    if isinstance(outer, dict):
        return outer
    if not isinstance(outer, list):
        return None
    for item in outer:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text":
            text = item.get("text", "")
            if not isinstance(text, str):
                continue
            try:
                inner = json.loads(text)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(inner, dict):
                return inner
    return None


def _detect_error(output: Any) -> str | None:
    """Return an error string when the tool output indicates failure.

    Checks for ``{"error": "..."}`` and ``{"success": False, ...}``
    patterns inside the unwrapped MCP result. Returns None for
    successful calls or outputs we can't decisively interpret.
    """
    inner = _unwrap_mcp_output(output)
    if not isinstance(inner, dict):
        return None
    err = inner.get("error")
    if err:
        return str(err)
    if inner.get("success") is False:
        msg = inner.get("message", "")
        if msg:
            return str(msg)
        return "Operation returned success=false"
    return None


def _persist_output(
    output: Any,
    *,
    tool_name: str,
    session_id: str,
    tool_preset: str,
    index: int,
) -> str | None:
    """Write ``output`` to the artifact store; return the artifact id.

    Uses the ``scratch`` artifact type (3-day TTL) — these are debug
    traces, not long-lived reference material. Tagged with the
    synthesized session id and the caller's ``tool_preset`` so they
    can be queried later via ``artifact_list``.
    """
    if output is None:
        return None
    try:
        from work_buddy.artifacts import save
    except Exception:
        return None

    # Prefer to unwrap LM Studio's MCP envelope so the artifact is clean
    # readable JSON rather than a string-of-string-of-string blob.
    unwrapped = _unwrap_mcp_output(output)
    if unwrapped is not None:
        content = json.dumps(unwrapped, indent=2, default=str)
    elif isinstance(output, (dict, list)):
        content = json.dumps(output, indent=2, default=str)
    elif isinstance(output, bytes):
        content = output.decode("utf-8", errors="replace")
    else:
        content = str(output)

    safe_tool = tool_name.replace("/", "-").replace(":", "-")
    slug = f"llm_tool_call_{safe_tool}_{index}"
    try:
        rec = save(
            content,
            type="scratch",
            slug=slug,
            ext="json",
            tags=[
                "llm_with_tools",
                f"preset:{tool_preset}",
                f"session:{session_id}",
                f"tool:{tool_name}",
            ],
            description=(
                f"Raw MCP tool output from llm_with_tools run "
                f"(session={session_id}, preset={tool_preset}, "
                f"tool={tool_name}, index={index})"
            ),
            session_id=session_id,
        )
        return rec.id
    except Exception:
        # Persisting is best-effort — don't fail the whole call if
        # the artifact store is unavailable.
        return None


def trim_tool_calls(
    raw_tool_calls: list[dict[str, Any]],
    *,
    persist_tool_results: bool,
    session_id: str,
    tool_preset: str,
) -> list[dict[str, Any]]:
    """Convert raw tool_calls into the trimmed response form.

    See the module docstring for the policy matrix. When any call in
    the batch errored, ALL outputs are persisted regardless of the
    ``persist_tool_results`` flag.
    """
    if not raw_tool_calls:
        return []

    # First pass: detect errors and compute the batch persist policy.
    errors: list[str | None] = []
    for call in raw_tool_calls:
        errors.append(_detect_error(call.get("output") if isinstance(call, dict) else None))
    any_error = any(e is not None for e in errors)
    should_persist = persist_tool_results or any_error

    trimmed: list[dict[str, Any]] = []
    for idx, (call, err) in enumerate(zip(raw_tool_calls, errors)):
        if not isinstance(call, dict):
            continue
        output = call.get("output")
        size_chars = len(output) if isinstance(output, str) else (
            len(json.dumps(output, default=str)) if output is not None else 0
        )

        entry: dict[str, Any] = {
            "tool": call.get("tool"),
            "arguments": call.get("arguments"),
            "status": "error" if err else "ok",
            "output_size_chars": size_chars,
            "provider_info": call.get("provider_info"),
        }
        # Preserve any extra fields LM Studio may include (e.g. "type")
        if call.get("type"):
            entry["type"] = call["type"]

        if err:
            entry["error_preview"] = err[:_ERROR_PREVIEW_MAX_CHARS]
            if len(err) > _ERROR_PREVIEW_MAX_CHARS:
                entry["error_preview"] += "…"

        if should_persist:
            artifact_id = _persist_output(
                output,
                tool_name=str(call.get("tool") or "unknown"),
                session_id=session_id,
                tool_preset=tool_preset,
                index=idx,
            )
            if artifact_id:
                entry["output_artifact_id"] = artifact_id
            else:
                entry["output_omitted"] = True
                entry["persist_failed"] = True
        else:
            entry["output_omitted"] = True

        trimmed.append(entry)

    return trimmed
