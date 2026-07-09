"""Supported harness registry."""

from __future__ import annotations

from work_buddy.harness.model import HarnessTarget


_HARNESS_FEATURE_ORDER = ("rules", "mcp", "commands", "skills")

_HARNESSES: dict[str, HarnessTarget] = {
    "claudecode": HarnessTarget(
        id="claudecode",
        label="Claude Code",
        rulesync_target="claudecode",
        description="Claude Code project surface: CLAUDE/command/MCP artifacts.",
        features=_HARNESS_FEATURE_ORDER,
    ),
    "codexcli": HarnessTarget(
        id="codexcli",
        label="Codex CLI",
        rulesync_target="codexcli",
        description="Codex project surface: AGENTS.md, MCP config, and skills.",
        features=("rules", "mcp", "skills"),
        simulate_skills=True,
        setup_ready=False,
        setup_note=(
            "experimental: Codex artifacts generate, but session hook/env "
            "propagation is not yet end-to-end setup-ready"
        ),
    ),
}


def list_harnesses() -> list[HarnessTarget]:
    return list(_HARNESSES.values())


def get_harness(harness_id: str) -> HarnessTarget:
    try:
        return _HARNESSES[harness_id]
    except KeyError as exc:
        known = ", ".join(sorted(_HARNESSES))
        raise ValueError(f"unknown harness {harness_id!r}; known: {known}") from exc


def resolve_harnesses(ids: list[str] | tuple[str, ...]) -> list[HarnessTarget]:
    return [get_harness(i) for i in ids]
