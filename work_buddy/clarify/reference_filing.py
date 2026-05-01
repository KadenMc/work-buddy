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


# ---------------------------------------------------------------------------
# Write step: apply_reference_proposal
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FilingApplyResult:
    """Outcome of :func:`apply_reference_proposal`.

    ``status`` is one of ``ok | suggested | failed``.  ``suggested``
    means the resolver chose tier 1 (suggest only) and no write
    happened; the user sees the candidate paths and decides.
    """

    status: str
    chosen_path: str | None = None
    action: str | None = None
    tier: int | None = None
    write_result: bool | None = None
    error: str | None = None
    blocker: str | None = None


def apply_reference_proposal(
    *,
    summary: str,
    verdict: FilingVerdict | dict | None,
    topic_text: str | None = None,
    risk_profile_json: str | None = None,
    config: dict[str, Any] | None = None,
    runner=None,
) -> FilingApplyResult:
    """Land a reference proposal at a vault path with tier-aware execution.

    Tier resolution (per ROADMAP §6 + Slice 4 risk model):

    - **Tier 1** ("here's where I'd file it") → return
      ``status='suggested'`` with the chosen candidate; no write.  The
      Resolution Surface renders the suggestion for user approval.
    - **Tier 3** ("I filed it; review the placement") → write the
      file AND return ``status='ok'``.  The dashboard's Daily Log /
      Review Queue surfaces the written placement for the user to
      confirm post-hoc.
    - **Tier 4** ("I filed it silently") → write the file AND return
      ``status='ok'``.  Surfaced in Daily Log only as a low-friction
      ledger entry.

    Args:
        summary: The text body to file (the captured reference content).
        verdict: A :class:`FilingVerdict` or its dict form (from
            :func:`parse_filing_verdict`).  When None, this function
            calls :func:`propose_reference_filing` first to produce
            one (using ``topic_text`` for the semantic search query).
        topic_text: Required when ``verdict`` is None; the seed text
            for the semantic-search candidate generation.
        risk_profile_json: Optional risk profile from the parent
            captured item.  When None, the safe-profile default applies
            (Slice 4 SAFE_PROFILE → tier 3 by heuristic for most
            filing actions).
        config: Optional config dict (forwarded to risk + filing).
        runner: Optional LLM runner for the proposal pass when
            ``verdict`` is None.  Skipped otherwise.

    Returns:
        :class:`FilingApplyResult` with status + chosen path +
        write outcome OR error.
    """
    # 1. Make sure we have a parsed verdict.
    if verdict is None:
        if not topic_text:
            return FilingApplyResult(
                status="failed",
                error="apply_reference_proposal: verdict OR topic_text required",
            )
        proposal = propose_reference_filing(
            topic_text=topic_text, runner=runner,
        )
        verdict = proposal.verdict
        if verdict is None:
            return FilingApplyResult(
                status="failed",
                error=proposal.error or "no parsed verdict",
            )
    elif isinstance(verdict, dict):
        verdict = parse_filing_verdict(verdict)
        if verdict is None:
            return FilingApplyResult(
                status="failed",
                error="verdict dict failed schema validation",
            )

    if not verdict.candidates:
        return FilingApplyResult(
            status="failed",
            error="verdict has no candidates",
        )

    # 2. Resolve tier against the risk profile.
    try:
        from work_buddy.automation.risk import resolve_operating_tier
        # For filing, treat the action as low-risk by default; the
        # confidence field acts as an additional cap below.
        decision = resolve_operating_tier(
            {"risk_profile_json": risk_profile_json},
            config=config,
        )
        tier = decision.operating
        blocker = decision.pipeline_blocker
    except Exception as exc:  # pragma: no cover -- defensive
        logger.warning("apply_reference_proposal: tier resolution failed: %s", exc)
        tier = 3  # default to review-required
        blocker = None

    # Confidence-based cap: low-confidence filings are always tier-1
    # regardless of risk tolerance (V2b honest signaling).
    if verdict.confidence < 0.3:
        tier = min(tier, 1)
        blocker = blocker or "inference_uncertain"

    pick = verdict.candidates[0]

    # 3. Tier 1: don't write.
    if tier <= 1:
        return FilingApplyResult(
            status="suggested",
            chosen_path=pick.path,
            action=pick.action,
            tier=tier,
            blocker=blocker,
        )

    # 4. Tier 3+: write.  Compose the body per action.
    body = _compose_filing_body(verdict=verdict, summary=summary)
    try:
        from work_buddy.obsidian import bridge
    except ImportError as exc:  # pragma: no cover
        return FilingApplyResult(
            status="failed", error=f"bridge import failed: {exc}",
            tier=tier, action=pick.action, chosen_path=pick.path,
        )

    try:
        if pick.action == "extend":
            wrote, actual_action = _extend_existing_file(
                bridge=bridge, path=pick.path, addendum=body,
                topic_label=verdict.topic_label,
            )
            # Slice 6 fix #6: when the extend target was missing the
            # helper degrades to a fresh write and reports
            # "extended_as_new"; surface that to the audit trail so
            # the user can spot the silent action change.
            if actual_action != pick.action:
                pick = FilingCandidate(
                    path=pick.path, action=actual_action,
                    rationale=pick.rationale,
                )
        elif pick.action == "sibling":
            base_sibling = _sibling_path_from(pick.path, verdict.topic_label)
            sibling_path = _resolve_unique_path(bridge, base_sibling)
            wrote = bridge.write_file(sibling_path, body)
            pick = FilingCandidate(
                path=sibling_path, action=pick.action,
                rationale=pick.rationale,
            )
        elif pick.action == "new_file":
            unique_path = _resolve_unique_path(bridge, pick.path)
            wrote = bridge.write_file(unique_path, body)
            if unique_path != pick.path:
                pick = FilingCandidate(
                    path=unique_path, action=pick.action,
                    rationale=pick.rationale,
                )
        else:
            return FilingApplyResult(
                status="failed",
                error=f"unknown action {pick.action!r}",
                tier=tier, chosen_path=pick.path,
            )
    except Exception as exc:
        logger.warning("apply_reference_proposal write failed: %s", exc)
        return FilingApplyResult(
            status="failed", error=str(exc),
            tier=tier, action=pick.action, chosen_path=pick.path,
        )

    return FilingApplyResult(
        status="ok" if wrote else "failed",
        chosen_path=pick.path,
        action=pick.action,
        tier=tier,
        write_result=wrote,
        error=None if wrote else "bridge.write_file returned False",
        blocker=blocker if not wrote else None,
    )


