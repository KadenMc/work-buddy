"""CLI entry point for context bundle collection.

As of phase 6 of the LLM + Context refactor this module is a thin
caller of :class:`work_buddy.context.ContextCollector` +
:class:`work_buddy.context.ContextCurator`. All the legacy collectors
still exist under ``work_buddy/collectors/*`` and are wrapped as
registered ``ContextSource``\\ s; the refactor keeps the on-disk bundle
shape intact so consumers that read ``agents/<session>/context/<ts>/
*_summary.md`` files don't need to change.

File-name mapping from source → bundle file:

    tasks source               → (skipped; structured, not for bundle)
    projects source            → (skipped; structured, not for bundle)
    obsidian                   → obsidian_summary.md
    obsidian_tasks             → tasks_summary.md
    obsidian_wellness          → wellness_summary.md
    git source                 → git_summary.md (rendered markdown)
    chrome source              → chrome_summary.md (rendered markdown)
    chat                       → chat_summary.md
    message                    → messages_summary.md
    vault                      → vault_summary.md
    calendar                   → calendar_summary.md
    day_planner                → day_planner_summary.md
    session_activity           → session_activity_summary.md
    agent_session_summary     → agent_session_summary.md
    datacore                   → datacore_summary.md
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from omegaconf import OmegaConf

from work_buddy import __version__
from work_buddy.agent_session import get_session_context_dir
from work_buddy.config import load_config
from work_buddy.context import (
    ContextCollector,
    ContextCurator,
    ContextDepth,
    ContextRequest,
)
from work_buddy.logging_config import get_logger

logger = get_logger(__name__)


# Default bundle set — source name → output filename. The structured
# wave-1 sources (tasks, projects) are intentionally omitted: their
# value is in the LLM-prompt path via build_triage_context, not in a
# markdown bundle file. The markdown counterparts that preserve the
# legacy bundle shape are ``obsidian_tasks`` and (via projects, if
# desired) a future wrapper.
_BUNDLE_MAP: dict[str, str] = {
    "obsidian":          "obsidian_summary.md",
    "obsidian_tasks":    "tasks_summary.md",
    "obsidian_wellness": "wellness_summary.md",
    "git":               "git_summary.md",
    "chat":              "chat_summary.md",
    "chrome":            "chrome_summary.md",
    "message":           "messages_summary.md",
    "vault":             "vault_summary.md",
    "calendar":          "calendar_summary.md",
    "day_planner":       "day_planner_summary.md",
    "session_activity":  "session_activity_summary.md",
    "agent_session_summary": "agent_session_summary.md",
    "datacore":          "datacore_summary.md",
    "projects":          "projects_summary.md",
}

# Back-compat: the old CLI used "chats" and "messages" / "obsidian"
# grouped differently. Map legacy --only names to source-name lists.
_LEGACY_ONLY_ALIAS: dict[str, list[str]] = {
    "chats":     ["chat"],
    "messages":  ["message"],
    "obsidian":  ["obsidian", "obsidian_tasks", "obsidian_wellness"],
    "projects":  ["projects"],
}

# Subset of _BUNDLE_MAP that the default "run all" pass includes. The
# datacore wrapper is included but its collector emits an empty string
# when CONTEXT_QUERIES is empty (see work_buddy/collectors/datacore_collector.py),
# so it costs nothing until queries are populated.
COLLECTORS = {
    "git", "obsidian", "chat", "chrome", "message",
    "vault", "calendar", "day_planner", "projects",
    "session_activity", "agent_session_summary",
    "obsidian_tasks", "obsidian_wellness", "datacore",
}

# Optional collectors — included in bundles but not in default set.
OPTIONAL_COLLECTORS: set[str] = set()


# Global overrides that expand into per-collector time windows.
_TIME_GLOBALS = {
    "hours": lambda h: h / 24.0,
    "days": lambda d: d,
}
_TIME_TARGETS = [
    ("git.detail_days", float),
    ("git.active_days", float),
    ("obsidian.journal_days", lambda d: max(1, int(d))),
    ("obsidian.recent_modified_days", float),
    ("chats.specstory_days", lambda d: max(1, int(d))),
    ("chats.claude_history_days", lambda d: max(1, int(d))),
    ("agent_session_summary.days", lambda d: max(1, int(d))),
]


def _expand_overrides(overrides: list[str]) -> list[str]:
    """Expand global shorthand overrides into per-collector dotlist entries.

    Unchanged from pre-refactor — the legacy collectors still read
    their options from ``cfg`` sections, and the new sources forward
    everything under ``request.custom`` into those same keys.
    """
    expanded = []
    specific = []
    for o in overrides:
        key, _, val = o.partition("=")
        if key in _TIME_GLOBALS and val:
            raw_days = _TIME_GLOBALS[key](float(val))
            for dotpath, coerce in _TIME_TARGETS:
                coerced = coerce(raw_days) if callable(coerce) else raw_days
                expanded.append(f"{dotpath}={coerced}")
        else:
            specific.append(o)
    return expanded + specific


def _make_bundle_dir(cfg: dict[str, Any]) -> Path:
    """Resolve the per-session context dir where bundle files land."""
    return get_session_context_dir()


def _write_meta(
    bundle_dir: Path,
    cfg: dict[str, Any],
    collectors_run: list[str],
    *,
    since: datetime | None = None,
    until: datetime | None = None,
) -> None:
    meta = {
        "version": __version__,
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "collectors_run": collectors_run,
        # The window every source was collected for (uniform per run). Records
        # what the bundle actually covers, so a reader never has to infer it.
        "window": {
            "since": since.isoformat() if since else None,
            "until": until.isoformat() if until else None,
        },
        "config": {
            "vault_root": cfg.get("vault_root"),
            "repos_root": cfg.get("repos_root"),
        },
    }
    (bundle_dir / "bundle_meta.json").write_text(
        json.dumps(meta, indent=2), encoding="utf-8",
    )


def _window_header(since: datetime | None, until: datetime | None) -> str:
    """A one-line window banner prepended to each bundle file, so every file
    states the exact window it was collected for (local wall-clock time)."""
    from work_buddy.timefmt import to_local_naive

    s = to_local_naive(since)
    u = to_local_naive(until)
    if s and u:
        label = f"{s.strftime('%Y-%m-%d %H:%M')} → {u.strftime('%Y-%m-%d %H:%M')}"
    elif s:
        label = f"since {s.strftime('%Y-%m-%d %H:%M')}"
    elif u:
        label = f"until {u.strftime('%Y-%m-%d %H:%M')}"
    else:
        return ""
    return f"*Window: {label}*\n\n"


def _sources_from_only(only: str | None) -> list[str]:
    """Resolve the ``--only`` arg to a list of source names."""
    if not only:
        return sorted(COLLECTORS)
    if only in _LEGACY_ONLY_ALIAS:
        return list(_LEGACY_ONLY_ALIAS[only])
    return [only]


def _custom_from_cfg(cfg: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Forward per-collector cfg sections into ``ContextRequest.custom``.

    Legacy collectors read options via ``cfg["<collector>"][key]``. The
    markdown-wrapper source builds a legacy ``cfg`` dict from
    ``request.custom_for(name)`` + window bounds, so we just copy the
    relevant config sections under source names. The ``_NAME_MAP``
    handles the few places where the source name doesn't match the
    cfg-section name (``chat`` ↔ ``chats``, ``message`` ↔ ``messages``).
    """
    _NAME_MAP = {
        "chat": "chats",
        "message": "messages",
    }
    custom: dict[str, dict[str, Any]] = {}
    for source_name in COLLECTORS | OPTIONAL_COLLECTORS:
        cfg_key = _NAME_MAP.get(source_name, source_name)
        section = cfg.get(cfg_key) or {}
        if section:
            # Flatten the section plus globally-scoped knobs the
            # legacy collectors read (vault_root, repos_root, since,
            # until, bundles_dir) so the collector sees the same cfg
            # shape it always did.
            forwarded = dict(section)
            for global_key in ("vault_root", "repos_root", "since",
                                "until", "bundles_dir"):
                if global_key in cfg and global_key not in forwarded:
                    forwarded[global_key] = cfg[global_key]
            custom[source_name] = forwarded
    return custom


