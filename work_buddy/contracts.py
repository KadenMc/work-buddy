"""Contract management for work-buddy.

Contracts are markdown files with YAML frontmatter stored in the
Obsidian vault (configured via ``contracts.vault_path`` in
``config.yaml``, resolved relative to ``vault_root``).  Each contract
represents a bounded unit of work (paper, deployment, grant, admin
task) with explicit scope, deadlines, and stop rules.
"""

import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

from work_buddy.config import load_config
from work_buddy.frontmatter import parse_frontmatter, scan_frontmatter, filter_by_status


def _default_contracts_dir() -> Path:
    """Resolve the default contracts directory from config.

    Reads ``contracts.vault_path`` (default ``"work-buddy/contracts"``)
    and resolves it relative to ``vault_root``.
    """
    cfg = load_config()
    vault_root = Path(cfg.get("vault_root", "."))
    subpath = cfg.get("contracts", {}).get("vault_path", "work-buddy/contracts")
    return vault_root / subpath

# Frontmatter fields that every contract should have
_KEY_FIELDS = ("title", "status", "deadline", "type")

# Body sections recognised in the contract markdown
_BODY_SECTIONS = (
    "Claim",
    "Why it matters",
    "Current Constraint",
    "Must-have evidence",
    "Optional / nice-to-have",
    "Kill rule",
    "Rescope rule",
    "Draft threshold",
)

# WIP limit for active contracts (enforced, not aspirational)
WIP_LIMIT = 3

_CHECKBOX_RE = re.compile(r"^- \[([ xX])\] (.+)$", re.MULTILINE)


def _contracts_dir(contracts_dir: Path | None) -> Path:
    """Resolve the contracts directory, creating it if absent."""
    d = contracts_dir or _default_contracts_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _parse_body_sections(body: str) -> dict[str, Any]:
    """Extract recognised sections from the contract body.

    Sections are identified by markdown headings (any level) whose text
    matches one of ``_BODY_SECTIONS``.  The content under each heading
    (up to the next heading) is captured.

    For the "Must-have evidence" section, checkbox items are also parsed
    into a list of ``{task, done}`` dicts.
    """
    sections: dict[str, Any] = {}
    heading_re = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)

    matches = list(heading_re.finditer(body))
    for i, m in enumerate(matches):
        title = m.group(2).strip()

        # Match against known section names (case-insensitive)
        matched_section: str | None = None
        for known in _BODY_SECTIONS:
            if title.lower() == known.lower():
                matched_section = known
                break
        if matched_section is None:
            continue

        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        content = body[start:end].strip()

        sections[matched_section] = content

        # Parse checkboxes for evidence sections
        if "evidence" in matched_section.lower():
            items = []
            for cb in _CHECKBOX_RE.finditer(content):
                items.append({
                    "task": cb.group(2).strip(),
                    "done": cb.group(1).lower() == "x",
                })
            sections[f"{matched_section}_items"] = items

    return sections


def _coerce_date(val: Any) -> date | None:
    """Try to coerce a value to a ``date`` object."""
    if isinstance(val, date) and not isinstance(val, datetime):
        return val
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, str):
        try:
            return date.fromisoformat(val)
        except ValueError:
            return None
    return None


# ── Public API ───────────────────────────────────────────────────────


def get_contracts_dir() -> Path:
    """Return the resolved contracts directory path (public helper)."""
    return _contracts_dir(None)


def load_contract(file_path: Path) -> dict:
    """Load a single contract file.

    Returns a dict containing all frontmatter fields, plus a
    ``sections`` key with parsed body sections, and ``path``.
    """
    fm, body = parse_frontmatter(file_path)
    sections = _parse_body_sections(body)
    return {**fm, "path": file_path, "frontmatter": fm, "sections": sections}


def load_all_contracts(contracts_dir: Path | None = None) -> list[dict]:
    """Load all contracts from the contracts/ directory."""
    d = _contracts_dir(contracts_dir)
    return [load_contract(p) for p in sorted(d.glob("*.md")) if p.is_file()]


def active_contracts(contracts_dir: Path | None = None) -> list[dict]:
    """Return contracts with ``status='active'``."""
    return [c for c in load_all_contracts(contracts_dir) if c.get("status") == "active"]


def contracts_summary(contracts_dir: Path | None = None) -> str:
    """Return a markdown summary of all contracts for agent consumption.

    Shows title, status, deadline, estimated_progress, and last_reviewed
    for each contract.
    """
    contracts = load_all_contracts(contracts_dir)
    if not contracts:
        return "No contracts found."

    lines = ["# Contracts Summary", ""]
    for c in contracts:
        title = c.get("title", c["path"].stem)
        status = c.get("status", "unknown")
        deadline = c.get("deadline", "none")
        progress = c.get("estimated_progress", "?")
        reviewed = c.get("last_reviewed", "never")
        ctype = c.get("type", "other")
        lines.append(f"- **{title}** [{ctype}] -- status: {status}, "
                      f"deadline: {deadline}, progress: {progress}%, "
                      f"last reviewed: {reviewed}")
    return "\n".join(lines)


def overdue_contracts(contracts_dir: Path | None = None) -> list[dict]:
    """Return contracts past their deadline that are not completed/abandoned."""
    today = date.today()
    results: list[dict] = []
    for c in load_all_contracts(contracts_dir):
        if c.get("status") in ("completed", "abandoned"):
            continue
        dl = _coerce_date(c.get("deadline"))
        if dl is not None and dl < today:
            results.append(c)
    return results


