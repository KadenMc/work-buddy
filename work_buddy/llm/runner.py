"""Core LLM task runner.

Dispatches requests to the right backend (Anthropic cloud by default,
or an OpenAI-compatible local server when a ``profile`` is set),
checks cache before calling, logs costs after, and returns structured
results. All configuration comes from ``config.yaml`` under the
``llm`` key. API key for Anthropic comes from ``ANTHROPIC_API_KEY``.

``httpx`` (used by the openai_compat backend) is pure Python — no C
extensions — so it's safe to invoke through ``asyncio.to_thread`` from
the MCP gateway, matching the anthropic SDK's safety profile.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from work_buddy.config import load_config
from work_buddy.logging_config import get_logger


class ModelTier(str, Enum):
    """Model tiers for access control. Tasks can lock to specific tiers."""

    HAIKU = "haiku"
    SONNET = "sonnet"
    OPUS = "opus"

logger = get_logger(__name__)


@dataclass
class TaskResult:
    """Result of an LLM task execution."""

    content: str  # raw response text
    parsed: dict | None = None  # JSON-parsed if applicable
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cached: bool = False  # True if served from cache
    cache_key: str | None = None
    error: str | None = None


def _get_llm_config() -> dict[str, Any]:
    cfg = load_config()
    return cfg.get("llm", {})


def _default_model() -> str:
    return _get_llm_config().get("default_model", "claude-haiku-4-5-20251001")


def _resolve_model_for_tier(tier: ModelTier) -> str:
    """Resolve a model tier to a concrete model ID from config."""
    models = _get_llm_config().get("models", {})
    defaults = {
        ModelTier.HAIKU: "claude-haiku-4-5-20251001",
        ModelTier.SONNET: "claude-sonnet-4-6",
        ModelTier.OPUS: "claude-opus-4-6",
    }
    return models.get(tier.value, defaults.get(tier, defaults[ModelTier.HAIKU]))


def run_task(
    *,
    task_id: str,
    system: str,
    user: str,
    model: str | None = None,
    max_tokens: int = 1024,
    temperature: float = 0,
    json_mode: bool = False,
    output_schema: dict | None = None,
    cache_ttl_minutes: int | None = None,
    content_hash: str | None = None,
    content_sample: str | None = None,
    trace_id: str | None = None,
    tier: ModelTier | None = None,
    allowed_tiers: list[ModelTier] | None = None,
    profile: str | None = None,
) -> TaskResult:
    """Run a single LLM task with optional caching.

    Args:
        task_id: Unique identifier for caching (e.g., "chrome_infer:github.com/repo").
        system: System prompt.
        user: User message content.
        model: Model to use. Default from config (Haiku). Overridden by ``tier``.
        max_tokens: Max response tokens.
        temperature: Sampling temperature.
        json_mode: If True, attempt to parse response as JSON (best-effort).
            Prefer ``output_schema`` for guaranteed structured output.
        output_schema: JSON Schema dict for constrained decoding. When provided,
            the API guarantees the response matches this schema exactly — no
            parsing failures, no missing fields. Implicitly enables JSON parsing.
            Uses Anthropic's ``output_config.format.json_schema``.
        cache_ttl_minutes: Cache TTL. None = use config default. 0 = no caching.
        content_hash: Hash of input content for cache invalidation.
        content_sample: ~500 char sample for fuzzy cache matching when hash differs.
        tier: Model tier to use (haiku/sonnet/opus). Overrides ``model``.
        allowed_tiers: If set, restricts which tiers can be used. Rejects
            requests for disallowed tiers. Used by classify() to lock tasks
            to Haiku — agents cannot escalate to Sonnet/Opus.
        profile: Named LLM profile (e.g. ``"local_general"``) declared
            under ``llm.profiles`` in config. Mutually exclusive with
            ``tier``. When set, dispatches to the configured backend
            (e.g. LM Studio) instead of Anthropic.

    Returns:
        TaskResult with response content, token counts, and cache status.
    """
    llm_cfg = _get_llm_config()

    if profile is not None and tier is not None:
        return TaskResult(
            content="",
            error="'profile' and 'tier' are mutually exclusive",
        )

    # Resolve profile → backend/model/limits, or fall back to tier/Anthropic
    profile_info: dict | None = None
    if profile is not None:
        try:
            from work_buddy.llm.profiles import resolve_profile
            profile_info = resolve_profile(profile)
        except KeyError as exc:
            return TaskResult(content="", error=str(exc))
        resolved_model = profile_info["model"]
        execution_mode = profile_info["execution_mode"]
        backend_id = profile_info["backend_id"]
        # Profile may cap output tokens more tightly than the caller asked for
        max_tokens = min(max_tokens, profile_info["max_output_tokens"])
    elif tier is not None:
        if allowed_tiers and tier not in allowed_tiers:
            allowed_str = ", ".join(t.value for t in allowed_tiers)
            return TaskResult(
                content="",
                error=f"Model tier '{tier.value}' not allowed for this task. Allowed: {allowed_str}",
            )
        resolved_model = _resolve_model_for_tier(tier)
        execution_mode = "cloud"
        backend_id = "anthropic_default"
    else:
        resolved_model = model or _default_model()
        execution_mode = "cloud"
        backend_id = "anthropic_default"

    # Scope the cache key by backend+model so Claude and local results
    # never collide when callers pass the same (system, user, schema).
    scoped_task_id = f"{backend_id}:{resolved_model}:{task_id}"
    ttl = cache_ttl_minutes if cache_ttl_minutes is not None else llm_cfg.get("cache_ttl_minutes", 30)

    # Check cache
    if ttl > 0:
        from work_buddy.llm.cache import get as cache_get

        cached = cache_get(scoped_task_id, content_hash=content_hash, content_sample=content_sample)
        if cached is not None:
            logger.info("Cache hit for task %s", scoped_task_id)
            # Log the cache hit for cost tracking (zero cost but counted)
            from work_buddy.llm.cost import log_call
            log_call(
                model=cached.get("model", resolved_model),
                input_tokens=cached.get("tokens", {}).get("input", 0),
                output_tokens=cached.get("tokens", {}).get("output", 0),
                task_id=task_id,
                trace_id=trace_id,
                cached=True,
                execution_mode=execution_mode,
                backend=backend_id,
            )
            return TaskResult(
                content=cached["result"].get("content", ""),
                parsed=cached["result"].get("parsed"),
                model=cached.get("model", resolved_model),
                input_tokens=cached.get("tokens", {}).get("input", 0),
                output_tokens=cached.get("tokens", {}).get("output", 0),
                cached=True,
                cache_key=scoped_task_id,
            )

    # Local profile path: dispatch via openai_compat backend.
    if profile_info is not None:
        return _run_profile(
            profile_info=profile_info,
            task_id=task_id,
            scoped_task_id=scoped_task_id,
            system=system,
            user=user,
            max_tokens=max_tokens,
            temperature=temperature,
            output_schema=output_schema,
            json_mode=json_mode,
            ttl=ttl,
            content_hash=content_hash,
            content_sample=content_sample,
            trace_id=trace_id,
        )

    # Call Anthropic API — check dedicated subagent key first, then general key
    api_key = os.environ.get("SUBAGENT_ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        # Try loading from .env file at repo root
        env_file = Path(__file__).parent.parent.parent / ".env"
        if env_file.exists():
            for line in env_file.read_text(encoding="utf-8").splitlines():
                if line.startswith("SUBAGENT_ANTHROPIC_API_KEY="):
                    api_key = line.split("=", 1)[1].strip()
                    break
                elif line.startswith("ANTHROPIC_API_KEY=") and not api_key:
                    api_key = line.split("=", 1)[1].strip()
    if not api_key:
        return TaskResult(
            content="",
            error="ANTHROPIC_API_KEY environment variable not set",
            model=resolved_model,
        )

    try:
        import anthropic

        client = anthropic.Anthropic(api_key=api_key)

        # Build API kwargs
        api_kwargs: dict[str, Any] = {
            "model": resolved_model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }

        # Structured output: constrained decoding via output_config
        if output_schema is not None:
            api_kwargs["output_config"] = {
                "format": {
                    "type": "json_schema",
                    "schema": output_schema,
                }
            }

        response = client.messages.create(**api_kwargs)

        content = response.content[0].text if response.content else ""
        input_tokens = response.usage.input_tokens
        output_tokens = response.usage.output_tokens

        # Parse JSON — guaranteed valid when output_schema was used,
        # best-effort when json_mode=True without a schema
        parsed = None
        if (output_schema is not None or json_mode) and content:
            try:
                text = content.strip()
                # Handle markdown-wrapped JSON (only needed for json_mode fallback)
                if text.startswith("```"):
                    text = text.split("\n", 1)[1] if "\n" in text else text[3:]
                    text = text.rsplit("```", 1)[0]
                parsed = json.loads(text)
            except (json.JSONDecodeError, IndexError):
                if output_schema is not None:
                    logger.error("Schema-constrained response failed to parse for task %s", task_id)
                else:
                    logger.warning("Failed to parse JSON from task %s", task_id)

        result = TaskResult(
            content=content,
            parsed=parsed,
            model=resolved_model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached=False,
            cache_key=scoped_task_id,
        )

        # Log cost
        from work_buddy.llm.cost import log_call

        log_call(
            model=resolved_model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            task_id=task_id,
            trace_id=trace_id,
            execution_mode=execution_mode,
            backend=backend_id,
        )

        # Cache result
        if ttl > 0:
            from work_buddy.llm.cache import put as cache_put

            cache_put(
                task_id=scoped_task_id,
                result={"content": content, "parsed": parsed},
                content_hash=content_hash,
                content_sample=content_sample,
                ttl_minutes=ttl,
                model=resolved_model,
                tokens={"input": input_tokens, "output": output_tokens},
            )

        logger.info(
            "LLM task %s: %d in / %d out tokens (%s)",
            task_id, input_tokens, output_tokens, resolved_model,
        )
        return result

    except Exception as exc:
        logger.exception("LLM task %s failed", task_id)
        return TaskResult(
            content="",
            error=str(exc),
            model=resolved_model,
        )


def _run_profile(
    *,
    profile_info: dict,
    task_id: str,
    scoped_task_id: str,
    system: str,
    user: str,
    max_tokens: int,
    temperature: float,
    output_schema: dict | None,
    json_mode: bool,
    ttl: int,
    content_hash: str | None,
    content_sample: str | None,
    trace_id: str | None,
) -> TaskResult:
    """Dispatch a profile-based request to the configured backend.

    Currently only ``openai_compat`` is supported. Adding a new provider
    means adding a branch here plus a new ``call_*`` backend function.
    """
    provider = profile_info["provider"]
    resolved_model = profile_info["model"]
    backend_id = profile_info["backend_id"]
    execution_mode = profile_info["execution_mode"]

    try:
        if provider == "openai_compat":
            from work_buddy.llm.backends import call_openai_compat

            backend_result = call_openai_compat(
                base_url=profile_info["base_url"],
                model=resolved_model,
                system=system,
                user=user,
                max_tokens=max_tokens,
                temperature=temperature,
                output_schema=output_schema,
                api_key_env=profile_info["api_key_env"],
            )
        else:
            return TaskResult(
                content="",
                error=f"Unknown backend provider: {provider!r}",
                model=resolved_model,
            )
    except Exception as exc:
        logger.exception("Profile %s backend call failed", backend_id)
        return TaskResult(
            content="",
            error=f"{type(exc).__name__}: {exc}",
            model=resolved_model,
        )

    content = backend_result["content"]
    input_tokens = backend_result["input_tokens"]
    output_tokens = backend_result["output_tokens"]
    server_model = backend_result["model"]

    parsed: dict | None = None
    if (output_schema is not None or json_mode) and content:
        try:
            text = content.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1] if "\n" in text else text[3:]
                text = text.rsplit("```", 1)[0]
            parsed = json.loads(text)
        except (json.JSONDecodeError, IndexError):
            if output_schema is not None:
                logger.error(
                    "Schema-constrained response failed to parse for task %s", task_id,
                )
            else:
                logger.warning("Failed to parse JSON from task %s", task_id)

    result = TaskResult(
        content=content,
        parsed=parsed,
        model=server_model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached=False,
        cache_key=scoped_task_id,
    )

    from work_buddy.llm.cost import log_call
    log_call(
        model=server_model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        task_id=task_id,
        trace_id=trace_id,
        execution_mode=execution_mode,
        backend=backend_id,
    )

    if ttl > 0:
        from work_buddy.llm.cache import put as cache_put
        cache_put(
            task_id=scoped_task_id,
            result={"content": content, "parsed": parsed},
            content_hash=content_hash,
            content_sample=content_sample,
            ttl_minutes=ttl,
            model=server_model,
            tokens={"input": input_tokens, "output": output_tokens},
        )

    logger.info(
        "LLM task %s: %d in / %d out tokens (%s via %s)",
        task_id, input_tokens, output_tokens, server_model, backend_id,
    )
    return result
