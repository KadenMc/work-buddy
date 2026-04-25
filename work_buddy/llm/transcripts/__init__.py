"""Claude Code transcript ingestion and cost computation.

This package vendors the JSONL-scanner mechanics from
`phuryn/claude-usage <https://github.com/phuryn/claude-usage>`_ (MIT,
Copyright (c) 2026 Pawel Huryn) and adapts the few touch points where
work-buddy's conventions differ from a stand-alone tool:

* The SQLite DB lives under ``data/cache/claude_transcripts.db`` and is
  registered in :data:`work_buddy.paths.RESOURCES`. This keeps every
  generated artifact under work-buddy's data root rather than scattering
  state into ``~/.claude/usage.db``.
* The Claude Code projects directory is configurable via
  ``llm.transcripts.projects_dirs`` in ``config.yaml``; default falls
  back to the upstream's ``~/.claude/projects`` discovery.
* ``print()`` is replaced with the standard work-buddy logger.

Phase 2 surfaces this data through the existing ``/api/costs`` route as
a second source alongside the first-party LLM cost log
(:mod:`work_buddy.llm.cost`). See
:mod:`work_buddy.dashboard.costs_transcripts` for the dashboard glue.

The cost rate table here is the one shipped with claude-usage (April
2026); it is intentionally NOT the same dict as
:data:`work_buddy.llm.cost._COST_PER_M_TOKENS`. Different code paths
have historically used different rate sources, and Phase 2 keeps them
separate to avoid quietly re-pricing existing entries.
"""
