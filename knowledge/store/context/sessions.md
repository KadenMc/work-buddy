---
name: Session Inspection
kind: concept
description: Random-access into canonical agent-harness conversation sessions, including Claude Code, Codex, and registered transcript providers
summary: 'ConversationSession resolves a native transcript through the provider registry, then wraps canonical turns with lazy indexing, span-to-turn mapping, metadata, search, and activity extraction.'
tags:
- context
- sessions
- conversations
- inspection
aliases:
- session inspector
- conversation browser
- session_get
- session_search
- session_expand
- session_locate
- session_commits
parents:
- context
---

Random-access into individual agent-harness conversation sessions. Built-in providers support Claude Code and Codex; external adapters can register through the `work_buddy.transcript_providers` entry-point group. Providers normalize native JSONL into canonical sessions, turns, and tool calls before the inspector, IR, observability, or Dashboard consumes it.

For the **find-a-prior-conversation-by-topic** flow, the system is layered. The top layer (`summary_search`) ranks compressed summaries and auto-drills via `session_search`; for very recent sessions (cron is 2h-cadence), error-status summaries, or exact-substring needles, fall back to `context_search(source="conversation")` against raw spans. The directions at `context/session-identify` (`/wb-session-identify`) recipe the full pattern; `disclosure/` carries the broader find → walk → read decision rule.

The capabilities below are the **leaf-level building blocks** for reading session contents once a session id is in hand:

- **Find which sessions mention X**: `summary_search(query, scope="conversation_session")` (preferred — ranks against compressed layer, drills via `session_search`); or `context_search(query, source="conversation")` for raw-span search.
- **Browse a session's messages**: `session_get(session_id, optional limit/offset/query/roles/message_types)`.
- **Find messages within a known session**: `session_get(session_id, query=...)` (substring filter) or `session_search(session_id, query=...)` (BM25 + dense within one session, resolves spans to turn indices).
- **Read full text around a message**: `session_expand(session_id, message_index, span)`.
- **Jump from a search hit to a turn**: `session_locate(session_id, span_index)`.
- **Pivot from a `summary_search` hit to walk the tree**: `drill_tree(domain="summary", node_id=hit['drill_node_id'], depth=...)`.
- **Git commits from agent sessions**: `session_commits(optional days)`. Pair with `context_git(annotate=true)` for git context tagged by session.
- **Resume a session locally**: the Dashboard selects the recorded harness and opens `claude --resume <id>` or `codex resume <id>` without sending a prompt. cwd is derived from canonical session metadata. Codex resume additionally requires an executable Codex CLI; transcript browsing itself does not.

Session ID resolution: canonical IDs preserve the provider's native session identity. Built-in Claude and Codex IDs are UUIDs, and all session_* capabilities accept unique partial prefixes (8 chars is the conventional display form). Three helpers share the same resolver:
- `resolve_session_id(partial)` returns full UUID.
- `resolve_session_path(partial)` returns (Path, full_uuid).
- `get_session_cwd(partial)` returns the provider-supplied working directory, or None. Used by native resume launchers to open the terminal in the right place.

`resolve_session_id` and `resolve_session_path` raise `FileNotFoundError` on zero or ambiguous matches; `get_session_cwd` returns None instead of raising. Convention: store full UUIDs in data, truncate to 8 chars only at display boundaries.

Architecture: `work_buddy/transcripts/` owns the provider protocol, built-in adapters, discovery, collision checks, and canonical models. `ConversationSession` wraps one resolved provider session with a lazy-loaded indexed turn list. Span-to-turn mapping replays the IR chunking algorithm for reliable `session_locate`. Metadata includes canonical and native session ids, harness/provider ids, cwd/project, message count, duration, timestamps, and tool usage.

Git/activity extraction consumes canonical provider tool calls, so Claude `Bash` and Codex function calls share commit, PR, and write attribution logic. `build_session_map(days)` returns `{short_hash: full_session_id}` for joining with `git_collector` output. `context_git(annotate=true)` uses this map to tag commit lines.

Files: `inspector.py` (ConversationSession class, session ID + cwd resolvers, commit extraction, 6 gateway handlers), `__init__.py` (empty package init).
