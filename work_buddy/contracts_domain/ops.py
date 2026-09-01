"""MCP-facing mutations for the Contracts SQLite authority."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from work_buddy.agent_session import get_originating_session
from work_buddy.contracts import ContractAuthorityError
from work_buddy.contracts_domain.provider import native_service_if_sealed


def create_contract(
    *,
    payload: str | Mapping[str, Any],
    client_mutation_id: str,
) -> dict[str, Any]:
    """Create one revisioned contract after explicit workflow confirmation."""

    if isinstance(payload, str):
        parsed = json.loads(payload)
    else:
        parsed = payload
    if not isinstance(parsed, Mapping):
        raise ValueError("payload must be a JSON object")
    if not isinstance(client_mutation_id, str) or not client_mutation_id.strip():
        raise ValueError("client_mutation_id is required")
    session_id = get_originating_session()
    if not session_id:
        raise ContractAuthorityError(
            "Contract creation requires an attributed Work Buddy agent session."
        )
    service = native_service_if_sealed()
    if service is None:
        raise ContractAuthorityError(
            "Contracts SQLite authority has not been sealed; no contract was created."
        )
    actor = "agent-run:" + hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    return service.create(
        dict(parsed),
        actor=actor,
        intent_id=client_mutation_id,
    )


__all__ = ["create_contract"]
