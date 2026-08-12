"""Issuer-qualified actor identity shared across Work Buddy boundaries."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Mapping


ACTOR_REF_SCHEMA = "wb.actor-ref/v1"
_OPAQUE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{7,127}$")
_ACTOR_KINDS = frozenset(
    {"human", "agent_run", "system", "service", "local_surface", "unknown"}
)


class InvalidActorReference(ValueError):
    """An actor reference is malformed or outside the v1 vocabulary."""


@dataclass(frozen=True, slots=True)
class ActorRef:
    """Stable actor identity qualified by issuer and tenant authority."""

    issuer_authority_id: str
    subject: str
    kind: str
    tenant_scope_id: str
    schema: str = ACTOR_REF_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != ACTOR_REF_SCHEMA:
            raise InvalidActorReference("unsupported actor-ref schema")
        for field_name in (
            "issuer_authority_id",
            "subject",
            "tenant_scope_id",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or _OPAQUE_ID_RE.fullmatch(value) is None:
                raise InvalidActorReference(f"invalid {field_name}")
        if not isinstance(self.kind, str) or self.kind not in _ACTOR_KINDS:
            raise InvalidActorReference("invalid actor kind")

    def to_dict(self) -> dict[str, str]:
        return {
            "schema": self.schema,
            "issuer_authority_id": self.issuer_authority_id,
            "subject": self.subject,
            "kind": self.kind,
            "tenant_scope_id": self.tenant_scope_id,
        }

    @property
    def canonical_id(self) -> str:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ActorRef":
        required = {
            "schema",
            "issuer_authority_id",
            "subject",
            "kind",
            "tenant_scope_id",
        }
        if not isinstance(value, Mapping) or set(value) != required:
            raise InvalidActorReference("actor-ref fields do not match the schema")
        if any(not isinstance(value[key], str) for key in required):
            raise InvalidActorReference("actor-ref values must be strings")
        return cls(
            issuer_authority_id=value["issuer_authority_id"],
            subject=value["subject"],
            kind=value["kind"],
            tenant_scope_id=value["tenant_scope_id"],
            schema=value["schema"],
        )


__all__ = ["ACTOR_REF_SCHEMA", "ActorRef", "InvalidActorReference"]