def stale_contracts(
    contracts_dir: Path | None = None,
    stale_days: int = 7,
) -> list[dict]:
    """Return contracts not reviewed in *stale_days* days."""
    today = date.today()
    results: list[dict] = []
    for c in load_all_contracts(contracts_dir):
        if c.get("status") in ("completed", "abandoned"):
            continue
        reviewed = _coerce_date(c.get("last_reviewed"))
        if reviewed is None or (today - reviewed).days >= stale_days:
            results.append(c)
    return results


def get_constraints(contracts_dir: Path | None = None) -> list[dict[str, Any]]:
    """Get active contracts with their current constraints.

    Returns a list of dicts with title, status, deadline, constraint
    (from frontmatter), constraint_detail (from body section), and path.
    """
    results = []
    for c in active_contracts(contracts_dir):
        title = c.get("title", c["path"].stem)
        constraint_fm = c.get("current_constraint", "")
        constraint_body = c.get("sections", {}).get("Current Constraint", "")

        # Use frontmatter value if set, otherwise extract from body
        constraint = constraint_fm or ""
        if not constraint and constraint_body:
            # Strip the template placeholder text
            lines = [
                l.strip() for l in constraint_body.split("\n")
                if l.strip()
                and not l.strip().startswith("_")
                and "bottleneck" not in l.lower()
                and "update this" not in l.lower()
            ]
            constraint = " ".join(lines) if lines else ""

        results.append({
            "title": title,
            "type": c.get("type", "other"),
            "status": c.get("status"),
            "deadline": str(c.get("deadline", "")),
            "deadline_type": c.get("deadline_type", ""),
            "progress": c.get("estimated_progress", 0),
            "constraint": constraint or "Not set",
            "last_reviewed": str(c.get("last_reviewed", "never")),
            "path": c["path"].as_posix(),
        })
    return results


def check_wip_limit(contracts_dir: Path | None = None) -> dict[str, Any]:
    """Check whether the active contract count exceeds the WIP limit.

    Returns:
        Dict with 'within_limit' (bool), 'active_count', 'limit',
        and 'active_titles' (list of active contract titles).
    """
    active = active_contracts(contracts_dir)
    titles = [c.get("title", c["path"].stem) for c in active]
    return {
        "within_limit": len(active) <= WIP_LIMIT,
        "active_count": len(active),
        "limit": WIP_LIMIT,
        "active_titles": titles,
    }


def contract_health_check(contracts_dir: Path | None = None) -> str:
    """Return a markdown health report covering all contracts.

    Checks:
    - Count by status
    - Whether there are too many active contracts (>2 is a flag)
    - Overdue or stale contracts
    - Whether any active contracts are papers (publication velocity)
    - Missing key fields (claim, deadline, kill_rule)
    """
    contracts = load_all_contracts(contracts_dir)
    if not contracts:
        return "## Contract Health\n\nNo contracts found. Consider creating one."

    # ── Status counts ────────────────────────────────────────────────
    status_counts: dict[str, int] = {}
    for c in contracts:
        s = c.get("status", "unknown")
        status_counts[s] = status_counts.get(s, 0) + 1

    active = [c for c in contracts if c.get("status") == "active"]
    overdue = overdue_contracts(contracts_dir)
    stale = stale_contracts(contracts_dir)

    lines = ["## Contract Health", ""]

    # Status breakdown
    lines.append("**By status:** " + ", ".join(
        f"{s}: {n}" for s, n in sorted(status_counts.items())
    ))

    # WIP limit check
    wip = check_wip_limit(contracts_dir)
    if not wip["within_limit"]:
        lines.append(
            f"\n**WIP VIOLATION:** {wip['active_count']} active contracts "
            f"(limit: {WIP_LIMIT}). "
            f"Pause or complete a contract before starting new work. "
            f"Active: {', '.join(wip['active_titles'])}"
        )
    elif len(active) == 0:
        lines.append("\n**Note:** No active contracts.")

    # Constraint check
    constraints = get_constraints(contracts_dir)
    missing_constraint = [c for c in constraints if c["constraint"] == "Not set"]
    if missing_constraint:
        lines.append("\n**Missing constraints:**")
        for c in missing_constraint:
            lines.append(f"- {c['title']}: no current_constraint set")

    # Paper check
    active_papers = [c for c in active if c.get("type") == "paper"]
    if active and not active_papers:
        lines.append(
            "\n**Flag:** No active paper contracts. "
            "If publication velocity is the goal, consider activating one."
        )

    # Overdue
    if overdue:
        lines.append("\n**Overdue:**")
        for c in overdue:
            lines.append(f"- {c.get('title', c['path'].stem)} "
                         f"(deadline: {c.get('deadline')})")

    # Stale
    if stale:
        lines.append("\n**Stale (not reviewed in 7+ days):**")
        for c in stale:
            lines.append(f"- {c.get('title', c['path'].stem)} "
                         f"(last reviewed: {c.get('last_reviewed', 'never')})")

    # Missing key fields
    missing_report: list[str] = []
    for c in active:
        title = c.get("title", c["path"].stem)
        missing: list[str] = []
        if not c.get("deadline"):
            missing.append("deadline")
        sections = c.get("sections", {})
        if not sections.get("Claim"):
            missing.append("claim")
        if not sections.get("Kill rule"):
            missing.append("kill_rule")
        if missing:
            missing_report.append(f"- {title}: missing {', '.join(missing)}")

    if missing_report:
        lines.append("\n**Incomplete active contracts:**")
        lines.extend(missing_report)

    return "\n".join(lines)
