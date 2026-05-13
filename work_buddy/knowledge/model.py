"""Knowledge unit type hierarchy for the unified knowledge store.

Two parallel hierarchies share a common base:

* **KnowledgeUnit** — abstract base with shared fields and methods
  * **PromptUnit** — system documentation (JSON-backed, ``knowledge/store/``)
    * DirectionsUnit, CapabilityUnit, WorkflowUnit
    * SystemUnit — coherent functional domain whose persistent state work-buddy owns
    * ServiceUnit — internal work-buddy component with a network surface
    * IntegrationUnit — connection to an external system
    * ReferenceUnit — Python module API surface documentation
    * ConceptUnit — architectural narrative or design prose
  * **VaultUnit** — personal knowledge (markdown-backed, Obsidian vault)

The DAG structure (parents/children) enables hierarchical navigation. Multi-parent
is supported and intended: a subsystem may live at one path for navigation and
declare additional parent systems via ``parents``.

Cross-unit content reuse goes through inline ``<<wb:path>>`` placeholders
in ``content["full"]`` (see ``_resolve_placeholders``). The earlier
``context_before`` / ``context_after`` chain mechanism was retired in
favour of placeholders, which give authors per-reference control over
recursion. Loaders silently ignore stale ``context_before`` /
``context_after`` keys in store JSON; the resolver no longer reads them.
"""

from __future__ import annotations

import argparse
import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Inline placeholder resolution — <<wb:path --flags>>
# ---------------------------------------------------------------------------

_PLACEHOLDER_RE = re.compile(r'<<wb:(.*?)>>')
"""Match ``<<wb:...>>`` placeholders in content strings.

Uses non-greedy ``.*?`` so multiple placeholders on the same line are
matched individually rather than swallowed into one span.
"""


# Recursion-mode contract surfaced through ``agent_docs``:
#
# * ``"default"`` — each placeholder honours its own ``--recursive`` flag.
#   This is the historical, author-controlled behaviour.
# * ``"all"`` — every placeholder expands transitively, regardless of the
#   per-placeholder flag. Bounded by ``_RECURSIVE_ALL_SIZE_CAP`` so a
#   pathological chain can't balloon the response.
# * ``"none"`` — every placeholder is preserved literally (``<<wb:...>>``
#   text). Useful when an authoring agent wants to read or edit a unit
#   without inlined foundations interfering.
_RECURSIVE_MODES = ("default", "all", "none")

_RECURSIVE_ALL_SIZE_CAP = 100_000  # bytes of expanded output before truncation
_TRUNCATION_MARKER = (
    f"<!-- wb: placeholder expansion truncated at "
    f"{_RECURSIVE_ALL_SIZE_CAP // 1000}KB cap -->"
)


def _new_budget(recursive_mode: str) -> list[int] | None:
    """Return a fresh mutable budget counter for top-level expansion.

    A list-of-one int is used so the same counter can be shared (and
    decremented) across the recursive call tree without threading a
    return value through every helper.

    Only ``"all"`` mode is bounded — the historical ``"default"`` mode is
    unchanged, and ``"none"`` never recurses so a cap is moot.
    """
    if recursive_mode == "all":
        return [_RECURSIVE_ALL_SIZE_CAP]
    return None


class _PlaceholderParser(argparse.ArgumentParser):
    """Argparse parser that raises instead of printing/exiting on error."""

    def error(self, message: str) -> None:  # type: ignore[override]
        raise ValueError(message)


def _build_placeholder_parser() -> _PlaceholderParser:
    """Build an argparse parser for placeholder flags.

    Positional: unit path (required).
    Flags: --recursive (opt-in transitive resolution).
    Extensible: --depth, --section, etc. can be added later.
    """
    parser = _PlaceholderParser(prog="wb-placeholder", add_help=False)
    parser.add_argument("path", type=str)
    parser.add_argument("--recursive", action="store_true", default=False)
    return parser


