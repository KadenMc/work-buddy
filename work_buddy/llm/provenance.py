"""First-class inference-call provenance — one authored-ish record per call.

Writes ``agents/<session>/inference_calls.jsonl`` (one JSON object per line),
beside the cost ledger (`llm_costs.jsonl`) but a distinct record: it answers
*what is calling models, and why*, across providers and both completions and
embeddings. It does NOT replace the cost ledger — cost stays authoritative for $.

The description is ``<call site>: <detail>``:
- **call site** is derived automatically from the caller chain (zero per-site
  effort, no LLM involved) — e.g. ``summarize``, ``classify``, mapped to a clean
  label where one is known.
- **detail** is an optional readily-available one-liner the call site attaches
  via :func:`work_buddy.inference.call_context.inference_detail` (e.g. a tab
  title). Absent → just the call-site label.

Capture is best-effort: a failure here never affects the real call. The
cross-session reader for the dashboard feed lives in the dashboard layer
(`dashboard/api.py`), reusing the cost tab's session-scan helpers.
"""

from __future__ import annotations

import inspect
import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Frames internal to the call machinery — skipped when deriving the call site.
_INTERNAL_FILES = {
    "provenance.py", "cost.py", "runner.py", "runner_v2.py",
    "call.py", "submit.py", "with_tools.py", "call_context.py",
}

# Light prettify for the common, high-traffic call sites (open question #1 —
# use the raw module name as-is otherwise). Keyed on the source module name.
_CALL_SITE_LABELS = {
    "summarize": "Summarize",
    "classify": "Classify",
    "recommend": "Triage",
    "intent": "Intent",
    "decomposed": "Decompose",
    "journal": "Journal",
    "dense": "Embed",
}


def _provenance_log_path() -> Path:
    """Resolve the provenance log path, routing to the originating session.

    Mirrors ``cost._cost_log_path`` so async-replayed (`llm_submit`) and sidecar
    calls land in the agent's dir, not the sidecar's.
    """
    from work_buddy.agent_session import (
        get_originating_session,
        get_session_dir,
    )

    override = get_originating_session()
    session_dir = get_session_dir(override) if override else get_session_dir()
    return session_dir / "inference_calls.jsonl"


def _derive_call_site() -> str:
    """First external frame in the caller chain → a clean call-site label."""
    for frame_info in inspect.stack()[1:]:
        name = Path(frame_info.filename).name
        if name in _INTERNAL_FILES:
            continue
        if name.startswith("<") or "site-packages" in frame_info.filename:
            break
        module = name.replace(".py", "")
        return _CALL_SITE_LABELS.get(module, module)
    return "inference"


def record_inference_call(
    *,
    kind: str,                       # "completion" | "embedding"
    model: str,
    provider: str,
    execution_mode: str,            # "local" | "cloud"
    status: str,                    # "ok" | "cached" | "error" | ...
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    item_count: int | None = None,  # embeddings: docs in the batch
    task_id: str | None = None,
    trace_id: str | None = None,
    call_id: str | None = None,
    detail: str | None = None,
    call_site: str | None = None,
    latency_ms: float | None = None,
    error: str | None = None,
) -> None:
    """Append one provenance record. Best-effort — never raises into the caller.

    ``call_id`` / ``detail`` default to the ambient values bound for the call
    (see :mod:`work_buddy.inference.call_context`); ``call_site`` is derived
    from the caller chain unless passed explicitly (embeddings pass it, since
    their meaningful site isn't the immediate caller).
    """
    try:
        from work_buddy.inference.call_context import (
            current_call_elapsed_ms,
            current_call_id,
            current_detail,
        )

        cid = call_id or current_call_id() or uuid.uuid4().hex[:12]
        det = detail if detail is not None else current_detail()
        site = call_site or _derive_call_site()
        description = f"{site}: {det}" if det else site
        # End-to-end latency for every provider (cloud included) when the caller
        # didn't measure it explicitly.
        if latency_ms is None:
            latency_ms = current_call_elapsed_ms()

        entry = {
            "call_id": cid,
            "call_site": site,
            "detail": det,
            "description": description,
            "kind": kind,
            "model": model,
            "provider": provider,
            "execution_mode": execution_mode,
            "status": status,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "item_count": item_count,
            "task_id": task_id,
            "trace_id": trace_id,
            "latency_ms": latency_ms,
            "error": error,
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }

        path = _provenance_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        logger.debug("provenance write failed", exc_info=True)
        return

    # Best-effort live push to the dashboard Inference-activity feed.
    try:
        from work_buddy.dashboard.events import publish_auto
        publish_auto("inference.call_logged", {
            "call_id": cid,
            "description": description,
            "kind": kind,
            "model": model,
            "execution_mode": execution_mode,
            "status": status,
        })
    except Exception:
        pass