def _compose_filing_body(*, verdict: FilingVerdict, summary: str) -> str:
    """Render the body that lands at the chosen path.

    For ``new_file`` / ``sibling`` we include a YAML frontmatter
    marker so the file is greppable as a filed reference + a topic
    heading + the summary body.  For ``extend`` callers concatenate
    via :func:`_extend_existing_file`; this function is also reused
    there to compose the appended section.
    """
    topic = verdict.topic_label or "Reference"
    parts: list[str] = []
    parts.append("---")
    parts.append("type: reference")
    if verdict.namespace_tags:
        parts.append("tags:")
        for t in verdict.namespace_tags:
            parts.append(f"  - {t}")
    parts.append("---")
    parts.append("")
    parts.append(f"# {topic}")
    parts.append("")
    parts.append(summary.strip())
    if verdict.candidates and verdict.candidates[0].rationale:
        parts.append("")
        parts.append("---")
        parts.append(
            f"*Filed by reference-filing pipeline.  Rationale: "
            f"{verdict.candidates[0].rationale}*"
        )
    return "\n".join(parts) + "\n"


def _extend_existing_file(
    *,
    bridge,
    path: str,
    addendum: str,
    topic_label: str | None,
) -> tuple[bool, str]:
    """Append a new section to an existing vault file (the ``extend`` action).

    Reads the file, appends a `## <topic_label>` heading + the
    addendum body, writes the result back.  Skips the YAML frontmatter
    block from ``addendum`` (the existing file already has its own
    frontmatter).

    Returns ``(wrote, action_taken)`` where ``action_taken`` is:
    - ``"extend"``         — target existed and was appended to.
    - ``"extended_as_new"``— target was missing; degraded to a fresh
                             write.  The audit trail surfaces this so
                             the user knows the LLM's "extend the
                             topical home" intent didn't actually
                             append (the file wasn't there to extend).

    Slice 6 fix #6: previously this silently returned a bool and
    callers recorded ``action='extend'`` regardless of whether the
    target existed.  The new tuple lets ``apply_reference_proposal``
    record the actual outcome.
    """
    existing = bridge.read_file(path)
    if existing is None:
        # Target missing — degrade to a new_file write but flag it so
        # the caller can surface the change.  The user's "extend"
        # intent presumed the target existed; if not, they should know.
        wrote = bridge.write_file(path, addendum)
        return wrote, "extended_as_new"

    # Strip the addendum's frontmatter (between leading --- and second ---).
    body_only = _strip_frontmatter(addendum)
    section_heading = (
        f"\n\n## {topic_label}\n\n" if topic_label else "\n\n## Reference\n\n"
    )
    new_content = existing.rstrip() + section_heading + body_only.lstrip()
    wrote = bridge.write_file(path, new_content)
    return wrote, "extend"


