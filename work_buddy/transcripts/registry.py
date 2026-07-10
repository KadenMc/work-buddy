"""Transcript-provider registry with third-party entry-point discovery."""

from __future__ import annotations

from importlib import metadata
import logging
from pathlib import Path
from typing import Iterable

from work_buddy.transcripts.base import TranscriptProvider
from work_buddy.transcripts.models import TranscriptSession


_PROVIDERS: dict[str, TranscriptProvider] = {}
_ENTRY_POINTS_LOADED = False
logger = logging.getLogger(__name__)


def register_provider(provider: TranscriptProvider, *, replace: bool = False) -> None:
    provider_id = str(provider.id)
    if provider_id in _PROVIDERS and not replace:
        raise ValueError(f"transcript provider {provider_id!r} is already registered")
    _PROVIDERS[provider_id] = provider


def providers(*, enabled_only: bool = True) -> list[TranscriptProvider]:
    _ensure_builtins()
    _load_entry_points()
    values = list(_PROVIDERS.values())
    if not enabled_only:
        return values
    enabled = set(_enabled_provider_ids())
    return [provider for provider in values if provider.id in enabled]


def get_provider(provider_id: str) -> TranscriptProvider:
    for provider in providers(enabled_only=False):
        if provider.id == provider_id:
            return provider
    known = ", ".join(sorted(p.id for p in providers(enabled_only=False)))
    raise ValueError(f"unknown transcript provider {provider_id!r}; known: {known}")


def discover_sessions(
    *,
    days: int,
    project_filter: list[str] | None = None,
    provider_ids: Iterable[str] | None = None,
) -> list[TranscriptSession]:
    selected = (
        [get_provider(provider_id) for provider_id in provider_ids]
        if provider_ids is not None
        else providers()
    )
    sessions: list[TranscriptSession] = []
    seen: dict[str, str] = {}
    for provider in selected:
        for session in provider.discover(days=days, project_filter=project_filter):
            owner = seen.get(session.session_id)
            if owner is not None and owner != provider.id:
                raise ValueError(
                    f"session id {session.session_id!r} is emitted by both "
                    f"{owner!r} and {provider.id!r}; provider ids must be collision-safe"
                )
            seen[session.session_id] = provider.id
            sessions.append(session)
    sessions.sort(key=lambda session: session.mtime, reverse=True)
    return sessions


def resolve_session(session_id: str) -> TranscriptSession:
    exact: list[TranscriptSession] = []
    prefixes: list[TranscriptSession] = []
    for session in discover_sessions(days=0):
        if session.session_id == session_id or session.native_session_id == session_id:
            exact.append(session)
        elif (
            session.session_id.startswith(session_id)
            or session.native_session_id.startswith(session_id)
        ):
            prefixes.append(session)
    matches = exact or prefixes
    if not matches:
        raise FileNotFoundError(f"No session found matching '{session_id}'")
    if len(matches) > 1:
        ids = [session.session_id[:20] for session in matches[:5]]
        raise FileNotFoundError(
            f"Ambiguous session ID '{session_id}' matches {len(matches)} sessions: {ids}"
        )
    return matches[0]


def session_from_path(path: Path) -> TranscriptSession:
    resolved = path.resolve()
    for provider in providers(enabled_only=False):
        session = provider.session_from_path(resolved)
        if session is not None:
            return session
    raise FileNotFoundError(f"No transcript provider recognizes {path}")


def provider_for_session(session: TranscriptSession) -> TranscriptProvider:
    return get_provider(session.provider_id)


def _ensure_builtins() -> None:
    if "claudecode" not in _PROVIDERS:
        from work_buddy.transcripts.providers.claude import ClaudeTranscriptProvider

        register_provider(ClaudeTranscriptProvider())
    if "codexcli" not in _PROVIDERS:
        from work_buddy.transcripts.providers.codex import CodexTranscriptProvider

        register_provider(CodexTranscriptProvider())


def _load_entry_points() -> None:
    global _ENTRY_POINTS_LOADED
    if _ENTRY_POINTS_LOADED:
        return
    _ENTRY_POINTS_LOADED = True
    try:
        entry_points = metadata.entry_points(group="work_buddy.transcript_providers")
    except TypeError:  # pragma: no cover - Python 3.9 compatibility shape
        entry_points = metadata.entry_points().get(
            "work_buddy.transcript_providers", []
        )
    for entry_point in entry_points:
        try:
            loaded = entry_point.load()
            provider = loaded() if isinstance(loaded, type) else loaded
            register_provider(provider)
        except Exception as exc:
            # One broken optional adapter must not take down the built-in
            # Claude/Codex providers or Dashboard Chats.
            logger.warning(
                "Could not load transcript provider entry point %s: %s",
                entry_point.name,
                exc,
            )


def _enabled_provider_ids() -> tuple[str, ...]:
    try:
        from work_buddy.config import load_config

        configured = (load_config().get("transcripts") or {}).get("enabled")
    except Exception:
        configured = None
    values = configured if configured is not None else ("claudecode", "codexcli")
    return tuple(str(value) for value in values)
