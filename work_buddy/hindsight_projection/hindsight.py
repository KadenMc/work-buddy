"""Production-shaped Hindsight destination adapter.

The retain call is intentionally wrapped by ``AgentExecutionProjectionDisclosure``;
calling ``upsert`` directly with protected content would bypass the required
disclosure manifest.  Tests inject a fake client/opener so the optional
Hindsight dependency is not required in the base development environment.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Callable
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from work_buddy.hindsight_projection.contracts import (
    DestinationObservation,
    DestinationObservationState,
    DestinationReceipt,
    ProjectionClaimSnapshot,
    ProjectionDestinationError,
    utc_now,
)


DERIVATIVE_CONTEXT = (
    "Derived projection of a currently confirmed Work Buddy Truth claim. "
    "This document is not a verbatim source and is not Truth authority."
)


class HindsightProjectionDestination:
    """Stable-document Hindsight adapter with inspectable generation tags."""

    def __init__(
        self,
        *,
        client_factory: Callable[[], Any] | None = None,
        bank_id: str | None = None,
        base_url: str | None = None,
        opener: Callable[..., Any] = urlopen,
        base_tags_factory: Callable[..., list[str]] | None = None,
    ) -> None:
        self._client_factory = client_factory
        self._bank_id = bank_id
        self._base_url = base_url
        self._opener = opener
        self._base_tags_factory = base_tags_factory

    @staticmethod
    def document_id(claim_id: str, policy_id: str) -> str:
        digest = hashlib.sha256(
            (claim_id + "\0" + policy_id).encode("utf-8")
        ).hexdigest()
        return f"wb-truth-{digest}"

    def _runtime(self) -> tuple[Any, str, str, Callable[..., list[str]]]:
        if (
            self._client_factory is None
            or self._bank_id is None
            or self._base_url is None
            or self._base_tags_factory is None
        ):
            # Lazy imports keep the base package usable when the optional
            # ``memory`` dependency extra is not installed.
            from work_buddy.memory.client import (
                _cfg,
                build_tags,
                get_bank_id,
                get_client,
            )

            client_factory = self._client_factory or get_client
            bank_id = self._bank_id or get_bank_id()
            base_url = self._base_url or str(
                _cfg().get("base_url", "http://localhost:8888")
            )
            tags_factory = self._base_tags_factory or build_tags
            return client_factory(), bank_id, base_url.rstrip("/"), tags_factory
        return (
            self._client_factory(),
            self._bank_id,
            self._base_url.rstrip("/"),
            self._base_tags_factory,
        )

    @staticmethod
    def _claim_tag(claim_id: str) -> str:
        return "truth-claim:" + hashlib.sha256(claim_id.encode("utf-8")).hexdigest()

    def upsert(
        self,
        snapshot: ProjectionClaimSnapshot,
        exact_content: bytes,
    ) -> DestinationReceipt:
        if exact_content != snapshot.proposition_bytes:
            raise ProjectionDestinationError(
                "destination received bytes that do not match the Truth snapshot"
            )
        try:
            text = exact_content.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ProjectionDestinationError("projection content is not UTF-8") from exc
        client, bank_id, _base_url, tags_factory = self._runtime()
        tags = tags_factory(
            "source:truth",
            "kind:semantic-derivative",
            "authority:truth",
            "truth-status:confirmed",
            f"truth-generation:{snapshot.claim_generation}",
            self._claim_tag(snapshot.claim_id),
            f"projection-method:{snapshot.projection_method}",
            "fidelity:derivative",
        )
        document_id = self.document_id(snapshot.claim_id, snapshot.policy_id)
        try:
            client.retain(
                bank_id=bank_id,
                content=text,
                context=DERIVATIVE_CONTEXT,
                document_id=document_id,
                timestamp=datetime.fromisoformat(
                    snapshot.evaluated_at.replace("Z", "+00:00")
                ),
                tags=tags,
            )
        except Exception as exc:
            # With an LLM-backed retain call, a generic client exception cannot
            # prove whether source bytes crossed the boundary.
            raise ProjectionDestinationError("Hindsight retain outcome is unknown") from exc
        return DestinationReceipt(
            document_id=document_id,
            claim_generation=snapshot.claim_generation,
            acknowledged_at=utc_now(),
        )

    def inspect(
        self,
        document_id: str,
        expected_generation: str,
    ) -> DestinationObservation:
        _client, bank_id, base_url, _tags_factory = self._runtime()
        url = (
            f"{base_url}/v1/default/banks/{quote(bank_id, safe='')}/documents/"
            f"{quote(document_id, safe='')}"
        )
        try:
            with self._opener(url, timeout=10) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            if exc.code == 404:
                return DestinationObservation(
                    state=DestinationObservationState.ABSENT,
                    document_id=document_id,
                )
            return DestinationObservation(
                state=DestinationObservationState.UNKNOWN,
                document_id=document_id,
            )
        except Exception:
            return DestinationObservation(
                state=DestinationObservationState.UNKNOWN,
                document_id=document_id,
            )
        if isinstance(payload, dict):
            candidate = payload
            for key in ("document", "item"):
                nested = candidate.get(key)
                if isinstance(nested, dict):
                    candidate = nested
                    break
            tags = candidate.get("tags", [])
        else:
            tags = []
        if not isinstance(tags, list):
            tags = []
        prefix = "truth-generation:"
        generation = next(
            (str(tag)[len(prefix) :] for tag in tags if str(tag).startswith(prefix)),
            None,
        )
        return DestinationObservation(
            state=(
                DestinationObservationState.PRESENT_MATCH
                if generation == expected_generation
                else DestinationObservationState.PRESENT_OTHER
            ),
            document_id=document_id,
            observed_generation=generation,
        )

    def remove(self, document_id: str) -> DestinationReceipt:
        _client, bank_id, base_url, _tags_factory = self._runtime()
        url = (
            f"{base_url}/v1/default/banks/{quote(bank_id, safe='')}/documents/"
            f"{quote(document_id, safe='')}"
        )
        request = Request(url, method="DELETE")
        try:
            with self._opener(request, timeout=15):
                pass
        except HTTPError as exc:
            if exc.code != 404:
                raise ProjectionDestinationError(
                    "Hindsight removal outcome is unknown"
                ) from exc
        except Exception as exc:
            raise ProjectionDestinationError(
                "Hindsight removal outcome is unknown"
            ) from exc
        return DestinationReceipt(
            document_id=document_id,
            claim_generation="0" * 64,
            acknowledged_at=utc_now(),
        )
