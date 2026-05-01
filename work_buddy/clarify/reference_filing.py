"""Slice 6: Reference filing pipeline (composition, not new pipeline).

Captures classified by Slice 3's Clarify pass with
``destination=reference`` get filed at the right vault location via
composition of existing systems:

1. ``context_smart.semantic_search`` for high-level associations
   (which existing vault content does this snippet relate to?).
2. ``SmartSource.drill_down(item_id, "content")`` to fetch the file
   content for the top-N candidates (the high-level→fine-grain bridge
   the ROADMAP §6 foundational principle calls out — uses the existing
   ``ContextSource.drill_down`` protocol on the vault's semantic index).
3. :func:`vault_schema` for organizing principles (folder map, tag
   taxonomy, active areas).
4. Clarify-style LLM call producing a filing verdict in
   :data:`FILING_VERDICT_SCHEMA`.

The proposal respects the Slice-4 risk model:

- Tier 1 → "here's where I'd file it" (suggest only).
- Tier 3 → "I filed it; review the placement" (post-execution review).
- Tier 4 → "I filed it silently" (autonomous, no surface).

The Slice-3 reference destination today is "log only — Slice 6 wires
actual filing"; this module is the implementation.  The actual write
(``bridge.write_file``) is left to a follow-up so the schema-and-
proposal layer ships first; the reasoning step in the workflow can
call ``apply_reference_proposal`` once the user (or risk resolver)
has approved.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from work_buddy.logging_config import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Filing verdict schema
# ---------------------------------------------------------------------------

# Per ROADMAP §6 — schema for the filing verdict produced by the
# Clarify-style LLM call at the end of the proposal pipeline.
FILING_VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "topic_label": {
            "type": "string",
            "description": (
                "Short noun phrase (≤8 words) naming the underlying "
                "topic. Used as the section heading if the verdict "
                "creates a new file or appends to an existing one."
            ),
        },
        "candidate_paths": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Vault-relative path. Existing path for "
                            "extend / sibling; chosen-but-not-yet-"
                            "existing path for new_file."
                        ),
                    },
                    "action": {
                        "type": "string",
                        "enum": ["extend", "sibling", "new_file"],
                        "description": (
                            "extend: append a section to the existing "
                            "file at path. sibling: create a new file "
                            "next to an existing one (path is the "
                            "neighbour). new_file: create a wholly new "
                            "file at path."
                        ),
                    },
                    "rationale": {
                        "type": "string",
                        "description": (
                            "One to two sentences. Cite the existing "
                            "file that motivated the action."
                        ),
                    },
                },
                "required": ["path", "action", "rationale"],
                "additionalProperties": False,
            },
            "minItems": 1,
        },
        "confidence": {
            "type": "number",
            "description": "0.0–1.0 self-assessed.",
        },
        "namespace_tags": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Suggested namespace tags for the filed item "
                "(without leading '#'). Optional — empty list means "
                "no namespace classification."
            ),
        },
    },
    "required": ["topic_label", "candidate_paths"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class FilingCandidate:
    """One candidate placement in a parsed filing verdict."""

    path: str
    action: str  # 'extend' | 'sibling' | 'new_file'
    rationale: str


@dataclass(frozen=True)
class FilingVerdict:
    """Parsed and validated output of the filing LLM call."""

    topic_label: str
    candidates: tuple[FilingCandidate, ...]
    confidence: float = 0.0
    namespace_tags: tuple[str, ...] = field(default_factory=tuple)


_VALID_ACTIONS = frozenset({"extend", "sibling", "new_file"})


def parse_filing_verdict(raw: str | Mapping[str, Any] | None) -> FilingVerdict | None:
    """Coerce the LLM's structured output into a :class:`FilingVerdict`.

    Returns None when the input is missing required fields rather
    than raising — the caller (proposal pipeline) downgrades to a
    "no candidate found" response.
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return None
    if not isinstance(raw, Mapping):
        return None

    topic = raw.get("topic_label")
    if not isinstance(topic, str) or not topic.strip():
        return None

    raw_candidates = raw.get("candidate_paths") or []
    if not isinstance(raw_candidates, list) or not raw_candidates:
        return None

    candidates: list[FilingCandidate] = []
    for c in raw_candidates:
        if not isinstance(c, Mapping):
            continue
        path = c.get("path")
        action = c.get("action")
        rationale = c.get("rationale", "")
        if (
            not isinstance(path, str)
            or not isinstance(action, str)
            or action not in _VALID_ACTIONS
        ):
            continue
        candidates.append(FilingCandidate(
            path=path.strip(),
            action=action,
            rationale=str(rationale).strip(),
        ))

    if not candidates:
        return None

    confidence = raw.get("confidence", 0.0)
    try:
        confidence = float(confidence)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    namespace_tags = raw.get("namespace_tags") or []
    if not isinstance(namespace_tags, list):
        namespace_tags = []
    namespace_tags = tuple(
        t.strip().lstrip("#") for t in namespace_tags if isinstance(t, str)
    )

    return FilingVerdict(
        topic_label=topic.strip(),
        candidates=tuple(candidates),
        confidence=confidence,
        namespace_tags=namespace_tags,
    )


