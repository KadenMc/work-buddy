"""Redacted Codex SDK account/model probe.

This module runs in a short-lived child process so a stuck local App Server can
be bounded by the caller's timeout.  Its stdout contract contains only safe
provider state and model catalog fields; account identifiers are never read
into the projection.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from .codex import (
    codex_subscription_config,
    read_effective_codex_config,
    validate_subscription_codex_config,
)


def _state_key(
    *,
    availability: str,
    runtime_version: str,
    model_ids: tuple[str, ...],
) -> str:
    material = "\0".join(
        ("codex", availability, "chatgpt", runtime_version, *sorted(model_ids))
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:20]


def _unavailable(
    *,
    availability: str,
    reason: str,
    runtime_version: str = "",
) -> dict[str, Any]:
    return {
        "availability": availability,
        "auth_mode": "chatgpt",
        "models": [],
        "unavailable_reason": reason,
        "state_key": _state_key(
            availability=availability,
            runtime_version=runtime_version,
            model_ids=(),
        ),
    }


def collect_redacted_probe(
    client_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Query ``account/read`` and ``model/list`` without projecting identity."""

    try:
        import openai_codex
        from openai_codex import Codex
    except ImportError:
        return _unavailable(
            availability="unavailable",
            reason="Codex support is not installed.",
        )

    runtime_version = str(getattr(openai_codex, "__version__", ""))
    require_effective_config = client_factory is None

    try:
        with TemporaryDirectory(
            prefix="work-buddy-codex-probe-",
            ignore_cleanup_errors=True,
        ) as host_directory:
            host_cwd = Path(host_directory).resolve()
            factory = client_factory or (
                lambda: Codex(
                    config=codex_subscription_config(
                        cwd=host_cwd,
                        env=os.environ,
                    )
                )
            )
            with factory() as codex:
                if require_effective_config:
                    effective_config = read_effective_codex_config(
                        codex,
                        cwd=host_cwd,
                    )
                    validate_subscription_codex_config(effective_config)
                account_response = codex.account(refresh_token=False)
                account_container = getattr(account_response, "account", None)
                account = getattr(account_container, "root", account_container)
                account_type = str(getattr(account, "type", "") or "")

                if account is None:
                    return _unavailable(
                        availability="auth_required",
                        reason="Sign in to Codex with ChatGPT.",
                        runtime_version=runtime_version,
                    )
                if account_type.casefold() != "chatgpt":
                    return _unavailable(
                        availability="unavailable",
                        reason=(
                            "Codex is not using a ChatGPT account. "
                            "Sign in to Codex with ChatGPT."
                        ),
                        runtime_version=runtime_version,
                    )

                response = codex.models(include_hidden=False)
            models: list[dict[str, Any]] = []
            seen: set[str] = set()
            for raw in getattr(response, "data", ()):
                if bool(getattr(raw, "hidden", False)):
                    continue
                model_id = str(getattr(raw, "model", "") or "").strip()
                if not model_id or model_id in seen:
                    continue
                seen.add(model_id)
                label = str(
                    getattr(raw, "display_name", "") or model_id
                ).strip()
                models.append(
                    {
                        "id": model_id,
                        "label": label,
                        "available": True,
                        "description": str(
                            getattr(raw, "description", "") or ""
                        ),
                        "unavailable_reason": "",
                        "is_default": bool(
                            getattr(raw, "is_default", False)
                        ),
                    }
                )

            if not models:
                return _unavailable(
                    availability="unavailable",
                    reason="Codex didn't report any available models.",
                    runtime_version=runtime_version,
                )

            return {
                "availability": "ready",
                "auth_mode": "chatgpt",
                "models": models,
                "unavailable_reason": "",
                "state_key": _state_key(
                    availability="ready",
                    runtime_version=runtime_version,
                    model_ids=tuple(model["id"] for model in models),
                ),
            }
    except Exception:
        # SDK/RPC exceptions may contain paths, process diagnostics, or account
        # details.  The parent receives only this fixed safe projection.
        return _unavailable(
            availability="unknown",
            reason="Codex couldn't be checked.",
            runtime_version=runtime_version,
        )


def main() -> int:
    payload = collect_redacted_probe()
    sys.stdout.write(json.dumps(payload, separators=(",", ":"), ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