def run_collection(
    cfg: dict[str, Any],
    only: str | None = None,
    dry_run: bool = False,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
) -> Path | None:
    """Run the context collection via ContextCollector + ContextCurator.

    Writes one ``<source>_summary.md`` per source into the session
    context dir, plus a ``bundle_meta.json`` manifest. Skips sources
    that return empty content.

    ``since`` / ``until`` set the exact fetch window on the request (aware
    datetimes). Explicit args win; otherwise a top-level ``cfg`` since/until
    (e.g. a ``collect since=... until=...`` CLI override) is parsed and used,
    so every source — including GitSource, which reads the request window
    rather than a cfg section — honors the same bound.
    """
    from work_buddy.timefmt import parse_time_bound

    sources = _sources_from_only(only)

    if dry_run:
        logger.info("Dry run — would collect: %s", ", ".join(sources))
        logger.info("Vault root: %s", cfg.get("vault_root"))
        logger.info("Repos root: %s", cfg.get("repos_root"))
        logger.info("Bundles dir: %s", cfg.get("bundles_dir"))
        return None

    since_dt = since if since is not None else parse_time_bound(cfg.get("since"))
    until_dt = until if until is not None else parse_time_bound(cfg.get("until"))

    bundle_dir = _make_bundle_dir(cfg)
    logger.info("Collecting context bundle -> %s", bundle_dir)

    # Bundle output wants the original legacy shape: each source
    # rendered at DEEP so we don't truncate collectors that emit 5-10KB
    # of content. Callers who want slimmer output are expected to go
    # through the curator with a smaller depth (see
    # triage/recommend.build_triage_context for a live example).
    request = ContextRequest(
        sources=sources,
        depth=ContextDepth.DEEP,
        window_days=1,
        since=since_dt,
        until=until_dt,
        custom=_custom_from_cfg(cfg),
    )

    # Lazy-import work_buddy.context — registers all sources on import.
    from work_buddy.context import sources as _sources_pkg  # noqa: F401

    context = ContextCollector().collect(request)
    curator = ContextCurator()
    collectors_run: list[str] = []

    for source_name in sources:
        filename = _BUNDLE_MAP.get(source_name)
        if not filename:
            logger.debug("collect: no bundle file mapping for source %r; skipping", source_name)
            continue
        section = context.section(source_name)
        if section is None:
            logger.debug("collect: no section produced for %r; skipping", source_name)
            continue
        # Render THIS section only — avoid bleed from siblings'
        # ordering. Use a throwaway Context so the curator sees one
        # source at a time.
        single = type(context)(
            sections={source_name: section}, request=request,
        )
        rendered = curator.curate(single, depth=ContextDepth.DEEP, header=None)
        if not rendered.strip():
            logger.debug("collect: %r rendered empty; skipping file", source_name)
            continue
        banner = _window_header(since_dt, until_dt)
        (bundle_dir / filename).write_text(banner + rendered, encoding="utf-8")
        collectors_run.append(source_name)
        logger.info("Collected %s -> %s", source_name, filename)

    _write_meta(bundle_dir, cfg, collectors_run, since=since_dt, until=until_dt)
    logger.info("Context bundle saved: %s", bundle_dir)
    return bundle_dir