def _resolve_placeholders(
    text: str,
    store: dict[str, KnowledgeUnit],
    *,
    recursive_mode: str = "default",
    _budget: list[int] | None = None,
) -> str:
    """Replace ``<<wb:path>>`` / ``<<wb:path --recursive>>`` in *text*.

    - ``recursive_mode="default"`` (historical behaviour): each placeholder
      honours its own ``--recursive`` flag. Plain inserts raw
      ``content["full"]``; flagged calls ``_resolve_full_content()`` on
      the referenced unit, which transitively resolves *its* placeholders.
    - ``recursive_mode="all"``: every placeholder expands transitively,
      regardless of per-placeholder flag. Bounded by the shared
      ``_budget`` counter.
    - ``recursive_mode="none"``: every placeholder is left literal
      (``<<wb:...>>``) — useful for editing/inspection where embedded
      foundations would obscure the unit's own prose.
    - Missing refs: placeholder replaced with an HTML comment (preserved
      in all recursion modes).
    - Cycles are prevented at load time by ``validate_dag()``, so no
      runtime tracking is needed.
    """
    if "<<wb:" not in text:
        return text

    if recursive_mode == "none":
        # Leave every placeholder literal. Saves the argparse pass.
        return text

    def _replace(match: re.Match) -> str:
        inner = match.group(1).strip()

        # Parse with argparse
        parser = _build_placeholder_parser()
        try:
            args, _unknown = parser.parse_known_args(inner.split())
        except (ValueError, SystemExit, Exception):
            # Malformed placeholder — treat entire inner text as path, no flags
            args = argparse.Namespace(path=inner.split()[0] if inner else "", recursive=False)

        path = args.path
        if not path:
            return match.group(0)  # leave as-is

        ref = store.get(path)
        if ref is None:
            return f"<!-- wb: {path} not found -->"

        # Budget gate (only meaningful in "all" mode where _budget is set):
        # if the caller has already emitted ~100KB of expanded content,
        # stop further expansions cold so the response can't balloon.
        if _budget is not None and _budget[0] <= 0:
            return _TRUNCATION_MARKER

        # Decide whether to recurse on this particular ref.
        expand_recursively = (
            recursive_mode == "all"
            or (recursive_mode == "default" and args.recursive)
        )

        # Pre-charge the budget against the raw body we're about to
        # inline. Doing this BEFORE descent means deeper recursive
        # levels see the depleted budget and short-circuit cleanly.
        # If we deferred the charge until after the recursive call,
        # the outermost frame would always see the full budget — the
        # cap would never fire on deep chains.
        raw = ref.content.get("full", ref.content.get("summary", ""))
        if _budget is not None:
            _budget[0] -= len(raw)

        if expand_recursively:
            content = ref._resolve_full_content(
                store,
                recursive_mode=recursive_mode,
                _budget=_budget,
            )
        else:
            content = raw

        if not content:
            return f"<!-- wb: {path} empty -->"

        return f"--- context from: {path} ---\n{content}"

    return _PLACEHOLDER_RE.sub(_replace, text)


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

