"""Domain service for the personal-knowledge SQLite authority."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Mapping, Sequence

from work_buddy.config import USER_TZ
from work_buddy.knowledge.personal.store import PersonalKnowledgeStore
from work_buddy.knowledge.vault_adapter import _extract_summary, _first_sentence


CATEGORY_PATHS: dict[str, str] = {
    "work_pattern": "work_patterns",
    "self_regulation": "self_regulation",
    "skill_gap": "skill_gaps",
    "feedback": "feedback",
    "preference": "preferences",
    "reference": "reference",
}


def slugify(name: str) -> str:
    slug = re.sub(r"[^\w\s-]", "", name.lower().strip())
    slug = re.sub(r"[\s_]+", "-", slug)
    return slug.strip("-")


def build_structured_body(
    name: str,
    definition: str,
    triggers: str,
    signals: str,
    default_response: str,
    evidence: str,
    date: str,
) -> str:
    sections = [f"# {name}"]
    if definition:
        sections.append(f"\n## Definition\n\n{definition}")
    if triggers:
        sections.append(f"\n## Typical Triggers\n\n{triggers}")
    if signals:
        sections.append(f"\n## Observable Signals\n\n{signals}")
    if default_response:
        sections.append(f"\n## Default Response\n\n{default_response}")
    sections.append("\n## Evidence")
    sections.append(f"\n* {date} - {evidence}" if evidence else "\n*No observations yet.*")
    return "\n".join(sections) + "\n"


class PersonalKnowledgeService:
    def __init__(self, store: PersonalKnowledgeStore | None = None) -> None:
        self.store = store or PersonalKnowledgeStore()

    @staticmethod
    def _invalidate() -> None:
        from work_buddy.knowledge.store import invalidate_vault

        invalidate_vault()

    def create(
        self,
        *,
        logical_path: str,
        name: str,
        body: str = "",
        description: str = "",
        summary: str = "",
        categories: Sequence[str] = (),
        aliases: Sequence[str] = (),
        tags: Sequence[str] = (),
        requires: Sequence[str] = (),
        parent_paths: Sequence[str] = (),
        reference_paths: Sequence[str] = (),
        severity: str = "",
        privacy_class: str = "private",
        disclosure_class: str = "local_only",
        body_mode: str = "plain",
        document_binding_id: str | None = None,
        document_store_id: str | None = None,
        document_id: str | None = None,
        interaction_contract_id: str = "personal_note/v1",
        interaction_contract_version: int = 1,
        source_ref: str | None = None,
        actor: str = "user",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        semantic_summary = summary or (_extract_summary(body) if body else "")
        semantic_description = description or _first_sentence(semantic_summary)
        result = self.store.create_unit(
            logical_path=logical_path,
            name=name,
            description=semantic_description,
            summary=semantic_summary,
            body=body if body_mode == "plain" else None,
            body_mode=body_mode,
            document_binding_id=document_binding_id,
            document_store_id=document_store_id,
            document_id=document_id,
            interaction_contract_id=interaction_contract_id,
            interaction_contract_version=interaction_contract_version,
            categories=categories,
            aliases=aliases,
            tags=tags,
            requires=requires,
            parent_paths=parent_paths,
            reference_paths=reference_paths,
            severity=severity,
            privacy_class=privacy_class,
            disclosure_class=disclosure_class,
            source_ref=source_ref,
            actor=actor,
            idempotency_key=idempotency_key,
        )
        self._invalidate()
        return result

    def update(
        self,
        identity: str,
        fields: Mapping[str, Any],
        *,
        expected_revision: int,
        actor: str = "user",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        result = self.store.update_unit(
            identity,
            fields,
            expected_revision=expected_revision,
            actor=actor,
            idempotency_key=idempotency_key,
        )
        self._invalidate()
        return result

    def observe(
        self,
        identity: str,
        evidence: str,
        *,
        expected_revision: int,
        observed_at: str | None = None,
        source_ref: str | None = None,
        actor: str = "user",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        result = self.store.append_observation(
            identity,
            evidence=evidence,
            observed_at=observed_at,
            source_ref=source_ref,
            expected_revision=expected_revision,
            actor=actor,
            idempotency_key=idempotency_key,
        )
        self._invalidate()
        return result

    def mint(
        self,
        *,
        name: str,
        category: str,
        content_body: str = "",
        severity: str = "",
        tags: str = "",
        evidence: str = "",
        definition: str = "",
        triggers: str = "",
        signals: str = "",
        default_response: str = "",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Compatibility surface for the existing ``knowledge_mint`` op."""

        mint_request = {
            "operation": "knowledge_mint",
            "name": name,
            "category": category,
            "content_body": content_body,
            "severity": severity,
            "tags": tags,
            "evidence": evidence,
            "definition": definition,
            "triggers": triggers,
            "signals": signals,
            "default_response": default_response,
        }
        if idempotency_key:
            replay = self.store.receipt_result(idempotency_key, mint_request)
            if replay is not None:
                replay.setdefault("vault_file", None)
                return replay
        slug = slugify(name)
        if not slug:
            return {"error": "name must contain at least one letter or number"}
        subdir = CATEGORY_PATHS.get(category, "")
        logical_path = f"personal/{subdir}/{slug}" if subdir else f"personal/{slug}"
        current = self.store.get_unit(logical_path)
        if current is not None:
            if not evidence:
                return {
                    "status": "exists",
                    "path": current["current_path"],
                    "unit_id": current["unit_id"],
                    "revision": current["current_revision"],
                    "vault_file": None,
                    "message": "Unit exists. Provide evidence to append.",
                }
            result = self.store.append_observation(
                current["unit_id"],
                evidence=evidence,
                expected_revision=current["current_revision"],
                observed_at=datetime.now(USER_TZ).date().isoformat(),
                actor="agent",
                idempotency_key=idempotency_key,
                idempotency_request=mint_request,
            )
            self._invalidate()
            result["observation_count"] = self.store.get_unit(current["unit_id"])[
                "observation_count"
            ]
            result["vault_file"] = None
            return result

        date = datetime.now(USER_TZ).date().isoformat()
        body = content_body or build_structured_body(
            name, definition, triggers, signals, default_response, evidence, date
        )
        tag_list = [part.strip() for part in tags.split(",") if part.strip()]
        if not tag_list:
            tag_list = [f"wb/metacognition/{category}"] if category else ["wb/metacognition"]
        result = self.store.create_unit(
            logical_path=logical_path,
            name=name,
            description=_first_sentence(definition or _extract_summary(body)),
            summary=definition or _extract_summary(body),
            body=body,
            categories=[category] if category else [],
            tags=tag_list,
            severity=severity,
            observation_count=1 if evidence else 0,
            last_observed=date if evidence else "",
            observations=(
                [{"observed_at": date, "evidence": evidence, "actor": "agent"}]
                if evidence else []
            ),
            actor="agent",
            idempotency_key=idempotency_key,
            idempotency_request=mint_request,
        )
        # Keep the old result key as an honest compatibility alias.  It is a
        # logical import/export locator now, never a writable vault filename.
        result["vault_file"] = None
        self._invalidate()
        return result