def main() -> None:
    """CLI entry point.

    Collector-specific options pass as dot-notation overrides::

        collect git.dirty_only=true chats.last=3
        collect git.detail_days=1 obsidian.journal_days=14
        collect --only chats chats.last=5

    Global time overrides expand to all applicable collectors::

        collect hours=6
        collect days=3
        collect hours=6 git.detail_days=1
    """
    parser = argparse.ArgumentParser(
        prog="collect",
        description="Collect a context bundle snapshot for work-buddy.",
    )
    parser.add_argument(
        "--only", default=None,
        help=(
            "Run only a single collector. Accepts a source name "
            "(e.g. 'git', 'obsidian_tasks', 'chat') or a legacy alias "
            "(e.g. 'chats' → chat, 'messages' → message, 'obsidian' → "
            "journal + tasks + wellness)."
        ),
    )
    parser.add_argument("--since", type=str, default=None)
    parser.add_argument("--until", type=str, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--config", type=Path, default=None,
        help="Path to config.yaml (default: config.yaml in repo root).",
    )
    parser.add_argument(
        "overrides", nargs="*",
        help="Dot-notation config overrides, e.g. git.dirty_only=true chats.last=3",
    )

    args = parser.parse_args()
    cfg = load_config(args.config)

    if args.overrides:
        base = OmegaConf.create(cfg)
        cli = OmegaConf.from_dotlist(_expand_overrides(args.overrides))
        cfg = OmegaConf.to_container(OmegaConf.merge(base, cli), resolve=True)

    if args.since:
        cfg["since"] = args.since
    if args.until:
        cfg["until"] = args.until

    try:
        run_collection(cfg, only=args.only, dry_run=args.dry_run)
    except KeyboardInterrupt:
        logger.info("Aborted.")
        sys.exit(1)


if __name__ == "__main__":
    main()