# ---------------------------------------------------------------------------
# vault_schema capability
# ---------------------------------------------------------------------------


def vault_schema(
    *,
    include_tags: bool = True,
    include_folders: bool = True,
    include_active: bool = True,
    folder_max_depth: int = 3,
    folder_top_n: int = 50,
    tag_top_n: int = 80,
) -> dict[str, Any]:
    """Quick introspection of vault organizing principles.

    Combines:

    - **Folder map** — directories under ``vault_root`` to depth
      ``folder_max_depth``, with file counts per folder.
    - **Tag taxonomy** — every tag with occurrence count from
      ``bridge.get_tags`` (best-effort; falls back to the local
      task-tag cache on bridge failure).
    - **Active areas** — active contracts + the namespace tags with
      the most recent task activity, so the LLM has a sense of "what
      the user actually works on right now."

    Returns:
        Dict with keys ``status``, ``vault_root``, ``folders``,
        ``tags``, ``active_contracts``, ``active_namespaces``,
        ``warnings``.  Each sub-section is omitted (empty list /
        dict) on failure rather than raising; the LLM caller checks
        the warnings list.

    This is a pure read; safe to call on every reference proposal.
    """
    out: dict[str, Any] = {
        "status": "ok",
        "vault_root": None,
        "folders": [],
        "tags": {},
        "active_contracts": [],
        "active_namespaces": [],
        "warnings": [],
    }

    # Vault root
    try:
        from work_buddy.config import load_config
        cfg = load_config() or {}
        vault_root = cfg.get("vault_root") or ""
        out["vault_root"] = vault_root
    except Exception as exc:
        out["warnings"].append(f"vault_root unavailable: {exc}")
        vault_root = ""

    # Folder map (filesystem walk; cheap on small vaults)
    if include_folders and vault_root:
        try:
            out["folders"] = _walk_vault_folders(
                Path(vault_root),
                max_depth=folder_max_depth,
                top_n=folder_top_n,
            )
        except Exception as exc:
            out["status"] = "degraded"
            out["warnings"].append(f"folder map: {exc}")

    # Tag taxonomy
    if include_tags:
        try:
            from work_buddy.obsidian import bridge
            tags = bridge.get_tags() or {}
            # Top-N by count, descending; preserve case from source.
            ranked = sorted(tags.items(), key=lambda kv: (-kv[1], kv[0]))
            out["tags"] = dict(ranked[:tag_top_n])
        except Exception as exc:
            out["warnings"].append(f"vault tags unavailable: {exc}")
            # Fallback: task-tag cache (still useful for namespace inference).
            try:
                from work_buddy.obsidian.tasks import store as _ts
                ns = _ts.distinct_namespace_tags(recent_days=14)
                out["tags"] = {f"#{n['tag']}": n["count"] for n in ns[:tag_top_n]}
            except Exception:  # pragma: no cover
                pass

    # Active areas
    if include_active:
        try:
            from work_buddy import contracts as contracts_mod
            actives = contracts_mod.active_contracts()
            out["active_contracts"] = [
                {"slug": c.get("slug"),
                 "title": c.get("title", c.get("slug")),
                 "namespace": c.get("namespace") or c.get("project")}
                for c in actives
            ]
        except Exception as exc:
            out["warnings"].append(f"active contracts unavailable: {exc}")

        try:
            from work_buddy.obsidian.tasks import store as _ts
            ns = _ts.distinct_namespace_tags(recent_days=14)
            ns.sort(
                key=lambda n: (
                    -(n.get("recent_count") or 0),
                    -(n.get("count") or 0),
                ),
            )
            out["active_namespaces"] = ns[:10]
        except Exception as exc:
            out["warnings"].append(f"namespaces unavailable: {exc}")

    return out