@dataclass
class KnowledgeUnit:
    """Base for all units in the knowledge system (system docs + personal)."""

    path: str                          # unique ID: "tasks/triage", "personal/metacognition/branch-explosion"
    kind: str                          # "directions" | "system" | "capability" | "workflow" | "personal"
    name: str                          # human label
    description: str                   # primary one-line summary
    aliases: list[str] = field(default_factory=list)   # alternative phrasings for search
    tags: list[str] = field(default_factory=list)       # search keywords
    content: dict[str, str] = field(default_factory=dict)  # {"summary": "...", "full": "..."}
    requires: list[str] = field(default_factory=list)   # tool/service dependencies

    # DAG structure — multiple parents and children, no cycles
    parents: list[str] = field(default_factory=list)
    children: list[str] = field(default_factory=list)

    # Dev notes — development-facing documentation that only surfaces when
    # the agent is in dev mode or explicitly requests dev=True.
    dev_notes: str = ""

    # Scope — populated by the loader, not serialized to JSON.
    scope: str = "system"              # "system" | "personal"

    def tier(
        self,
        depth: str = "summary",
        store: dict[str, KnowledgeUnit] | None = None,
        dev: bool = False,
        *,
        recursive_mode: str = "default",
    ) -> dict[str, Any]:
        """Return unit data at the requested depth.

        - "index": name + description + kind + children (for navigation)
        - "summary": above + content["summary"] + kind-specific fields
        - "full": above + content["full"] + all fields, with placeholder resolution

        Args:
            depth: One of "index", "summary", "full".
            store: Unit store dict for resolving ``<<wb:path>>`` placeholders
                   at ``depth="full"``. When None, content is returned with
                   placeholders unresolved (used by the bare full-index path).
            dev: When True, include dev_notes in full-depth output.
            recursive_mode: Placeholder recursion control at ``depth="full"``.
                One of ``"default"`` (per-flag), ``"all"`` (force transitive
                expansion, capped), or ``"none"`` (preserve literal markup).
                See ``_RECURSIVE_MODES`` for the contract. Ignored at
                ``depth="index"`` / ``"summary"`` and when ``store is None``.
        """
        base: dict[str, Any] = {
            "path": self.path,
            "kind": self.kind,
            "name": self.name,
            "description": self.description,
            "children": self.children,
            "parents": self.parents,
            "scope": self.scope,
        }

        if depth == "index":
            base["tags"] = self.tags
            return base

        # summary and full both include kind-specific fields
        base["tags"] = self.tags
        base["aliases"] = self.aliases
        base["requires"] = self.requires
        base.update(self._kind_fields())

        if depth == "summary":
            base["content"] = self.content.get("summary", "")
            if self.dev_notes:
                base["has_dev_notes"] = True
        else:  # full
            base["content"] = self._resolve_full_content(
                store,
                recursive_mode=recursive_mode,
            )
            if dev and self.dev_notes:
                base["dev_notes"] = self.dev_notes

        return base

    def _resolve_full_content(
        self,
        store: dict[str, KnowledgeUnit] | None,
        *,
        recursive_mode: str = "default",
        _budget: list[int] | None = None,
    ) -> str:
        """Return ``content["full"]`` with inline placeholders resolved.

        When *store* is None, returns the unit's own content unresolved
        (used by the bare full-index path which doesn't have a store
        handle).

        ``recursive_mode`` controls placeholder expansion — see
        ``_RECURSIVE_MODES`` and ``_resolve_placeholders``. ``_budget`` is
        a shared counter for the ``"all"`` mode size cap; the top-level
        caller passes ``None`` and we lazily allocate one so every
        recursive descendant debits the same budget.

        Cycles are prevented at load time by ``validate_dag()`` — no
        runtime tracking is needed here.

        The retired ``context_before`` / ``context_after`` mechanism
        used to splice referenced units' content as ``--- context
        from: X ---`` blocks before and after this body; placeholders
        replaced it.
        """
        own = self.content.get("full", self.content.get("summary", ""))

        if store is None:
            return own

        # Lazy-init the shared budget on the top-level call. Recursive
        # descendants inherit the existing counter so the cap applies
        # across the whole expansion tree, not per-level.
        if _budget is None:
            _budget = _new_budget(recursive_mode)

        return _resolve_placeholders(
            own,
            store,
            recursive_mode=recursive_mode,
            _budget=_budget,
        )

    def _kind_fields(self) -> dict[str, Any]:
        """Override in subclasses to add kind-specific fields to tier output."""
        return {}

    def search_phrases(self) -> list[str]:
        """Build search candidate texts from multiple framings."""
        phrases = [
            self.name.replace("-", " ").replace("_", " "),
            self.description,
            *self.aliases,
            " ".join(self.tags),
            f"{self.name} {self.description}",
        ]
        return [p for p in phrases if p]

    def to_dict(self) -> dict[str, Any]:
        """Serialize to JSON-compatible dict."""
        d: dict[str, Any] = {
            "kind": self.kind,
            "name": self.name,
            "description": self.description,
        }
        # Only include non-empty optional fields
        if self.aliases:
            d["aliases"] = self.aliases
        if self.tags:
            d["tags"] = self.tags
        if self.content:
            d["content"] = self.content
        if self.requires:
            d["requires"] = self.requires
        if self.parents:
            d["parents"] = self.parents
        if self.children:
            d["children"] = self.children
        if self.dev_notes:
            d["dev_notes"] = self.dev_notes
        # Add kind-specific fields
        d.update(self._kind_dict())
        return d

    def _kind_dict(self) -> dict[str, Any]:
        """Override in subclasses to add kind-specific fields to serialization."""
        return {}


