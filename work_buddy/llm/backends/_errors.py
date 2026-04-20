"""Interpret local-inference failures into actionable error messages.

Both backends (``openai_compat`` and ``lmstudio_native``) can fail in
similar ways: the server isn't running, LM Link has dropped the
compute device, the requested model isn't loaded anywhere, or LM
Studio returns a vague 4xx/5xx. Raw ``httpx.HTTPStatusError`` strings
like ``"Client error '400 Bad Request' for url ..."`` are unhelpful
to an agent trying to decide what to do next.

This module converts those into a structured ``LocalInferenceError``
carrying a human-facing message, a kind discriminator, and a remedy
hint the agent can relay to the user.
"""

from __future__ import annotations

from typing import Any

import httpx


class LocalInferenceError(Exception):
    """Structured error for local inference failures.

    Attributes:
        message: Human-readable description.
        kind: Category discriminator — one of:
            * ``"server_unreachable"`` — LM Studio server isn't up / port closed
            * ``"model_not_loaded"`` — requested model not on any linked device
            * ``"model_unsupported"`` — server rejected the model for this endpoint
            * ``"bad_request"`` — 4xx with an otherwise unclassified body
            * ``"server_error"`` — 5xx with no matched sub-pattern
            * ``"timeout"`` — read or connect timeout (our side gave up)
            * ``"mcp_gateway_timeout"`` — LM Studio's MCP integration call to the
              work-buddy gateway exceeded LM Studio's deadline (JSON-RPC -32001).
              Means the gateway took too long to reply to a tool dispatch.
            * ``"mcp_fetch_failed"`` — LM Studio's HTTP fetch to the work-buddy
              gateway failed at the transport layer (TCP reset, refused, etc).
            * ``"lm_link_dropped"`` — LM Studio lost its LM Link connection to
              the compute device mid-call.
            * ``"context_exceeded"`` — prompt (+ tool schema + reasoning tokens)
              exceeded the model's configured context window. The effective
              cap is the "Context Length" slider on the loaded model in
              LM Studio, NOT the ``context_length`` in ``config.local.yaml``.
            * ``"malformed_response"`` — 2xx but body didn't parse
            * ``"unknown"`` — nothing else fit
        hint: Concrete next-step for the user to unblock themselves.
        raw: The underlying HTTP response body (if any) for logging.
    """

    def __init__(
        self,
        message: str,
        *,
        kind: str,
        hint: str = "",
        raw: Any = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.hint = hint
        self.raw = raw

    def to_dict(self, *, model: str = "") -> dict[str, Any]:
        """Serialize for inclusion in an agent-facing response payload."""
        return {
            "error": str(self),
            "error_kind": self.kind,
            "hint": self.hint,
            "model": model,
        }


def interpret_httpx_exception(
    exc: Exception,
    *,
    model: str,
    endpoint: str,
    server_label: str = "LM Studio",
) -> LocalInferenceError:
    """Convert an httpx-raised exception into a LocalInferenceError.

    Args:
        exc: The exception caught from an httpx call.
        model: Requested model id, to include in messages.
        endpoint: Which endpoint was hit (e.g. ``"/api/v1/chat"``) —
            surfaces in the hint so users know which surface failed.
        server_label: Friendly name for the server in messages.
    """
    # Connection-level failures → server not running
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
        return LocalInferenceError(
            f"{server_label} is not reachable at the configured base URL.",
            kind="server_unreachable",
            hint=(
                f"Open LM Studio on the main machine and start its server "
                f"(Developer tab → Start Server). Confirm "
                f"`curl <base_url>/v1/models` succeeds before retrying."
            ),
            raw=str(exc),
        )

    if isinstance(exc, (httpx.ReadTimeout, httpx.PoolTimeout)):
        return LocalInferenceError(
            f"Timed out waiting for {server_label} to respond.",
            kind="timeout",
            hint=(
                "Local inference can be slow — especially first-token "
                "latency on a cold model load or partial GPU offload. "
                "Consider a smaller model, raising the client timeout, "
                "or submitting the job asynchronously via llm_submit."
            ),
            raw=str(exc),
        )

    if isinstance(exc, httpx.HTTPStatusError):
        return _interpret_status_error(exc, model=model, endpoint=endpoint,
                                        server_label=server_label)

    # Generic httpx error we didn't model
    if isinstance(exc, httpx.HTTPError):
        return LocalInferenceError(
            f"HTTP error talking to {server_label}: {exc}",
            kind="unknown",
            hint="",
            raw=str(exc),
        )

    # Non-httpx — bubble through with wrapping so callers have a uniform shape
    return LocalInferenceError(
        f"{type(exc).__name__}: {exc}",
        kind="unknown",
        hint="",
        raw=str(exc),
    )


def _interpret_status_error(
    exc: httpx.HTTPStatusError,
    *,
    model: str,
    endpoint: str,
    server_label: str,
) -> LocalInferenceError:
    body = _safe_json(exc.response)
    err = _extract_error_dict(body)
    status = exc.response.status_code
    message = (err.get("message") or "").strip()
    code = (err.get("code") or "").strip()
    msg_lower = message.lower()

    # --- Model-not-loaded family -------------------------------------------
    # LM Studio variants observed in the wild:
    #   "Invalid model identifier '<id>'. There are no downloaded llm models."
    #   "No models loaded. Please load a model in the developer page ..."
    #   "Model '<id>' not found"
    # These all mean: the model the caller asked for isn't reachable on
    # any linked device.
    if (
        code == "model_not_found"
        or "no models loaded" in msg_lower
        or "no downloaded llm models" in msg_lower
        or "invalid model identifier" in msg_lower
        or "model not found" in msg_lower
    ):
        return LocalInferenceError(
            (
                f"Model {model!r} is not loaded on any device reachable "
                f"from LM Studio."
            ),
            kind="model_not_loaded",
            hint=(
                "Verify: (1) LM Studio is running on the main machine; "
                "(2) on the compute laptop, LM Link shows as Connected; "
                "(3) the requested model appears as Loaded in the laptop's "
                "Chat tab. Then `curl <base_url>/v1/models` on main should "
                "list the remote models. If only local models appear, the "
                "laptop isn't surfacing via LM Link — re-establish the link."
            ),
            raw=body,
        )

    # --- Endpoint-not-supported-for-model family ---------------------------
    # Some model types (embeddings) can't be called on a chat endpoint,
    # some chat endpoints may not support specific model capabilities.
    if "not supported" in msg_lower or "unsupported" in msg_lower:
        return LocalInferenceError(
            f"LM Studio refused model {model!r} on endpoint {endpoint}: {message}",
            kind="model_unsupported",
            hint=(
                "The model may not be chat-capable (e.g. an embedding model), "
                "or the endpoint may not support it. Try a different "
                "profile, or check the model's capabilities in LM Studio."
            ),
            raw=body,
        )

    # --- Context window exceeded -------------------------------------------
    # LM Studio returns HTTP 500 with a body like:
    #   {"error": "Context size has been exceeded."}
    # or variants ("Prompt exceeds context", "Context length exceeded",
    # "Input is too long for this model's context"). This is distinct from
    # the server error family below — it's actionable by the user (resize
    # context, shorten prompt, narrow tool preset) rather than a server bug.
    if status >= 400 and (
        "context size" in msg_lower
        or ("context" in msg_lower and ("exceed" in msg_lower or "too long" in msg_lower))
        or "prompt is too long" in msg_lower
        or "input is too long" in msg_lower
    ):
        return LocalInferenceError(
            (
                f"{server_label} rejected the request because the prompt "
                f"(plus tool schema and reasoning tokens) exceeds the "
                f"loaded model's context window."
                + (f" Server message: {message}" if message else "")
            ),
            kind="context_exceeded",
            hint=(
                "Three levers, in order of least-to-most disruptive: "
                "(1) increase the 'Context Length' slider on the loaded "
                "model in LM Studio and reload it — the effective cap is "
                "LM Studio's setting, not config.local.yaml; "
                "(2) narrow the tool preset so the wb_run schema carries "
                "fewer capability params (see work_buddy/llm/tool_presets.py); "
                "(3) shorten the system/user prompt, including pre-fetched "
                "context blocks. For reasoning models, also consider "
                "raising max_tokens — the model may be emitting a long "
                "hidden thinking block before any visible output."
            ),
            raw=body,
        )

    # --- MCP integrations-path failures (5xx with telling body) ------------
    # When a local model uses LM Studio's `integrations` tool-loop to hit
    # the work-buddy MCP gateway, failures there surface as an HTTP 500
    # from /api/v1/chat with a very specific body message. The raw text
    # ("MCP error -32001") is inscrutable unless you know that -32001 is
    # JSON-RPC's "server error / request timeout" code set by the client.
    # Surface the real meaning inline so callers don't have to pattern-match.
    if status >= 500:
        if "-32001" in message or "request timed out" in msg_lower:
            return LocalInferenceError(
                (
                    "LM Studio's MCP integration call to the work-buddy "
                    "gateway timed out waiting for a response (JSON-RPC "
                    "-32001). The gateway at localhost:5126/mcp did not "
                    "reply before LM Studio's deadline — usually means a "
                    "specific tool dispatch is slow or the gateway's event "
                    "loop is blocked."
                ),
                kind="mcp_gateway_timeout",
                hint=(
                    "First: `curl -s http://localhost:5126/health` — if "
                    "that's slow (>100ms), the gateway itself is blocked. "
                    "Check sidecar logs for 'Registry build slow' warnings. "
                    "If the gateway is fast but this still times out, the "
                    "specific capability the model tried to call is slow "
                    "(or hanging on a sync import — see "
                    "architecture/mcp-import-discipline). Reproduce with "
                    "`persist_tool_results=True` on llm_with_tools to see "
                    "which tool dispatch stalled."
                ),
                raw=body,
            )

        if "fetch failed" in msg_lower:
            return LocalInferenceError(
                (
                    "LM Studio's HTTP fetch to the work-buddy MCP gateway "
                    "failed at the transport layer (not a JSON-RPC timeout "
                    "— the TCP connection itself was refused or reset)."
                ),
                kind="mcp_fetch_failed",
                hint=(
                    "Check (1) the gateway is actually listening: "
                    "`curl -sf http://localhost:5126/health`; (2) no "
                    "firewall/AV is interfering with localhost:5126; "
                    "(3) if /health responds, the failure was transient — "
                    "a simple retry usually succeeds."
                ),
                raw=body,
            )

        if (
            "lm link" in msg_lower
            or "peer_keepalive_timeout" in msg_lower
            or "peer keepalive timeout" in msg_lower
        ):
            return LocalInferenceError(
                (
                    "LM Studio's LM Link connection to the compute device "
                    "dropped mid-call (peer keepalive timeout). Inference "
                    "is routed through LM Link, so the main machine can "
                    "serve a model loaded on a remote laptop — if that "
                    "link drops, every call fails until it's re-established."
                ),
                kind="lm_link_dropped",
                hint=(
                    "On the compute device: confirm LM Studio is running, "
                    "the model is loaded, and Tailscale (or whatever "
                    "transport LM Link uses) is connected. On the main "
                    "machine: restart LM Studio's server, then verify "
                    "`curl <base_url>/v1/models` lists the remote model."
                ),
                raw=body,
            )

    # --- Generic classification by status code -----------------------------
    if 400 <= status < 500:
        return LocalInferenceError(
            (
                f"{server_label} rejected the request at {endpoint} "
                f"(HTTP {status})"
                + (f": {message}" if message else ".")
            ),
            kind="bad_request",
            hint=(
                "The request payload was rejected. This is usually a shape "
                "mismatch between our backend and the LM Studio version in "
                "use. If you just upgraded LM Studio, the native-endpoint "
                "schema may have changed — check the body field in the raw "
                "error."
            ),
            raw=body,
        )

    return LocalInferenceError(
        (
            f"{server_label} returned HTTP {status} at {endpoint}"
            + (f": {message}" if message else ".")
        ),
        kind="server_error",
        hint=(
            "LM Studio's server encountered an internal error. Check its "
            "console/log on the main machine for a stack trace and retry."
        ),
        raw=body,
    )


def _safe_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except Exception:
        try:
            return response.text
        except Exception:
            return None


def _extract_error_dict(body: Any) -> dict[str, Any]:
    """Pull {message, code, type} from the variety of LM Studio shapes."""
    if isinstance(body, dict):
        # Direct shape: {error: {message, code, ...}}
        err = body.get("error")
        if isinstance(err, dict):
            return err
        # Some LM Studio versions return ``{"error": "<string>"}`` where
        # the raw string IS the message. Lift it into a message dict so
        # downstream matchers (e.g. context_exceeded) can pattern-match.
        if isinstance(err, str) and err:
            return {"message": err}
        # Sometimes the top-level is the error itself.
        if "message" in body or "code" in body:
            return body  # type: ignore[return-value]
    # Plain-string body (rare, but seen on some proxies).
    if isinstance(body, str) and body:
        return {"message": body}
    return {}
