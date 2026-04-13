"""CLI entry point for context bundle collection."""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from omegaconf import OmegaConf

from work_buddy import __version__
from work_buddy.config import load_config
from work_buddy.agent_session import get_session_context_dir
from work_buddy.logging_config import get_logger
from work_buddy.collectors import git_collector, obsidian_collector, chat_collector, chrome_collector, message_collector, smart_collector, calendar_collector, day_planner_collector, datacore_collector, project_collector, session_activity_collector

logger = get_logger(__name__)

COLLECTORS = {"git", "obsidian", "chats", "chrome", "messages", "smart", "calendar", "day_planner", "projects", "session_activity"}

# Optional collectors — included in bundles but not in default set.
# Datacore: configurable query runner. No-op when CONTEXT_QUERIES is empty.
# Add to COLLECTORS when high-signal queries are identified.
OPTIONAL_COLLECTORS = {"datacore"}

# Global overrides that expand into per-collector time windows.
# Mapped as: global_key -> [(dotpath, transform), ...]
# Transforms convert the raw value (float) to the appropriate type per collector.
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
]


def _expand_overrides(overrides: list[str]) -> list[str]:
    """Expand global shorthand overrides into per-collector dotlist entries.

    Global overrides (``hours=6``, ``days=3``) are expanded first so that
    specific overrides always win::

        collect hours=6 git.detail_days=1
        # -> all windows = 0.25 days, except git.detail_days = 1
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
    # Specific overrides come last so they take priority in from_dotlist
    return expanded + specific


def _make_bundle_dir(cfg: dict[str, Any]) -> Path:
    """Create and return the pack directory for this run.

    Writes into agents/<session>/context/<timestamp>/ so packs are tied to the
    agent session that generated them, with each collection as a separate snapshot.
    """
    return get_session_context_dir()


def _write_meta(bundle_dir: Path, cfg: dict[str, Any], collectors_run: list[str]) -> None:
    """Write pack metadata JSON."""
    meta = {
        "version": __version__,
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "collectors_run": collectors_run,
        "config": {
            "vault_root": cfg.get("vault_root"),
            "repos_root": cfg.get("repos_root"),
        },
    }
    meta_path = bundle_dir / "bundle_meta.json"
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")


def run_collection(
    cfg: dict[str, Any],
    only: str | None = None,
    dry_run: bool = False,
) -> Path | None:
    """Run the context collection.

    Collector-specific options are read from ``cfg`` sections:
    - ``cfg["git"]["dirty_only"]`` — only include repos with uncommitted changes
    - ``cfg["chats"]["last"]`` — limit to the N most recent sessions
    """
    collectors_to_run = {only} if only else COLLECTORS

    if dry_run:
        logger.info("Dry run — would collect: %s", ", ".join(sorted(collectors_to_run)))
        logger.info("Vault root: %s", cfg.get("vault_root"))
        logger.info("Repos root: %s", cfg.get("repos_root"))
        logger.info("Bundles dir: %s", cfg.get("bundles_dir"))
        if "git" in collectors_to_run:
            logger.info("Git: detail_days=%s, active_days=%s, dirty_only=%s",
                        cfg["git"]["detail_days"], cfg["git"]["active_days"],
                        cfg["git"].get("dirty_only", False))
        if "obsidian" in collectors_to_run:
            logger.info("Obsidian: journal_days=%s, recent_modified_days=%s",
                        cfg["obsidian"]["journal_days"], cfg["obsidian"]["recent_modified_days"])
        if "chats" in collectors_to_run:
            logger.info("Chats: specstory_days=%s, claude_history_days=%s, last=%s",
                        cfg["chats"]["specstory_days"], cfg["chats"]["claude_history_days"],
                        cfg["chats"].get("last"))
        return None

    bundle_dir = _make_bundle_dir(cfg)
    logger.info("Collecting context bundle -> %s", bundle_dir)

    collectors_run = []

    if "git" in collectors_to_run:
        logger.info("Collecting git status...")
        git_md = git_collector.collect(cfg)
        (bundle_dir / "git_summary.md").write_text(git_md, encoding="utf-8")
        collectors_run.append("git")
        logger.info("Git collection complete.")

    if "obsidian" in collectors_to_run:
        logger.info("Collecting Obsidian context...")
        obsidian_md, tasks_md = obsidian_collector.collect(cfg)
        (bundle_dir / "obsidian_summary.md").write_text(obsidian_md, encoding="utf-8")
        (bundle_dir / "tasks_summary.md").write_text(tasks_md, encoding="utf-8")
        wellness_md = obsidian_collector.collect_wellness(cfg)
        (bundle_dir / "wellness_summary.md").write_text(wellness_md, encoding="utf-8")
        collectors_run.append("obsidian")
        logger.info("Obsidian collection complete.")

    if "chats" in collectors_to_run:
        logger.info("Collecting chat history...")
        chat_md = chat_collector.collect(cfg)
        (bundle_dir / "chat_summary.md").write_text(chat_md, encoding="utf-8")
        collectors_run.append("chats")
        logger.info("Chat collection complete.")

    if "chrome" in collectors_to_run:
        logger.info("Collecting Chrome tabs...")
        chrome_md = chrome_collector.collect(cfg)
        (bundle_dir / "chrome_summary.md").write_text(chrome_md, encoding="utf-8")
        collectors_run.append("chrome")
        logger.info("Chrome collection complete.")

    if "messages" in collectors_to_run:
        logger.info("Collecting message state...")
        messages_md = message_collector.collect(cfg)
        (bundle_dir / "messages_summary.md").write_text(messages_md, encoding="utf-8")
        collectors_run.append("messages")
        logger.info("Message collection complete.")

    if "smart" in collectors_to_run:
        logger.info("Collecting Smart context...")
        smart_md = smart_collector.collect(cfg)
        (bundle_dir / "smart_summary.md").write_text(smart_md, encoding="utf-8")
        collectors_run.append("smart")
        logger.info("Smart collection complete.")

    if "calendar" in collectors_to_run:
        logger.info("Collecting calendar schedule...")
        calendar_md = calendar_collector.collect(cfg)
        (bundle_dir / "calendar_summary.md").write_text(calendar_md, encoding="utf-8")
        collectors_run.append("calendar")
        logger.info("Calendar collection complete.")

    if "day_planner" in collectors_to_run:
        logger.info("Collecting day planner schedule...")
        dp_md = day_planner_collector.collect(cfg)
        (bundle_dir / "day_planner_summary.md").write_text(dp_md, encoding="utf-8")
        collectors_run.append("day_planner")
        logger.info("Day planner collection complete.")

    if "projects" in collectors_to_run:
        logger.info("Collecting project context...")
        projects_md = project_collector.collect(cfg)
        (bundle_dir / "projects_summary.md").write_text(projects_md, encoding="utf-8")
        collectors_run.append("projects")
        logger.info("Project collection complete.")

    if "session_activity" in collectors_to_run:
        logger.info("Collecting session activity ledger...")
        activity_md = session_activity_collector.collect(cfg)
        (bundle_dir / "session_activity_summary.md").write_text(activity_md, encoding="utf-8")
        collectors_run.append("session_activity")
        logger.info("Session activity collection complete.")

    if "datacore" in collectors_to_run:
        logger.info("Collecting Datacore vault structure...")
        datacore_md = datacore_collector.collect(cfg)
        (bundle_dir / "datacore_summary.md").write_text(datacore_md, encoding="utf-8")
        collectors_run.append("datacore")
        logger.info("Datacore collection complete.")

    _write_meta(bundle_dir, cfg, collectors_run)
    logger.info("Context bundle saved: %s", bundle_dir)
    return bundle_dir


def main() -> None:
    """CLI entry point.

    Collector-specific options are passed as dot-notation overrides::

        collect git.dirty_only=true chats.last=3
        collect git.detail_days=1 obsidian.journal_days=14
        collect --only chats chats.last=5

    Global time overrides expand to all applicable collectors::

        collect hours=6                        # all windows = 6 hours
        collect days=3                         # all windows = 3 days
        collect hours=6 git.detail_days=1      # 6h everywhere, except git detail = 1 day

    Any key in config.yaml can be overridden this way. See config.yaml
    for available options per collector.
    """
    parser = argparse.ArgumentParser(
        prog="collect",
        description="Collect a context bundle snapshot for work-buddy.",
    )
    parser.add_argument(
        "--only",
        choices=sorted(COLLECTORS),
        help="Run only a single collector.",
    )
    parser.add_argument(
        "--since",
        type=str,
        default=None,
        help="ISO datetime for range start (e.g. 2026-04-02T05:00:00). Overrides *_days.",
    )
    parser.add_argument(
        "--until",
        type=str,
        default=None,
        help="ISO datetime for range end (e.g. 2026-04-02T17:00:00). Overrides *_days.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be collected without writing files.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to config.yaml (default: config.yaml in repo root).",
    )
    parser.add_argument(
        "overrides",
        nargs="*",
        help="Dot-notation config overrides, e.g. git.dirty_only=true chats.last=3",
    )

    args = parser.parse_args()
    cfg = load_config(args.config)

    # Expand global shorthands and merge CLI overrides into config
    if args.overrides:
        base = OmegaConf.create(cfg)
        cli = OmegaConf.from_dotlist(_expand_overrides(args.overrides))
        cfg = OmegaConf.to_container(OmegaConf.merge(base, cli), resolve=True)

    # Inject explicit time range into config (collectors read cfg["since"]/cfg["until"])
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
