from __future__ import annotations

import io
import json

from work_buddy.hindsight_projection.contracts import DestinationObservationState
from work_buddy.hindsight_projection.hindsight import (
    DERIVATIVE_CONTEXT,
    HindsightProjectionDestination,
)

from .conftest import make_snapshot, make_spec


class _Client:
    def __init__(self) -> None:
        self.calls = []

    def retain(self, **kwargs):
        self.calls.append(kwargs)
        return {"ok": True}


class _Response:
    def __init__(self, payload=b"{}") -> None:
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self._payload


def test_hindsight_receives_exact_proposition_and_explicit_derivative_labels() -> None:
    client = _Client()
    requests = []

    def opener(request, timeout):
        requests.append(request)
        return _Response()

    destination = HindsightProjectionDestination(
        client_factory=lambda: client,
        bank_id="personal",
        base_url="http://hindsight.test",
        opener=opener,
        base_tags_factory=lambda *tags: list(tags),
    )
    snapshot = make_snapshot(make_spec())
    receipt = destination.upsert(snapshot, snapshot.proposition_bytes)

    call = client.calls[0]
    assert call["content"] == snapshot.proposition
    assert call["context"] == DERIVATIVE_CONTEXT
    assert "kind:semantic-derivative" in call["tags"]
    assert "fidelity:derivative" in call["tags"]
    assert not any("verbatim" in tag for tag in call["tags"])
    assert receipt.document_id == destination.document_id(
        snapshot.claim_id, snapshot.policy_id
    )


def test_inspection_uses_generation_tag_and_delete_is_idempotent() -> None:
    snapshot = make_snapshot(make_spec())
    payload = json.dumps(
        {"tags": [f"truth-generation:{snapshot.claim_generation}"]}
    ).encode()
    requests = []

    def opener(request, timeout):
        requests.append(request)
        return _Response(payload if isinstance(request, str) else b"{}")

    destination = HindsightProjectionDestination(
        client_factory=lambda: _Client(),
        bank_id="personal",
        base_url="http://hindsight.test",
        opener=opener,
        base_tags_factory=lambda *tags: list(tags),
    )
    document_id = destination.document_id(snapshot.claim_id, snapshot.policy_id)
    observation = destination.inspect(document_id, snapshot.claim_generation)
    assert observation.state is DestinationObservationState.PRESENT_MATCH
    destination.remove(document_id)
    assert getattr(requests[-1], "method", None) == "DELETE"
