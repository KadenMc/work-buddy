"""Control graph — unified view-model over preferences, requirements, health, and registry.

The ``control`` package is a pure view-model layer. It consumes data from
the existing subsystems (``health``, ``mcp_server.registry``, ``tools``)
and exposes a single :class:`ControlNode` graph that the dashboard's
Settings tab renders and that agents can query for transitive
dependency impact analysis.

Nothing inside ``control`` owns state. Every graph build reads fresh
inputs from the authoritative sources. A module-level TTL cache in
:mod:`work_buddy.control.graph` avoids recomputing more often than
every ~45 s; preference writes invalidate the cache eagerly.

Public entry points:

    from work_buddy.control.graph import build_graph, invalidate_graph
    from work_buddy.control.capability_resolver import resolve_dependencies
"""
