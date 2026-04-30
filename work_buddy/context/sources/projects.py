"""``projects`` context source — active projects + contracts.

Consolidates the projects + contracts fetching that lived in
:func:`work_buddy.clarify.recommend.build_triage_context`. Emits two
kinds of items — tagged by ``type`` so the renderer can split them
into separate prompt sections.

Depth semantics:
  - BRIEF:  slug only (tight — names just anchor which area of work).
  - NORMAL: slug + first-sentence description (up to 140 chars).
  - DEEP:   slug + full description (capped at 400 chars to keep
            prompts sane).

Contracts render under their own "Active Contracts" heading. They're
high-signal and rare (usually ≤5 at a time) so we don't truncate the
list even at BRIEF depth — only the per-item detail shrinks.
"""

from __future__ import annotations

from typing import Any

from work_buddy.context.types import (
    BaseContextSource,
    ContextDepth,
    ContextRequest,
    ContextSection,
)
from work_buddy.context import registry as _registry
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)


_PROJECT_DESC_BRIEF_MAX = 0         # no description in brief
_PROJECT_DESC_NORMAL_MAX = 140      # one-sentence cut
_PROJECT_DESC_DEEP_MAX = 400        # full, but still capped
_CONTRACT_CLAIM_DEEP_MAX = 400


class ProjectsSource(BaseContextSource):
    """Active projects + contracts. Registered at module import."""

    name = "projects"

    def collect(self, request: ContextRequest) -> ContextSection:
        status_filter = request.custom_for(self.name).get("status", "active")
        projects = _collect_projects(status_filter)
        contracts = _collect_contracts()
        items: list[dict[str, Any]] = []
        for p in projects:
            items.append({"type": "project", **p})
        for c in contracts:
            items.append({"type": "contract", **c})
        return ContextSection(
            source=self.name,
            items=items,
            metadata={
                "project_count": len(projects),
                "contract_count": len(contracts),
                "status_filter": status_filter,
            },
        )

    def render(self, section: ContextSection, depth: ContextDepth) -> str:
        items = section.items or []
        if not items:
            return ""

        projects = [i for i in items if i.get("type") == "project"]
        contracts = [i for i in items if i.get("type") == "contract"]

        parts: list[str] = []

        if projects:
            parts.append(f"### Active Projects ({len(projects)})")
            for p in projects:
                slug = p.get("slug", "?")
                desc = _truncate_description(
                    p.get("description", "") or "",
                    depth=depth,
                )
                parts.append(f"- {slug}" + (f" — {desc}" if desc else ""))
            parts.append("")

        if contracts:
            parts.append(f"### Active Contracts ({len(contracts)})")
            for c in contracts:
                title = c.get("title", "")
                deadline = f" (deadline: {c['deadline']})" if c.get("deadline") else ""
                parts.append(f"- {title}{deadline}")
                claim = c.get("claim", "")
                if claim and depth >= ContextDepth.NORMAL:
                    if depth < ContextDepth.DEEP and len(claim) > _PROJECT_DESC_NORMAL_MAX:
                        claim = claim[: _PROJECT_DESC_NORMAL_MAX - 1].rstrip() + "…"
                    elif len(claim) > _CONTRACT_CLAIM_DEEP_MAX:
                        claim = claim[: _CONTRACT_CLAIM_DEEP_MAX - 1].rstrip() + "…"
                    parts.append(f"  Claim: {claim}")
            parts.append("")

        return "\n".join(parts).rstrip()

    def is_stale(
        self,
        cached: ContextSection,
        request: ContextRequest,
    ) -> bool:
        """Stat the projects SQLite DB; compare mtime to cache write time."""
        from pathlib import Path
        try:
            from work_buddy.projects.store import _db_path
            path = Path(_db_path())
        except Exception:
            return False
        if not path.exists():
            return False
        cached_at = cached.fetched_at.timestamp()
        return path.stat().st_mtime > cached_at

    def drill_down(self, item_id: str, field: str) -> dict[str, Any]:
        """``field='description'`` returns the full project description.

        ``item_id`` matches the project slug. Contracts can be drilled
        via ``item_id=<title>`` and ``field='full'`` — returns every
        stored field.
        """
        if field == "description":
            project = _get_project(item_id)
            if not project:
                raise KeyError(f"Unknown project slug: {item_id!r}")
            return {
                "slug": project.get("slug"),
                "description": project.get("description") or "",
            }

        if field == "full":
            project = _get_project(item_id)
            if project:
                return {"slug": item_id, **project}
            contract = _get_contract(item_id)
            if contract:
                return {"title": item_id, **contract}
            raise KeyError(f"No project or contract with id {item_id!r}")

        raise KeyError(
            f"ProjectsSource.drill_down: unknown field {field!r}. "
            "Valid: 'description', 'full'."
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _collect_projects(status: str | None) -> list[dict[str, Any]]:
    try:
        from work_buddy.projects.store import list_projects
        rows = list_projects(status=status) if status else list_projects()
    except Exception as exc:
        logger.debug("projects source: list_projects failed: %s", exc)
        return []
    return [
        {
            "slug": p["slug"],
            "name": p.get("name", p["slug"]),
            "status": p.get("status", ""),
            "description": p.get("description") or "",
        }
        for p in rows
    ]


def _collect_contracts() -> list[dict[str, Any]]:
    try:
        from work_buddy.contracts import active_contracts
        rows = active_contracts()
    except Exception as exc:
        logger.debug("projects source: active_contracts failed: %s", exc)
        return []
    return [
        {
            "title": c.get("title", ""),
            "status": c.get("status", ""),
            "deadline": c.get("deadline", ""),
            "claim": c.get("claim", ""),
        }
        for c in rows
    ]


def _get_project(slug: str) -> dict[str, Any] | None:
    try:
        from work_buddy.projects.store import list_projects
        for p in list_projects():
            if p["slug"] == slug:
                return dict(p)
    except Exception as exc:
        logger.debug("projects source: get_project failed: %s", exc)
    return None


def _get_contract(title: str) -> dict[str, Any] | None:
    try:
        from work_buddy.contracts import active_contracts
        for c in active_contracts():
            if c.get("title") == title:
                return dict(c)
    except Exception as exc:
        logger.debug("projects source: get_contract failed: %s", exc)
    return None


def _truncate_description(desc: str, *, depth: ContextDepth) -> str:
    if not desc:
        return ""
    if depth == ContextDepth.BRIEF:
        return ""
    if depth == ContextDepth.NORMAL:
        cap = _PROJECT_DESC_NORMAL_MAX
    else:
        cap = _PROJECT_DESC_DEEP_MAX
    if len(desc) <= cap:
        return desc
    first_sentence = desc.split(". ", 1)[0]
    if first_sentence and len(first_sentence) <= cap:
        return first_sentence + ("." if not first_sentence.endswith(".") else "")
    return desc[: cap - 1].rstrip() + "…"


# ---------------------------------------------------------------------------
# Register
# ---------------------------------------------------------------------------


_registry.register(ProjectsSource())
