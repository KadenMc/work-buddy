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
            * ``"server_error"`` — 5xx
            * ``"timeout"`` — read or connect timeout
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
        # Sometimes the top-level is the error itself.
        if "message" in body or "code" in body:
            return body  # type: ignore[return-value]
    return {}
