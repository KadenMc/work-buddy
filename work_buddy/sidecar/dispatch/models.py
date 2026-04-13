"""Agent spawn models — structured types for the Tier 3 execution layer.

These models formalize the sidecar's agent spawning capabilities into
explicit spawn modes. Previously this was an implicit ``persistent: bool``
branch; now each mode is a first-class concept that carries its own
semantics for session lifecycle, resumability, and visibility.

Spawn modes
-----------
- **headless_ephemeral**: Fire-and-forget ``claude -p`` + ``--no-session-persistence``.
  No durable session state. Used for simple one-shot tasks.
- **headless_persistent**: ``claude -p`` without ``--no-session-persistence``.
  Session state is saved and the ``session_id`` is captured in the agent
  registry. Can be resumed later via ``claude --resume <id>`` (callback
  path in ``notifications/store.py``). Note: resumes currently use
  ``--no-session-persistence``, so persistence is one-write — the initial
  run's context is preserved, but resume runs do not accumulate further
  state.
- **interactive_persistent**: Reserved for launching a user-visible
  interactive Claude Code session. Implementation deferred until the
  interactive runner is configured. Placeholder raises ``NotImplementedError``.

Agent targets
-------------
Currently only ``new_agent`` (spawn a fresh session). The ``agent_target``
field is present to support future semantic routing where the sidecar
might choose to resume an existing agent rather than spawn a new one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class SpawnMode(str, Enum):
    """How a new agent session should be launched."""

    HEADLESS_EPHEMERAL = "headless_ephemeral"
    HEADLESS_PERSISTENT = "headless_persistent"
    INTERACTIVE_PERSISTENT = "interactive_persistent"

    @property
    def is_headless(self) -> bool:
        return self in (SpawnMode.HEADLESS_EPHEMERAL, SpawnMode.HEADLESS_PERSISTENT)

    @property
    def is_persistent(self) -> bool:
        return self in (SpawnMode.HEADLESS_PERSISTENT, SpawnMode.INTERACTIVE_PERSISTENT)

    @property
    def is_resumable(self) -> bool:
        """Whether the session_id is meaningful for future resume."""
        return self.is_persistent

    @property
    def is_visible(self) -> bool:
        """Whether the session appears in the user's interactive picker."""
        return self == SpawnMode.INTERACTIVE_PERSISTENT


class AgentTarget(str, Enum):
    """Where to direct the agent work."""

    NEW_AGENT = "new_agent"
    # Future: EXISTING_AGENT = "existing_agent" — semantic routing


@dataclass
class SpawnResult:
    """Structured result from an agent spawn operation.

    Captures everything needed for tracking, resuming, and routing
    to this agent in the future. Written to the agent registry for
    persistent modes.
    """

    # Identity
    agent_target: str = AgentTarget.NEW_AGENT.value
    spawn_mode: str = SpawnMode.HEADLESS_EPHEMERAL.value
    session_name: str = ""       # e.g. "daemon:my-job"
    session_id: str | None = None  # Claude-assigned, from JSON output

    # Lifecycle
    resumable: bool = False      # Can be resumed via --resume
    visible: bool = False        # Appears in interactive picker
    status: str = "spawned"      # spawned | completed | failed | resumed | expired

    # Execution outcome
    result_text: str = ""
    error: str = ""
    return_code: int | None = None
    elapsed_seconds: float | None = None

    # Provenance
    source_job: str = ""         # Job name that triggered this spawn
    source_workflow: str = ""    # Workflow step, if applicable
    source_message: str = ""     # Message ID, if message-dispatched

    # Metadata
    created_at: str = ""
    last_resumed_at: str | None = None
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize for JSON storage."""
        from dataclasses import asdict
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SpawnResult:
        """Deserialize from a dict, ignoring unknown fields."""
        known = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in known}
        return cls(**filtered)

    @classmethod
    def for_mode(
        cls,
        mode: SpawnMode,
        *,
        session_name: str = "",
        session_id: str | None = None,
        source_job: str = "",
        **kwargs: Any,
    ) -> SpawnResult:
        """Factory: create a SpawnResult with mode-appropriate defaults."""
        return cls(
            spawn_mode=mode.value,
            session_name=session_name,
            session_id=session_id,
            resumable=mode.is_resumable,
            visible=mode.is_visible,
            source_job=source_job,
            **kwargs,
        )
