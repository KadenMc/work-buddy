"""Entity registry — a reference-resolution layer for the user's named world.

A registry of the named things in the user's world (people, places,
institutions, projects, concepts) and what each one *means to the user*,
so an agent never has to be re-told basic facts.

Authored-only: entities exist because an agent or the user created them.
No corpus scanner, no LLM extraction, no candidate-discovery flow. The
description field carries authored prose, including relationship context
("Max McKeen — Kaden's younger brother.").

Resolution federation lives one layer up in
:func:`work_buddy.mcp_server.context_wrappers.entity_resolve`, which
queries this store *and* the project registry as parallel resolution
sources and merges results at read time. The entity store remains the
sole owner of entity rows; projects stay sole owner of project rows.

The reference index (``entity_references``) is append-only and tracks
where each entity has been mentioned, with timestamps. References
survive document evolution — a later edit that removes the mention does
not retroactively erase the historical reference.

See ``architecture/entity-registry`` and ``entities/`` in the knowledge
store for the agent-facing surface.
"""

from __future__ import annotations
