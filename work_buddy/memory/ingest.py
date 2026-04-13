"""Retain helpers — structured ingestion into Hindsight.

Each function sends content to the personal bank with appropriate tags
and context.  Hindsight's server-side LLM pipeline handles fact
extraction, entity recognition, and observation synthesis automatically.

Best-practice notes (from Hindsight docs):
* Do NOT pre-summarize; send raw/lightly-structured content so the
  extraction pipeline can identify entities and relationships.
* Always provide ``context`` describing the data source.
* Use stable ``document_id`` for upsert behaviour (avoid duplicates).
* Set ``timestamp`` from the content's real time, not wall-clock time.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from work_buddy.memory.client import build_tags, get_bank_id, get_project_bank_id, get_client
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Lazy project bank bootstrap
# ---------------------------------------------------------------------------

_PROJECT_BANK_ENSURED = False


def _ensure_project_bank_once() -> None:
    """Bootstrap the Hindsight project bank exactly once per process.

    Idempotent — ensure_project_bank handles "already exists" gracefully.
    Failures are logged as warnings and do not abort the calling operation.
    """
    global _PROJECT_BANK_ENSURED
    if _PROJECT_BANK_ENSURED:
        return
    try:
        from work_buddy.memory.setup import ensure_project_bank
        ensure_project_bank()
        _PROJECT_BANK_ENSURED = True
    except Exception:
        logger.warning("Failed to ensure project bank", exc_info=True)


def retain_context_bundle_summary(
    bundle_dir: Path,
    session_id: str,
    *,
    extra_tags: list[str] | None = None,
) -> dict[str, Any] | None:
    """Retain content from a context bundle into the personal bank.

    Reads each summary file in *bundle_dir* and retains the combined
    content.  Returns the retain response, or ``None`` on failure.
    """
    parts: list[str] = []
    for md_file in sorted(bundle_dir.glob("*.md")):
        parts.append(f"## {md_file.stem}\n\n{md_file.read_text(encoding='utf-8')}")

    if not parts:
        logger.warning("No .md files in pack dir %s — skipping retain", bundle_dir)
        return None

    content = "\n\n---\n\n".join(parts)

    # Timestamp from bundle_meta if available, else directory name, else now
    ts = _bundle_timestamp(bundle_dir)

    tags = build_tags(
        "source:context-bundle",
        f"session:{session_id}",
        "domain:work",
        *(extra_tags or []),
    )

    client = get_client()
    try:
        resp = client.retain(
            bank_id=get_bank_id(),
            content=content,
            context="work-buddy context bundle: git activity, journal entries, tasks, messages, and vault state",
            document_id=f"context-bundle-{bundle_dir.name}",
            timestamp=ts,
            tags=tags,
        )
        logger.info("Retained context bundle %s (%d chars)", bundle_dir.name, len(content))
        return resp
    except Exception:
        logger.exception("Failed to retain context bundle %s", bundle_dir.name)
        return None


def retain_journal_insights(
    entries: list[str],
    date_str: str,
    *,
    extra_tags: list[str] | None = None,
) -> dict[str, Any] | None:
    """Retain insights extracted during journal processing."""
    if not entries:
        return None

    content = "\n".join(entries)
    tags = build_tags(
        "source:journal",
        "kind:summary",
        *(extra_tags or []),
    )

    client = get_client()
    try:
        resp = client.retain(
            bank_id=get_bank_id(),
            content=content,
            context=f"work-buddy journal synthesis for {date_str}",
            document_id=f"journal-{date_str}",
            timestamp=datetime.fromisoformat(date_str + "T23:59:00"),
            tags=tags,
        )
        logger.info("Retained journal insights for %s", date_str)
        return resp
    except Exception:
        logger.exception("Failed to retain journal insights for %s", date_str)
        return None


def retain_workflow_outcome(
    workflow_name: str,
    outcome: str,
    session_id: str,
    *,
    extra_tags: list[str] | None = None,
) -> dict[str, Any] | None:
    """Retain a notable workflow outcome (decision, blocker, pattern)."""
    tags = build_tags(
        "source:workflow",
        f"workflow:{workflow_name}",
        f"session:{session_id}",
        *(extra_tags or []),
    )

    client = get_client()
    try:
        resp = client.retain(
            bank_id=get_bank_id(),
            content=outcome,
            context=f"work-buddy workflow outcome from '{workflow_name}'",
            timestamp=datetime.now(timezone.utc),
            tags=tags,
        )
        logger.info("Retained workflow outcome for '%s'", workflow_name)
        return resp
    except Exception:
        logger.exception("Failed to retain workflow outcome for '%s'", workflow_name)
        return None


def retain_personal_note(
    content: str,
    *,
    kind: str = "preference",
    domain: str = "life",
    extra_tags: list[str] | None = None,
) -> dict[str, Any] | None:
    """Explicitly retain a personal fact pinned by the user or agent."""
    tags = build_tags(
        "source:manual",
        f"kind:{kind}",
        f"domain:{domain}",
        *(extra_tags or []),
    )

    client = get_client()
    try:
        resp = client.retain(
            bank_id=get_bank_id(),
            content=content,
            context="explicitly pinned personal memory",
            timestamp=datetime.now(timezone.utc),
            tags=tags,
        )
        logger.info("Retained personal note (%s/%s, %d chars)", kind, domain, len(content))
        return resp
    except Exception:
        logger.exception("Failed to retain personal note")
        return None


# ---------------------------------------------------------------------------
# Project memory bank
# ---------------------------------------------------------------------------

def retain_project_observation(
    project_slug: str,
    content: str,
    source: str = "chat",
    session_id: str | None = None,
    *,
    extra_tags: list[str] | None = None,
) -> dict[str, Any] | None:
    """Retain a project observation into the project memory bank.

    Observations are append-only temporal records: decisions, feedback,
    pivots, blockers, or anything that shapes a project's trajectory.
    Each call creates a new document (no dedup).
    """
    _ensure_project_bank_once()
    now = datetime.now(timezone.utc)
    tag_parts = [f"project:{project_slug}", f"source:{source}"]
    if session_id:
        tag_parts.append(f"session:{session_id}")
    tags = build_tags(*tag_parts, *(extra_tags or []))

    client = get_client()
    try:
        resp = client.retain(
            bank_id=get_project_bank_id(),
            content=content,
            context=f"Project observation for '{project_slug}' via {source}",
            document_id=f"project-obs-{project_slug}-{now.isoformat()}",
            timestamp=now,
            tags=tags,
        )
        logger.info(
            "Retained project observation for '%s' (%s, %d chars)",
            project_slug, source, len(content),
        )
        return resp
    except Exception:
        logger.exception("Failed to retain project observation for '%s'", project_slug)
        return None


def retain_project_state_file(
    project_slug: str,
    state_content: str,
    *,
    extra_tags: list[str] | None = None,
) -> dict[str, Any] | None:
    """Retain a STATE.md snapshot into the project memory bank.

    Uses a stable document_id for upsert behaviour — the latest STATE.md
    replaces the prior version.
    """
    _ensure_project_bank_once()
    tags = build_tags(
        f"project:{project_slug}",
        "source:state-file",
        *(extra_tags or []),
    )

    client = get_client()
    try:
        resp = client.retain(
            bank_id=get_project_bank_id(),
            content=state_content,
            context=f"STATE.md snapshot for project '{project_slug}'",
            document_id=f"state-file-{project_slug}",
            timestamp=datetime.now(timezone.utc),
            tags=tags,
        )
        logger.info("Retained STATE.md for '%s' (%d chars)", project_slug, len(state_content))
        return resp
    except Exception:
        logger.exception("Failed to retain STATE.md for '%s'", project_slug)
        return None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _bundle_timestamp(bundle_dir: Path) -> datetime:
    """Best-effort timestamp from a context bundle directory."""
    import json

    meta = bundle_dir / "bundle_meta.json"
    if meta.exists():
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
            ts_str = data.get("collected_at")
            if ts_str:
                return datetime.fromisoformat(ts_str)
        except (json.JSONDecodeError, ValueError):
            pass

    # Fall back to directory name (e.g. "2026-04-03T14-30-00")
    try:
        return datetime.fromisoformat(bundle_dir.name.replace("T", "T").replace("-", ":", 2))
    except ValueError:
        return datetime.now(timezone.utc)
