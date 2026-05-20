---
name: Session Inspection
kind: concept
description: Random-access into individual Claude Code conversation sessions — browsing, search, context expansion, git commit extraction
summary: 'ConversationSession wraps JSONL session files with lazy-loaded indexing, span-to-turn mapping, and metadata extraction. Capabilities: session_get (browse), session_search (hybrid search within session), session_expand (full context around a message), session_locate (jump from search hit to conversation), session_commits (git commits made by agents).'
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

Random-access into individual Claude Code conversation sessions. Provides paginated browsing, context expansion, and bridging from IR search hits to message-level results.

For the structured **find-a-prior-conversation-then-drill** flow (parallel cross-session sweep → score candidates → in-session drill → report key turns), follow `context/session-identify` (slash command `/wb-session-identify`). The capabilities below are the building blocks; that directions unit is the recipe.

When to use what:
- Find which sessions mention X: context_search (then drill in)
- Browse a session's messages: session_get with session_id + optional limit/offset/query/roles/message_types
- Find messages in a known session: session_get with query param (substring filter)
- Read full text around a message: session_expand with session_id + message_index
- Jump from search hit to conversation: session_locate with session_id + span_index
- Semantic/keyword search within session: session_search with session_id + query
- Git commits from agent sessions: session_commits with optional days param
- Git context with session attribution: context_git with annotate=true
- Resume a session locally: the session_resume capability (registered in the sidecar category) opens a new terminal running `claude --resume <id>` without sending a prompt. cwd is auto-derived via get_session_cwd. Also exposed on the dashboard as a Resume button in each chat's header.

Session ID resolution: All IDs are 36-char UUIDs. All session_* capabilities accept partial prefix IDs (8 chars is canonical short form). Three helpers on the same shape:
- resolve_session_id(partial) returns full UUID.
- resolve_session_path(partial) returns (Path, full_uuid).
- get_session_cwd(partial) returns the working directory the session ran in (scanned from the first JSONL record carrying a cwd field), or None. Used by session_resume and begin_session to open the terminal in the right place.

resolve_session_id and resolve_session_path raise FileNotFoundError on zero or ambiguous matches; get_session_cwd returns None instead of raising. Convention: store full UUIDs in data, truncate to 8 chars only at display boundaries.

Architecture: ConversationSession wraps a JSONL session file with lazy-loaded in-memory indexed turn list (via iter_session_turns from chat_collector.py). Span-to-turn mapping replays the IR chunking algorithm for reliable session_locate. Metadata: message count, duration, timestamps, tool usage summary.

Git commit extraction: session_commits parses raw JSONL entries (bypassing iter_session_turns which drops tool I/O) to find Bash tool calls containing git commit and their results. build_session_map(days) returns {short_hash: full_session_id} for joining with git_collector output. context_git with annotate=true uses this map to tag commit lines.

Files: inspector.py (ConversationSession class, session ID + cwd resolvers, commit extraction, 6 gateway handlers), __init__.py (empty package init).
