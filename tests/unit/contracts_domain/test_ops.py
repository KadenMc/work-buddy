from __future__ import annotations

import pytest

from work_buddy.contracts import ContractAuthorityError
from work_buddy.contracts_domain import ops


class _Service:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def create(self, payload, *, actor, intent_id):
        self.calls.append(
            {"payload": payload, "actor": actor, "intent_id": intent_id}
        )
        return {"contract_id": "a" * 32, **payload}


def test_create_contract_uses_sealed_native_service_and_session_actor(monkeypatch) -> None:
    service = _Service()
    monkeypatch.setattr(ops, "get_originating_session", lambda: "session-12345678")
    monkeypatch.setattr(ops, "native_service_if_sealed", lambda: service)

    result = ops.create_contract(
        payload='{"title":"Draft","status":"draft"}',
        client_mutation_id="contract-create-0001",
    )

    assert result["contract_id"] == "a" * 32
    assert service.calls[0]["payload"] == {"title": "Draft", "status": "draft"}
    assert service.calls[0]["actor"].startswith("agent-run:")
    assert service.calls[0]["intent_id"] == "contract-create-0001"


def test_create_contract_refuses_unsealed_authority(monkeypatch) -> None:
    monkeypatch.setattr(ops, "get_originating_session", lambda: "session-12345678")
    monkeypatch.setattr(ops, "native_service_if_sealed", lambda: None)

    with pytest.raises(ContractAuthorityError):
        ops.create_contract(
            payload={"title": "Draft"},
            client_mutation_id="contract-create-0002",
        )