def _walk_vault_folders(
    vault: Path,
    *,
    max_depth: int,
    top_n: int,
) -> list[dict[str, Any]]:
    """Walk the vault to ``max_depth`` and return per-folder file counts.

    Returns the top ``top_n`` folders by markdown-file count.  Skips
    hidden / dot-prefixed dirs and well-known noise (``__pycache__``,
    ``.obsidian``, ``.git``, ``data``).  This isn't recursive into
    folders beyond ``max_depth`` — we want a coarse map, not an
    exhaustive index.
    """
    skip = {
        ".obsidian", ".git", "__pycache__", ".trash", "node_modules",
        ".venv", "venv", "data",
    }
    result: list[dict[str, Any]] = []

    def _walk(here: Path, depth: int):
        if depth > max_depth:
            return
        try:
            entries = list(here.iterdir())
        except (OSError, PermissionError):
            return
        md_count = sum(
            1 for e in entries
            if e.is_file() and e.suffix == ".md"
        )
        rel = here.relative_to(vault).as_posix() if here != vault else "."
        result.append({
            "path": rel,
            "depth": depth,
            "md_count": md_count,
        })
        if depth == max_depth:
            return
        for e in entries:
            if not e.is_dir():
                continue
            if e.name.startswith(".") or e.name in skip:
                continue
            _walk(e, depth + 1)

    _walk(vault, 0)
    result.sort(key=lambda r: (-r["md_count"], r["path"]))
    return result[:top_n]


# ---------------------------------------------------------------------------
# Reference proposal pipeline
# ---------------------------------------------------------------------------

_FILING_SYSTEM_PROMPT = """\
You are filing a reference snippet captured from the user's research
flow into their Obsidian vault.  You will receive:

1. The reference text itself.
2. A vault schema (folder map + tag taxonomy + active areas).
3. Up to N semantic-search candidates from the vault's existing
   content, with content snippets.

Produce a filing verdict in the candidate-paths schema:

  topic_label: short noun-phrase naming the topic
  candidate_paths: list of {path, action, rationale} where action is
    "extend"  — append a section to an existing file at <path>
    "sibling" — create a NEW file alongside the file at <path>
    "new_file"— create a NEW file at the chosen <path>
  confidence: 0.0–1.0
  namespace_tags: optional list of namespace tags (no leading '#')

Rules:

- Prefer ``extend`` over ``new_file`` when an existing file is a clear
  topical home (the semantic-search top hit is a strong signal here).
  ROADMAP V1c (carrying cost discipline) — adding to a thriving file
  beats sprouting orphans.
- Prefer ``sibling`` when the topic is similar but not the same as
  an existing file (a peer note rather than a new section).
- Reserve ``new_file`` for genuinely new topics where no existing
  vault file fits.
- Path must be vault-relative.  Use the folder map to choose a
  plausible directory.
- Cite the file that motivated each candidate in the rationale.
- Output exactly ONE verdict; multiple candidate_paths are alternative
  placements ranked best-first.
- Refuse honestly: if no good placement exists, set confidence < 0.3
  and propose ``new_file`` at a safe default location with a clearly
  hedged rationale.
"""


@dataclass
class ReferenceProposal:
    """Result of :func:`propose_reference_filing`.

    ``verdict`` is None when the LLM couldn't produce a parseable
    proposal; the rest of the fields are still populated so the
    surface can show ``candidates`` (the raw semantic-search hits)
    even when the LLM step failed.
    """

    topic_text: str
    candidates: list[dict[str, Any]]  # raw semantic hits + content snippets
    schema: dict[str, Any]            # vault_schema output
    verdict: FilingVerdict | None
    raw_llm_output: str | None = None
    error: str | None = None


