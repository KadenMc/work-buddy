"""Knowledge unit type hierarchy for the unified knowledge store.

Two parallel hierarchies share a common base:

* **KnowledgeUnit** — abstract base with shared fields and methods
  * **PromptUnit** — system documentation (JSON-backed, ``knowledge/store/``)
    * DirectionsUnit, SystemUnit, CapabilityUnit, WorkflowUnit
  * **VaultUnit** — personal knowledge (markdown-backed, Obsidian vault)

The DAG structure (parents/children) enables hierarchical navigation.
Context chaining (context_before/context_after) enables automatic content
inclusion without duplication — use sparingly for genuine shared foundations.
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Inline placeholder resolution — <<wb:path --flags>>
# ---------------------------------------------------------------------------

_PLACEHOLDER_RE = re.compile(r'<<wb:(.*?)>>')
"""Match ``<<wb:...>>`` placeholders in content strings.

Uses non-greedy ``.*?`` so multiple placeholders on the same line are
matched individually rather than swallowed into one span.
"""


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
) -> str:
    """Replace ``<<wb:path>>`` / ``<<wb:path --recursive>>`` in *text*.

    - Default: inserts the referenced unit's raw ``content["full"]``.
    - ``--recursive``: calls ``_resolve_full_content()`` on the referenced
      unit, which transitively resolves its own placeholders and context
      chains.
    - Missing refs: placeholder replaced with an HTML comment.
    - Cycles are prevented at load time by ``validate_dag()``, so no
      runtime tracking is needed.
    """
    if "<<wb:" not in text:
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

        if args.recursive:
            content = ref._resolve_full_content(store)
        else:
            content = ref.content.get("full", ref.content.get("summary", ""))

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

    # Context chaining — automatic content inclusion at depth="full".
    # Referenced units' content is prepended (before) or appended (after).
    # Non-recursive: chains do not transitively resolve their own chains.
    # Use sparingly for genuine shared foundations, not loose associations.
    context_before: list[str] = field(default_factory=list)
    context_after: list[str] = field(default_factory=list)

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
    ) -> dict[str, Any]:
        """Return unit data at the requested depth.

        - "index": name + description + kind + children (for navigation)
        - "summary": above + content["summary"] + kind-specific fields
        - "full": above + content["full"] + all fields, with chain resolution

        Args:
            depth: One of "index", "summary", "full".
            store: Unit store dict for resolving context chains at depth="full".
                   When None, chains are returned as path lists without resolution.
            dev: When True, include dev_notes in full-depth output.
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
            if self.context_before:
                base["context_before"] = self.context_before
            if self.context_after:
                base["context_after"] = self.context_after
            return base

        # summary and full both include kind-specific fields
        base["tags"] = self.tags
        base["aliases"] = self.aliases
        base["requires"] = self.requires
        if self.context_before:
            base["context_before"] = self.context_before
        if self.context_after:
            base["context_after"] = self.context_after
        base.update(self._kind_fields())

        if depth == "summary":
            base["content"] = self.content.get("summary", "")
            if self.dev_notes:
                base["has_dev_notes"] = True
        else:  # full
            base["content"] = self._resolve_full_content(store)
            if dev and self.dev_notes:
                base["dev_notes"] = self.dev_notes

        return base

    def _resolve_full_content(
        self,
        store: dict[str, KnowledgeUnit] | None,
    ) -> str:
        """Build full content with context chain + placeholder resolution.

        Resolution order:
        1. Prepend ``context_before`` references (non-recursive).
        2. Own content with ``<<wb:...>>`` placeholders resolved inline.
        3. Append ``context_after`` references (non-recursive).

        When *store* is None, returns the unit's own content only (chains
        and placeholders listed as metadata in tier output, not resolved).

        Cycles are prevented at load time by ``validate_dag()`` — no
        runtime tracking is needed here.
        """
        own = self.content.get("full", self.content.get("summary", ""))

        if store is None:
            return own

        # Resolve inline placeholders in own content
        own = _resolve_placeholders(own, store)

        if not self.context_before and not self.context_after:
            return own

        parts: list[str] = []

        # Prepend context_before
        for ref_path in self.context_before:
            ref = store.get(ref_path)
            if ref is not None:
                ref_content = ref.content.get("full", ref.content.get("summary", ""))
                if ref_content:
                    parts.append(f"--- context from: {ref_path} ---\n{ref_content}")

        # Own content (placeholders already resolved)
        parts.append(own)

        # Append context_after
        for ref_path in self.context_after:
            ref = store.get(ref_path)
            if ref is not None:
                ref_content = ref.content.get("full", ref.content.get("summary", ""))
                if ref_content:
                    parts.append(f"--- context from: {ref_path} ---\n{ref_content}")

        return "\n\n".join(parts)

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
        if self.context_before:
            d["context_before"] = self.context_before
        if self.context_after:
            d["context_after"] = self.context_after
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
    """System knowledge unit (JSON-backed). Exists for type discrimination.

    All system documentation units inherit from this. The class itself adds
    no fields — it serves as the type boundary between system and personal
    knowledge in isinstance checks and store scoping.
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
    """System documentation — architecture, integration guides, reference."""

    kind: str = field(default="system", init=False)
    ports: list[int] = field(default_factory=list)          # service ports
    entry_points: list[str] = field(default_factory=list)   # key Python modules

    def _kind_fields(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        if self.ports:
            d["ports"] = self.ports
        if self.entry_points:
            d["entry_points"] = self.entry_points
        return d

    _kind_dict = _kind_fields


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
    "capability": CapabilityUnit,
    "workflow": WorkflowUnit,
    "personal": VaultUnit,
}


def unit_from_dict(path: str, data: dict[str, Any]) -> KnowledgeUnit:
    """Deserialize a JSON dict into the appropriate KnowledgeUnit subclass."""
    kind = data.get("kind", "system")
    cls = _KIND_MAP.get(kind, PromptUnit)

    # Extract base fields (shared by all unit types)
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
        "context_before": data.get("context_before", []),
        "context_after": data.get("context_after", []),
        "dev_notes": data.get("dev_notes", ""),
    }

    # Extract kind-specific fields based on class
    if cls is DirectionsUnit:
        base_kwargs["trigger"] = data.get("trigger", "")
        base_kwargs["command"] = data.get("command")
        base_kwargs["workflow"] = data.get("workflow")
        base_kwargs["capabilities"] = data.get("capabilities", [])
    elif cls is SystemUnit:
        base_kwargs["ports"] = data.get("ports", [])
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
    elif cls is VaultUnit:
        base_kwargs["category"] = data.get("category", "")
        base_kwargs["severity"] = data.get("severity", "")
        base_kwargs["last_observed"] = data.get("last_observed", "")
        base_kwargs["observation_count"] = data.get("observation_count", 0)
        base_kwargs["source_file"] = data.get("source_file", "")

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
    - context_before / context_after references
    - ``<<wb:path>>`` inline placeholder references in content

    Also warns (non-fatal) about broken references.
    """
    import networkx as nx

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

        # Context chain edges
        for ref in unit.context_before:
            if ref not in units:
                errors.append(f"{path}: context_before '{ref}' not found (may be cross-scope)")
            else:
                g.add_edge(path, ref)

        for ref in unit.context_after:
            if ref not in units:
                errors.append(f"{path}: context_after '{ref}' not found (may be cross-scope)")
            else:
                g.add_edge(path, ref)

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
