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

from dataclasses import dataclass, field
from typing import Any


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

    # Scope — populated by the loader, not serialized to JSON.
    scope: str = "system"              # "system" | "personal"

    def tier(
        self,
        depth: str = "summary",
        store: dict[str, KnowledgeUnit] | None = None,
    ) -> dict[str, Any]:
        """Return unit data at the requested depth.

        - "index": name + description + kind + children (for navigation)
        - "summary": above + content["summary"] + kind-specific fields
        - "full": above + content["full"] + all fields, with chain resolution

        Args:
            depth: One of "index", "summary", "full".
            store: Unit store dict for resolving context chains at depth="full".
                   When None, chains are returned as path lists without resolution.
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
        else:  # full
            base["content"] = self._resolve_full_content(store)

        return base

    def _resolve_full_content(
        self,
        store: dict[str, KnowledgeUnit] | None,
    ) -> str:
        """Build full content with context chain resolution.

        When *store* is provided, referenced units' ``content["full"]`` is
        prepended/appended with separators.  When *store* is None, returns
        the unit's own content only (chains listed as metadata in tier output).
        """
        own = self.content.get("full", self.content.get("summary", ""))

        if store is None or (not self.context_before and not self.context_after):
            return own

        parts: list[str] = []

        # Prepend context_before
        for ref_path in self.context_before:
            ref = store.get(ref_path)
            if ref is not None:
                ref_content = ref.content.get("full", ref.content.get("summary", ""))
                if ref_content:
                    parts.append(f"--- context from: {ref_path} ---\n{ref_content}")

        # Own content
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


def validate_dag(units: dict[str, KnowledgeUnit]) -> list[str]:
    """Check for cycles in the parent/child DAG. Returns list of errors.

    Also warns (non-fatal) about broken context chain references.
    """
    errors: list[str] = []

    # Check referential integrity — parents/children
    for path, unit in units.items():
        for parent in unit.parents:
            if parent not in units:
                errors.append(f"{path}: parent '{parent}' not found in store")
        for child in unit.children:
            if child not in units:
                errors.append(f"{path}: child '{child}' not found in store")

    # Warn about broken context chains (non-fatal — cross-scope refs are expected)
    for path, unit in units.items():
        for ref in unit.context_before:
            if ref not in units:
                errors.append(f"{path}: context_before '{ref}' not found (may be cross-scope)")
        for ref in unit.context_after:
            if ref not in units:
                errors.append(f"{path}: context_after '{ref}' not found (may be cross-scope)")

    # Check for cycles via DFS
    visited: set[str] = set()
    in_stack: set[str] = set()

    def _dfs(node: str) -> bool:
        if node in in_stack:
            return True  # cycle
        if node in visited:
            return False
        visited.add(node)
        in_stack.add(node)
        unit = units.get(node)
        if unit:
            for child in unit.children:
                if _dfs(child):
                    errors.append(f"Cycle detected involving: {node} → {child}")
                    return True
        in_stack.discard(node)
        return False

    for path in units:
        if path not in visited:
            _dfs(path)

    return errors
