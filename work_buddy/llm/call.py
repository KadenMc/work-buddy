"""General-purpose LLM call capability for the MCP gateway.

Wraps ``run_task()`` with auto-generated cache keys and schema resolution,
so callers never need to think about ``task_id`` or file paths.

Schemas can be provided inline (dict) or by name (str) — named schemas
resolve to JSON files in ``work_buddy/llm/schemas/``.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from work_buddy.logging_config import get_logger

logger = get_logger(__name__)

_SCHEMAS_DIR = Path(__file__).parent / "schemas"


def _resolve_schema(output_schema: dict | str | None) -> dict | None:
    """Resolve an output schema from inline dict or stored schema name.

    Accepts:
      - ``None`` → freeform text (no schema)
      - ``dict`` → inline JSON Schema, passed through directly
      - ``str``  → name of a stored schema in ``work_buddy/llm/schemas/``.
        Normalised: case-folded, ``.json`` extension stripped if present.
        E.g. ``"Email_Triage"``, ``"email_triage.json"``, ``"email_triage"``
        all resolve to ``schemas/email_triage.json``.
    """
    if output_schema is None:
        return None
    if isinstance(output_schema, dict):
        return output_schema

    # String → resolve to file
    name = output_schema.strip()
    # Strip .json extension (case-insensitive) if present
    if name.lower().endswith(".json"):
        name = name[: -len(".json")]
    name = name.lower()

    schema_path = _SCHEMAS_DIR / f"{name}.json"
    if not schema_path.exists():
        available = sorted(p.stem for p in _SCHEMAS_DIR.glob("*.json"))
        avail_str = ", ".join(available) if available else "(none)"
        raise FileNotFoundError(
            f"Schema '{output_schema}' not found at {schema_path}. "
            f"Available schemas: {avail_str}"
        )

    return json.loads(schema_path.read_text(encoding="utf-8"))


def _make_task_id(system: str, user: str, schema: dict | None) -> str:
    """Auto-generate a cache-key task_id from call content."""
    h = hashlib.sha256()
    h.update(system.encode())
    h.update(user.encode())
    if schema is not None:
        h.update(json.dumps(schema, sort_keys=True).encode())
    return f"llm_call:{h.hexdigest()[:12]}"


def llm_call(
    *,
    system: str,
    user: str,
    output_schema: dict | str | None = None,
    tier: str = "haiku",
    max_tokens: int = 1024,
    temperature: float = 0.0,
    cache_ttl_minutes: int | None = None,
) -> dict[str, Any]:
    """Make a single LLM API call with optional structured output.

    This is the general-purpose "Tier 2" execution primitive — cheaper than
    spawning a full agent session, more capable than pure Python.

    Args:
        system: System prompt.
        user: User message.
        output_schema: JSON Schema for constrained output. Pass a dict for
            inline schemas, or a string name to load from
            ``work_buddy/llm/schemas/<name>.json``.  Omit for freeform text.
        tier: Model tier — ``"haiku"`` (default), ``"sonnet"``, or ``"opus"``.
        max_tokens: Max response tokens.
        temperature: Sampling temperature.
        cache_ttl_minutes: Cache TTL. ``None`` = config default, ``0`` = skip.

    Returns:
        Dict with ``content`` (raw text), ``parsed`` (dict if schema used),
        ``model``, ``input_tokens``, ``output_tokens``, ``cached``, ``error``.
    """
    from work_buddy.llm.runner import ModelTier, run_task

    # Resolve schema
    resolved_schema = _resolve_schema(output_schema)

    # Auto-generate task_id for caching / cost tracking
    task_id = _make_task_id(system, user, resolved_schema)

    # Map tier string to enum
    try:
        model_tier = ModelTier(tier.lower())
    except ValueError:
        return {
            "content": "",
            "parsed": None,
            "model": "",
            "input_tokens": 0,
            "output_tokens": 0,
            "cached": False,
            "error": f"Invalid tier '{tier}'. Must be one of: haiku, sonnet, opus",
        }

    result = run_task(
        task_id=task_id,
        system=system,
        user=user,
        output_schema=resolved_schema,
        tier=model_tier,
        max_tokens=max_tokens,
        temperature=temperature,
        cache_ttl_minutes=cache_ttl_minutes,
    )

    return {
        "content": result.content,
        "parsed": result.parsed,
        "model": result.model,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "cached": result.cached,
        "error": result.error,
    }
