"""Named LLM profile resolution.

A *profile* binds a human-friendly name (e.g., ``local_general``) to a
concrete backend host + model + generation limits. Profiles and backends
are declared in ``config.yaml`` / ``config.local.yaml``:

    llm:
      backends:
        lmstudio_local:
          base_url: "http://localhost:1234/v1"
          api_key_env: ""
      profiles:
        local_general:
          backend: lmstudio_local
          model: "qwen/qwen3-4b"
          max_output_tokens: 1024
          context_length: 8192
          execution_mode: local

The endpoint *kind* (``lmstudio_native`` vs ``openai_compat``) is **not**
configured here — it is derived from the tier binding at dispatch time.
The tier's ``tool_support`` flag determines the endpoint: tool-calling
tiers use the native endpoint; non-tool tiers use openai-compat (so LM
Studio's JIT auto-load works on cold requests). See
:mod:`work_buddy.llm.tiers` for tier definitions.

Legacy ``provider:`` keys on backend entries are tolerated for backward
compatibility but ignored; a warning is emitted when they disagree with
the tier-binding choice.

The ``execution_mode`` value (``local`` or ``cloud``) is threaded into
cost-log entries so local inference doesn't get priced against Claude's
per-token table.
"""

from __future__ import annotations

from typing import Any

from work_buddy.config import load_config


def _llm_cfg() -> dict[str, Any]:
    return load_config().get("llm", {}) or {}


def list_profiles() -> list[str]:
    """Return the names of all configured profiles."""
    return sorted((_llm_cfg().get("profiles") or {}).keys())


def resolve_profile(name: str) -> dict[str, Any]:
    """Resolve a named profile to a concrete backend host + model + limits.

    Returns a dict with:
        backend_id: str
        provider: str           — DEPRECATED: ignored by dispatch.
                                  Preserved only so ``_run_profile`` can
                                  warn on mismatch with the tier binding.
        base_url: str
        api_key_env: str | None
        model: str
        max_output_tokens: int
        context_length: int
        execution_mode: str     — "local" or "cloud"

    Raises:
        KeyError with the list of available profiles when ``name`` is
        unknown, or when the profile references a missing backend.
    """
    cfg = _llm_cfg()
    profiles = cfg.get("profiles") or {}
    backends = cfg.get("backends") or {}

    profile = profiles.get(name)
    if profile is None:
        available = ", ".join(sorted(profiles.keys())) or "(none configured)"
        raise KeyError(
            f"Unknown LLM profile {name!r}. Available: {available}. "
            f"Define it under llm.profiles in config.yaml or config.local.yaml."
        )

    backend_id = profile.get("backend")
    if not backend_id:
        raise KeyError(
            f"Profile {name!r} is missing required 'backend' field."
        )

    backend = backends.get(backend_id)
    if backend is None:
        available = ", ".join(sorted(backends.keys())) or "(none configured)"
        raise KeyError(
            f"Profile {name!r} references backend {backend_id!r}, "
            f"which is not defined. Available backends: {available}."
        )

    return {
        "backend_id": backend_id,
        # Preserved only for mismatch-warning in _run_profile; dispatch
        # uses the tier-binding's ``backend`` field instead.
        "provider": backend.get("provider"),
        "base_url": backend.get("base_url", ""),
        "api_key_env": backend.get("api_key_env") or None,
        "model": profile.get("model", ""),
        "max_output_tokens": int(profile.get("max_output_tokens", 1024)),
        "context_length": int(profile.get("context_length", 8192)),
        "execution_mode": profile.get("execution_mode", "local"),
    }