def _strip_frontmatter(text: str) -> str:
    """Remove a leading YAML frontmatter block from ``text``."""
    if not text.startswith("---"):
        return text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return text
    return parts[2].lstrip("\n")


def _resolve_unique_path(bridge, base_path: str, *, max_attempts: int = 50) -> str:
    """Return ``base_path`` if free, else ``base-2.md``, ``base-3.md``, …

    Slice 6 fix #5: ``sibling`` and ``new_file`` actions previously
    overwrote any existing file at the chosen path (consent-gated by
    bridge.write_file but still destructive on a slug collision).
    This helper consults bridge.read_file to detect existence and
    derives a numbered alternative on collision.

    Falls back to a timestamp suffix after ``max_attempts`` to avoid
    pathological loops.  Defaults to 50 because reference-filing
    collisions are expected to be rare; if a user collides 50 times
    on the same topic-label something else is wrong.
    """
    from pathlib import PurePosixPath
    p = PurePosixPath(base_path)
    suffix = p.suffix or ".md"
    stem_path = p.with_suffix("").as_posix()  # 'a/b/c' from 'a/b/c.md'

    try:
        # First-attempt: the bare path.
        existing = bridge.read_file(base_path)
    except Exception:
        # Bridge failure: assume free; the write_file call will surface
        # a real error if needed.  Don't block filing on a probe glitch.
        return base_path
    if existing is None:
        return base_path

    for n in range(2, max_attempts + 1):
        candidate = f"{stem_path}-{n}{suffix}"
        try:
            if bridge.read_file(candidate) is None:
                return candidate
        except Exception:
            return candidate

    # Pathological — fall back to timestamp.
    from datetime import datetime, timezone
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"{stem_path}-{stamp}{suffix}"


def _sibling_path_from(neighbour_path: str, topic_label: str | None) -> str:
    """Derive a new file path next to ``neighbour_path``."""
    from pathlib import PurePosixPath
    p = PurePosixPath(neighbour_path)
    parent = p.parent.as_posix() if str(p.parent) not in (".", "") else ""
    slug_base = (topic_label or p.stem + "-related").lower()
    slug = "".join(
        c if (c.isalnum() or c in "-_") else "-" for c in slug_base
    ).strip("-") or "reference"
    candidate = f"{parent}/{slug}.md" if parent else f"{slug}.md"
    return candidate


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
