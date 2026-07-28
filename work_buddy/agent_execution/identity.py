"""Provider-independent execution identity prompt plumbing."""

from __future__ import annotations

import json

from .models import AgentSpawnRequest


def prompt_with_execution_identity(
    request: AgentSpawnRequest,
    *,
    harness_id: str,
) -> str:
    """Prepend the exact Work Buddy identity the hosted agent must initialize.

    Providers isolate ``WORK_BUDDY_SESSION_ID`` to the same caller-supplied
    value.  The explicit prompt contract avoids relying on lifecycle hooks or
    a native harness thread ID, neither of which is the durable Co-work lease
    identity.
    """

    session_json = json.dumps(request.session_id, ensure_ascii=True)
    harness_json = json.dumps(harness_id, ensure_ascii=True)
    return (
        "## Work Buddy execution identity\n\n"
        "Before calling any other Work Buddy tool, call `wb_init` exactly once "
        "with these exact arguments:\n"
        f"- `session_id={session_json}`\n"
        f"- `harness_id={harness_json}`\n"
        "Do not substitute an inherited bootstrap ID or a native harness "
        "thread ID for this execution identity.\n\n"
        "## Document-agent brief\n\n"
        f"{request.prompt}"
    )