def propose_reference_filing(
    *,
    topic_text: str,
    top_k: int = 5,
    snippet_chars: int = 800,
    runner=None,
    tier=None,
) -> ReferenceProposal:
    """Compose the reference-filing pipeline end-to-end.

    Args:
        topic_text: The reference snippet to file.
        top_k: How many semantic candidates to fetch + drill down on.
        snippet_chars: Max characters per drill-down content snippet
            forwarded to the LLM (avoid blowing the context window
            on long files).
        runner: Optional :class:`work_buddy.llm.LLMRunner` instance.
            When None, the function builds candidates + schema but
            skips the LLM step (used by tests + by callers that just
            want the data).
        tier: Optional model tier override.

    Returns:
        :class:`ReferenceProposal`.  ``verdict`` is None when the LLM
        is skipped or its output didn't parse; the rest of the dict
        is still populated.
    """
    schema = vault_schema()
    candidates = _gather_semantic_candidates(topic_text, top_k=top_k)

    # Drill down on each candidate to fetch the file content snippet.
    enriched_candidates: list[dict[str, Any]] = []
    for c in candidates:
        key = c.get("key") or ""
        snippet = ""
        try:
            from work_buddy.context import registry as cr
            smart = cr.get("smart")
            if smart is not None:
                content_blob = smart.drill_down(key, "content")
                content = (content_blob or {}).get("content") or ""
                snippet = content[:snippet_chars]
        except (NotImplementedError, KeyError, Exception) as exc:
            logger.debug(
                "propose_reference_filing: drill_down skipped for %s: %s",
                key, exc,
            )
        enriched_candidates.append({
            "key": key,
            "score": c.get("score"),
            "snippet": snippet,
        })

    if runner is None:
        return ReferenceProposal(
            topic_text=topic_text,
            candidates=enriched_candidates,
            schema=schema,
            verdict=None,
        )

    # Run the LLM if a runner was supplied.
    user_prompt = _render_filing_user_prompt(
        topic_text=topic_text,
        candidates=enriched_candidates,
        schema=schema,
    )

    try:
        from work_buddy.clarify.verdict_call import call_for_verdict
        from work_buddy.llm import ModelTier
        resp = call_for_verdict(
            runner=runner,
            tier=tier or ModelTier.FRONTIER_BALANCED,
            system=_FILING_SYSTEM_PROMPT,
            user=user_prompt,
            output_schema=FILING_VERDICT_SCHEMA,
            required_fields=("topic_label", "candidate_paths"),
            caller="reference_filing",
            item_id=None,
        )
    except Exception as exc:  # pragma: no cover — defensive
        return ReferenceProposal(
            topic_text=topic_text,
            candidates=enriched_candidates,
            schema=schema,
            verdict=None,
            error=f"LLM call failed: {exc}",
        )

    if resp.is_error():
        return ReferenceProposal(
            topic_text=topic_text,
            candidates=enriched_candidates,
            schema=schema,
            verdict=None,
            raw_llm_output=resp.content,
            error=resp.error,
        )

    verdict = parse_filing_verdict(resp.structured_output or {})
    return ReferenceProposal(
        topic_text=topic_text,
        candidates=enriched_candidates,
        schema=schema,
        verdict=verdict,
        raw_llm_output=resp.content,
    )


def _gather_semantic_candidates(
    topic_text: str,
    *,
    top_k: int,
) -> list[dict[str, Any]]:
    """Run semantic_search on the snippet; return top-K hits or [] on failure."""
    try:
        from work_buddy.obsidian.smart.env import semantic_search
        return semantic_search(topic_text, limit=top_k) or []
    except Exception as exc:
        logger.debug(
            "propose_reference_filing: semantic_search failed: %s", exc,
        )
        return []


def _render_filing_user_prompt(
    *,
    topic_text: str,
    candidates: list[dict[str, Any]],
    schema: dict[str, Any],
) -> str:
    """Compose the user prompt for the filing LLM call."""
    lines: list[str] = []
    lines.append("## Reference text\n")
    lines.append(topic_text.strip())
    lines.append("")
    lines.append("## Vault schema\n")
    lines.append(f"vault_root: {schema.get('vault_root') or '(unknown)'}")
    folders = schema.get("folders") or []
    if folders:
        lines.append("Top folders (path · md_count):")
        for f in folders[:25]:
            lines.append(f"  - {f['path']} · {f['md_count']}")
    tags = schema.get("tags") or {}
    if tags:
        lines.append("Top tags (count):")
        for tg, cnt in list(tags.items())[:30]:
            lines.append(f"  - {tg} · {cnt}")
    actives = schema.get("active_namespaces") or []
    if actives:
        lines.append("Active namespaces (recent task activity):")
        for ns in actives[:10]:
            lines.append(
                f"  - {ns.get('tag')} · count {ns.get('count')} "
                f"· recent {ns.get('recent_count', 0)}"
            )
    lines.append("")
    lines.append("## Semantic-search candidates")
    if not candidates:
        lines.append("(none)")
    else:
        for i, c in enumerate(candidates, 1):
            score = c.get("score")
            score_str = f"{score:.3f}" if isinstance(score, (int, float)) else "?"
            lines.append(f"### Candidate {i}: {c.get('key')} (score {score_str})\n")
            snippet = c.get("snippet") or ""
            if snippet:
                lines.append("```\n" + snippet[:600] + "\n```")
            else:
                lines.append("(content snippet unavailable)")
            lines.append("")
    return "\n".join(lines)
