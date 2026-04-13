"""Collect Day Planner schedule from today's journal.

Reads the Day Planner section from the current journal and formats
it as a markdown summary showing planned vs. completed blocks.

Requires Obsidian running with the Day Planner plugin installed.
Degrades gracefully if unavailable.
"""

from datetime import datetime
from typing import Any

from work_buddy.logging_config import get_logger

logger = get_logger(__name__)


def collect(cfg: dict[str, Any]) -> str:
    """Collect today's Day Planner entries and return a markdown summary.

    Returns a formatted markdown string suitable for context bundles.
    Returns a "not available" message if Obsidian or the plugin is unreachable.
    """
    from work_buddy.obsidian import bridge

    try:
        available = bridge.is_available()
    except Exception:
        available = False

    if not available:
        logger.info("Obsidian bridge not available — skipping day planner collection")
        return _unavailable_report("Obsidian bridge not reachable")

    try:
        from work_buddy.obsidian.day_planner import check_ready
        status = check_ready()
        if not status.get("ready"):
            reason = status.get("reason", "unknown")
            logger.info("Day Planner not ready: %s", reason)
            return _unavailable_report(f"Day Planner plugin: {reason}")
    except Exception as e:
        logger.warning("Day Planner check_ready failed: %s", e)
        return _unavailable_report(f"check_ready error: {e}")

    today_str = datetime.now().strftime("%Y-%m-%d")
    vault_root = cfg["vault_root"]
    journal_dir = cfg.get("obsidian", {}).get("journal_dir", "journal")
    journal_path = f"{journal_dir}/{today_str}.md"

    try:
        from work_buddy.obsidian.day_planner import get_todays_plan
        plan = get_todays_plan(journal_path)
        return _format_plan(plan, today_str)
    except Exception as e:
        logger.warning("Day Planner collection failed: %s", e)
        return _unavailable_report(f"Failed to read plan: {e}")


def _format_plan(plan: dict, date_str: str) -> str:
    """Format plan data into a readable markdown summary."""
    lines = ["# Day Planner — Today's Schedule"]
    lines.append("")
    lines.append(f"**Date:** {date_str}")

    if not plan.get("found"):
        lines.append("")
        lines.append("No Day Planner section found in today's journal.")
        return "\n".join(lines)

    entries = plan.get("entries", [])
    unscheduled = plan.get("unscheduled", [])
    total = len(entries) + len(unscheduled)

    if total == 0:
        lines.append("")
        lines.append("Day Planner section exists but is empty (no plan generated yet).")
        return "\n".join(lines)

    checked = sum(1 for e in entries if e.get("checked"))
    checked += sum(1 for e in unscheduled if e.get("checked"))

    lines.append(f"**Entries:** {total} ({checked} completed, {total - checked} remaining)")
    lines.append("")

    # Scheduled entries
    if entries:
        lines.append("## Scheduled")
        for e in entries:
            mark = "x" if e.get("checked") else " "
            lines.append(
                f"- [{mark}] {e['time_start']} - {e['time_end']} {e['text']}"
            )

    # Unscheduled entries
    if unscheduled:
        lines.append("")
        lines.append("## Unscheduled")
        for e in unscheduled:
            mark = "x" if e.get("checked") else " "
            lines.append(f"- [{mark}] {e['text']}")

    return "\n".join(lines)


def _unavailable_report(reason: str) -> str:
    """Generate a minimal report when Day Planner data is not available."""
    return (
        "# Day Planner\n\n"
        f"Day Planner data not available: {reason}\n\n"
        "To enable: open Obsidian, ensure the Day Planner plugin is installed."
    )