# ---------------------------------------------------------------------------
# System knowledge — JSON-backed (PromptUnit hierarchy)
# ---------------------------------------------------------------------------

@dataclass
class PromptUnit(KnowledgeUnit):
    """System knowledge unit (JSON-backed). Abstract type boundary.

    All system documentation units inherit from this. The class itself adds
    no fields — it serves as the type boundary between system and personal
    knowledge in isinstance checks and store scoping.

    PromptUnit also serves as a last-resort fallback container when
    ``unit_from_dict`` encounters an on-disk ``kind`` that doesn't match
    any typed subclass. That path emits a warning and is not the intended
    home for new kinds: introduce a typed subclass and register it in
    ``_KIND_MAP`` whenever a new ``kind`` is added.
    """
    pass


# ---------------------------------------------------------------------------
# Directions — behavioral "how to do X"
# ---------------------------------------------------------------------------

@dataclass
class DirectionsUnit(PromptUnit):
    """Behavioral directions — migrated from slash commands and workflow reasoning steps."""

    kind: str = field(default="directions", init=False)
    trigger: str = ""                  # when to use: "user wants to triage tasks"
    command: str | None = None         # slash command: "wb-task-triage"
    workflow: str | None = None        # linked workflow path: "tasks/task-triage"
    capabilities: list[str] = field(default_factory=list)  # MCP capability paths used

    def _kind_fields(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        if self.trigger:
            d["trigger"] = self.trigger
        if self.command:
            d["command"] = self.command
        if self.workflow:
            d["workflow"] = self.workflow
        if self.capabilities:
            d["capabilities"] = self.capabilities
        return d

    _kind_dict = _kind_fields  # same for serialization

    def search_phrases(self) -> list[str]:
        phrases = super().search_phrases()
        if self.trigger:
            phrases.append(self.trigger)
        return phrases


# ---------------------------------------------------------------------------
# System — reference docs, "what is X"
# ---------------------------------------------------------------------------

@dataclass
class SystemUnit(PromptUnit):
    """System — coherent functional domain whose persistent state work-buddy owns.

    A ``SystemUnit`` is a domain anchor (e.g., ``tasks``, ``triage``, ``inline``)
    whose operational details — capabilities, schemas, lifecycle — live on its
    children. The unit itself is prose-first; structured fields (ports,
    entry_points) belong on more specific kinds (``ServiceUnit``,
    ``IntegrationUnit``, ``ReferenceUnit``).

    Memory ownership is the strongest disambiguator: a ``system`` is a domain
    work-buddy persists state for. Domains whose state lives outside (Obsidian
    vault, Thunderbird, etc.) belong to ``IntegrationUnit`` instead.
    """

    kind: str = field(default="system", init=False)


# ---------------------------------------------------------------------------
# Service — internal work-buddy component with network surface
# ---------------------------------------------------------------------------

@dataclass
class ServiceUnit(PromptUnit):
    """Service — internal work-buddy component listening on a port (sidecar-managed).

    The dashboard, messaging service, embedding service, and MCP gateway are
    all ``ServiceUnit``s. ``IntegrationUnit`` covers the *external* counterpart
    (a service work-buddy talks to but doesn't run).
    """

    kind: str = field(default="service", init=False)
    ports: list[int] = field(default_factory=list)
    health_url: str = ""                               # e.g. "/health"
    entry_points: list[str] = field(default_factory=list)

    def _kind_fields(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        if self.ports:
            d["ports"] = self.ports
        if self.health_url:
            d["health_url"] = self.health_url
        if self.entry_points:
            d["entry_points"] = self.entry_points
        return d

    _kind_dict = _kind_fields


# ---------------------------------------------------------------------------
# Integration — connection to an external system
# ---------------------------------------------------------------------------

@dataclass
class IntegrationUnit(PromptUnit):
    """Integration — connection to an external system whose state lives elsewhere.

    Examples: Obsidian (vault state lives in the user's vault), Thunderbird,
    Tailscale, LM Studio. Integrations may run an internal bridge (a Flask
    app, a CLI wrapper) — the bridge's port belongs here, but the integration's
    identity is the external dependency, not the bridge mechanism.
    """

    kind: str = field(default="integration", init=False)
    external_system: str = ""                          # "Obsidian" / "Thunderbird" / ...
    bridge_module: str = ""                            # Python module wrapping the integration
    ports: list[int] = field(default_factory=list)     # bridge ports (if any)
    entry_points: list[str] = field(default_factory=list)

    def _kind_fields(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        if self.external_system:
            d["external_system"] = self.external_system
        if self.bridge_module:
            d["bridge_module"] = self.bridge_module
        if self.ports:
            d["ports"] = self.ports
        if self.entry_points:
            d["entry_points"] = self.entry_points
        return d

    _kind_dict = _kind_fields


# ---------------------------------------------------------------------------
# Reference — Python module API surface documentation
# ---------------------------------------------------------------------------

@dataclass
class ReferenceUnit(PromptUnit):
    """Reference — documents the API surface of one or more Python modules.

    Anchored on ``entry_points``: fully-qualified Python identifiers
    (functions, classes, constants) that constitute the module's public
    surface. Reference units are entry-points-led; their content describes
    what those entry points do, not architectural narrative about the
    surrounding subsystem.
    """

    kind: str = field(default="reference", init=False)
    entry_points: list[str] = field(default_factory=list)

    def _kind_fields(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        if self.entry_points:
            d["entry_points"] = self.entry_points
        return d

    _kind_dict = _kind_fields


# ---------------------------------------------------------------------------
# Concept — architectural narrative or design prose
# ---------------------------------------------------------------------------

@dataclass
class ConceptUnit(PromptUnit):
    """Concept — architectural narrative, design philosophy, or domain heading.

    Concepts are prose-first with no structured fields beyond the base. Use
    when the unit explains *how* or *why* something is the way it is, or when
    it serves as a navigational heading for a category of related docs
    (``architecture``, ``dev``, ``metacognition``).
    """

    kind: str = field(default="concept", init=False)


# ---------------------------------------------------------------------------
# Capability — MCP callable metadata
# ---------------------------------------------------------------------------

@dataclass
class CapabilityUnit(PromptUnit):
    """MCP capability — callable function metadata from the registry."""

    kind: str = field(default="capability", init=False)
    capability_name: str = ""          # MCP name: "task_create"
    category: str = ""                 # registry category: "tasks"
    parameters: dict[str, Any] = field(default_factory=dict)  # param schema
    mutates_state: bool = False
    retry_policy: str = "manual"
    consent_required: bool = False

    def _kind_fields(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "capability_name": self.capability_name,
            "category": self.category,
        }
        if self.parameters:
            d["parameters"] = self.parameters
        if self.mutates_state:
            d["mutates_state"] = True
            d["retry_policy"] = self.retry_policy
        if self.consent_required:
            d["consent_required"] = True
        return d

    _kind_dict = _kind_fields


# ---------------------------------------------------------------------------
# Workflow — multi-step DAG structure
# ---------------------------------------------------------------------------

@dataclass
class WorkflowUnit(PromptUnit):
    """Workflow — DAG structure and execution policy."""

    kind: str = field(default="workflow", init=False)
    workflow_name: str = ""            # "task-triage", "morning-routine"
    execution: str = "main"            # "main" | "subagent"
    allow_override: bool = True
    steps: list[dict[str, Any]] = field(default_factory=list)  # DAG nodes
    step_instructions: dict[str, str] = field(default_factory=dict)  # {step_id: text}
    command: str | None = None         # slash command: "wb-task-triage"
    # Optional caller-provided initial params schema; mirrors
    # ``Capability.parameters`` shape ``{name: {type, description, required}}``.
    # Workflows that omit this field reject any non-empty params at start.
    params_schema: dict[str, dict[str, Any]] = field(default_factory=dict)

    def _kind_fields(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "workflow_name": self.workflow_name,
            "execution": self.execution,
        }
        if not self.allow_override:
            d["allow_override"] = False
        if self.steps:
            d["steps"] = self.steps
        if self.step_instructions:
            d["step_instructions"] = self.step_instructions
        if self.command:
            d["command"] = self.command
        if self.params_schema:
            d["params_schema"] = self.params_schema
        return d

    _kind_dict = _kind_fields


# ---------------------------------------------------------------------------
# Personal knowledge — vault-backed (VaultUnit)
# ---------------------------------------------------------------------------

@dataclass
class VaultUnit(KnowledgeUnit):
    """Personal knowledge unit (markdown-backed, lives in Obsidian vault).

    Created by the minting workflow or manually by the user. Loaded from
    markdown files with YAML frontmatter by the vault adapter.
    """

    kind: str = field(default="personal", init=False)
    scope: str = field(default="personal", init=False)

    category: str = ""                 # work_pattern | self_regulation | skill_gap | feedback | preference | reference
    severity: str = ""                 # HIGH | MODERATE | LOW (optional, category-dependent)
    last_observed: str = ""            # ISO date of most recent evidence
    observation_count: int = 0         # how many times this has been observed
    source_file: str = ""              # vault-relative path to the .md file

    def _kind_fields(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        if self.category:
            d["category"] = self.category
        if self.severity:
            d["severity"] = self.severity
        if self.last_observed:
            d["last_observed"] = self.last_observed
        if self.observation_count:
            d["observation_count"] = self.observation_count
        if self.source_file:
            d["source_file"] = self.source_file
        return d

    _kind_dict = _kind_fields

    def search_phrases(self) -> list[str]:
        phrases = super().search_phrases()
        if self.category:
            phrases.append(self.category.replace("_", " "))
        if self.severity:
            phrases.append(self.severity.lower())
        return phrases


# ---------------------------------------------------------------------------
# Deserialization
# ---------------------------------------------------------------------------

_KIND_MAP: dict[str, type[KnowledgeUnit]] = {
    "directions": DirectionsUnit,
    "system": SystemUnit,
    "service": ServiceUnit,
    "integration": IntegrationUnit,
    "reference": ReferenceUnit,
    "concept": ConceptUnit,
    "capability": CapabilityUnit,
    "workflow": WorkflowUnit,
    "personal": VaultUnit,
}


def unit_from_dict(path: str, data: dict[str, Any]) -> KnowledgeUnit:
    """Deserialize a JSON dict into the appropriate KnowledgeUnit subclass."""
    kind = data.get("kind", "system")
    cls = _KIND_MAP.get(kind, PromptUnit)

    # Extract base fields (shared by all unit types).
    # ``context_before`` / ``context_after`` were retired; the loader
    # silently drops them if a JSON file still has the keys so we
    # don't have to back-fill every legacy store file in one pass.
    base_kwargs: dict[str, Any] = {
        "path": path,
        "name": data.get("name", path.rsplit("/", 1)[-1]),
        "description": data.get("description", ""),
        "aliases": data.get("aliases", []),
        "tags": data.get("tags", []),
        "content": data.get("content", {}),
        "requires": data.get("requires", []),
        "parents": data.get("parents", []),
        "children": data.get("children", []),
        "dev_notes": data.get("dev_notes", ""),
    }

    # Extract kind-specific fields based on class
    if cls is DirectionsUnit:
        base_kwargs["trigger"] = data.get("trigger", "")
        base_kwargs["command"] = data.get("command")
        base_kwargs["workflow"] = data.get("workflow")
        base_kwargs["capabilities"] = data.get("capabilities", [])
    elif cls is SystemUnit or cls is ConceptUnit:
        # Both are prose-first with no kind-specific fields beyond the base.
        pass
    elif cls is ServiceUnit:
        base_kwargs["ports"] = data.get("ports", [])
        base_kwargs["health_url"] = data.get("health_url", "")
        base_kwargs["entry_points"] = data.get("entry_points", [])
    elif cls is IntegrationUnit:
        base_kwargs["external_system"] = data.get("external_system", "")
        base_kwargs["bridge_module"] = data.get("bridge_module", "")
        base_kwargs["ports"] = data.get("ports", [])
        base_kwargs["entry_points"] = data.get("entry_points", [])
    elif cls is ReferenceUnit:
        base_kwargs["entry_points"] = data.get("entry_points", [])
    elif cls is CapabilityUnit:
        base_kwargs["capability_name"] = data.get("capability_name", "")
        base_kwargs["category"] = data.get("category", "")
        base_kwargs["parameters"] = data.get("parameters", {})
        base_kwargs["mutates_state"] = data.get("mutates_state", False)
        base_kwargs["retry_policy"] = data.get("retry_policy", "manual")
        base_kwargs["consent_required"] = data.get("consent_required", False)
    elif cls is WorkflowUnit:
        base_kwargs["workflow_name"] = data.get("workflow_name", "")
        base_kwargs["execution"] = data.get("execution", "main")
        base_kwargs["allow_override"] = data.get("allow_override", True)
        base_kwargs["steps"] = data.get("steps", [])
        base_kwargs["step_instructions"] = data.get("step_instructions", {})
        base_kwargs["command"] = data.get("command")
        base_kwargs["params_schema"] = data.get("params_schema", {})
    elif cls is VaultUnit:
        base_kwargs["category"] = data.get("category", "")
        base_kwargs["severity"] = data.get("severity", "")
        base_kwargs["last_observed"] = data.get("last_observed", "")
        base_kwargs["observation_count"] = data.get("observation_count", 0)
        base_kwargs["source_file"] = data.get("source_file", "")
    elif cls is PromptUnit:
        # Last-resort fallback: the JSON's ``kind`` didn't match any typed
        # subclass. Surface this loudly so the next ad-hoc kind doesn't
        # silently break downstream consumers (e.g., docs_gen renderers).
        # The fix is always to introduce a typed subclass and register it
        # in ``_KIND_MAP``; this fallback only prevents load-time crashes.
        logger.warning(
            "Unknown unit kind %r at %s — falling back to bare PromptUnit. "
            "Add a typed subclass to _KIND_MAP if this kind is intentional.",
            kind, path,
        )
        base_kwargs["kind"] = kind

    return cls(**base_kwargs)


def _extract_placeholder_refs(content: dict[str, str]) -> list[str]:
    """Extract unit paths referenced by ``<<wb:...>>`` placeholders in content."""
    full = content.get("full", "")
    if "<<wb:" not in full:
        return []
    return [m.split()[0] for m in _PLACEHOLDER_RE.findall(full) if m.strip()]


def validate_dag(units: dict[str, KnowledgeUnit]) -> list[str]:
    """Check for cycles across all reference types. Returns list of errors.

    Builds a networkx DiGraph with edges from:
    - parent → child relationships
    - ``<<wb:path>>`` inline placeholder references in content

    Also warns (non-fatal) about broken references.

    Degrades gracefully when networkx is unavailable (e.g. minimal CI
    installs): skips cycle detection and returns an empty error list
    with a logged warning, rather than crashing the store load.
    """
    try:
        import networkx as nx
    except ImportError:
        import logging
        logging.getLogger(__name__).warning(
            "networkx not installed — skipping DAG cycle detection. "
            "Install networkx (pip install networkx) for full validation."
        )
        return []

    errors: list[str] = []
    g = nx.DiGraph()

    # Add all units as nodes
    for path in units:
        g.add_node(path)

    for path, unit in units.items():
        # Parent → child edges
        for child in unit.children:
            if child not in units:
                errors.append(f"{path}: child '{child}' not found in store")
            else:
                g.add_edge(path, child)

        for parent in unit.parents:
            if parent not in units:
                errors.append(f"{path}: parent '{parent}' not found in store")
            # parent→child edges are already added from the parent side

        # Inline placeholder edges
        for ref in _extract_placeholder_refs(unit.content):
            if ref in units:
                g.add_edge(path, ref)
            # Don't warn about missing placeholder refs here — they're
            # reported at resolution time with HTML comments.

    # Check for cycles
    if not nx.is_directed_acyclic_graph(g):
        try:
            cycle = nx.find_cycle(g, orientation="original")
            cycle_str = " → ".join(f"{u}" for u, v, _ in cycle)
            errors.append(f"Cycle detected: {cycle_str}")
        except nx.NetworkXNoCycle:
            pass  # race — DAG became acyclic between check and find

    return errors
