"""Compatibility projection and authority selection for contract readers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from work_buddy.contracts_domain.service import ContractService
from work_buddy.contracts_domain.store import ContractStore, default_db_path
from work_buddy.installed_authority import require_domain_store_open


def native_service_if_sealed() -> ContractService | None:
    """Return the SQLite provider only after its import cohort is sealed.

    Absence of the database is deliberately non-creating so ordinary reads do
    not perform a cutover or even create an empty authority file.
    """

    path = default_db_path()
    require_domain_store_open("contracts", path)
    store = ContractStore(path)
    if not store.exists():
        return None
    return ContractService(store) if store.is_native_authority() else None


def _section_text(snapshot: dict[str, Any], kind: str) -> str:
    values = [
        item["text"]
        for item in snapshot["constraints"]
        if item["kind"] == kind and item["state"] == "current"
    ]
    return "\n\n".join(values)


def legacy_projection(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Preserve the established ``work_buddy.contracts`` result shape."""

    aliases = snapshot.get("aliases", [])
    legacy_path = next(
        (
            item["alias_display"]
            for item in aliases
            if item["alias_kind"] == "legacy_path"
        ),
        None,
    )
    logical = next(
        (
            item["alias_display"]
            for item in aliases
            if item["alias_kind"] == "logical_name"
        ),
        snapshot["contract_id"],
    )
    path = Path(legacy_path or f"{logical}.md")
    dates = snapshot.get("dates", {})
    evidence = snapshot.get("evidence_links", [])
    must = [item for item in evidence if item["requirement"] == "must_have"]
    optional = [item for item in evidence if item["requirement"] == "optional"]

    def evidence_text(items: list[dict[str, Any]]) -> str:
        return "\n".join(
            f"- [{'x' if item['state'] == 'satisfied' else ' '}] "
            f"{item.get('label') or item['evidence_ref']}"
            for item in items
        )

    claim = "\n\n".join(
        item["text"]
        for item in snapshot.get("commitments", [])
        if item["kind"] == "claim" and item["state"] != "waived"
    )
    why = "\n\n".join(
        item["text"]
        for item in snapshot.get("commitments", [])
        if item["kind"] == "why_it_matters" and item["state"] != "waived"
    )
    sections = {
        "Claim": claim,
        "Why it matters": why,
        "Current Constraint": _section_text(snapshot, "current"),
        "Must-have evidence": evidence_text(must),
        "Must-have evidence_items": [
            {
                "task": item.get("label") or item["evidence_ref"],
                "done": item["state"] == "satisfied",
            }
            for item in must
        ],
        "Optional / nice-to-have": evidence_text(optional),
        "Optional / nice-to-have_items": [
            {
                "task": item.get("label") or item["evidence_ref"],
                "done": item["state"] == "satisfied",
            }
            for item in optional
        ],
        "Kill rule": _section_text(snapshot, "kill_rule"),
        "Rescope rule": _section_text(snapshot, "rescope_rule"),
        "Draft threshold": _section_text(snapshot, "draft_threshold"),
    }
    frontmatter: dict[str, Any] = {
        "id": snapshot["contract_id"],
        "title": snapshot["title"],
        "status": snapshot["status"],
        "type": snapshot["type"],
        "estimated_progress": snapshot["estimated_progress"],
        "privacy_class": snapshot["privacy_class"],
        "revision": snapshot["current_revision"],
    }
    for key in ("deadline", "last_reviewed", "start_date", "completed_at"):
        if key in dates:
            frontmatter[key] = dates[key]["value"]
    if "deadline_type" in snapshot.get("health_inputs", {}):
        frontmatter["deadline_type"] = snapshot["health_inputs"]["deadline_type"]
    current_constraint = sections["Current Constraint"]
    if current_constraint:
        frontmatter["current_constraint"] = current_constraint
    return {
        **frontmatter,
        "path": path,
        "frontmatter": dict(frontmatter),
        "sections": sections,
        "contract_id": snapshot["contract_id"],
        "current_revision": snapshot["current_revision"],
        "lifecycle": snapshot["lifecycle"],
        "structured": snapshot,
    }


def list_legacy_shape(service: ContractService) -> list[dict[str, Any]]:
    return [legacy_projection(item) for item in service.list()]


__all__ = ["legacy_projection", "list_legacy_shape", "native_service_if_sealed"]
