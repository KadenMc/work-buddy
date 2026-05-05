"""Claude Code usage ingestion and cost computation.

This package vendors the JSONL-scanner mechanics from
`phuryn/claude-usage <https://github.com/phuryn/claude-usage>`_ (MIT,
Copyright (c) 2026 Pawel Huryn) and adapts the few touch points where
work-buddy's conventions differ from a stand-alone tool:

* The SQLite DB lives under ``<data_root>/cache/claude_code_usage.db`` and is
  registered in :data:`work_buddy.paths.RESOURCES` as
  ``cache/claude-code-usage``. This keeps every generated artifact under
  work-buddy's data root rather than scattering state into
  ``~/.claude/usage.db``.
* The Claude Code projects directory is configurable via
  ``llm.claude_code_usage.projects_dirs`` in ``config.yaml``; default
  falls back to the upstream's ``~/.claude/projects`` discovery.
* ``print()`` is replaced with the standard work-buddy logger.

This data is exposed through ``/api/costs?source=claude_code`` (the
dashboard) and via the ``claude_code_usage_scan`` MCP capability. The
unified ``llm_costs_query`` capability also reads from this source.

The cost rate table at :mod:`work_buddy.llm.claude_code_usage.pricing`
is now the **canonical** rate source for the whole repo; the
per-call cost log (:mod:`work_buddy.llm.cost`) imports
:func:`calc_cost` from here.
"""
