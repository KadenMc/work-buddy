"""Capability registry — discovers and indexes work-buddy functions and workflows.

The registry is built once at first access by scanning Python modules and
loading workflow definitions from the knowledge store. It powers the
``wb_search`` gateway tool.

Workflow DAG structure, step instructions, auto_run specs, and execution
policy are stored in ``knowledge/store/workflows.json`` as WorkflowUnit
entries. The conductor reads these at runtime via
``_discover_workflows_from_store()``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from work_buddy.frontmatter import parse_frontmatter

# v5 Stage 1.5: capability/workflow definitions get four new fields
# (is_action, available_in, intrinsic_amplifiers,
# parameter_schema_for_action, requires_post_review). The
# InvocationContext enum lives in work_buddy.threads.enums (a
# pure-data module with no other work_buddy deps, so this import is
# cycle-safe).
from work_buddy.threads.enums import InvocationContext

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).parent.parent.parent
_SLASH_CMD_DIR = _REPO_ROOT / ".claude" / "commands"


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class Capability:
    """A simple callable function exposed through the gateway."""

    name: str
    description: str
    category: str  # messaging, contracts, status, journal, memory, tasks, context
    parameters: dict[str, dict[str, Any]]  # {name: {type, description, required}}
    callable: Callable
    search_aliases: list[str] = field(default_factory=list)  # extra phrases for search scoring
    param_aliases: dict[str, str] = field(default_factory=dict)  # {alias: canonical} e.g. {"target_date": "target"}
    requires: list[str] = field(default_factory=list)  # tool/component IDs, e.g. ["obsidian", "hindsight"]
    # Names of other capabilities this capability calls directly. Used by
    # the control graph to resolve transitive component dependencies
    # (e.g. a workflow step invokes `task_toggle` which requires `obsidian`,
    # so the step and workflow inherit the `obsidian` dependency).
    # Empty list means "audited, no invocations"; missing entries are
    # treated the same — see tests/unit/test_registry_invariants.py.
    invokes: list[str] = field(default_factory=list)
    mutates_state: bool = False  # whether this capability modifies state
    retry_policy: str = "manual"  # "replay" | "verify_first" | "manual"
    # When True (default), the gateway auto-enqueues transient failures
    # of non-mutating capabilities for background retry. Capabilities
    # that represent real work with non-recoverable failure modes (e.g.
    # local-LLM calls where a timeout means the model is hung and
    # retrying wastes tokens and spams consent prompts) should set this
    # to False to keep the failure in the caller's face.
    auto_retry: bool = True
    slash_command: str | None = None  # e.g. "wb-journal-update"
    consent_operations: list[str] = field(default_factory=list)  # @requires_consent op IDs this capability may trigger
    # Effect manifest for multi-effect capabilities — used by
    # ``verify_post_write_effects`` to detect "some effects landed,
    # some didn't" partial states after a PostWriteUncertain. Capabilities
    # WITHOUT a manifest fall back to single-effect verify (the existing
    # behavior). See ``work_buddy.obsidian.effects.EffectSpec`` for the
    # schema and ``architecture/capability-registry`` for the picking
    # rule. Capabilities with a manifest MUST be idempotent on retry —
    # the partial-state recovery path retries the full capability.
    effects: list[Any] = field(default_factory=list)  # list[EffectSpec]

    # ---------------- v5 Stage 1.5 fields (defaults preserve v4) ----
    # See data/designs/gtd/reimagined/DESIGN.md §10.

    # Whether this capability appears in the v5 Action Catalog (i.e.
    # whether action inference may propose it as the action to take
    # for a Thread). False by default; capabilities the FSM should
    # be able to dispatch as Standard Actions opt in by setting True.
    is_action: bool = False

    # Per DESIGN.md §10.3 — set of contexts where this capability is
    # discoverable / callable. The default mirrors what existing v4
    # capabilities expect: every context EXCEPT FSM_INTERNAL (which
    # is reserved for FSM-engine-only operations the agent should
    # never see directly). Sensitive capabilities and FSM internals
    # override this set.
    available_in: set[InvocationContext] = field(
        default_factory=lambda: {
            InvocationContext.AGENT_CONVERSATION,
            InvocationContext.AGENT_AUTONOMOUS,
            InvocationContext.ACTION_PROPOSAL,
            InvocationContext.USER_INVOCATION,
        }
    )

    # Per DESIGN.md §10.4 — risk amplifiers intrinsic to the action
    # (regardless of caller / thread). Composed with Thread.risk_profile
    # at execution time. e.g. ``send_email`` → {"reversibility":
    # "irreversible", "regret_potential": "high"}.
    intrinsic_amplifiers: dict[str, str] = field(default_factory=dict)

    # If is_action=True: the JSONSchema (or simplified parameter
    # spec) the inference module proposes parameters against. Falls
    # back to the existing ``parameters`` field if not set, but
    # action templates with non-trivial parameter shapes should
    # provide an explicit schema.
    parameter_schema_for_action: dict[str, Any] = field(default_factory=dict)

    # Per DESIGN.md §7.2 / §7.7 (R7.6) — when the FSM dispatches this
    # action, should the resulting Thread enter `awaiting_review`
    # after `executing` succeeds? Most actions do NOT (False, default
    # — the Thread goes straight to `done`). Action templates that
    # produce output the user must validate (drafts, summaries,
    # decompositions) opt in by setting True.
    requires_post_review: bool = False


@dataclass
class AutoRun:
    """Specification for a step the conductor executes automatically.

    The callable is imported lazily at execution time. Only ``work_buddy.*``
    import paths are allowed (enforced by the conductor).

    ``input_map`` wires prior step results into kwargs. Each key is a kwarg
    name; each value is a step ID whose result becomes that kwarg's value.
    Example: ``input_map: {cfg: load-config}`` passes the ``load-config``
    step's result as the ``cfg`` keyword argument.
    """

    callable: str  # dotted Python path, e.g. "work_buddy.morning.get_morning_config"
    kwargs: dict[str, Any] = field(default_factory=dict)
    input_map: dict[str, str] = field(default_factory=dict)  # kwarg_name → step_id
    timeout: int = 30  # seconds


@dataclass
class ResultVisibility:
    """Controls what portion of a step result the agent sees inline.

    The full result is always stored in the DAG on disk.  This spec only
    affects what appears in the MCP response.  Agents can retrieve full
    results on-demand via ``wb_step_result``.

    Modes:
      full    — complete result (up to ``_STEP_RESULT_CAP``)
      summary — manifest with structural hints (keys, sizes)
      none    — bare status card (step_id, size, retrievable flag)
      auto    — full if <=10 KB serialized, else summary (default)
    """

    mode: str = "auto"  # "full" | "summary" | "none" | "auto"
    include_keys: list[str] = field(default_factory=list)   # for summary: only these keys inline
    exclude_keys: list[str] = field(default_factory=list)   # for summary: omit these keys


@dataclass
class WorkflowStep:
    """A single step in a workflow procedure."""

    id: str
    name: str
    instruction: str  # what to tell the agent
    step_type: str  # "code" or "reasoning"
    depends_on: list[str] = field(default_factory=list)
    execution: str = "main"  # "main" or "subagent"
    workflow_file: str | None = None  # sub-workflow reference
    optional: bool = False
    requires: list[str] = field(default_factory=list)  # tool IDs for conductor gating
    # Capability names this step calls. For auto_run steps, this is typically
    # a single capability owning the callable; for reasoning steps it's the
    # capabilities the agent is instructed to invoke. Populated via
    # `invokes: [...]` in workflows.json. The control-graph capability
    # resolver walks this to compute transitive component dependencies.
    invokes: list[str] = field(default_factory=list)
    auto_run: AutoRun | None = None  # conductor auto-executes this step
    result_schema: dict[str, Any] | None = None  # validate agent output before storing
    requires_individual_consent: bool = False  # if True, workflow blanket consent is suspended for this step
    visibility: ResultVisibility | None = None  # controls inline result exposure; None = auto


@dataclass
class WorkflowDefinition:
    """A multi-step workflow with DAG structure."""

    name: str
    description: str
    workflow_file: str  # relative path from repo root
    execution: str  # default execution policy
    allow_override: bool = True
    steps: list[WorkflowStep] = field(default_factory=list)
    context: str = ""  # philosophy, "What NOT to do" sections
    slash_command: str | None = None  # e.g. "wb-morning"
    # Computed at registry-build time: the union of all tool/component IDs
    # required by this workflow's steps — both `step.requires` directly and
    # the `requires` of capabilities named in `step.invokes`. Do not
    # hand-author; see `_compute_workflow_requires()`.
    requires: list[str] = field(default_factory=list)

    # ---------------- v5 Stage 1.5 fields (defaults preserve v4) ----
    # See DESIGN.md §10. Workflows can also be Action Catalog
    # entries (i.e. a Standard Action whose execution dispatches
    # into the workflow conductor). The fields mirror Capability's.

    is_action: bool = False
    available_in: set[InvocationContext] = field(
        default_factory=lambda: {
            InvocationContext.AGENT_CONVERSATION,
            InvocationContext.AGENT_AUTONOMOUS,
            InvocationContext.ACTION_PROPOSAL,
            InvocationContext.USER_INVOCATION,
        }
    )
    intrinsic_amplifiers: dict[str, str] = field(default_factory=dict)
    parameter_schema_for_action: dict[str, Any] = field(default_factory=dict)
    requires_post_review: bool = False

    # Forward-looking: if a graduated improvised action got promoted
    # into the catalog, record the originating Thread for provenance.
    improvised_origin_thread_id: str | None = None


# ---------------------------------------------------------------------------
# Registry singleton
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, Capability | WorkflowDefinition] | None = None

# Stash of full ``Capability`` objects for capabilities filtered out of the
# live registry by the `_build_registry` filter pass (because their tool
# requirements aren't met). Populated alongside ``DISABLED_CAPABILITIES``.
# Used by ``work_buddy.recovery.recheck_disabled_capability`` to restore a
# capability to the live registry without re-running the full registry
# build (~6s + sys.modules purge). Keys MUST stay in sync with
# ``DISABLED_CAPABILITIES`` keys; see invariant tests.
#
# Cleared at the top of every ``_build_registry()`` invocation so a stale
# Capability whose closure references a purged module never survives a
# reload (mcp_registry_reload purges work_buddy.* from sys.modules).
_DISABLED_REGISTRY: dict[str, Capability] = {}


def get_registry() -> dict[str, Capability | WorkflowDefinition]:
    """Return the registry, building it on first access."""
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = _build_registry()
    return _REGISTRY


def get_disabled_registry() -> dict[str, Capability]:
    """Return the stash of full Capability objects for disabled capabilities.

    Read-only access for ``work_buddy.recovery.recheck_disabled_capability``
    and observability tools. The dict is mutated only inside
    ``_build_registry()`` (cleared + repopulated) and inside the recovery
    module under its lock (popped on successful restore). Callers MUST
    NOT mutate it directly.
    """
    # Trigger a build if the registry hasn't been initialised — populates
    # _DISABLED_REGISTRY as a side effect.
    get_registry()
    return _DISABLED_REGISTRY


def invalidate_registry() -> None:
    """Clear the cached registry so it rebuilds on next access.

    Also purges ``work_buddy.*`` modules from ``sys.modules`` so deferred
    imports in capability builders re-read the current source code.
    Clears tool probe cache so tools are re-probed on rebuild.

    **Re-bootstraps v5 Threads after the purge.** Purging
    ``work_buddy.threads.engine`` from sys.modules nukes the
    process-global ``_REGISTERED_SIDE_EFFECTS`` dict, which is where
    the v5 FSM state-entry handlers (enqueue inference, publish
    Resolution Surface card, etc.) live. Without re-bootstrap, the
    next FSM transition after a registry reload would land in a
    wait state with no handlers registered, so capabilities like
    ``journal_v5_scan`` would silently dead-end at AWAITING_INFERENCE.
    Discovered live 2026-05-03 when ``mcp_registry_reload`` followed
    by ``journal_v5_scan`` produced a thread that never got enqueued.
    """
    import sys
    from work_buddy.tools import invalidate_tool_status

    global _REGISTRY
    _REGISTRY = None
    invalidate_tool_status()

    # Purge work_buddy modules so next import picks up new code.
    # Exclude the MCP server's own module to avoid breaking the
    # running event loop.
    keep_prefixes = ("work_buddy.mcp_server.server",)
    to_remove = [
        k for k in sys.modules
        if k.startswith("work_buddy.") and not k.startswith(keep_prefixes)
    ]
    for k in to_remove:
        del sys.modules[k]

    # Re-bootstrap v5 Threads in this subprocess. Best-effort: if
    # bootstrap fails (e.g. budget hook init issue), the registry is
    # still valid — capabilities will work, but FSM transitions on
    # Threads won't fire side effects. Fail loud so the user notices.
    try:
        from work_buddy.threads.bootstrap import bootstrap_for_subprocess
        bootstrap_for_subprocess(subprocess_name="mcp-gateway-reload")
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning(
            "v5 Threads re-bootstrap after registry reload failed: %s. "
            "FSM state-entry handlers may be missing; spawn capabilities "
            "could dead-end. Restart the gateway to recover.",
            exc,
        )


def _disabled_reason(capability_name: str) -> str:
    """Human-readable reason a capability is disabled in the live registry.

    Returns a string like "Dependency unavailable: obsidian (probe says
    'Bridge unreachable', last probe Ns ago)" so an agent consuming
    ``wb_search`` results can distinguish "backing service is down"
    from "your session's ACL doesn't allow this" AND know HOW LONG
    the dep has been down + WHY — two very different problems that
    used to share a single ``unavailable: true`` flag and mislead
    reasoning models into the wrong conclusion.

    Post-CP-A5 the message also distinguishes three states per missing
    tool:

    1. **Probe still failing** — auto-recovery already tried (CP-A3) or
       the cool-down hasn't expired; tool is genuinely down. Format:
       "<tool> probe failed Ns ago: '<reason>'".
    2. **Probe now passing but cap still in DISABLED_CAPABILITIES** —
       rare race; suggest mcp_registry_reload. Format: "<tool> probe
       reports available but capability not yet in registry".
    3. **No probe data yet** — cold-start race; agent should retry or
       run mcp_registry_reload. Format: "<tool> probe hasn't completed
       yet".
    """
    try:
        from work_buddy.tools import DISABLED_CAPABILITIES, get_tool_status
        deps = DISABLED_CAPABILITIES.get(capability_name)
        if not deps:
            return "Not registered in the live capability set"

        # Pull fresh probe state per missing tool. get_tool_status returns
        # {tools: {tool_id: {available, probe_ms, reason, ...}}, ...}.
        tool_status = get_tool_status().get("tools", {})

        # Compute probe age once from the tool_status.json mtime. This is
        # cheaper than per-tool tracking and good enough for a human-
        # readable diagnostic. Falls back gracefully if the file is
        # missing or unreadable.
        probe_age_str = _format_probe_age()

        per_tool: list[str] = []
        for dep in deps:
            entry = tool_status.get(dep)
            if entry is None:
                # State 3: no probe data yet. Cold start.
                per_tool.append(
                    f"{dep} (no probe data yet — wait {probe_age_str} or "
                    f"run mcp_registry_reload)"
                )
                continue
            if entry.get("available"):
                # State 2: probe passing but cap still disabled. This
                # happens if the user calls a disabled cap WITHOUT going
                # through the wb_run dispatch path (which would auto-
                # recover via CP-A3) — e.g. wb_search hits.
                per_tool.append(
                    f"{dep} (probe reports available — run "
                    f"mcp_registry_reload to re-enable this capability)"
                )
                continue
            # State 1: probe still failing.
            reason = entry.get("reason") or "no reason recorded"
            per_tool.append(
                f"{dep} (probe failed {probe_age_str}: '{reason}')"
            )

        return "Dependency unavailable: " + "; ".join(per_tool)
    except Exception:
        # Defensive fallback: if anything in the enriched path fails,
        # don't crash wb_search — return a usable string.
        try:
            from work_buddy.tools import DISABLED_CAPABILITIES
            deps = DISABLED_CAPABILITIES.get(capability_name)
            if deps:
                return f"Dependency unavailable: {', '.join(deps)}"
        except Exception:
            pass
    return "Not registered in the live capability set"


def _format_probe_age() -> str:
    """Approximate "Ns ago" label for the most recent probe sweep.

    Reads the mtime of ``data/runtime/tool_status.json`` (written
    atomically by every ``probe_all`` and ``reprobe_one`` call). Returns
    a short human-readable interval like ``"3s ago"``, ``"2m ago"``,
    or ``"unknown"`` if the file is missing or unreadable.
    """
    try:
        import time
        from work_buddy.tools import _TOOL_STATUS_FILE

        mtime = _TOOL_STATUS_FILE.stat().st_mtime
        elapsed = max(0.0, time.time() - mtime)
        if elapsed < 60:
            return f"{int(elapsed)}s ago"
        if elapsed < 3600:
            return f"{int(elapsed / 60)}m ago"
        return f"{int(elapsed / 3600)}h ago"
    except Exception:
        return "(probe age unknown)"


def search_registry(
    query: str,
    category: str | None = None,
    top_n: int = 3,
) -> list[dict[str, Any]]:
    """Search capabilities and workflows.

    Searches the unified knowledge store first (includes directions,
    system docs, capabilities, and workflows). Falls back to the legacy
    registry-only search if the store is unavailable.

    Empty query returns all entries (browse mode). Category filter is
    applied after scoring.
    """
    reg = get_registry()

    # Exact name match — check registry first, then store
    normalized = query.replace("-", "_").replace(" ", "_")
    exact = reg.get(query) or reg.get(normalized)
    if exact is not None:
        result = _entry_to_dict(exact)
        result["search_score"] = 1.0
        return [result]

    # Exact name not in registry (maybe filtered out) — check store by path
    try:
        from work_buddy.knowledge.store import load_store
        from work_buddy.knowledge.model import CapabilityUnit, WorkflowUnit
        store = load_store()
        # Search store for a CapabilityUnit with this exact capability_name
        for path, unit in store.items():
            if isinstance(unit, CapabilityUnit) and unit.capability_name == query:
                result = {
                    "name": unit.capability_name,
                    "description": unit.description,
                    "category": unit.category,
                    "type": "function",
                    "parameters": unit.parameters,
                    "search_score": 1.0,
                    "disabled": True,
                    "disabled_reason": _disabled_reason(unit.capability_name),
                    # Back-compat alias — remove after 2026-Q3
                    "unavailable": True,
                }
                return [result]
            if isinstance(unit, WorkflowUnit) and unit.workflow_name == query:
                # Exact-name hit in the store only means the registry
                # didn't have it — same "tool deps unmet" condition
                # as the CapabilityUnit branch above. Flag it.
                result = {
                    "name": unit.workflow_name,
                    "description": unit.description,
                    "category": "workflow",
                    "type": "workflow",
                    "search_score": 1.0,
                    "disabled": True,
                    "disabled_reason": _disabled_reason(unit.workflow_name),
                    # Back-compat alias — remove after 2026-Q3
                    "unavailable": True,
                }
                return [result]
    except Exception:
        pass  # Store unavailable, continue to search

    # Try unified store search (richer: includes directions + system docs)
    try:
        store_results = _search_via_store(query, category, top_n)
        if store_results is not None:
            return store_results
    except Exception:
        pass  # Fall through to legacy search

    # Legacy: Empty query = browse mode
    if not query:
        results = []
        for entry in reg.values():
            if category:
                entry_cat = entry.category if isinstance(entry, Capability) else "workflow"
                if entry_cat != category:
                    continue
            results.append(_entry_to_dict(entry))
        return results

    # Legacy: Hybrid search over registry only
    import time
    from work_buddy.mcp_server.search import hybrid_search, _log_to_file, _get_search_log
    _lf = _get_search_log()
    _log_to_file(_lf, f"Starting hybrid search for: {query!r}")
    t_start = time.time()
    scored = hybrid_search(query, reg, top_n=top_n)
    _log_to_file(_lf, f"Total search time: {time.time()-t_start:.2f}s")

    results = []
    for item in scored:
        name = item["name"]
        entry = reg.get(name)
        if entry is None:
            continue
        if category:
            entry_cat = entry.category if isinstance(entry, Capability) else "workflow"
            if entry_cat != category:
                continue
        result = _entry_to_dict(entry)
        result["search_score"] = item["score"]
        results.append(result)

    return results


def _search_via_store(
    query: str,
    category: str | None,
    top_n: int,
) -> list[dict[str, Any]] | None:
    """Search the unified knowledge store, returning registry-compatible dicts.

    Returns None if the store is unavailable or empty, triggering fallback.
    For capability/workflow results, enriches with registry execution metadata
    (parameters, steps) so wb_search callers get the same shape they expect.
    """
    from work_buddy.knowledge.search import search as store_search
    from work_buddy.knowledge.model import CapabilityUnit, WorkflowUnit

    # Map category to store kind for filtering
    kind = None
    if category == "workflow":
        kind = "workflow"

    r = store_search(query=query, kind=kind, depth="summary", top_n=top_n)
    if r.get("count", 0) == 0 and not query:
        return None  # Empty store, fall back

    results = []
    reg = get_registry()

    for hit in r.get("results", []):
        unit_kind = hit.get("kind", "")
        unit_name = hit.get("name", "")
        score = hit.get("score", 0.0)

        # For capabilities: return registry-compatible dict
        if unit_kind == "capability":
            cap_name = hit.get("capability_name", "")
            entry = reg.get(cap_name)
            if entry is not None:
                result = _entry_to_dict(entry)
                result["search_score"] = score
                results.append(result)
                continue
            # Capability not in registry (tool requirements unmet) — show from store
            result = {
                "name": cap_name,
                "description": hit.get("description", ""),
                "category": hit.get("category", ""),
                "type": "function",
                "parameters": hit.get("parameters", {}),
                "search_score": score,
                "disabled": True,
                "disabled_reason": _disabled_reason(cap_name),
                # Back-compat alias — remove after 2026-Q3
                "unavailable": True,
            }
            if category and result["category"] != category:
                continue
            results.append(result)

        # For workflows: return registry-compatible dict
        elif unit_kind == "workflow":
            wf_name = hit.get("workflow_name", "")
            entry = reg.get(wf_name)
            if entry is not None:
                result = _entry_to_dict(entry)
                result["search_score"] = score
                results.append(result)
                continue
            # Workflow in the store but not registered live — mirror
            # the CapabilityUnit branch above and flag it clearly so
            # agents don't try to call a workflow whose dependencies
            # aren't met.
            result = {
                "name": wf_name,
                "description": hit.get("description", ""),
                "category": "workflow",
                "type": "workflow",
                "search_score": score,
                "disabled": True,
                "disabled_reason": _disabled_reason(wf_name),
                # Back-compat alias — remove after 2026-Q3
                "unavailable": True,
            }
            results.append(result)

        # For directions/system: new result type
        else:
            result = {
                "name": hit.get("path", unit_name),
                "description": hit.get("description", ""),
                "category": unit_kind,
                "type": unit_kind,
                "search_score": score,
            }
            content = hit.get("content", "")
            if content:
                result["content_preview"] = content[:500]
            if category and result["category"] != category:
                continue
            results.append(result)

    return results if results else None


def get_entry(name: str) -> Capability | WorkflowDefinition | None:
    """Look up a single registry entry by exact name."""
    return get_registry().get(name)


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------

def _entry_to_dict(entry: Capability | WorkflowDefinition) -> dict[str, Any]:
    """Convert a registry entry to a JSON-friendly dict."""
    if isinstance(entry, Capability):
        d = {
            "name": entry.name,
            "description": entry.description,
            "category": entry.category,
            "type": "function",
            "parameters": entry.parameters,
        }
        if entry.mutates_state:
            d["mutates_state"] = True
            d["retry_policy"] = entry.retry_policy
        if entry.slash_command:
            d["slash_command"] = entry.slash_command
        return d
    else:
        d = {
            "name": entry.name,
            "description": entry.description,
            "category": "workflow",
            "type": "workflow",
            "execution": entry.execution,
            "steps": [
                {"id": s.id, "name": s.name, "step_type": s.step_type,
                 "execution": s.execution, "depends_on": s.depends_on,
                 "workflow_file": s.workflow_file}
                for s in entry.steps
            ],
        }
        if entry.slash_command:
            d["slash_command"] = entry.slash_command
        return d


# ---------------------------------------------------------------------------
# Knowledge index warm-up
# ---------------------------------------------------------------------------

def _warm_knowledge_index() -> None:
    """Build the knowledge search index eagerly during registry init.

    BM25 builds inline (~50ms). Dense vectors are built in a background
    thread because the embedding service may still be loading models
    when the MCP server starts. If dense fails, BM25 is still ready.
    """
    import threading
    from work_buddy.knowledge.store import load_store
    from work_buddy.knowledge.index import get_index

    idx = get_index()
    if idx.is_built:
        return  # already warm

    store = load_store(scope="all")
    if not store:
        return

    # Build BM25 inline (fast, no external deps)
    idx.build(store, skip_dense=True)
    gen = idx._generation  # snapshot for the background thread

    # Build dense vectors in background (embedding service may be slow).
    # Two parallel signals: content (asymmetric 768-d) and aliases (symmetric
    # 1024-d). Each is independent — if one fails, the other still lands.
    #
    # Cold-start race fix (2026-05-04): the warmup thread previously fired
    # embed batches immediately after the registry build, which was often
    # 30-40s before the embedding service finished its first model load.
    # The batches timed out, the service returned None, and the user saw
    # ``Embedding service unavailable during knowledge alias dense build``
    # warnings on every cold sidecar start. Now we poll
    # ``embedding.client.wait_until_available`` (~30s budget) before either
    # build fires. If the wait times out we log an INFO line and return —
    # search still works via BM25 fallback; the next periodic rebuild
    # picks up the dense signals once the service warms up.
    def _build_dense() -> None:
        try:
            from work_buddy.embedding.client import wait_until_available
        except Exception as e:  # defensive — embedding module shouldn't fail to import
            logger.info(
                "knowledge-dense-warmup: embedding client unavailable "
                "(%s); skipping dense build for this cycle.", e,
            )
            return
        if not wait_until_available(timeout_s=30.0, interval_s=0.5):
            logger.info(
                "knowledge-dense-warmup: embedding service didn't reach "
                "'ok' within 30s; skipping dense build. Search will use "
                "BM25-only ranking until the next periodic rebuild.",
            )
            return
        try:
            idx._build_content_vectors(expected_generation=gen)
        except Exception:
            pass  # logged inside the builder
        try:
            idx._build_alias_vectors(expected_generation=gen)
        except Exception:
            pass  # logged inside the builder

    thread = threading.Thread(
        target=_build_dense,
        name="knowledge-dense-warmup",
        daemon=True,
    )
    thread.start()


# ---------------------------------------------------------------------------
# Registry builder
# ---------------------------------------------------------------------------

def _build_registry() -> dict[str, Capability | WorkflowDefinition]:
    """Scan modules and workflow files to populate the registry.

    !! IMPORT DEADLOCK RISK !!
    Capability callables run via asyncio.to_thread(). Any callable that
    does a deferred import of a C-extension module (numpy, sqlite3, etc.)
    can permanently deadlock the MCP server. See the knowledge store unit
    architecture/mcp-import-discipline for the full explanation.
    When adding capabilities, ensure callables only use
    lightweight imports (urllib, json, pathlib) or HTTP calls to services.
    """
    import time
    from work_buddy.mcp_server.search import _log_to_file, _get_search_log
    from work_buddy.tools import (
        _register_default_probes, probe_all, is_tool_available,
        DISABLED_CAPABILITIES,
    )
    _lf = _get_search_log()
    _log_to_file(_lf, "Registry build starting...")

    _build_start = time.time()
    _section_times: dict[str, float] = {}

    # --- Tool probes ---
    t = time.time()
    _register_default_probes()
    tool_status = probe_all(force=True)
    available = [tid for tid, s in tool_status.items() if s["available"]]
    unavailable = [tid for tid, s in tool_status.items() if not s["available"]]
    _section_times["tool_probes"] = time.time() - t
    _log_to_file(_lf, f"  tool_probes: {_section_times['tool_probes']:.2f}s — "
                       f"available={available}, unavailable={unavailable}")

    registry: dict[str, Capability | WorkflowDefinition] = {}

    for label, fn in [
        ("messaging", _messaging_capabilities),
        ("contracts", _contract_capabilities),
        ("status", _status_capabilities),
        ("journal", _journal_capabilities),
        ("memory", _memory_capabilities),
        ("pipelines", _pipeline_capabilities),
        ("threads", _thread_capabilities),
        ("tasks", _task_capabilities),
        ("context", _context_capabilities),
        ("projects", _project_capabilities),
        ("sidecar", _sidecar_capabilities),
        ("llm", _llm_capabilities),
        ("consent", _consent_capabilities),
        ("notifications", _notification_capabilities),
        ("conversations", _conversation_capabilities),
        ("inline", _inline_capabilities),
        ("remote_session", _remote_session_capabilities),
        ("ledger", _ledger_capabilities),
        ("knowledge", _knowledge_capabilities),
        ("artifacts", _artifact_capabilities),
        ("email", _email_capabilities),
    ]:
        t = time.time()
        try:
            for cap in fn():
                registry[cap.name] = cap
            _section_times[f"cap:{label}"] = time.time() - t
            _log_to_file(_lf, f"  {label}: {time.time()-t:.2f}s")
        except Exception as e:
            _section_times[f"cap:{label}"] = time.time() - t
            _log_to_file(_lf, f"  {label}: FAILED in {time.time()-t:.2f}s — {e}")

    # --- Filter capabilities with unmet tool requirements ---
    t = time.time()
    # Auto-extract requires from @requires_tool decorated callables
    for cap in list(registry.values()):
        if isinstance(cap, Capability):
            inferred = getattr(cap.callable, '_requires_tools', [])
            if inferred and not cap.requires:
                cap.requires = list(inferred)

    DISABLED_CAPABILITIES.clear()
    # CP-A1: also clear the full-Capability stash. Critical for
    # closure-correctness across mcp_registry_reload (which purges
    # sys.modules); a Capability stashed during the previous build
    # would dereference a now-dead module if it survived.
    _DISABLED_REGISTRY.clear()
    for name in list(registry):
        entry = registry[name]
        if isinstance(entry, Capability) and entry.requires:
            missing = [t_id for t_id in entry.requires if not is_tool_available(t_id)]
            if missing:
                DISABLED_CAPABILITIES[name] = missing
                # CP-A1: stash the full Capability object so the recovery
                # module can restore it without rebuilding the registry.
                _DISABLED_REGISTRY[name] = entry
                del registry[name]

    if DISABLED_CAPABILITIES:
        _log_to_file(_lf, f"  filtered: {len(DISABLED_CAPABILITIES)} capabilities "
                           f"disabled due to missing tools")
    else:
        _log_to_file(_lf, f"  filtered: 0 capabilities disabled (all tools available)")
    _log_to_file(_lf, f"  filter_pass: {time.time()-t:.2f}s")

    t = time.time()
    for wf in _discover_workflows_from_store():
        registry[wf.name] = wf
    _log_to_file(_lf, f"  workflows (store): {time.time()-t:.2f}s")

    # Compute WorkflowDefinition.requires as the union of each step's
    # `requires` plus the `requires` of capabilities named in step.invokes.
    # Computed, never hand-authored.
    t = time.time()
    _compute_workflow_requires(registry)
    _log_to_file(_lf, f"  workflow_requires: {time.time()-t:.2f}s")

    # Populate slash_command on all entries from .claude/commands/ frontmatter
    t = time.time()
    slash_index = _build_slash_command_index()
    for target_name, cmd_stem in slash_index.items():
        entry = registry.get(target_name)
        if entry is not None:
            entry.slash_command = cmd_stem
    _log_to_file(_lf, f"  slash_commands: {time.time()-t:.2f}s ({len(slash_index)} mapped)")
    _log_to_file(_lf, f"Registry built: {len(registry)} entries")

    # --- Warm up the knowledge search index ---
    # BM25 builds inline (~50ms), dense vectors build in a background thread
    # since the embedding service may still be loading models at this point.
    t = time.time()
    try:
        _warm_knowledge_index()
        _section_times["knowledge_index"] = time.time() - t
        _log_to_file(_lf, f"  knowledge_index: {_section_times['knowledge_index']:.2f}s (BM25 inline, dense in background)")
    except Exception as e:
        _section_times["knowledge_index"] = time.time() - t
        _log_to_file(_lf, f"  knowledge_index: FAILED in {_section_times['knowledge_index']:.2f}s — {e}")

    # --- Slow-rebuild warning ---
    # The registry rebuild blocks whoever triggered it. If it happens on
    # the MCP event loop (a missing asyncio.to_thread around a registry
    # call), /health will also block — which historically made the sidecar
    # supervisor mark mcp_gateway unhealthy and auto-restart it, dropping
    # every Claude Code SSE stream in the process. Surface the duration
    # with a WARNING + per-section breakdown so that regression is
    # visible in sidecar logs rather than silent.
    total = time.time() - _build_start
    if total > 5.0:
        slowest = sorted(_section_times.items(), key=lambda kv: -kv[1])[:5]
        breakdown = ", ".join(f"{k}={v:.1f}s" for k, v in slowest)
        logger.warning(
            "Registry build slow: %.1fs total (%s). If this ran on the "
            "asyncio event loop, /health is blocked for the duration. "
            "Check architecture/mcp-import-discipline for async hygiene.",
            total, breakdown,
        )
    else:
        logger.debug("Registry build: %.2fs total", total)

    return registry


# ---------------------------------------------------------------------------
# Function capabilities (unchanged)
# ---------------------------------------------------------------------------

def _messaging_capabilities() -> list[Capability]:
    from work_buddy.messaging import client

    return [
        Capability(
            name="send_message",
            description="Send a message to another agent or project",
            category="messaging",
            parameters={
                "sender": {"type": "str", "description": "Sender project name", "required": True},
                "recipient": {"type": "str", "description": "Recipient project name", "required": True},
                "type": {"type": "str", "description": "Message type: status-update, question, result, escalation", "required": True},
                "subject": {"type": "str", "description": "Message subject", "required": True},
                "body": {"type": "str", "description": "Message body text", "required": False},
                "thread_id": {"type": "str", "description": "Thread ID to continue a conversation", "required": False},
                "priority": {"type": "str", "description": "Priority: low, normal, high, urgent", "required": False},
            },
            callable=client.send_message,
            search_aliases=[
                "send a message",
                "message another agent",
                "contact another session",
                "ping another claude",
                "notify another project",
                "write to another session",
                "inter-agent message",
            ],
        ),
        Capability(
            name="query_messages",
            description="Query messages by recipient, sender, status, or limit",
            category="messaging",
            parameters={
                "recipient": {"type": "str", "description": "Filter by recipient", "required": False},
                "sender": {"type": "str", "description": "Filter by sender", "required": False},
                "status": {"type": "str", "description": "Filter by status (e.g., pending)", "required": False},
                "limit": {"type": "int", "description": "Max messages to return (default 50)", "required": False},
            },
            callable=client.query_messages,
            search_aliases=[
                "list messages",
                "find messages",
                "show inter-agent messages",
                "recent messages",
                "what messages have arrived",
                "search messaging log",
                "filter messages",
            ],
        ),
        Capability(
            name="read_message",
            description="Fetch a single message with full body content",
            category="messaging",
            parameters={
                "msg_id": {"type": "str", "description": "Message ID", "required": True},
                "session": {"type": "str", "description": "Session ID for read-tracking", "required": False},
            },
            callable=client.read_message,
            search_aliases=[
                "open a message",
                "read message contents",
                "full message body",
                "view specific message",
                "fetch one message",
                "show message body",
            ],
        ),
        Capability(
            name="reply_to_message",
            description="Reply to an existing message",
            category="messaging",
            parameters={
                "msg_id": {"type": "str", "description": "ID of message to reply to", "required": True},
                "sender": {"type": "str", "description": "Sender project name", "required": True},
                "body": {"type": "str", "description": "Reply body text", "required": True},
                "type": {"type": "str", "description": "Reply type (default: ack)", "required": False},
            },
            callable=client.reply,
            search_aliases=[
                "respond to message",
                "answer a message",
                "reply to another agent",
                "continue conversation",
                "message reply",
                "acknowledge message",
            ],
        ),
        Capability(
            name="update_message_status",
            description="Update a message's status (e.g., pending → resolved)",
            category="messaging",
            parameters={
                "msg_id": {"type": "str", "description": "Message ID", "required": True},
                "new_status": {"type": "str", "description": "New status value", "required": True},
            },
            callable=client.update_status,
            search_aliases=[
                "mark message read",
                "change message status",
                "resolve a message",
                "update message state",
                "close out a message",
                "message status change",
            ],
        ),
        Capability(
            name="get_thread",
            description="Get all messages in a conversation thread",
            category="messaging",
            parameters={
                "thread_id": {"type": "str", "description": "Thread ID", "required": True},
            },
            callable=client.get_thread,
            search_aliases=[
                "view conversation thread",
                "message thread history",
                "all messages in thread",
                "read threaded messages",
                "conversation history",
                "thread transcript",
            ],
        ),
    ]


def _contract_capabilities() -> list[Capability]:
    from work_buddy import contracts

    return [
        Capability(
            name="contracts_summary",
            description="Markdown summary of all contracts with title, status, deadline, progress",
            category="contracts",
            parameters={},
            callable=contracts.contracts_summary,
            requires=["obsidian"],
            search_aliases=[
                "list contracts",
                "show my commitments",
                "all contracts overview",
                "what am I working on",
                "active work commitments",
                "contract list",
                "status of my deliverables",
            ],
        ),
        Capability(
            name="contract_health",
            description="Health check report: status counts, overdue, stale, missing fields",
            category="contracts",
            parameters={},
            callable=contracts.contract_health_check,
            requires=["obsidian"],
            search_aliases=[
                "are my commitments on track",
                "check contract status",
                "contract health check",
                "deliverable health",
                "are contracts healthy",
                "check commitments",
                "paper status",
                "commitment health",
                "check project health",
            ],
        ),
        Capability(
            name="active_contracts",
            description="List all contracts with status=active",
            category="contracts",
            parameters={},
            callable=contracts.active_contracts,
            requires=["obsidian"],
            search_aliases=[
                "current commitments",
                "what's active",
                "active work",
                "ongoing contracts",
                "open deliverables",
                "in-progress papers",
                "live contracts",
            ],
        ),
        Capability(
            name="overdue_contracts",
            description="List contracts past their deadline",
            category="contracts",
            parameters={},
            callable=contracts.overdue_contracts,
            requires=["obsidian"],
            search_aliases=[
                "late contracts",
                "past due deliverables",
                "missed deadlines",
                "overdue work",
                "what's late",
                "past deadline",
                "contracts over deadline",
            ],
        ),
        Capability(
            name="stale_contracts",
            description="List contracts not reviewed in N days (default 7)",
            category="contracts",
            parameters={
                "stale_days": {"type": "int", "description": "Days since last review (default 7)", "required": False},
            },
            callable=contracts.stale_contracts,
            requires=["obsidian"],
            search_aliases=[
                "forgotten contracts",
                "not reviewed recently",
                "stale commitments",
                "unvisited contracts",
                "dormant work",
                "contracts needing review",
                "neglected contracts",
            ],
        ),
    ]


def _status_capabilities() -> list[Capability]:
    from work_buddy.messaging import client
    from work_buddy import agent_session
    from work_buddy.mcp_server.tools.gateway import retry_operation as _retry_operation

    def _tailscale_status() -> dict:
        """Check Tailscale daemon status and Serve configuration."""
        import subprocess
        import json as _json

        result: dict = {"installed": False, "running": False, "serve": None}

        # Check tailscale status
        try:
            proc = subprocess.run(
                ["tailscale", "status", "--json"],
                capture_output=True, text=True, timeout=10,
            )
            if proc.returncode != 0:
                result["installed"] = True
                result["error"] = proc.stderr.strip()[:200]
                return result

            data = _json.loads(proc.stdout)
            result["installed"] = True
            result["running"] = True
            result["backend_state"] = data.get("BackendState", "")
            result["tailnet"] = data.get("MagicDNSSuffix", "")
            result["self"] = {
                "name": data.get("Self", {}).get("HostName", ""),
                "dns_name": data.get("Self", {}).get("DNSName", ""),
                "online": data.get("Self", {}).get("Online", False),
                "os": data.get("Self", {}).get("OS", ""),
                "ips": data.get("Self", {}).get("TailscaleIPs", []),
            }
            peers = data.get("Peer", {})
            result["peers"] = [
                {
                    "name": p.get("HostName", ""),
                    "dns_name": p.get("DNSName", ""),
                    "online": p.get("Online", False),
                    "os": p.get("OS", ""),
                    "last_seen": p.get("LastSeen", ""),
                }
                for p in peers.values()
            ]
        except FileNotFoundError:
            return result
        except Exception as exc:
            result["error"] = str(exc)[:200]
            return result

        # Check tailscale serve status
        try:
            serve_proc = subprocess.run(
                ["tailscale", "serve", "status", "--json"],
                capture_output=True, text=True, timeout=5,
            )
            if serve_proc.returncode == 0 and serve_proc.stdout.strip():
                result["serve"] = _json.loads(serve_proc.stdout)
            else:
                result["serve"] = None
        except Exception:
            result["serve"] = None

        return result

    def _feature_status(verbose: bool = False, force: bool = False) -> dict:
        """Show which tools, features, and capabilities are available or disabled.

        When ``force=True``, re-runs every tool probe fresh rather than
        reading the cached result from the last probe sweep. Use this
        when you suspect a cached "unavailable" is stale — e.g., the
        user just started Obsidian and wants to confirm the bridge is
        now up.
        """
        from work_buddy.tools import get_tool_status, probe_all
        from work_buddy.health.preferences import load_preferences
        from work_buddy.health.requirements import RequirementChecker

        if force:
            probe_all(force=True)

        result = get_tool_status()
        if not verbose:
            # Compact: just tool names and disabled capability names
            result["tools"] = {
                tid: {"available": s["available"], "reason": s.get("reason", "")}
                for tid, s in result.get("tools", {}).items()
            }

        # Include user preferences
        prefs = load_preferences()
        result["preferences"] = {
            comp_id: pref.to_dict()
            for comp_id, pref in prefs.items()
        }

        # Include requirement summary (lightweight)
        try:
            checker = RequirementChecker()
            req_results = checker.check_bootstrap()
            result["bootstrap_requirements"] = checker.summarize(req_results)
        except Exception:
            result["bootstrap_requirements"] = {"error": "Could not check requirements"}

        return result

    def _setup_wizard(
        mode: str = "status",
        component: str = "",
        updates: dict | None = None,
    ) -> dict:
        """Setup wizard — comprehensive setup, diagnostics, and preferences.

        Modes:
            status      — Quick health + requirements overview (default).
            guided      — Interactive first-time setup with structured steps.
            diagnose    — Deep diagnostic for a specific component.
            preferences — View/edit feature preferences.
        """
        from work_buddy.health.wizard import SetupWizard
        wizard = SetupWizard()

        if mode == "guided":
            return wizard.guided()
        elif mode == "diagnose":
            if not component:
                return {"error": "diagnose mode requires a component parameter"}
            return wizard.diagnose(component)
        elif mode == "preferences":
            return wizard.preferences(updates=updates)
        else:
            return wizard.status()

    def _setup_help(component: str = "all") -> dict:
        """Diagnose component health. Runs automated check sequences.

        If a specific component is given, walks its dependency chain and
        runs diagnostic checks — stopping at the first failure with a
        root cause and fix suggestion.

        If "all" (default), returns a health overview of all components
        with any unhealthy ones highlighted.
        """
        from work_buddy.health import HealthEngine, DiagnosticRunner
        from work_buddy.health.components import COMPONENT_CATALOG

        if component != "all" and component in COMPONENT_CATALOG:
            runner = DiagnosticRunner()
            result = runner.diagnose(component)
            # Also include the engine's merged status for context
            engine = HealthEngine()
            health = engine.get_component(component)
            return {
                "mode": "diagnose",
                "component": component,
                "engine_status": health.to_dict() if health else None,
                "diagnostic": result.to_dict(),
            }

        # Overview mode
        engine = HealthEngine()
        overview = engine.get_all()

        # Run diagnostics only on unhealthy components
        unhealthy_ids = [
            c["id"] for c in overview["components"]
            if c["status"] not in ("healthy", "disabled")
        ]
        diagnostics = {}
        if unhealthy_ids:
            runner = DiagnosticRunner()
            for cid in unhealthy_ids:
                diagnostics[cid] = runner.diagnose(cid).to_dict()

        return {
            "mode": "overview",
            "summary": overview["summary"],
            "components": overview["components"],
            "diagnostics": diagnostics,
            "available_components": sorted(COMPONENT_CATALOG.keys()),
        }

    return [
        Capability(
            name="feature_status",
            description=(
                "Show which tools, features, and capabilities are available or "
                "disabled, and why. Use this to diagnose missing integrations."
            ),
            category="status",
            parameters={
                "verbose": {
                    "type": "bool",
                    "description": "Include probe timing and config details",
                    "required": False,
                },
                "force": {
                    "type": "bool",
                    "description": (
                        "Re-run all tool probes fresh instead of reading "
                        "the cached result. Use when a previously-failed "
                        "tool (e.g. Obsidian) may now be available."
                    ),
                    "required": False,
                },
            },
            callable=_feature_status,
            search_aliases=[
                "tools", "integrations", "what's available",
                "feature toggle", "disabled", "unavailable",
            ],
        ),
        Capability(
            name="setup_help",
            description=(
                "Diagnose why a component isn't working. Runs automated check "
                "sequences that walk dependency chains and stop at the first failure "
                "with a root cause and fix suggestion. Use 'all' for an overview of "
                "all components, or specify a component ID (e.g. 'hindsight', "
                "'obsidian', 'postgresql') for targeted diagnostics."
            ),
            category="status",
            parameters={
                "component": {
                    "type": "str",
                    "description": (
                        "Component ID to diagnose, or 'all' for overview. "
                        "Available: postgresql, obsidian, hindsight, chrome_extension, "
                        "messaging, embedding, telegram, dashboard, smart_connections, "
                        "datacore, google_calendar"
                    ),
                    "required": False,
                },
            },
            callable=_setup_help,
            search_aliases=[
                "diagnose", "troubleshoot", "debug",
                "why not working", "fix", "health check", "what's wrong",
            ],
        ),
        Capability(
            name="setup_wizard",
            description=(
                "Comprehensive setup wizard for work-buddy. Validates bootstrap "
                "requirements, checks feature health, manages user preferences "
                "(wanted/unwanted features), and provides guided first-time setup. "
                "Modes: 'status' (quick overview), 'guided' (interactive walkthrough), "
                "'diagnose' (deep diagnostic for one component), 'preferences' (view/edit)."
            ),
            category="status",
            parameters={
                "mode": {
                    "type": "str",
                    "description": (
                        "Wizard mode: 'status' (default), 'guided', 'diagnose', 'preferences'"
                    ),
                    "required": False,
                },
                "component": {
                    "type": "str",
                    "description": "Component ID for 'diagnose' mode",
                    "required": False,
                },
                "updates": {
                    "type": "dict",
                    "description": (
                        "Preference updates for 'preferences' mode. "
                        "Dict of component_id -> {wanted: bool, reason: str}"
                    ),
                    "required": False,
                },
            },
            callable=_setup_wizard,
            # `preferences` mode with `updates` is the only mutating path;
            # other modes are read-only. Marked mutating because a single name
            # covers both. The consent gate (setup.write_preferences on
            # apply_preference_updates) fires only when `updates` is passed,
            # so read-only calls don't prompt. Not listed in
            # consent_operations because pre-flight would prompt on every
            # call — gateway's runtime ConsentRequired fallback handles it.
            mutates_state=True,
            search_aliases=[
                "setup", "wizard", "configure", "preferences", "onboarding",
                "first time", "requirements", "bootstrap", "wanted", "unwanted",
            ],
            slash_command="wb-setup",
        ),
        Capability(
            name="service_health",
            description="Check if the messaging service is running",
            category="status",
            parameters={},
            callable=client.is_service_running,
            search_aliases=[
                "is messaging working",
                "messaging service up",
                "check messaging daemon",
                "messaging alive",
                "is messaging service healthy",
                "messaging service status",
            ],
        ),
        Capability(
            name="list_sessions",
            description="List all known agent sessions with metadata",
            category="status",
            parameters={},
            callable=agent_session.list_sessions,
            search_aliases=[
                "what sessions exist",
                "all agent sessions",
                "session directory",
                "recent sessions",
                "known agents",
                "list agent sessions",
            ],
        ),
        Capability(
            name="mcp_registry_reload",
            description=(
                "HEAVY: invalidate and rebuild the capability registry. "
                "Re-probes every tool, purges work_buddy.* from sys.modules, "
                "rebuilds every capability (~6-8s). Use ONLY after code "
                "changes to existing capability callables. For transient "
                "tool-probe failures (capability stuck in disabled state), "
                "use work_buddy.recovery.recheck_disabled_capability(name) "
                "instead — it re-probes only the relevant tools with a "
                "30s cool-down. See architecture/capability-registry."
            ),
            category="status",
            parameters={},
            callable=invalidate_registry,
            search_aliases=[
                "reload capabilities",
                "refresh tools",
                "pick up new capabilities",
                "reload registry",
                "register new functions",
                "hot reload MCP",
                "rebuild capability registry",
            ],
        ),
        Capability(
            name="retry",
            description=(
                "Retry a previously recorded operation by its ID. "
                "Use wb_status() to discover recent/pending operations after a timeout. "
                "Operations with retry_policy='manual' cannot be auto-retried. "
                "Operations with an active execution lease will be refused to prevent "
                "double-dispatch."
            ),
            category="operations",
            parameters={
                "operation_id": {
                    "type": "str",
                    "required": True,
                    "description": "The operation ID from a wb_run response or wb_status output",
                },
            },
            callable=_retry_operation,
            mutates_state=True,
            retry_policy="manual",
            search_aliases=["retry", "replay", "re-run", "retry operation"],
        ),
        Capability(
            name="tailscale_status",
            description=(
                "Check Tailscale VPN status: daemon state, tailnet identity, "
                "online peers, and Serve configuration (published ports)."
            ),
            category="status",
            parameters={},
            callable=_tailscale_status,
            search_aliases=["vpn", "tailscale", "tailnet", "remote access", "serve"],
            slash_command="wb-tailscale-status",
        ),
    ]


def _context_capabilities() -> list[Capability]:
    # !! IMPORT DEADLOCK RISK — READ BEFORE EDITING !!
    # Callables registered here run via asyncio.to_thread() in the MCP server.
    # Deferred imports of modules that load C extensions (numpy, sqlite3, etc.)
    # WILL deadlock the event loop. See architecture/mcp-import-discipline.
    # Safe: urllib, json, pathlib. Unsafe: ir.store, ir.engine, ir.dense.
    # Use HTTP calls to the embedding service instead of direct imports.
    from work_buddy.mcp_server.context_wrappers import (
        get_git_context,
        get_obsidian_context,
        get_tasks_context,
        get_wellness_context,
        get_chat_context,
        get_chrome_context,
        get_messages_context,
        get_smart_context,
        get_calendar_context,
        get_projects_context,
        collect_bundle,
        chrome_activity,
        chrome_infer,
        chrome_content,
        triage_execute,
        chrome_tab_close,
        chrome_tab_group,
        chrome_tab_move,
        triage_item_detail_wrapper,
        llm_costs,
        datacore_status,
        datacore_query,
        datacore_fullquery,
        datacore_validate,
        datacore_get_page,
        datacore_evaluate,
        datacore_schema,
        datacore_compile_plan,
        datacore_run_plan,
    )
    from work_buddy.embedding.client import ir_index as _ir_index_client
    from work_buddy.sessions.inspector import (
        session_get as _session_get,
        session_expand as _session_expand,
        session_locate as _session_locate,
        session_search as _session_search,
        session_commits as _session_commits,
        session_uncommitted as _session_uncommitted,
        session_wb_activity as _session_wb_activity,
    )

    def _format_result_header(r: dict) -> str:
        """Render the per-result header line, dispatching on source.

        Each source surfaces different identifying metadata; the conversation
        format ``[project] session`` is meaningless for a task note hit.
        Unknown sources fall back to ``[source] doc_id`` so a header always
        renders something useful even before a dedicated formatter exists.
        """
        meta = r.get("metadata", {}) or {}
        source = r.get("source", "")
        doc_id = r.get("doc_id", "")

        if source == "conversation":
            proj = meta.get("project_name") or "?"
            sid = (meta.get("session_id") or "?")[:12]
            return f"### [{proj}] {sid}"

        if source == "task_note":
            tid = meta.get("task_id") or "?"
            state = meta.get("task_state") or "?"
            return f"### [task] {tid} ({state})"

        if source == "docs":
            kind = meta.get("kind") or "doc"
            path = meta.get("path") or doc_id
            return f"### [{kind}] {path}"

        if source == "chrome":
            title = meta.get("title") or meta.get("tab_title") or "?"
            return f"### [tab] {title[:80]}"

        if source == "projects":
            slug = meta.get("slug") or doc_id
            name = meta.get("name") or slug
            return f"### [project] {name} ({slug})"

        # Generic fallback — no source-specific knowledge needed.
        return f"### [{source or 'result'}] {doc_id[:48]}"

    def _format_results(results: list[dict], label: str) -> str:
        """Format structured result dicts into markdown."""
        if not results:
            return f"No results from {label}."

        lines = [f"*{len(results)} result(s) from {label}*", ""]
        for r in results:
            lines.append(_format_result_header(r))
            scores = []
            if r.get("bm25_score"):
                scores.append(f"bm25={r['bm25_score']:.3f}")
            if r.get("dense_score"):
                scores.append(f"dense={r['dense_score']:.3f}")
            # Per-projection breakdown for multi-projection sources (e.g.
            # task_note's body / line). Only surfaces when present so the
            # conversation/docs cases stay terse.
            proj_scores = r.get("projection_scores") or {}
            for key in sorted(proj_scores):
                val = proj_scores[key]
                if val:
                    scores.append(f"{key}={val:.3f}")
            if r.get("recency_weight") is not None:
                scores.append(f"recency={r['recency_weight']:.2f}")
            if scores:
                lines.append(f"*Score: {r['score']:.4f} ({', '.join(scores)})*")
            else:
                lines.append(f"*Score: {r['score']:.4f}*")
            lines.append("")
            if r.get("display_text"):
                preview = r["display_text"][:300]
                lines.append(f"> {preview}")
                lines.append("")
        return "\n".join(lines)

    def _ir_search_dispatch(
        query: str,
        *,
        top_k: int = 10,
        source: str | None = None,
        scope: str | None = None,
        method: str = "keyword,semantic",
        recency: bool | None = None,
    ) -> str:
        """Search indexed content using configurable method(s).

        Thin wrapper: delegates to ir.search.search() for structured results,
        then formats to markdown via _format_results().
        """
        from work_buddy.ir.search import search as _ir_search

        results = _ir_search(
            query, top_k=top_k, source=source, scope=scope,
            method=method, recency=recency,
        )
        if isinstance(results, str):
            return results  # error message

        methods = [m.strip() for m in method.split(",") if m.strip()]
        label = "+".join(methods)
        if len(methods) > 1:
            label += " (RRF fused)"
        return _format_results(results, label)

    def _ir_index_dispatch(
        action: str = "build",
        source: str = "conversation",
        days: int = 30,
        force: bool = False,
    ) -> str:
        """Build or check the IR index via the embedding service."""
        import json

        result = _ir_index_client(
            action, source=source, days=days, force=force,
        )
        if result is None:
            return json.dumps({
                "error": "Embedding service unavailable. Start it with: Start-ScheduledTask -TaskName 'WB-Embedding'"
            })
        return json.dumps(result, indent=2)

    return [
        Capability(
            name="context_git",
            description="Recent git activity across all repos: commits, diffs, dirty trees. Pass annotate=true to tag commits made by agent sessions with their session ID.",
            category="context",
            search_aliases=[
                "what changed in git",
                "recent commits",
                "repo status",
                "code changes",
                "git diff",
                "which session made this commit",
                "agent commits",
            ],
            parameters={
                "days": {"type": "int", "description": "Lookback window for commit history (default 7)", "required": False},
                "dirty_only": {"type": "bool", "description": "Only repos with uncommitted changes (default false)", "required": False},
                "annotate": {"type": "bool", "description": "Tag commits made by agent sessions with session ID (default false). Slower — scans JSONL files.", "required": False},
            },
            param_aliases={"since": "days"},
            callable=get_git_context,
        ),
        Capability(
            name="context_obsidian",
            description="Obsidian vault summary: journal entries, recently modified notes",
            category="context",
            search_aliases=[
                "vault notes",
                "journal entries",
                "what's in obsidian",
                "recent notes",
                "daily journal",
            ],
            parameters={
                "journal_days": {"type": "int", "description": "Days of journal entries (default 7)", "required": False},
                "modified_days": {"type": "int", "description": "Days of recently modified files (default 3)", "required": False},
            },
            callable=get_obsidian_context,
            requires=["obsidian"],
        ),
        Capability(
            name="context_tasks",
            description="Obsidian task summary: outstanding tasks + recent state changes (last 48h by default)",
            category="context",
            search_aliases=[
                "outstanding tasks",
                "task list",
                "what needs doing",
                "todo items",
                "task events",
                "task history",
                "task changes",
            ],
            parameters={
                "journal_days": {"type": "int", "description": "Days of journal entries to scan (default 7)", "required": False},
                "event_hours": {"type": "int", "description": "Hours of task state history to include (default 48, from config). Pass 0 to suppress.", "required": False},
            },
            callable=get_tasks_context,
            requires=["obsidian"],
        ),
        Capability(
            name="context_wellness",
            description="Wellness tracker summary from recent journal entries",
            category="context",
            search_aliases=[
                "wellness",
                "health tracking",
                "sleep exercise mood",
                "self-care data",
            ],
            parameters={
                "days": {"type": "int", "description": "Days of wellness data (default 14)", "required": False},
            },
            callable=get_wellness_context,
        ),
        Capability(
            name="context_chat",
            description="Recent Claude Code conversations and CLI history with tool usage, duration, and outcome snippets",
            category="context",
            search_aliases=[
                "recent conversations",
                "claude code sessions",
                "what sessions happened",
                "chat history",
                "agent sessions",
                "conversation log",
            ],
            parameters={
                "days": {"type": "int", "description": "Lookback window (default 7)", "required": False},
                "last": {"type": "int", "description": "Cap sessions returned per source", "required": False},
            },
            param_aliases={"since": "days"},
            callable=get_chat_context,
        ),
        Capability(
            name="context_search",
            description="Search indexed content (conversations, documents, tabs). Requires IR index — build with ir_index first. Methods: 'substring' (exact match, no embedding service), 'keyword' (BM25), 'semantic' (dense), or comma-delimited combo like 'keyword,semantic' (default, RRF fused).",
            category="context",
            search_aliases=[
                "find conversation",
                "search sessions",
                "which session had",
                "conversation about",
                "look up chat",
                "search index",
                "information retrieval",
            ],
            parameters={
                "query": {"type": "str", "description": "Search query", "required": True},
                "top_k": {"type": "int", "description": "Max results (default 10)", "required": False},
                "source": {"type": "str", "description": "Filter by source type (e.g. 'conversation'). Default: all sources.", "required": False},
                "scope": {"type": "str", "description": "Narrow to a specific item within a source (e.g. a session_id for conversations, a tab_id for Chrome tabs). Uses doc_id prefix matching.", "required": False},
                "method": {"type": "str", "description": "Search method(s). 'substring' (exact, no service needed), 'keyword' (BM25), 'semantic' (dense), or comma-delimited like 'keyword,semantic' (default). substring is solo-only.", "required": False},
                "recency": {"type": "bool", "description": "Apply recency bias to favor recent results (default true). Set false to rank purely by text relevance.", "required": False},
            },
            callable=_ir_search_dispatch,
        ),
        Capability(
            name="session_get",
            description="Browse messages in a Claude Code session. Paginated with role/type filtering. Use after context_search finds a session.",
            category="context",
            search_aliases=[
                "inspect session",
                "browse conversation",
                "session messages",
                "read session",
                "drill into session",
            ],
            parameters={
                "session_id": {"type": "str", "description": "Full or partial (8-char) session UUID", "required": True},
                "offset": {"type": "int", "description": "Start index (default 0)", "required": False},
                "limit": {"type": "int", "description": "Max messages to return (default 10)", "required": False},
                "roles": {"type": "str", "description": "Comma-separated: 'user', 'assistant'", "required": False},
                "message_types": {"type": "str", "description": "Comma-separated: 'text' (has text content), 'tool_use' (has tool calls)", "required": False},
                "query": {"type": "str", "description": "Substring filter on message text (case-insensitive)", "required": False},
            },
            callable=_session_get,
        ),
        Capability(
            name="session_expand",
            description="Full context around a specific message in a session. Returns untruncated text for the target and surrounding messages.",
            category="context",
            search_aliases=[
                "expand message",
                "message context",
                "surrounding messages",
                "zoom into a message",
                "show context window",
                "what came before this message",
                "conversation context around message",
            ],
            parameters={
                "session_id": {"type": "str", "description": "Full or partial (8-char) session UUID", "required": True},
                "message_index": {"type": "int", "description": "Zero-based message index", "required": True},
                "context_window": {"type": "int", "description": "Messages before+after to include (default 5)", "required": False},
            },
            callable=_session_expand,
        ),
        Capability(
            name="session_locate",
            description="Jump from a context_search hit to the relevant conversation page. Takes a span_index from search result metadata and returns messages centered on that chunk.",
            category="context",
            search_aliases=[
                "find chunk in session",
                "locate search hit",
                "span to messages",
                "search result context",
            ],
            parameters={
                "session_id": {"type": "str", "description": "Full or partial (8-char) session UUID", "required": True},
                "span_index": {"type": "int", "description": "IR span index from context_search result metadata", "required": True},
            },
            callable=_session_locate,
        ),
        Capability(
            name="session_search",
            description="Hybrid search within a single session. Uses IR (keyword/semantic/substring) scoped to the session, then resolves chunk hits to message-level results via the span map.",
            category="context",
            search_aliases=[
                "search in session",
                "find in conversation",
                "session query",
                "semantic session search",
            ],
            parameters={
                "session_id": {"type": "str", "description": "Full or partial session UUID", "required": True},
                "query": {"type": "str", "description": "Search query", "required": True},
                "method": {"type": "str", "description": "Search method: 'substring', 'keyword', 'semantic', or 'keyword,semantic' (default)", "required": False},
                "top_k": {"type": "int", "description": "Max chunk hits to resolve (default 5)", "required": False},
            },
            callable=_session_search,
        ),
        Capability(
            name="session_commits",
            description="Extract git commits made during Claude Code sessions. Parses raw JSONL for Bash tool calls containing 'git commit' and their results. Scope to one session or scan all recent sessions.",
            category="context",
            search_aliases=[
                "commits in session",
                "what was committed",
                "git commits from conversation",
                "session git history",
                "what did the agent commit",
            ],
            parameters={
                "session_id": {"type": "str", "description": "Full or partial session UUID. If omitted, scans all recent sessions.", "required": False},
                "days": {"type": "int", "description": "Lookback window when scanning all sessions (default 7)", "required": False},
            },
            callable=_session_commits,
        ),
        Capability(
            name="session_uncommitted",
            description="Find agent sessions that wrote files still present in dirty git state. Answers: 'which sessions wrote code that was never committed?' Cross-references Write/Edit/NotebookEdit tool calls against git status --porcelain across all repos.",
            category="context",
            search_aliases=[
                "who didn't commit",
                "uncommitted agent writes",
                "sessions with dirty files",
                "what did the agent write but not commit",
                "dirty files from sessions",
            ],
            parameters={
                "days": {"type": "int", "description": "Lookback window for scanning sessions (default 7)", "required": False},
            },
            callable=_session_uncommitted,
        ),
        Capability(
            name="session_wb_activity",
            description="Summary of what a session did through work-buddy's MCP gateway — capabilities invoked, workflows run, errors, key artifacts. Reads from the per-session activity ledger.",
            category="context",
            search_aliases=[
                "what did the session do",
                "session work-buddy activity",
                "mcp activity",
                "gateway activity",
            ],
            parameters={
                "session_id": {"type": "str", "description": "Session ID to query. Default: current session.", "required": False},
            },
            callable=_session_wb_activity,
        ),
        Capability(
            name="ir_index",
            description="Build or check the IR search index. Run 'build' to (re)encode dense vectors for indexed documents; 'status' returns per-source counts including dense_eligible_docs (how many docs CAN be encoded) and pending_eligible (real backlog — NOT doc_count vs vector_count, which is misleading because sources like conversation intentionally leave dense_text empty for tool-only spans).",
            category="context",
            search_aliases=[
                "build index",
                "index conversations",
                "conversation index",
                "search index status",
                "reindex",
            ],
            parameters={
                "action": {"type": "str", "description": "'build' (default) or 'status'", "required": False},
                "source": {"type": "str", "description": "Source to index: 'conversation' (default)", "required": False},
                "days": {"type": "int", "description": "Index sessions from last N days (default 30)", "required": False},
                "force": {"type": "bool", "description": "Rebuild from scratch (default False)", "required": False},
            },
            callable=_ir_index_dispatch,
        ),
        Capability(
            name="context_chrome",
            description="Currently open Chrome tabs (requires Chrome extension running)",
            category="context",
            search_aliases=[
                "open tabs",
                "browser tabs",
                "what's in chrome",
                "tabs I have open",
                "my browser state",
                "chrome tabs right now",
                "what am I looking at",
                "currently open tabs",
            ],
            parameters={},
            callable=get_chrome_context,
            requires=["chrome_extension"],
        ),
        Capability(
            name="chrome_activity",
            description="Query Chrome browsing history from the rolling tab ledger. Supports: hot_tabs (ranked by engagement), changes (opened/closed/navigated/engaged/moved), sessions (domain clusters), tabs_at (snapshot at a time), context (tab proximity and window layout), details (full URLs by filter), status (ledger health). Output is compact (no URLs) — use details query for full URLs.",
            category="context",
            search_aliases=[
                "browsing history",
                "what was I browsing",
                "chrome tab history",
                "tab activity",
                "what tabs were open",
                "browsing sessions",
                "hot tabs",
                "browser activity",
            ],
            parameters={
                "query": {"type": "str", "description": "Query type: hot_tabs, changes, sessions, tabs_at, context, details, status (default: hot_tabs)", "required": False},
                "since": {"type": "str", "description": "Start of window. Relative ('2h', '1d') or ISO datetime (default: 2h)", "required": False},
                "until": {"type": "str", "description": "End of window. Default: now.", "required": False},
                "limit": {"type": "int", "description": "Max results for hot_tabs (default 20)", "required": False},
                "timestamp": {"type": "str", "description": "For tabs_at/details queries: ISO datetime or relative shorthand", "required": False},
                "filter": {"type": "str", "description": "For details query: domain or title substring to match", "required": False},
            },
            callable=chrome_activity,
            requires=["chrome_extension"],
        ),
        Capability(
            name="chrome_infer",
            description="Infer what the user is working on by reading page content from engaged Chrome tabs and analyzing with Haiku. Evaluates provided theories against actual page evidence. Caches results per tab to avoid redundant API calls. ~$0.001/call.",
            category="context",
            search_aliases=[
                "what am I working on",
                "browsing analysis",
                "page content analysis",
                "infer activity from tabs",
                "chrome page content",
                "what is the user doing",
            ],
            parameters={
                "since": {"type": "str", "description": "Lookback window. Relative ('1h', '30m') or ISO datetime. Default: 1h.", "required": False},
                "theories": {"type": "str", "description": "Comma-separated theories to evaluate (e.g., 'researching pricing, writing code')", "required": False},
                "tab_limit": {"type": "int", "description": "Max tabs to analyze (default 5)", "required": False},
            },
            callable=chrome_infer,
            requires=["chrome_extension"],
        ),
        Capability(
            name="chrome_content",
            description="Extract full page text from currently-open Chrome tabs. Filter by domain or title substring, or get top-engagement tabs. Free — no LLM calls. Use for single-tab inspection or reading specific page content.",
            category="context",
            search_aliases=[
                "page text",
                "tab content",
                "read tab",
                "extract tab text",
                "what's on this tab",
                "show tab content",
            ],
            parameters={
                "tab_filter": {"type": "str", "description": "Domain or title substring to match (e.g., 'github', 'obsidian'). If not set, returns top-engagement tabs.", "required": False},
                "tab_limit": {"type": "int", "description": "Max tabs to extract (default 5)", "required": False},
                "max_chars": {"type": "int", "description": "Max characters per tab (default 5000)", "required": False},
            },
            callable=chrome_content,
            requires=["chrome_extension"],
        ),
        Capability(
            name="triage_item_detail",
            description="Retrieve the Haiku summary and/or raw content for a specific triage item. Use during triage review to inspect items with content gaps. Works for any source (Chrome tabs, journal entries, conversations). Requires a prior triage pipeline run.",
            category="context",
            search_aliases=[
                "tab summary",
                "item detail",
                "triage detail",
                "inspect item",
                "page summary",
                "what is this tab",
            ],
            parameters={
                "item_id": {"type": "str", "description": "TriageItem ID (e.g., 'tab_786de35645')", "required": True},
                "include_raw": {"type": "bool", "description": "Also return raw content (page text, etc.). Default: false — prefer summaries.", "required": False},
                "max_raw_chars": {"type": "int", "description": "Max characters of raw content (default 5000)", "required": False},
            },
            callable=triage_item_detail_wrapper,
            requires=["chrome_extension"],
        ),
        # ── Triage execution ───────────────────────────────────
        Capability(
            name="triage_execute",
            description=(
                "Execute triage decisions from the review view. Takes the user's "
                "Phase 2 response (group_decisions, reassignments) and the original "
                "presentation, then performs all actions: close tabs, create tasks, "
                "record into tasks, organize tab groups."
            ),
            category="context",
            parameters={
                "decisions": {"type": "dict", "description": "The Phase 2 review response (group_decisions + reassignments)", "required": True},
                "presentation": {"type": "dict", "description": "The original presentation dict (for item metadata)", "required": True},
            },
            callable=triage_execute,
            search_aliases=[
                "execute triage",
                "apply triage",
                "triage actions",
                "carry out triage decisions",
                "apply cleanup decisions",
                "commit triage plan",
                "run triage executor",
            ],
            requires=["chrome_extension"],
            mutates_state=True,
        ),

        # ── Background triage (source-agnostic) ─────────────────
        Capability(
            name="triage_submit",
            description=(
                "Record a triage verdict for one item of an active background "
                "triage run. Called by local agents in the triage_agent preset; "
                "validates the run/item and writes a PoolEntry. Does not dispatch "
                "a modal or mutate the vault. Safe to call from any context — "
                "unknown run_ids return a structured error."
            ),
            category="context",
            search_aliases=[
                "submit triage verdict",
                "record triage decision",
                "emit triage recommendation",
            ],
            parameters={
                "run_id": {"type": "str", "description": "The producer-assigned background-triage run id (from the agent's prompt).", "required": True},
                "item_id": {"type": "str", "description": "The id of the item this verdict applies to.", "required": True},
                "rationale": {"type": "str", "description": "One-to-three-sentence justification.", "required": True},
                "group_intent": {"type": "str", "description": "Short noun-phrase naming the underlying intent (≤8 words, distinct from the action name and the rationale). Used as the card title in the Resolution Surface.", "required": False},
                "confidence": {"type": "float", "description": "Optional [0,1] confidence score.", "required": False},
                # ---- Slice 3 multi-record fields (preferred for new captures) ----
                "records": {"type": "list", "description": "Slice 3+: list of records produced from the captured item. Each is {destination: 'task'|'reference'|'calendar_only'|'delete', task_proposal/reference_proposal/calendar_proposal/delete_reason: ...}. Empty list = 'no record produced'. Mutually exclusive with refusal.", "required": False},
                "refusal": {"type": "dict", "description": "Slice 3+: {question: '...', missing_context: [...]} when the agent doesn't have enough context to commit a verdict. Renders as a clarification card on the Resolution Surface. Mutually exclusive with records.", "required": False},
                "pipeline_blocker": {"type": "dict", "description": "Slice 1.5: typed stop reason per ROADMAP §3.3. String (just kind) or dict with kind + optional detail. Surfaced as a typed badge on the Resolution Surface card.", "required": False},
                # ---- Legacy single-action fields (Slice 1 compatibility) ----
                "recommended_action": {"type": "str", "description": "Legacy: one of close, group, create_task, record_into_task, leave. Required ONLY when records/refusal are not provided.", "required": False},
                "target_task_id": {"type": "str", "description": "Legacy: for record_into_task, the existing task id.", "required": False},
                "suggested_task_text": {"type": "str", "description": "Legacy: for create_task, the proposed task body.", "required": False},
                "related_item_ids": {"type": "list", "description": "Legacy: other item_ids from this run that belong to the same cluster.", "required": False},
            },
            callable=(lambda **kw: __import__(
                "work_buddy.clarify.capabilities.triage_submit",
                fromlist=["triage_submit"],
            ).triage_submit(**kw)),
            mutates_state=True,
            auto_retry=False,
        ),
        Capability(
            name="resolution_surface_pool",
            description=(
                "Open the dashboard Resolution Surface over pending Clarify "
                "proposals. Aggregates unreviewed ClarifyEntries, composes a "
                "presentation (Slice 3 multi-record schema or legacy "
                "single-action), dispatches the modal, and (on response) "
                "executes the user's decisions via the Clarify executor. "
                "On-demand — nothing fires automatically. "
                "Slice 3 rename of triage_review_pool; the legacy name is "
                "kept as a search alias so old callers still resolve."
            ),
            category="context",
            search_aliases=[
                "review clarify proposals",
                "open resolution surface",
                "drain clarify pool",
                "review pending clarify",
                # Legacy aliases — Slice 1/1.5 callers used these names.
                "triage_review_pool",
                "review triage proposals",
                "open triage modal",
                "drain triage pool",
                "review pending triage",
            ],
            parameters={
                "source": {"type": "str", "description": "Optional source filter (e.g. 'journal_thread', 'chrome_tab').", "required": False},
                "adapter": {"type": "str", "description": "Optional adapter-name filter (e.g. 'journal_triage').", "required": False},
                "since": {"type": "str", "description": "ISO timestamp; only entries created at-or-after are included.", "required": False},
                "max_items": {"type": "int", "description": "Safety cap on pending entries loaded (default 100).", "required": False},
                "dispatch": {"type": "bool", "description": "False to compose the presentation without opening the modal.", "required": False},
            },
            callable=(lambda **kw: __import__(
                "work_buddy.clarify.capabilities.triage_review_pool",
                fromlist=["triage_review_pool"],
            ).triage_review_pool(**kw)),
            mutates_state=True,
            auto_retry=False,
        ),
        # Legacy capability name alias for Slice 1/1.5 callers. Same
        # callable, same parameters — just a different name in the
        # registry so ``wb_run("triage_review_pool", ...)`` still
        # resolves. Search hits surface ``resolution_surface_pool``
        # first (it's the canonical name); this entry exists purely
        # for back-compat through the Slice 1→3 cutover.
        Capability(
            name="triage_review_pool",
            description=(
                "Legacy alias for ``resolution_surface_pool`` (Slice 3 "
                "rename). Identical behavior — opens the dashboard "
                "Resolution Surface over pending Clarify proposals. "
                "Prefer ``resolution_surface_pool`` for new callers."
            ),
            category="context",
            search_aliases=["legacy", "deprecated"],
            parameters={
                "source": {"type": "str", "description": "Optional source filter (e.g. 'journal_thread', 'chrome_tab').", "required": False},
                "adapter": {"type": "str", "description": "Optional adapter-name filter (e.g. 'journal_triage').", "required": False},
                "since": {"type": "str", "description": "ISO timestamp; only entries created at-or-after are included.", "required": False},
                "max_items": {"type": "int", "description": "Safety cap on pending entries loaded (default 100).", "required": False},
                "dispatch": {"type": "bool", "description": "False to compose the presentation without opening the modal.", "required": False},
            },
            callable=(lambda **kw: __import__(
                "work_buddy.clarify.capabilities.triage_review_pool",
                fromlist=["triage_review_pool"],
            ).triage_review_pool(**kw)),
            mutates_state=True,
            auto_retry=False,
        ),
        Capability(
            name="triage_pool_quarantine_entry",
            description=(
                "Quarantine a single pool entry by (run_id, item_id). "
                "Lighter-weight counterpart to triage_pool_sweep — used "
                "when the caller already knows a specific entry is "
                "stale (e.g. an 'open in app' action click returned "
                "email_message_not_found). Idempotent."
            ),
            category="context",
            search_aliases=[
                "quarantine pool entry",
                "drop one triage card",
                "mark triage entry gone",
                "remove pool entry source removed",
            ],
            parameters={
                "run_id": {"type": "str", "description": "Pool run id (visible on the Review-card group as pool_run_id).", "required": True},
                "item_id": {"type": "str", "description": "TriageItem id (visible on the Review-card item as id).", "required": True},
                "reason": {"type": "str", "description": "Quarantine reason. Defaults to 'source_removed' to match the sweep's reason taxonomy.", "required": False},
            },
            callable=(lambda **kw: __import__(
                "work_buddy.clarify.capabilities.triage_pool_quarantine_entry",
                fromlist=["triage_pool_quarantine_entry"],
            ).triage_pool_quarantine_entry(**kw)),
            mutates_state=True,
            auto_retry=False,
        ),
        Capability(
            name="triage_pool_sweep",
            description=(
                "Daily liveness pass over the triage pool (Slice 1). Walks every "
                "pending PoolEntry: marks expired entries as 'stale' (TTL from "
                "the source descriptor) and quarantines entries whose source no "
                "longer resolves (file deleted, journal text drifted beyond the "
                "match threshold, etc.). Source-specific behavior lives in the "
                "source descriptor registry (work_buddy/triage/sources.py). "
                "Non-destructive: state changes only; entries stay on disk."
            ),
            category="context",
            search_aliases=[
                "sweep triage pool",
                "quarantine stale triage",
                "triage liveness check",
                "drop ghost pool entries",
                "purge stale triage",
            ],
            parameters={
                "dry_run": {"type": "bool", "description": "When true, computes what would change but does not write back. Useful for rehearsal.", "required": False},
                "source": {"type": "str", "description": "Optional source filter (e.g. 'journal_thread', 'inline'). Other sources untouched.", "required": False},
                "max_entries": {"type": "int", "description": "Safety cap on entries inspected per pass.", "required": False},
            },
            callable=(lambda **kw: __import__(
                "work_buddy.clarify.capabilities.triage_pool_sweep",
                fromlist=["triage_pool_sweep"],
            ).triage_pool_sweep(**kw)),
            mutates_state=True,
            auto_retry=False,
        ),

        # ── Chrome tab mutations ────────────────────────────────
        # All three are flagged is_action=True so they show up in
        # the per-source action library / action chip dropdown for
        # Chrome group sub-threads. The thread-level route capabilities
        # (chrome_route_to_tasks, chrome_route_to_umbrella_task) are
        # registered alongside.
        Capability(
            name="chrome_tab_close",
            description="Close specified Chrome tabs by tab ID. Returns count of closed/missing tabs.",
            category="context",
            is_action=True,
            intrinsic_amplifiers={
                "irreversibility": "moderate",
                "regret_potential": "moderate",
            },
            parameters={
                "tab_ids": {"type": "list", "description": "List of Chrome tab IDs (integers) to close", "required": True},
            },
            callable=chrome_tab_close,
            search_aliases=[
                "close tab",
                "remove tab",
                "close chrome",
                "kill tabs",
                "close browser tabs",
                "dismiss tabs",
                "close tab by id",
            ],
            requires=["chrome_extension"],
            mutates_state=True,
        ),
        Capability(
            name="chrome_tab_group",
            description="Create a Chrome tab group or add tabs to an existing group. Returns the group ID.",
            category="context",
            is_action=True,
            intrinsic_amplifiers={
                "irreversibility": "low",
                "regret_potential": "low",
            },
            parameters={
                "tab_ids": {"type": "list", "description": "List of Chrome tab IDs to group", "required": True},
                "title": {"type": "str", "description": "Group title displayed in Chrome", "required": False},
                "color": {"type": "str", "description": "Group color: grey, blue, red, yellow, green, pink, purple, cyan, orange", "required": False},
                "group_id": {"type": "int", "description": "Existing group ID to add to (omit to create new group)", "required": False},
            },
            callable=chrome_tab_group,
            search_aliases=[
                "group tab",
                "tab group",
                "organize tabs",
                "bundle tabs together",
                "create tab group",
                "add to tab group",
                "organize browser",
            ],
            requires=["chrome_extension"],
            mutates_state=True,
        ),
        Capability(
            name="chrome_tab_move",
            description="Move Chrome tabs to a specific position or window.",
            category="context",
            is_action=True,
            intrinsic_amplifiers={
                "irreversibility": "low",
                "regret_potential": "low",
            },
            parameters={
                "tab_ids": {"type": "list", "description": "List of Chrome tab IDs to move", "required": True},
                "index": {"type": "int", "description": "Position index (-1 = end of window)", "required": False},
                "window_id": {"type": "int", "description": "Target window ID (omit for current window)", "required": False},
            },
            callable=chrome_tab_move,
            search_aliases=[
                "move tab",
                "reorder tabs",
                "rearrange tabs",
                "shift chrome tabs",
                "send tab to another window",
                "reposition tab",
            ],
            requires=["chrome_extension"],
            mutates_state=True,
        ),
        Capability(
            name="chrome_route_to_tasks",
            description=(
                "Walk a Chrome-group thread's tabs and create one task "
                "per tab. Each tab's title becomes the task text; the "
                "URL goes into a linked summary note."
            ),
            category="context",
            is_action=True,
            intrinsic_amplifiers={
                "irreversibility": "low",
                "regret_potential": "low",
            },
            parameters={
                "thread_id": {"type": "str", "description": "Chrome group sub-thread to route", "required": True},
                "urgency": {"type": "str", "description": "low | medium (default) | high", "required": False},
                "project": {"type": "str", "description": "Project slug applied to every created task", "required": False},
            },
            callable=(lambda **kw: __import__(
                "work_buddy.collectors.chrome_thread_actions",
                fromlist=["chrome_route_to_tasks"],
            ).chrome_route_to_tasks(**kw)),
            requires=["obsidian"],
            mutates_state=True,
            search_aliases=[
                "create tasks from chrome group",
                "tabs to task list",
                "spin out tabs as tasks",
            ],
        ),
        Capability(
            name="chrome_route_to_umbrella_task",
            description=(
                "Create a single task representing the whole Chrome "
                "group. The cluster label becomes the task text; the "
                "tabs are listed in the linked summary note."
            ),
            category="context",
            is_action=True,
            intrinsic_amplifiers={
                "irreversibility": "low",
                "regret_potential": "low",
            },
            parameters={
                "thread_id": {"type": "str", "description": "Chrome group sub-thread to route", "required": True},
                "urgency": {"type": "str", "description": "low | medium (default) | high", "required": False},
                "project": {"type": "str", "description": "Project slug for the task", "required": False},
                "title_override": {"type": "str", "description": "Override the task text; defaults to the cluster label", "required": False},
            },
            callable=(lambda **kw: __import__(
                "work_buddy.collectors.chrome_thread_actions",
                fromlist=["chrome_route_to_umbrella_task"],
            ).chrome_route_to_umbrella_task(**kw)),
            requires=["obsidian"],
            mutates_state=True,
            search_aliases=[
                "create umbrella task from chrome group",
                "single task for tab group",
            ],
        ),
        Capability(
            name="llm_costs",
            description="Check LLM token usage, costs, and breakdown for this session. Shows per-task costs, per-model costs, cache hit rates, and top callers.",
            category="status",
            search_aliases=[
                "llm costs",
                "token usage",
                "api costs",
                "how much has haiku cost",
                "llm spending",
            ],
            parameters={
                "breakdown": {"type": "bool", "description": "Show per-task and per-model breakdown (default: false)", "required": False},
            },
            callable=llm_costs,
        ),
        Capability(
            name="context_messages",
            description="Inter-agent messaging state: pending, recent, unread messages",
            category="context",
            search_aliases=[
                "pending messages",
                "agent messages",
                "inbox messages",
                "unread messages",
                "what messages are pending",
                "incoming inter-agent mail",
                "messaging state summary",
            ],
            parameters={},
            callable=get_messages_context,
        ),
        Capability(
            name="context_smart",
            description="Smart Connections context: semantically related notes to active contracts",
            category="context",
            search_aliases=[
                "related notes",
                "semantic search vault",
                "smart connections",
                "find related vault notes",
                "similar notes to contracts",
                "semantically linked notes",
                "what's related to my work",
            ],
            parameters={},
            callable=get_smart_context,
            requires=["obsidian", "smart_connections"],
        ),
        Capability(
            name="context_calendar",
            description="Google Calendar schedule for a given date. Also checks plugin readiness.",
            category="context",
            search_aliases=[
                "today's schedule",
                "calendar events",
                "meetings today",
                "what's on the calendar",
                "calendar ready",
            ],
            parameters={
                "date": {"type": "str", "description": "Date (YYYY-MM-DD). Default: today.", "required": False},
                "check_ready": {"type": "bool", "description": "Return only readiness check, no schedule fetch (default false)", "required": False},
            },
            callable=get_calendar_context,
            requires=["obsidian", "google_calendar"],
        ),
        # ── Datacore (structured vault query) ──────────────────────
        Capability(
            name="datacore_status",
            description="Check if Datacore plugin is installed, initialized, and queryable. Returns version, index revision, and object type counts.",
            category="context",
            search_aliases=[
                "datacore ready",
                "datacore check",
                "vault index status",
                "is datacore running",
                "check vault index",
                "datacore plugin status",
                "datacore health",
            ],
            parameters={},
            callable=datacore_status,
            requires=["obsidian", "datacore"],
        ),
        Capability(
            name="datacore_query",
            description="Execute a Datacore query against the vault index. Supports @page, @section, @block, @task, @list-item, @codeblock with filters like path(), tags, childof(), parentof(). Returns serialized results.",
            category="context",
            search_aliases=[
                "query vault",
                "search vault structure",
                "find pages",
                "find tasks datacore",
                "structural vault query",
                "datacore search",
            ],
            parameters={
                "query": {"type": "str", "description": "Datacore query string (e.g. '@page and path(\"journal\")')", "required": True},
                "fields": {"type": "str", "description": "Comma-separated fields to include (e.g. '$path,$tags'). Default: all.", "required": False},
                "limit": {"type": "int", "description": "Max results (default 50)", "required": False},
            },
            callable=datacore_query,
            requires=["obsidian", "datacore"],
        ),
        Capability(
            name="datacore_fullquery",
            description="Execute a Datacore query with timing and revision metadata. Same as datacore_query but includes duration_s and revision.",
            category="context",
            search_aliases=[
                "datacore fullquery",
                "timed vault query",
                "detailed datacore query",
                "vault query with timing",
                "datacore query debug",
                "query timing metadata",
            ],
            parameters={
                "query": {"type": "str", "description": "Datacore query string", "required": True},
                "fields": {"type": "str", "description": "Comma-separated fields. Default: all.", "required": False},
                "limit": {"type": "int", "description": "Max results (default 50)", "required": False},
            },
            callable=datacore_fullquery,
            requires=["obsidian", "datacore"],
        ),
        Capability(
            name="datacore_validate",
            description="Validate a Datacore query string without executing it. Returns parse error details if invalid.",
            category="context",
            search_aliases=[
                "validate query",
                "check query syntax",
                "lint datacore query",
                "verify query parses",
                "parse check",
                "query validator",
            ],
            parameters={
                "query": {"type": "str", "description": "Datacore query string to validate", "required": True},
            },
            callable=datacore_validate,
            requires=["obsidian", "datacore"],
        ),
        Capability(
            name="datacore_get_page",
            description="Get a single vault page by path with Datacore metadata: frontmatter, sections, tags, links, timestamps.",
            category="context",
            search_aliases=[
                "page metadata",
                "vault page details",
                "note structure",
                "look up a note",
                "fetch note metadata",
                "get vault page",
                "what's in this note",
                "page frontmatter",
            ],
            parameters={
                "path": {"type": "str", "description": "Vault-relative path (e.g. 'journal/2026-04-09.md')", "required": True},
                "fields": {"type": "str", "description": "Comma-separated fields. Default: all.", "required": False},
            },
            callable=datacore_get_page,
            requires=["obsidian", "datacore"],
        ),
        Capability(
            name="datacore_evaluate",
            description="Evaluate a Datacore expression (e.g. arithmetic, field access).",
            category="context",
            search_aliases=[
                "datacore eval",
                "evaluate expression",
                "compute datacore expression",
                "datacore calculation",
                "eval vault expression",
                "datacore formula",
            ],
            parameters={
                "expression": {"type": "str", "description": "Datacore expression", "required": True},
                "source_path": {"type": "str", "description": "Vault path for 'this' context", "required": False},
            },
            callable=datacore_evaluate,
            requires=["obsidian", "datacore"],
        ),
        Capability(
            name="datacore_schema",
            description="Summarize the vault's Datacore schema: object types, top tags, frontmatter keys, path prefixes. Use before building queries to understand what's available.",
            category="context",
            search_aliases=[
                "vault schema",
                "what tags exist",
                "vault structure overview",
                "frontmatter keys",
                "path prefixes",
            ],
            parameters={},
            callable=datacore_schema,
            requires=["obsidian", "datacore"],
        ),
        Capability(
            name="datacore_compile_plan",
            description="Compile a structured JSON query plan into a Datacore query string. Plan keys: target (required), path, tags, tags_any, status, text_contains, exists, frontmatter, child_of, parent, expressions, negate.",
            category="context",
            search_aliases=[
                "compile query plan",
                "plan to query",
                "structured query",
                "build datacore query",
            ],
            parameters={
                "plan_json": {"type": "str", "description": "JSON string of the query plan", "required": True},
            },
            callable=datacore_compile_plan,
            requires=["obsidian", "datacore"],
        ),
        Capability(
            name="datacore_run_plan",
            description="Compile and execute a structured query plan in one step. Preferred over raw datacore_query when building queries programmatically — the plan schema is simpler and validates before execution.",
            category="context",
            search_aliases=[
                "run query plan",
                "execute plan",
                "natural language vault query",
                "structured vault search",
            ],
            parameters={
                "plan_json": {"type": "str", "description": "JSON string of the query plan", "required": True},
                "fields": {"type": "str", "description": "Comma-separated fields. Default: all.", "required": False},
                "limit": {"type": "int", "description": "Max results (default 50)", "required": False},
            },
            callable=datacore_run_plan,
            requires=["obsidian", "datacore"],
        ),
        # ── Projects ──────────────────────────────────────────────
        Capability(
            name="context_projects",
            description="Active projects with identity, state, and trajectory — synthesized from vault directories, STATE.md files in repos, task tags, git activity, and contracts",
            category="context",
            search_aliases=[
                "projects",
                "what projects",
                "active projects",
                "current work",
                "project state",
                "project list",
            ],
            parameters={},
            callable=get_projects_context,
        ),
        # ── Context bundle ────────────────────────────────────────
        Capability(
            name="context_bundle",
            description="Run all (or selected) collectors and save a context bundle to disk. Use individual collectors (context_git, context_chat, etc.) when you only need one source.",
            category="context",
            search_aliases=[
                "full context collection",
                "context bundle",
                "collect everything",
                "snapshot all context",
            ],
            parameters={
                "days": {"type": "int", "description": "Override all time windows to N days", "required": False},
                "hours": {"type": "int", "description": "Override all time windows to N hours", "required": False},
                "only": {"type": "str", "description": "Comma-separated collectors (e.g. 'git,chats'). Default: all.", "required": False},
            },
            callable=collect_bundle,
        ),
        # ── Unified context pipeline (phase 7 of the LLM+Context refactor) ──
        # Two capabilities over the work_buddy.context stack:
        #   - context_block     → collect + curate in one call, returns
        #                         rendered markdown or JSON.
        #   - context_drill_down → expand one item (task note, commit
        #                         message, project description, …).
        # Both are thin wrappers — the real API lives in
        # work_buddy.context.{collector, curator, sources.*}.
        Capability(
            name="context_block",
            description=(
                "Collect + render a context block from registered sources "
                "(git, tasks, projects, chrome, obsidian, obsidian_tasks, "
                "obsidian_wellness, calendar, day_planner, session_activity, "
                "chat, message, smart, datacore). Structured sources (git / "
                "tasks / projects / chrome) emit curated prompt text; the "
                "rest wrap legacy collectors. Supports per-source depth, "
                "target_date windows, max_chars budget, markdown or JSON "
                "output, and cache reuse via max_age_seconds."
            ),
            category="context",
            search_aliases=[
                "user context",
                "what is the user working on",
                "active tasks projects commits",
                "context packet",
                "assemble context",
                "build context block",
            ],
            parameters={
                "sources": {"type": "list", "description": "Source names to include. Default: all registered.", "required": False},
                "exclude": {"type": "list", "description": "Source names to drop from the default set.", "required": False},
                "depth": {"type": "str", "description": "brief | normal | deep (default normal).", "required": False},
                "per_source_depth": {"type": "dict", "description": "{source: depth} overrides for individual sources.", "required": False},
                "target_date": {"type": "str", "description": "YYYY-MM-DD. Default: today (no time window shift).", "required": False},
                "window_days": {"type": "int", "description": "Window size around target_date (default 1).", "required": False},
                "max_chars": {"type": "int", "description": "Rendering budget. Truncates respecting section boundaries when possible.", "required": False},
                "max_age_seconds": {"type": "int", "description": "Use cached fetch if younger than this. None (default) = always fresh. 0 = use any cached.", "required": False},
                "custom": {"type": "dict", "description": "Per-source ad-hoc params forwarded to each source's collect().", "required": False},
                "format": {"type": "str", "description": "markdown (default) or json.", "required": False},
            },
            callable=_context_block,
        ),
        Capability(
            name="context_drill_down",
            description=(
                "Expand one item from a context source. Works on structured "
                "wave-1 sources that implement drill_down — tasks (field: "
                "'note' / 'line'), git (field: 'full_message' / 'diff_stats'), "
                "projects (field: 'description' / 'full'). Wave-2/3 markdown "
                "wrappers don't implement drill-down — the prompt already "
                "holds their full body at DEEP depth."
            ),
            category="context",
            search_aliases=[
                "show full commit message",
                "show full task note",
                "expand project description",
                "drill down on item",
                "get more detail",
            ],
            parameters={
                "source": {"type": "str", "description": "Source name (tasks / git / projects).", "required": True},
                "item_id": {"type": "str", "description": "Item identifier within the source (task_id / commit sha / project slug).", "required": True},
                "field": {"type": "str", "description": "Which expansion to return. See source docs for valid fields.", "required": True},
            },
            callable=_context_drill_down,
        ),
    ]


def _context_block(
    sources: list[str] | None = None,
    exclude: list[str] | None = None,
    depth: str = "normal",
    per_source_depth: dict[str, str] | None = None,
    target_date: str | None = None,
    window_days: int = 1,
    max_chars: int | None = None,
    max_age_seconds: int | None = None,
    custom: dict[str, dict] | None = None,
    format: str = "markdown",
) -> dict[str, Any]:
    """MCP callable for the ``context_block`` capability.

    Top-level so the capability's ``callable`` reference stays stable
    across registry rebuilds. Returns a dict with ``rendered`` (the
    block) and ``sources`` (per-source item counts + metadata) so MCP
    clients can inspect what was included.
    """
    from datetime import date as _date

    from work_buddy.context import (
        ContextCollector,
        ContextCurator,
        ContextDepth,
        ContextRequest,
    )
    from work_buddy.context import sources as _sources_pkg  # registers sources
    _ = _sources_pkg  # silence unused-import warning

    try:
        depth_enum = ContextDepth[depth.upper()]
    except KeyError:
        return {"error": f"depth must be one of: brief, normal, deep; got {depth!r}"}

    per_depth: dict[str, ContextDepth] | None = None
    if per_source_depth:
        try:
            per_depth = {k: ContextDepth[v.upper()] for k, v in per_source_depth.items()}
        except KeyError as exc:
            return {"error": f"per_source_depth value invalid: {exc}"}

    target: _date | None = None
    if target_date:
        try:
            target = _date.fromisoformat(target_date)
        except ValueError:
            return {"error": f"target_date must be YYYY-MM-DD; got {target_date!r}"}

    if format not in ("markdown", "json"):
        return {"error": f"format must be 'markdown' or 'json'; got {format!r}"}

    req = ContextRequest(
        sources=sources,
        exclude=exclude,
        depth=depth_enum,
        per_source_depth=per_depth,
        target_date=target,
        window_days=window_days,
        max_chars=max_chars,
        max_age_seconds=max_age_seconds,
        custom=custom,
    )

    ctx = ContextCollector().collect(req)
    rendered = ContextCurator().curate(
        ctx,
        depth=depth_enum,
        per_source_depth=per_depth,
        max_chars=max_chars,
        format=format,
    )

    sources_manifest = {
        name: {
            "item_count": len(section.items),
            "metadata": section.metadata,
            "fetched_at": section.fetched_at.isoformat(),
        }
        for name, section in ctx.sections.items()
    }

    return {
        "rendered": rendered,
        "sources": sources_manifest,
        "format": format,
    }


def _context_drill_down(source: str, item_id: str, field: str) -> dict[str, Any]:
    """MCP callable for ``context_drill_down``."""
    from work_buddy.context import registry as _ctx_registry
    from work_buddy.context import sources as _sources_pkg  # registers sources
    _ = _sources_pkg

    src = _ctx_registry.get(source)
    if src is None:
        return {
            "error": f"Unknown source {source!r}. Registered: {_ctx_registry.names()}",
        }

    try:
        return src.drill_down(item_id, field)
    except NotImplementedError as exc:
        return {"error": str(exc), "error_kind": "not_implemented"}
    except KeyError as exc:
        return {"error": str(exc), "error_kind": "not_found"}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}", "error_kind": "unknown"}


def _project_capabilities() -> list[Capability]:
    from work_buddy.mcp_server.context_wrappers import (
        project_list,
        project_get,
        project_observe,
        project_update,
        project_create,
        project_delete,
        project_discover,
        project_memory,
    )

    return [
        Capability(
            name="project_list",
            description="List all projects with observation counts, optionally filtered by status",
            category="projects",
            search_aliases=[
                "list projects",
                "what projects exist",
                "show projects",
                "all projects",
            ],
            parameters={
                "status": {"type": "str", "description": "Filter by status: active, paused, past, future, inferred", "required": False},
            },
            callable=project_list,
        ),
        Capability(
            name="project_get",
            description="Get a single project with its recent observations (identity + state + trajectory)",
            category="projects",
            search_aliases=[
                "project details",
                "project info",
                "project state",
                "project observations",
            ],
            parameters={
                "slug": {"type": "str", "description": "Project slug (e.g. 'my-project')", "required": True},
            },
            callable=project_get,
        ),
        Capability(
            name="project_observe",
            description="Record an observation about a project — strategic decisions, supervisor feedback, pivots, blockers, or anything that shapes trajectory but wouldn't appear in code or tasks",
            category="projects",
            search_aliases=[
                "observe project",
                "project note",
                "project update",
                "record decision",
                "project pivot",
            ],
            parameters={
                "project": {"type": "str", "description": "Project slug (e.g. 'my-project')", "required": True},
                "content": {"type": "str", "description": "The observation — what happened, what it means, what changed", "required": True},
            },
            callable=project_observe,
            mutates_state=True,
        ),
        Capability(
            name="project_update",
            description="Update a project's identity: name, status, or description",
            category="projects",
            search_aliases=[
                "rename project",
                "change project status",
                "describe project",
                "pause project",
                "archive project",
            ],
            parameters={
                "slug": {"type": "str", "description": "Project slug", "required": True},
                "name": {"type": "str", "description": "New human-readable name", "required": False},
                "status": {"type": "str", "description": "New status: active, paused, past, future, inferred", "required": False},
                "description": {"type": "str", "description": "What is this project? (embeddable text)", "required": False},
            },
            callable=project_update,
            mutates_state=True,
        ),
        Capability(
            name="project_create",
            description="Manually create a project that the collector can't discover (books, businesses, admin workflows, etc.)",
            category="projects",
            search_aliases=[
                "new project",
                "create project",
                "add project",
                "manually create project",
                "register new project",
                "start tracking a project",
                "project registry new entry",
            ],
            parameters={
                "slug": {"type": "str", "description": "Unique identifier (lowercase, hyphens)", "required": True},
                "name": {"type": "str", "description": "Human-readable project name", "required": True},
                "status": {"type": "str", "description": "Status: active (default), paused, past, future", "required": False},
                "description": {"type": "str", "description": "What is this project?", "required": False},
            },
            callable=project_create,
            mutates_state=True,
        ),
        Capability(
            name="project_memory",
            description="Read from the project memory bank (Hindsight-backed). Modes: 'search' (semantic recall, optionally scoped to one project), 'model' (fetch a mental model: project-landscape, active-risks, recent-decisions, inter-project-deps), 'recent' (latest project memories)",
            category="projects",
            search_aliases=[
                "project recall",
                "project memory",
                "project search",
                "project history",
                "project decisions",
                "project landscape",
            ],
            parameters={
                "query": {"type": "str", "description": "Search query for project memories", "required": False},
                "mode": {"type": "str", "description": "search (default), model, or recent", "required": False},
                "model_id": {"type": "str", "description": "Mental model ID for mode=model (default: project-landscape)", "required": False},
                "project": {"type": "str", "description": "Project slug to scope search (omit for cross-project)", "required": False},
                "budget": {"type": "str", "description": "Retrieval depth: low, mid (default), high", "required": False},
            },
            callable=project_memory,
            requires=["hindsight"],
        ),
        Capability(
            name="project_discover",
            description="Discover project candidates from task tags and git repos not yet in the registry. Returns candidates for agent review — evaluate each and use project_create to promote real projects.",
            category="projects",
            search_aliases=[
                "discover projects",
                "find new projects",
                "project candidates",
                "unregistered projects",
            ],
            parameters={},
            callable=project_discover,
        ),
        Capability(
            name="project_delete",
            description="Delete a project from the identity registry (consent-gated). Hindsight memories are preserved.",
            category="projects",
            search_aliases=[
                "delete project",
                "remove project",
                "drop project",
                "unregister project",
                "remove project from registry",
                "delete project slug",
                "drop project identity",
            ],
            parameters={
                "slug": {"type": "str", "description": "Project slug to delete", "required": True},
            },
            callable=project_delete,
            mutates_state=True,
        ),
    ]


def _journal_capabilities() -> list[Capability]:
    from work_buddy import journal
    from work_buddy.journal_backlog import read_running_notes
    from work_buddy.mcp_server.context_wrappers import (
        activity_timeline,
        day_planner,
        hot_files,
        journal_sign_in,
        journal_write,
    )

    return [
        Capability(
            name="journal_state",
            description="Read journal state: target date, activity window, existing entries",
            category="journal",
            parameters={
                "target": {"type": "str", "description": "Date target: 'today', 'yesterday', or YYYY-MM-DD", "required": False},
            },
            param_aliases={"target_date": "target", "date": "target"},
            callable=journal.read_journal_state,
            requires=["obsidian"],
            search_aliases=[
                "journal status",
                "today's journal state",
                "journal target date",
                "what's in today's journal",
                "activity window",
                "current journal entries",
                "journal metadata",
            ],
        ),
        Capability(
            name="activity_timeline",
            description="Infer recent activity from journal entries and optionally deeper signals. Returns a structured timeline with events, gaps, and relative timestamps. Use for understanding what happened during a time window.",
            search_aliases=[
                "what happened recently",
                "recent activity",
                "activity timeline",
                "what have I been doing",
                "infer activity",
                "activity digest",
                "journal entries structured",
            ],
            category="journal",
            parameters={
                "since": {"type": "str", "description": "ISO datetime or relative shorthand (e.g. '2h', '1d', '30m')", "required": True},
                "until": {"type": "str", "description": "ISO datetime. Default: now.", "required": False},
                "deep": {"type": "bool", "description": "Also collect git/chat/vault signals (default: false)", "required": False},
                "target_date": {"type": "str", "description": "Journal date YYYY-MM-DD (default: inferred from since)", "required": False},
            },
            callable=activity_timeline,
            requires=["obsidian"],
        ),
        Capability(
            name="hot_files",
            description="Rank vault files by activity intensity, fusing modification frequency (vault events) with writing intensity (Keep the Rhythm). Hierarchically collapses busy directories to prevent context flooding. Use sub_directory to drill into a specific area.",
            search_aliases=[
                "hot files",
                "most edited files",
                "active files",
                "what files changed",
                "recently modified",
                "frequently edited",
                "vault activity",
            ],
            category="journal",
            parameters={
                "since": {"type": "str", "description": "Relative shorthand ('7d', '2h') or ISO date ('2026-04-01')", "required": True},
                "sub_directory": {"type": "str", "description": "Vault-relative path to drill into (e.g. 'repos/work-buddy'). Shows file-level detail.", "required": False},
                "collapse_threshold": {"type": "int", "description": "Max files per directory before collapsing (default 5)", "required": False},
            },
            callable=hot_files,
            requires=["obsidian"],
        ),
        Capability(
            name="running_notes",
            description="Read the Running Notes section from the user's daily journal. This is the primary stream-of-consciousness capture zone where the user records ideas, observations, and notes throughout the day. Supports filtering by date range, last N days, or same-day only. Call with same_day=true for just today's entries, or days=N for recent history.",
            search_aliases=[
                "journal notes today",
                "read daily notes",
                "user's recent thoughts and observations",
                "stream of consciousness capture",
                "journal running notes content",
            ],
            category="journal",
            parameters={
                "same_day": {"type": "bool", "description": "Only notes from the journal's own date (no carried-over content)", "required": False},
                "days": {"type": "int", "description": "Most recent N days (today=1). Cannot combine with start/stop.", "required": False},
                "start": {"type": "str", "description": "Include notes from this date onward (YYYY-MM-DD, inclusive)", "required": False},
                "stop": {"type": "str", "description": "Include notes up to this date (YYYY-MM-DD, inclusive)", "required": False},
                "journal_date": {"type": "str", "description": "Journal file date (YYYY-MM-DD). Default: today.", "required": False},
            },
            callable=read_running_notes,
            requires=["obsidian"],
        ),
        Capability(
            name="journal_sign_in",
            description="Read sign-in state (sleep/energy/mood/check-in/motto) and wellness trends, optionally write fields. Composite: replaces separate extract_sign_in + interpret_wellness + write_sign_in calls.",
            category="journal",
            search_aliases=[
                "sign in",
                "morning check in",
                "sleep energy mood",
                "wellness trends",
                "write sign in",
            ],
            parameters={
                "target": {"type": "str", "description": "Date target: 'today', 'yesterday', or YYYY-MM-DD. Default: today.", "required": False},
                "write_fields": {"type": "str", "description": "JSON dict of fields to write (e.g. {\"sleep\": 7, \"mood\": 8}). Consent-gated. Omit for read-only.", "required": False},
            },
            callable=journal_sign_in,
            requires=["obsidian"],
            mutates_state=True,
            consent_operations=["morning.write_sign_in"],
        ),
        Capability(
            name="journal_write",
            description="Append log entries or persist a briefing to the journal. For log entries: pass time/description tuples. For briefing: pass markdown to wrap in a callout.",
            category="journal",
            search_aliases=[
                "write journal",
                "append log",
                "journal entry",
                "persist briefing",
                "update log",
            ],
            parameters={
                "mode": {"type": "str", "description": "'log_entries' (default) or 'briefing'", "required": False},
                "target": {"type": "str", "description": "Date target: 'today', 'yesterday', or YYYY-MM-DD", "required": False},
                "entries": {"type": "str", "description": "For log_entries: JSON list of [time, description] tuples", "required": False},
                "briefing_md": {"type": "str", "description": "For briefing mode: markdown string", "required": False},
            },
            param_aliases={"target_date": "target", "date": "target"},
            callable=journal_write,
            requires=["obsidian"],
            mutates_state=True,
            consent_operations=["update_journal_entry", "morning.persist_briefing"],
            # ``verify_first`` so EditorConflict gets re-invoked from scratch
            # by the sidecar (each retry re-reads + re-applies). Trade-off:
            # a mid-write timeout that *did* persist server-side could
            # duplicate a log entry. Low probability; preferable to silent
            # clobber-the-user's-typing.
            retry_policy="verify_first",
        ),
        Capability(
            name="day_planner",
            description="Day Planner operations: check plugin status, read current plan, generate schedule from events+tasks, or write plan to journal. Composite: replaces separate check_ready/get_plan/generate/write/resync calls.",
            category="journal",
            search_aliases=[
                "day planner",
                "time blocking",
                "schedule",
                "daily plan",
                "time blocks",
            ],
            parameters={
                "action": {"type": "str", "description": "'status', 'read', 'generate', 'write', or 'generate_and_write'", "required": True},
                "target": {"type": "str", "description": "Date target for read/write. Default: today.", "required": False},
                "calendar_events": {"type": "str", "description": "For generate: JSON list of events. Flat shape {start: 'HH:MM', end: 'HH:MM', summary/description/text: '...', past?: bool} OR Google Calendar API shape {start: {dateTime: ISO}, end: {dateTime: ISO}, summary, timeStatus}. See wrapper docstring for full spec.", "required": False},
                "focused_tasks": {"type": "str", "description": "For generate: JSON list of task dicts. Required: 'description' or 'text'. Optional: 'duration' (int minutes, overrides config default), 'time_start' ('HH:MM' — pins task to that time; goes unscheduled on conflict).", "required": False},
                "config_overrides": {"type": "str", "description": "JSON dict of day_planner config overrides (work_hours, default_task_duration, break_interval, clamp_to_now — default True, prevents placement in the past).", "required": False},
            },
            callable=day_planner,
            requires=["obsidian"],
            mutates_state=True,
        ),
        Capability(
            name="vault_write_at_location",
            description=(
                "Insert content at a specific section in a vault note. "
                "Configurable note (path or resolver like 'latest_journal', 'today'), "
                "section (header text), and position ('top' or 'bottom' of section). "
                "Used by Telegram capture and general-purpose vault writing."
            ),
            category="journal",
            search_aliases=[
                "write at location", "vault write", "section write",
                "append to section", "insert into note", "capture",
            ],
            parameters={
                "content": {"type": "str", "description": "Text to insert", "required": True},
                "note": {"type": "str", "description": "Note path or resolver: 'latest_journal' (default), 'today', or explicit vault-relative path", "required": False},
                "section": {"type": "str", "description": "Header text identifying target section (default: 'Running Notes')", "required": False},
                "position": {"type": "str", "description": "'top' (default) or 'bottom' of section", "required": False},
                "source": {"type": "str", "description": "Source metadata tag (e.g. 'telegram') — appended as #wb/capture/<source>", "required": False},
            },
            callable=lambda **kw: __import__("work_buddy.obsidian.vault_writer", fromlist=["write_at_location"]).write_at_location(**kw),
            requires=["obsidian"],
            mutates_state=True,
            # ``verify_first`` so the sidecar retry sweep re-invokes this
            # capability from scratch on transient failure. Critical for
            # the EditorConflict path: each retry must re-read the file +
            # re-find the section + re-splice. Retrying the same payload
            # at a lower layer would clobber any typing the user managed
            # to auto-save during the wait. Trade-off: a mid-write TCP
            # timeout that *did* persist server-side would duplicate the
            # insert on retry. Low-probability vs. the high-probability
            # silent-clobber case we're avoiding.
            retry_policy="verify_first",
        ),
        Capability(
            name="obsidian_retry",
            description=(
                "Synchronous bridge-aware retry for Obsidian-dependent capabilities. "
                "Checks bridge health before each attempt, waits between retries, and "
                "returns a structured result. Use when you need the result before "
                "proceeding (e.g., step 1 of a multi-step task). For fire-and-forget "
                "retries, the gateway's automatic background retry handles it."
            ),
            category="obsidian",
            search_aliases=[
                "obsidian retry", "bridge retry", "retry with bridge",
                "retry task create", "bridge failure", "obsidian unavailable",
            ],
            parameters={
                "operation_id": {
                    "type": "str",
                    "required": True,
                    "description": (
                        "Operation ID from a previously failed or timed-out "
                        "call (included in wb_run/consent_request timeout "
                        "returns; visible via wb_status). Capability name "
                        "and params are loaded from the record, so the "
                        "agent does not re-supply them. If you don't have "
                        "an operation_id you don't need retry — just call "
                        "the capability directly; the gateway's automatic "
                        "background retry handles transient bridge hiccups."
                    ),
                },
                "max_retries": {
                    "type": "int",
                    "required": False,
                    "description": "Maximum number of attempts including the first (default: 3)",
                },
                "wait_seconds": {
                    "type": "int",
                    "required": False,
                    "description": "Seconds to wait between attempts (default: 60)",
                },
            },
            callable=lambda **kw: __import__("work_buddy.obsidian.retry", fromlist=["obsidian_retry"]).obsidian_retry(**kw),
            # INTENTIONALLY no ``requires=["obsidian"]``: this is the
            # one capability whose job is to ride out bridge outages.
            # Gating it on the bridge being up would short-circuit the
            # very recovery path it was built for — agents hitting a
            # bridge failure would then also hit "obsidian_retry is
            # unavailable" and have no escape hatch. The inner retry
            # loop health-checks the bridge between attempts itself.
            retry_policy="manual",
        ),
        # ── Background triage producer (journal) ────────────────
        Capability(
            name="journal_triage_scan",
            description=(
                "Run one background-triage pass over today's Running Notes "
                "section. Segments the same-day content into threads via "
                "the local LLM, enriches each with hybrid-IR context, and "
                "(when triage.verdict_pass.enabled=true) asks Sonnet "
                "(escalating to Opus on failure) for a constrained-JSON "
                "verdict per thread. Slice 1 default is verdict_pass off — "
                "captures land in the pool as raw entries (verdict={raw: "
                "true}) until Slice 3 brings GTD-shaped verdicts back. "
                "Never mutates the vault. Idempotent on unchanged content. "
                "Cadence is a sidecar-job concern; safe to call manually "
                "for testing."
            ),
            category="journal",
            search_aliases=[
                "journal triage scan",
                "background running notes triage",
                "process running notes proposals",
                "triage running notes",
            ],
            parameters={
                "journal_date": {"type": "str", "description": "YYYY-MM-DD. Default: today.", "required": False},
                "force": {"type": "bool", "description": "Ignore the unchanged-content idempotence gate.", "required": False},
                "profile": {"type": "str", "description": "Override the configured triage.segment_profile (segmentation LLM, not the agent).", "required": False},
                "tier": {"type": "str", "description": "Override the starting ModelTier for the agent (default frontier_balanced).", "required": False},
                "enrich": {"type": "bool", "description": "Pre-fetch hybrid-IR context per candidate (default True).", "required": False},
                "dry_run": {"type": "bool", "description": "Collect + enrich candidates but skip the agent loop.", "required": False},
            },
            callable=(lambda **kw: __import__(
                "work_buddy.clarify.capabilities.journal_triage_scan",
                fromlist=["journal_triage_scan"],
            ).journal_triage_scan(**kw)),
            requires=["obsidian"],
            mutates_state=True,
            # Producer uses LLMRunner's escalation internally — retries
            # at the tier level on timeout / context-exceeded. No outer
            # retry-on-timeout needed (would just stack queued passes).
            auto_retry=False,
        ),
        # NOTE: ``threads_v5_seed_test_data`` was a temporary
        # scaffolding capability used during Stage 4 to populate the
        # Threads tab with fake data. It was removed as part of the
        # autonomy implementation cleanup — real journal/Chrome
        # source pipelines now produce live threads. The
        # ``seed_test_data`` Python module remains in
        # work_buddy/threads/ for any future debugging needs but is
        # no longer registered as a callable capability.

        # The legacy ``journal_v5_scan`` capability is gone — the
        # canonical entry point is now ``run_source_pipeline`` (see
        # ``_pipeline_capabilities()``), which dispatches to
        # ``JournalBacklogPipeline`` end-to-end. The unified pipeline
        # subsumes segmentation + manifest tagging + clustering + LLM
        # refinement + per-cluster action proposals into one call.

        # ── Inline-selection triage producer ────────────────────
        Capability(
            name="inline_triage_scan",
            description=(
                "Run one triage pass over a single user-sent Obsidian "
                "selection (from the 'Send to agent' right-click or tag). "
                "Builds one TriageItem with source='inline'. When "
                "triage.verdict_pass.enabled=true, collects the "
                "user-context packet and asks Sonnet for a constrained-JSON "
                "verdict (escalates to Opus on timeout / context-exceeded / "
                "empty content / rate-limited). Slice 1 default is "
                "verdict_pass off — captures land in the pool as raw "
                "entries (verdict={raw: true}) until Slice 3 brings the "
                "GTD-shaped multi-record schema back. User-initiated, "
                "so force=True by default."
            ),
            category="triage",
            search_aliases=[
                "send to agent",
                "inline selection triage",
                "triage selection",
                "obsidian selection handoff",
            ],
            parameters={
                "file_path": {"type": "str", "description": "Vault-relative source path.", "required": True},
                "selection": {"type": "str", "description": "The user's literal selection.", "required": False},
                "paragraph": {"type": "str", "description": "Surrounding paragraph (used when selection is empty).", "required": False},
                "cursor_line": {"type": "int", "description": "0-indexed cursor line.", "required": False},
                "hint": {"type": "str", "description": "Optional user-typed intent hint.", "required": False},
                "force": {"type": "bool", "description": "Bypass idempotence gate (default True for user-initiated).", "required": False},
                "tier": {"type": "str", "description": "Override the starting ModelTier (default frontier_balanced).", "required": False},
                "enrich": {"type": "bool", "description": "Include the user-context packet (default True).", "required": False},
                "dry_run": {"type": "bool", "description": "Collect the item, skip the LLM call.", "required": False},
            },
            callable=(lambda **kw: __import__(
                "work_buddy.clarify.capabilities.inline_triage_scan",
                fromlist=["inline_triage_scan"],
            ).inline_triage_scan(**kw)),
            requires=["obsidian"],
            mutates_state=True,
            auto_retry=False,
        ),
        # ----------------------------------------------------------------
        # Journal-backlog per-thread actions — backing the journal
        # pipeline's per-group action library. Each walks the target
        # thread's ``context_items`` and routes through the existing
        # ``journal_backlog/route.py`` primitives (consent-gated).
        # ----------------------------------------------------------------
        Capability(
            name="journal_route_to_tasks",
            description=(
                "Walk a journal-group thread's context items and create "
                "one task per item in the master task list. Each item's "
                "label becomes the task text. Continue-on-error: a single "
                "failed item doesn't block the rest."
            ),
            category="journal",
            is_action=True,
            intrinsic_amplifiers={
                "irreversibility": "low",
                "regret_potential": "low",
            },
            parameters={
                "thread_id": {"type": "str", "description": "Group sub-thread to route", "required": True},
                "vault_root": {"type": "str", "description": "Override the configured vault root", "required": False},
                "urgency": {"type": "str", "description": "low | medium (default) | high", "required": False},
                "project": {"type": "str", "description": "Project slug applied to every created task", "required": False},
            },
            callable=(lambda **kw: __import__(
                "work_buddy.journal_backlog.thread_actions",
                fromlist=["journal_route_to_tasks"],
            ).journal_route_to_tasks(**kw)),
            requires=["obsidian"],
            mutates_state=True,
            search_aliases=[
                "create tasks from journal group",
                "route group to task list",
                "spin out group as tasks",
            ],
        ),
        Capability(
            name="journal_route_to_considerations",
            description=(
                "Walk a journal-group thread's context items and create "
                "one consideration note per item. Each item's label "
                "becomes the title; raw text becomes the body."
            ),
            category="journal",
            is_action=True,
            intrinsic_amplifiers={
                "irreversibility": "low",
                "regret_potential": "low",
            },
            parameters={
                "thread_id": {"type": "str", "description": "Group sub-thread to route", "required": True},
                "vault_root": {"type": "str", "description": "Override the configured vault root", "required": False},
                "project": {"type": "str", "description": "Project slug for all new considerations (default 'inbox')", "required": False},
                "type": {"type": "str", "description": "Consideration type (default 'consideration')", "required": False},
                "status": {"type": "str", "description": "Initial status (default 'open')", "required": False},
            },
            callable=(lambda **kw: __import__(
                "work_buddy.journal_backlog.thread_actions",
                fromlist=["journal_route_to_considerations"],
            ).journal_route_to_considerations(**kw)),
            requires=["obsidian"],
            mutates_state=True,
            search_aliases=[
                "create considerations from journal group",
                "route group to considerations",
            ],
        ),
        Capability(
            name="journal_append_to_note",
            description=(
                "Append all items in a journal-group thread as bullets "
                "to a single existing vault note. Useful for "
                "project-observation clusters."
            ),
            category="journal",
            is_action=True,
            intrinsic_amplifiers={
                "irreversibility": "low",
                "regret_potential": "low",
            },
            parameters={
                "thread_id": {"type": "str", "description": "Group sub-thread to route", "required": True},
                "note_path": {"type": "str", "description": "Vault-relative note to append to", "required": True},
                "vault_root": {"type": "str", "description": "Override the configured vault root", "required": False},
                "bullet_prefix": {"type": "str", "description": "Bullet marker (default '- ')", "required": False},
            },
            callable=(lambda **kw: __import__(
                "work_buddy.journal_backlog.thread_actions",
                fromlist=["journal_append_to_note"],
            ).journal_append_to_note(**kw)),
            requires=["obsidian"],
            mutates_state=True,
            search_aliases=[
                "append journal group to note",
                "log group items to project note",
            ],
        ),
        Capability(
            name="journal_rewrite_running_notes",
            description=(
                "Remove processed lines from today's daily note. "
                "Consent-gated wrapper around "
                "``journal_backlog.rewrite_running_notes``. Umbrella-"
                "level cleanup: typically run after all the umbrella's "
                "groups have been routed."
            ),
            category="journal",
            is_action=True,
            intrinsic_amplifiers={
                "irreversibility": "moderate",
                "regret_potential": "moderate",
            },
            parameters={
                "preview": {"type": "dict", "description": "Output of build_rewrite_preview", "required": True},
                "vault_root": {"type": "str", "description": "Override the configured vault root", "required": False},
            },
            callable=(lambda **kw: __import__(
                "work_buddy.journal_backlog.rewrite",
                fromlist=["rewrite_running_notes"],
            ).rewrite_running_notes(**kw)),
            requires=["obsidian"],
            mutates_state=True,
            search_aliases=[
                "rewrite daily note",
                "remove processed journal lines",
                "clean up running notes",
            ],
        ),
    ]


def _memory_capabilities() -> list[Capability]:
    from work_buddy.memory import (
        reflect_on_query,
        retain_personal_note,
    )
    from work_buddy.memory.query import memory_read, prune_memories

    return [
        Capability(
            name="memory_read",
            description=(
                "Read from personal memory (Hindsight). No LLM cost. "
                "Modes: 'search' (default) — semantic + keyword recall, "
                "use descriptive topic phrases with specific entity names "
                "for best results; 'model' — fetch a mental model by ID; "
                "'recent' — list latest memories."
            ),
            category="memory",
            parameters={
                "query": {
                    "type": "str",
                    "description": (
                        "Descriptive topic phrase for search mode. Use specific "
                        "terminology and entity names (e.g. named work-pattern "
                        "vocabulary) rather than generic labels like 'blindspots'. "
                        "Ignored for model/recent modes."
                    ),
                    "required": False,
                },
                "mode": {
                    "type": "str",
                    "description": (
                        "search (default) — semantic + keyword recall. "
                        "model — fetch a mental model (self-profile, work-patterns, "
                        "blindspots, preferences, current-constraints). "
                        "recent — list N most recent memories."
                    ),
                    "required": False,
                },
                "model_id": {
                    "type": "str",
                    "description": "Mental model ID for mode=model (default: self-profile)",
                    "required": False,
                },
                "limit": {
                    "type": "int",
                    "description": "Max memories for mode=recent (default 20)",
                    "required": False,
                },
                "budget": {
                    "type": "str",
                    "description": "Retrieval depth for mode=search: low (fast, default), mid, high",
                    "required": False,
                },
            },
            callable=memory_read,
            requires=["hindsight"],
            search_aliases=[
                "what do I remember",
                "recall memory",
                "search hindsight",
                "retrieve memories",
                "personal memory search",
                "remember what I told you",
                "recall my preferences",
                "search personal memory",
            ],
        ),
        Capability(
            name="memory_write",
            description="Store a personal fact, preference, or constraint in memory",
            category="memory",
            parameters={
                "content": {"type": "str", "description": "The fact or preference to remember", "required": True},
                "kind": {"type": "str", "description": "Memory kind: preference, habit, constraint, blindspot, relationship, decision, life-context (default preference)", "required": False},
                "domain": {"type": "str", "description": "Domain: work, life, health (default life)", "required": False},
            },
            callable=retain_personal_note,
            requires=["hindsight"],
            search_aliases=[
                "remember this",
                "save to memory",
                "store a preference",
                "add memory",
                "record fact",
                "save to hindsight",
                "memorize this",
            ],
        ),
        Capability(
            name="memory_reflect",
            description=(
                "LLM-powered reasoning over memories. CONSENT-GATED: triggers "
                "a server-side LLM call against your Anthropic API key (~1-3K "
                "tokens per call). Use memory_read for free retrieval first."
            ),
            category="memory",
            parameters={
                "query": {"type": "str", "description": "Question to reason about using memory", "required": True},
                "budget": {"type": "str", "description": "Retrieval depth: low (default), mid, high", "required": False},
            },
            callable=reflect_on_query,
            requires=["hindsight"],
            consent_operations=["memory_reflect"],
            search_aliases=[
                "reason about memories",
                "analyze my memories",
                "LLM reflection on memory",
                "think about memories",
                "memory synthesis",
                "synthesize from memory",
            ],
        ),
        Capability(
            name="memory_prune",
            description=(
                "Delete memories from the bank. CONSENT-GATED, IRREVERSIBLE. "
                "Call with no args to list documents for review. Then provide "
                "document_id to delete a specific document's memories, or "
                "memory_type to bulk-delete a category (world/experience/observation)."
            ),
            category="memory",
            parameters={
                "document_id": {
                    "type": "str",
                    "description": "Delete a specific document and its derived memories",
                    "required": False,
                },
                "memory_type": {
                    "type": "str",
                    "description": "Bulk delete by type: world, experience, or observation",
                    "required": False,
                },
            },
            callable=prune_memories,
            requires=["hindsight"],
            search_aliases=[
                "forget memories",
                "delete memory bank",
                "clear memory",
                "remove memories",
                "prune hindsight",
                "wipe memories",
            ],
        ),
    ]


def _task_capabilities() -> list[Capability]:
    from work_buddy.obsidian.tasks import (
        daily_briefing,
        review_inbox,
        stale_check,
        task_search,
        update_task,
        archive_completed,
        weekly_review_data,
    )
    from work_buddy.obsidian.tasks.mutations import (
        assign_task,
        create_task,
        create_task_effects_resolver as _create_task_effects_resolver,
        delete_task,
        read_task,
        set_task_tags_on_line,
        toggle_task,
        update_task_description,
    )
    from work_buddy.obsidian.effects import EffectSpec as _EffectSpec
    from work_buddy.obsidian.tasks.sync import task_sync
    from work_buddy.obsidian.tasks.namespace_suggest import (
        namespace_lookup,
        task_namespace_suggest,
    )
    from work_buddy.contracts import get_constraints, check_wip_limit
    from work_buddy.mcp_server.context_wrappers import task_scattered

    return [
        Capability(
            name="task_briefing",
            description="Daily task status summary with contract constraints, MITs, focused, overdue, stale, suggestions",
            category="tasks",
            parameters={},
            callable=daily_briefing,
            requires=["obsidian"],
            search_aliases=[
                "what do I need to do today",
                "daily task summary",
                "my tasks overview",
                "today's tasks",
                "MITs and focused tasks",
                "task dashboard",
                "daily planning overview",
                "my current work status",
            ],
        ),
        Capability(
            name="task_review_inbox",
            description="Get inbox tasks with suggested actions (mit, snooze, kill, needs_date)",
            category="tasks",
            parameters={},
            callable=review_inbox,
            requires=["obsidian"],
            search_aliases=[
                "review new tasks",
                "inbox triage",
                "undecided tasks",
                "new task review",
                "inbox items",
                "what's in my inbox",
                "decide on new tasks",
            ],
        ),
        Capability(
            name="task_stale_check",
            description="Find forgotten/stale tasks across inbox, snoozed, MIT, and focused",
            category="tasks",
            parameters={},
            callable=stale_check,
            requires=["obsidian"],
            search_aliases=[
                "forgotten tasks",
                "stale todos",
                "neglected tasks",
                "tasks I haven't touched",
                "what tasks are rotting",
                "dormant tasks",
                "tasks going stale",
            ],
        ),
        Capability(
            name="task_create",
            description=(
                "Create a new task in the master task list. Optionally attach "
                "a note file for details/subtasks. Slice 2 GTD vocabulary "
                "(task_kind, density, outcome_text, next_action_text, "
                "definition_of_done, creation_effort, user_involvement, "
                "creation_provenance, deadline, dependency) is optional and "
                "defaults to 'looks like a legacy manually-authored task'. "
                "Agent-driven creators should set creation_provenance "
                "(e.g. 'agent_inferred_from_journal') and lower "
                "user_involvement."
            ),
            category="tasks",
            # v5: this is the canonical Standard Action for "create a
            # new task". Without this flag the action-inference catalog
            # is empty and the agent falls back to improvised/suggestion
            # plans for what should be a one-call task creation.
            is_action=True,
            intrinsic_amplifiers={
                "irreversibility": "low",
                "regret_potential": "low",
            },
            parameters={
                "task_text": {"type": "str", "description": "Short single-line task description (NO newlines — will be rejected)", "required": True},
                "urgency": {"type": "str", "description": "Urgency: low, medium (default), high", "required": False},
                "project": {"type": "str", "description": "Project slug (added as #projects/<slug>)", "required": False},
                "due_date": {"type": "str", "description": "Due date as YYYY-MM-DD", "required": False},
                "contract": {"type": "str", "description": "Contract slug this task serves", "required": False},
                "summary": {"type": "str", "description": "If provided, creates a linked note file with this summary", "required": False},
                "tags": {"type": "list[str]", "description": "Namespace tags (no leading '#'), e.g. ['paper/ecg-classifier', 'experiment/augmentation']. Appended to the task line; picked up into the tag cache on next task_sync.", "required": False},
                # Slice 2 GTD vocabulary -----------------------------------
                "task_kind": {"type": "str", "description": "Slice 2: 'task' (default) | 'periodic' | 'habit'.", "required": False},
                "density": {"type": "str", "description": "Slice 2: 'sparse' (default) | 'developed'. ('dense' is forward-compat for Slice 7+.)", "required": False},
                "outcome_text": {"type": "str", "description": "Slice 2: desired end-state for developed tasks (e.g. 'ETF tracking habit running').", "required": False},
                "next_action_text": {"type": "str", "description": "Slice 2: specific physical action for developed tasks (e.g. 'Set up weekly cron job').", "required": False},
                "definition_of_done": {"type": "str", "description": "Slice 2: closing signal for the task.", "required": False},
                "creation_effort": {"type": "str", "description": "Slice 2: 'sparse' | 'medium' | 'developed' (default — assumes manual creation).", "required": False},
                "user_involvement": {"type": "str", "description": "Slice 2: 'low' | 'medium' | 'high' (default — assumes manual creation).", "required": False},
                "creation_provenance": {"type": "str", "description": "Slice 2: 'manual' (default) | 'agent_inferred_from_journal' | 'agent_inferred_from_chrome' | 'agent_inferred_from_inline' | other.", "required": False},
                "has_deadline": {"type": "bool", "description": "Slice 2: True when deadline_date is set; signal for deadline-aware resurfacing in Slice 8.", "required": False},
                "deadline_date": {"type": "str", "description": "Slice 2: ISO date YYYY-MM-DD when has_deadline=True.", "required": False},
                "has_dependency": {"type": "bool", "description": "Slice 2: True when this task is blocked on someone or something.", "required": False},
                "dependency_hint": {"type": "str", "description": "Slice 2: free-text hint about the dependency (e.g. 'needs Ben’s review').", "required": False},
            },
            callable=create_task,
            search_aliases=["new task", "add task", "create todo", "add todo"],
            requires=["obsidian"],
            mutates_state=True,
            retry_policy="verify_first",
            consent_operations=["tasks.create_task", "obsidian.write_file"],
            # Fix-(b): multi-effect manifest. ``task_create`` writes a
            # note file (if summary provided) AND appends a line to
            # the master task list. Without this manifest, the PWU
            # verifier sees only the path on the exception (whichever
            # write happened first) and could declare "verified" while
            # the second effect is missing. Resolver pulls task_id /
            # note_uuid from the C.2 idempotency cache.
            effects=[
                _EffectSpec(
                    kind="file_write",
                    path_template="tasks/notes/{note_uuid}.md",
                    witness_template="{task_text}",
                    witness_mode="substring",
                    resolver=_create_task_effects_resolver,
                ),
                _EffectSpec(
                    kind="line_append",
                    path="tasks/master-task-list.md",
                    witness_template="🆔 {task_id}",
                    witness_mode="substring",
                    resolver=_create_task_effects_resolver,
                ),
            ],
        ),
        Capability(
            name="task_set_tags",
            description="Replace the namespace tags on an existing task's master-list line. Preserves #todo, #projects/*, wikilinks, and plugin emojis.",
            category="tasks",
            parameters={
                "task_id": {"type": "str", "description": "Task ID (e.g., 't-a3f8c1e2')", "required": True},
                "namespace_tags": {"type": "list[str]", "description": "Replacement list of namespace tags (no leading '#'). An empty list strips all user namespace tags.", "required": True},
            },
            callable=set_task_tags_on_line,
            search_aliases=[
                "tag task",
                "retag task",
                "add namespace to task",
                "remove tag from task",
                "set task namespace",
            ],
            requires=["obsidian"],
            mutates_state=True,
            retry_policy="verify_first",
            consent_operations=["tasks.update_task", "obsidian.write_file"],
        ),
        Capability(
            name="task_namespace_suggest",
            description="Rank existing namespace tags by relevance to a task text (hybrid BM25+embedding via the shared embedding service; falls back to token overlap). Returns ranked candidates from the existing universe only — it does not propose new namespaces. The calling agent decides whether to apply suggestions, add more, or mint a new namespace.",
            category="tasks",
            parameters={
                "task_text": {"type": "str", "description": "The task description to score against", "required": True},
                "contract": {"type": "str", "description": "Optional contract slug for boosting related namespaces", "required": False},
                "project": {"type": "str", "description": "Optional project slug for boosting related namespaces", "required": False},
                "limit": {"type": "int", "description": "Max suggestions (default 3)", "required": False},
            },
            callable=task_namespace_suggest,
            search_aliases=[
                "suggest task tags",
                "propose namespace",
                "which namespace fits this task",
                "tag suggestion",
            ],
            requires=[],
        ),
        Capability(
            name="namespace_lookup",
            description="Return the closest existing namespace tags to a single query. Designed for the 'did you mean?' check before minting a brand-new namespace — the agent calls this to confirm a proposed namespace isn't a near-duplicate of something that already exists.",
            category="tasks",
            parameters={
                "query": {"type": "str", "description": "Candidate namespace path or label (with or without leading '#')", "required": True},
                "limit": {"type": "int", "description": "Max results (default 5)", "required": False},
            },
            callable=namespace_lookup,
            search_aliases=[
                "did you mean namespace",
                "nearest namespace",
                "check namespace exists",
                "namespace duplicate check",
            ],
            requires=[],
        ),
        Capability(
            name="task_assign",
            description="Claim a task for the current session and get full context (text, note, metadata)",
            category="tasks",
            parameters={
                "task_id": {"type": "str", "description": "Task ID (e.g., 't-a3f8c1e2')", "required": True},
            },
            callable=assign_task,
            search_aliases=["assign task", "claim task", "work on task", "start task"],
            requires=["obsidian"],
        ),
        Capability(
            name="task_read",
            description="Read a task's full context (text, note, metadata) without claiming it for the current session",
            category="tasks",
            parameters={
                "task_id": {"type": "str", "description": "Task ID (e.g., 't-a3f8c1e2')", "required": True},
            },
            callable=read_task,
            search_aliases=["read task", "view task", "show task", "inspect task", "look at task"],
            requires=["obsidian"],
        ),
        Capability(
            name="task_toggle",
            description="Mark a task complete, incomplete, or toggle. Handles checkbox, done date, and store state atomically. Use done=true to complete, done=false to reopen, omit to toggle. Consent-gated.",
            category="tasks",
            parameters={
                "task_id": {"type": "str", "description": "Task ID (e.g., 't-a3f8c1e2')", "required": True},
                "done": {"type": "bool", "description": "True=complete, False=incomplete, omit=toggle", "required": False},
            },
            callable=toggle_task,
            search_aliases=["finish task", "done task", "complete todo", "mark done", "uncomplete task", "reopen task"],
            requires=["obsidian"],
            mutates_state=True,
            retry_policy="verify_first",
            consent_operations=["tasks.toggle_task", "obsidian.write_file"],
        ),
        Capability(
            name="task_delete",
            description="Permanently delete a task: remove line, note file, and store record. Consent-gated.",
            category="tasks",
            parameters={
                "task_id": {"type": "str", "description": "Task ID (e.g., 't-a3f8c1e2')", "required": True},
            },
            callable=delete_task,
            search_aliases=[
                "remove task",
                "delete todo",
                "destroy task",
                "permanently delete task",
                "get rid of task",
                "erase todo",
                "drop task",
            ],
            requires=["obsidian"],
            mutates_state=True,
            retry_policy="manual",
            consent_operations=["tasks.delete_task", "obsidian.write_file", "obsidian.eval_js"],
        ),
        Capability(
            name="task_change_state",
            description="Update task metadata: state (not completion), urgency, due date. Cannot set state='done' — use task_toggle for completion.",
            category="tasks",
            parameters={
                "task_id": {"type": "str", "description": "Task ID (e.g., 't-a3f8c1e2')", "required": False},
                "description_match": {"type": "str", "description": "Description substring (fallback)", "required": False},
                "state": {"type": "str", "description": "New state: inbox, mit, focused, snoozed. NOT done — use task_toggle.", "required": False},
                "urgency": {"type": "str", "description": "New urgency: low, medium, high", "required": False},
                "due_date": {"type": "str", "description": "Due date as YYYY-MM-DD", "required": False},
            },
            callable=update_task,
            requires=["obsidian"],
            mutates_state=True,
            retry_policy="verify_first",
            consent_operations=["tasks.update_task", "obsidian.write_file"],
            search_aliases=[
                "change task state",
                "mit this task",
                "focus a task",
                "snooze a task",
                "update task urgency",
                "change due date",
                "promote task to MIT",
                "move task to inbox",
            ],
        ),
        Capability(
            name="task_update_description",
            description=(
                "Rewrite the description text on a task line. Preserves "
                "checkbox, #todo, #projects/*, namespace tags, wikilinks, "
                "🆔 + ID, plugin emojis (📅, ✅, urgency). Updates the "
                "store's description column in lockstep. Use this instead "
                "of filesystem-direct edits — it routes through the same "
                "consent-aware, retry-aware path as the other mutations "
                "and avoids the read-modify-write race on the master "
                "task list."
            ),
            category="tasks",
            parameters={
                "task_id": {"type": "str", "description": "Task ID (e.g., 't-a3f8c1e2')", "required": True},
                "new_description": {"type": "str", "description": "New description text. Single line; whitespace is collapsed.", "required": True},
                "file_path": {"type": "str", "description": "Vault-relative path. Default: tasks/master-task-list.md", "required": False},
            },
            callable=update_task_description,
            requires=["obsidian"],
            mutates_state=True,
            retry_policy="verify_first",
            consent_operations=["tasks.update_task", "obsidian.write_file"],
            search_aliases=[
                "rename task",
                "rewrite task",
                "edit task description",
                "change task text",
                "update task wording",
                "rephrase task",
                "rewrite task line",
            ],
        ),
        Capability(
            name="task_search",
            description=(
                "Search tasks by description text via the SQLite store. "
                "Bridge-independent — works even when Obsidian isn't "
                "running. Returns task records (full task_metadata "
                "rows) ordered most-recently-updated first. For "
                "full-text search over task NOTE bodies (the "
                "[[uuid|📓]]-linked detail files), use "
                "context_search(source='task_note') instead — that's "
                "hybrid retrieval over note content; this is exact-text "
                "search over the line description."
            ),
            category="tasks",
            parameters={
                "query": {"type": "str", "description": "Substring to search for in task description text. Empty string returns nothing.", "required": True},
                "limit": {"type": "int", "description": "Max results (default 50)", "required": False},
                "include_archived": {"type": "bool", "description": "Include archived tasks (default False)", "required": False},
                "include_done": {"type": "bool", "description": "Include completed tasks (default True)", "required": False},
            },
            callable=task_search,
            requires=[],  # SQLite-only — no bridge needed
            search_aliases=[
                "find task",
                "search tasks",
                "find a task by name",
                "look up task",
                "task by description",
                "tasks containing",
                "search task descriptions",
            ],
        ),
        Capability(
            name="task_archive",
            description="Move completed tasks from master list to tasks/archive.md",
            category="tasks",
            parameters={
                "older_than_days": {"type": "int", "description": "Only archive tasks done N+ days ago (default 0 = all)", "required": False},
            },
            callable=archive_completed,
            requires=["obsidian"],
            consent_operations=["tasks.archive", "obsidian.write_file"],
            search_aliases=[
                "archive done tasks",
                "clean up completed tasks",
                "move completed to archive",
                "task cleanup",
                "archive old tasks",
                "tidy task list",
            ],
        ),
        Capability(
            name="weekly_review_data",
            description="Gather all data for the weekly review: contracts, constraints, WIP, tasks, staleness, suggestions",
            category="tasks",
            parameters={},
            callable=weekly_review_data,
            requires=["obsidian"],
            search_aliases=[
                "weekly review data",
                "weekly planning data",
                "strategic review input",
                "gather weekly state",
                "weekly MIT data",
                "prepare weekly review",
            ],
        ),
        Capability(
            name="task_sync",
            description="Compare master task list against SQLite store: detect orphans, create missing store records, report checkbox mismatches",
            category="tasks",
            parameters={},
            callable=task_sync,
            search_aliases=["sync tasks", "reconcile tasks", "task discrepancy", "task watcher"],
            requires=["obsidian"],
        ),
        Capability(
            name="task_scattered",
            description="Find open tasks scattered across the vault outside the master task list. Groups by file with counts. Uses Datacore structural queries.",
            category="tasks",
            parameters={
                "limit": {"type": "int", "description": "Max tasks to scan (default 100)", "required": False},
            },
            callable=task_scattered,
            search_aliases=[
                "scattered tasks",
                "orphan tasks",
                "tasks outside master list",
                "forgotten tasks",
                "tasks in journal",
                "tasks in docs",
            ],
            requires=["obsidian"],
        ),
        Capability(
            name="contract_constraints",
            description="Get active contracts with their current bottleneck constraints",
            category="contracts",
            parameters={},
            callable=get_constraints,
            requires=["obsidian"],
            search_aliases=[
                "what's blocking contracts",
                "contract bottlenecks",
                "constraints on active work",
                "contract blockers",
                "where are contracts stuck",
                "blocking issues per contract",
            ],
        ),
        Capability(
            name="contract_wip_check",
            description="Check if active contract count is within the WIP limit (max 3)",
            category="contracts",
            parameters={},
            callable=check_wip_limit,
            requires=["obsidian"],
            search_aliases=[
                "am I overcommitted",
                "work in progress limit",
                "WIP check",
                "too many contracts",
                "how many active commitments",
                "over WIP",
            ],
        ),
    ]


def _pipeline_capabilities() -> list[Capability]:
    """The unified ``run_source_pipeline`` entry point.

    Replaces the per-source ``journal_v5_scan`` capability and serves
    as the single MCP/wb_run entry for triggering any source-pipeline
    run (Chrome, journal, future). Slash commands +
    ``daily-journal/process-backlog`` workflow dispatch through this.
    """
    from work_buddy.pipelines.capability import run_source_pipeline

    return [
        Capability(
            name="run_source_pipeline",
            description=(
                "Run an end-to-end source pipeline: collect raw items, "
                "annotate with LLM tags + summary, embedding-cluster, "
                "Sonnet-refine cluster boundaries + per-cluster action "
                "proposals, and spawn a group umbrella thread + group "
                "sub-threads with the items as ContextItems. Replaces "
                "the per-source journal/chrome scan entry points."
            ),
            category="threads",
            parameters={
                "source": {"type": "str", "description": "Registered pipeline name (e.g. 'chrome_triage', 'journal_backlog')", "required": True},
                "journal_date": {"type": "str", "description": "Journal pipeline: YYYY-MM-DD; defaults to today", "required": False},
                "profile": {"type": "str", "description": "Journal pipeline: override segmentation tier profile", "required": False},
                "engagement_window": {"type": "str", "description": "Chrome pipeline: engagement lookback (e.g. '12h', '24h')", "required": False},
                "include_summaries": {"type": "bool", "description": "Chrome pipeline: attach cached Haiku summaries (default True)", "required": False},
                "summary": {"type": "str", "description": "Chrome pipeline: human-readable scrape summary for the umbrella title", "required": False},
                "scrape_id": {"type": "str", "description": "Chrome pipeline: per-scrape id surfaced in audit metadata", "required": False},
            },
            callable=run_source_pipeline,
            mutates_state=True,
            search_aliases=[
                "run pipeline",
                "process backlog",
                "scan journal",
                "triage chrome",
                "run source pipeline",
                "spawn group threads",
            ],
        ),
    ]


def _thread_capabilities() -> list[Capability]:
    """Universal thread actions — apply to any thread regardless of
    source. Wired into the per-source action library by the pipelines
    runner so every group sub-thread carries dismiss / defer / rename
    affordances on its action chip alongside any source-specific
    actions.
    """
    from work_buddy.threads.universal_actions import (
        thread_defer,
        thread_dismiss,
        thread_rename,
    )

    return [
        Capability(
            name="thread_dismiss",
            description=(
                "Mark a thread as dismissed via the standard FSM "
                "transition. For group sub-threads this is the "
                "'do nothing with this cluster' action. For umbrellas "
                "it cascades through the existing dismiss flow."
            ),
            category="threads",
            is_action=True,
            intrinsic_amplifiers={
                "irreversibility": "low",
                "regret_potential": "low",
            },
            parameters={
                "thread_id": {"type": "str", "description": "Thread to dismiss", "required": True},
                "reason": {"type": "str", "description": "Optional free-text reason recorded on the dismiss event", "required": False},
            },
            callable=thread_dismiss,
            mutates_state=True,
            search_aliases=["dismiss thread", "skip thread", "ignore thread", "drop thread"],
        ),
        Capability(
            name="thread_defer",
            description=(
                "Defer a thread so it resurfaces at a future time. "
                "Sets the cached resurface_at field; the existing Later "
                "mechanic re-surfaces the thread when the time arrives."
            ),
            category="threads",
            is_action=True,
            intrinsic_amplifiers={
                "irreversibility": "low",
                "regret_potential": "low",
            },
            parameters={
                "thread_id": {"type": "str", "description": "Thread to defer", "required": True},
                "duration_hours": {"type": "float", "description": "Hours to wait before resurfacing (default 24)", "required": False},
                "resurface_at": {"type": "str", "description": "Absolute ISO timestamp to resurface at (overrides duration_hours)", "required": False},
            },
            callable=thread_defer,
            mutates_state=True,
            search_aliases=["snooze thread", "later", "remind me later", "defer thread"],
        ),
        Capability(
            name="thread_rename",
            description=(
                "Rewrite a thread's title (and description) — used by "
                "the action-chip 'Rename' affordance and by the LLM "
                "cluster-refinement step when it overrides an "
                "algorithmic cluster label."
            ),
            category="threads",
            is_action=True,
            intrinsic_amplifiers={
                "irreversibility": "low",
                "regret_potential": "low",
            },
            parameters={
                "thread_id": {"type": "str", "description": "Thread to rename", "required": True},
                "new_title": {"type": "str", "description": "New title (also used as description)", "required": True},
            },
            callable=thread_rename,
            mutates_state=True,
            search_aliases=["rename thread", "change thread title", "edit cluster label"],
        ),
    ]


def _llm_capabilities() -> list[Capability]:
    """General-purpose LLM call capability.

    Uses ``work_buddy.llm.call.llm_call`` which wraps ``run_task()``.
    Both the ``anthropic`` SDK and ``httpx`` (used by the openai_compat
    backend) are pure Python — no C extensions — so they're safe in
    ``asyncio.to_thread()`` with no import-deadlock risk.
    """
    from work_buddy.llm.call import llm_call
    from work_buddy.llm.submit import llm_submit
    from work_buddy.llm.with_tools import llm_with_tools
    from work_buddy.llm.tool_presets import PRESETS as _LLM_TOOL_PRESETS

    # `llm_with_tools` lets a local model call any capability whitelisted
    # in either readonly_safe or readonly_context. For the control graph,
    # those names are transitively invoked — so union the preset contents
    # at registry-build time. Computed (not hand-authored) so drift is
    # impossible: edit a preset in tool_presets.py and the control graph
    # re-resolves automatically.
    _llm_with_tools_invokes = sorted(
        _LLM_TOOL_PRESETS.get("readonly_safe", frozenset())
        | _LLM_TOOL_PRESETS.get("readonly_context", frozenset())
    )

    return [
        Capability(
            name="llm_call",
            description=(
                "Make a single LLM API call (Tier 2 execution). Cheaper than "
                "spawning a full agent session. Supports freeform text or "
                "structured JSON output via output_schema (inline dict or "
                "named schema from work_buddy/llm/schemas/). Routes to Claude "
                "via 'tier' or to a local/remote OpenAI-compatible server "
                "(LM Studio, vLLM, Ollama) via 'profile'. Handles caching "
                "and cost tracking automatically."
            ),
            category="llm",
            search_aliases=[
                "api call",
                "llm call",
                "claude api",
                "structured output",
                "haiku call",
                "simple reasoning",
                "cheap llm",
                "classify",
                "analyze",
                "local llm",
                "lm studio",
                "qwen",
            ],
            parameters={
                "system": {
                    "type": "str",
                    "description": "System prompt",
                    "required": True,
                },
                "user": {
                    "type": "str",
                    "description": "User message content",
                    "required": True,
                },
                "output_schema": {
                    "type": "dict|str",
                    "description": (
                        "JSON Schema for structured output. Pass a dict for "
                        "inline schemas, or a string name to load from "
                        "work_buddy/llm/schemas/<name>.json. Omit for freeform text."
                    ),
                    "required": False,
                },
                "tier": {
                    "type": "str",
                    "description": (
                        "Cloud model tier: 'haiku' (default if no profile given), "
                        "'sonnet', or 'opus'. Mutually exclusive with 'profile'."
                    ),
                    "required": False,
                },
                "profile": {
                    "type": "str",
                    "description": (
                        "Named local/remote profile (e.g. 'local_general') "
                        "declared under llm.profiles in config. Routes through "
                        "the profile's backend instead of Anthropic. Mutually "
                        "exclusive with 'tier'."
                    ),
                    "required": False,
                },
                "max_tokens": {
                    "type": "int",
                    "description": "Max response tokens (default: 1024)",
                    "required": False,
                },
                "temperature": {
                    "type": "float",
                    "description": "Sampling temperature (default: 0.0)",
                    "required": False,
                },
                "cache_ttl_minutes": {
                    "type": "int",
                    "description": "Cache TTL in minutes. None=config default, 0=no cache.",
                    "required": False,
                },
            },
            callable=llm_call,
        ),
        Capability(
            name="llm_submit",
            description=(
                "Asynchronously submit an llm_call for background execution. "
                "Returns immediately with an operation_id; the sidecar's "
                "retry sweep invokes llm_call with your params and messages "
                "the originating session on completion. Use when local "
                "inference latency (tens of seconds) would block the caller "
                "unnecessarily. For synchronous bounded calls use llm_call. "
                "Cloud tier calls are already fast — no point submitting them; "
                "profile is therefore required."
            ),
            category="llm",
            search_aliases=[
                "async llm",
                "background llm",
                "queue llm call",
                "submit llm",
                "defer llm",
                "fire and forget",
                "autodream",
                "background inference",
            ],
            parameters={
                "system": {
                    "type": "str",
                    "description": "System prompt",
                    "required": True,
                },
                "user": {
                    "type": "str",
                    "description": "User message content",
                    "required": True,
                },
                "profile": {
                    "type": "str",
                    "description": (
                        "Named local/remote profile (e.g. 'local_general'). "
                        "Required — submits are for local profiles only."
                    ),
                    "required": True,
                },
                "output_schema": {
                    "type": "dict|str",
                    "description": (
                        "JSON Schema for structured output. Pass a dict for "
                        "inline schemas, or a string name to load from "
                        "work_buddy/llm/schemas/<name>.json. Omit for freeform."
                    ),
                    "required": False,
                },
                "max_tokens": {
                    "type": "int",
                    "description": "Max response tokens (default: 1024)",
                    "required": False,
                },
                "temperature": {
                    "type": "float",
                    "description": "Sampling temperature (default: 0.0)",
                    "required": False,
                },
                "cache_ttl_minutes": {
                    "type": "int",
                    "description": "Cache TTL in minutes. None=config default, 0=no cache.",
                    "required": False,
                },
            },
            callable=llm_submit,
            # Submit is already the async mechanism. Gateway-level retry
            # would double-queue and cause loops.
            auto_retry=False,
        ),
        Capability(
            name="llm_with_tools",
            description=(
                "Invoke a local model with restricted work-buddy MCP tool "
                "access, so it can look things up (projects, tasks, journal, "
                "context) while answering. Tool access is limited to a "
                "named preset defined in work_buddy/llm/tool_presets.py "
                "(currently: 'readonly_safe', 'readonly_context'). No "
                "arbitrary tool list accepted at call time — presets are "
                "the security boundary. Requires 'profile' and 'tool_preset'."
            ),
            category="llm",
            search_aliases=[
                "local llm with tools",
                "llm tool access",
                "mcp tools local",
                "contextualize local",
                "local model tools",
                "lm studio mcp",
                "qwen with tools",
                "tool use local",
            ],
            parameters={
                "system": {
                    "type": "str",
                    "description": "System prompt (becomes 'instructions' on the native chat request)",
                    "required": True,
                },
                "user": {
                    "type": "str",
                    "description": "User query (becomes 'input')",
                    "required": True,
                },
                "profile": {
                    "type": "str",
                    "description": "Named local profile (e.g., 'local_general') — must be LM Studio-backed",
                    "required": True,
                },
                "tool_preset": {
                    "type": "str",
                    "description": (
                        "Named whitelist of allowed work-buddy tools. "
                        "Currently: 'readonly_safe', 'readonly_context'. "
                        "Presets are code, not config — defined in "
                        "work_buddy/llm/tool_presets.py."
                    ),
                    "required": True,
                },
                "required_capabilities": {
                    "type": "list[str]",
                    "description": (
                        "Optional list of capability names the model "
                        "MUST be able to call (e.g. ['update-journal', "
                        "'journal_write']). Pre-flight checked against "
                        "the preset; if any are missing, the call "
                        "fails fast with an explicit error. Use this "
                        "to catch goal-preset mismatches — e.g. "
                        "running a workflow from a read-only preset "
                        "that doesn't include the workflow's name."
                    ),
                    "required": False,
                },
                "previous_response_id": {
                    "type": "str",
                    "description": "Continue a prior LM Studio stateful-chat turn",
                    "required": False,
                },
                "max_tokens": {
                    "type": "int",
                    "description": "Output budget. Default 4096 (tool-calling eats tokens).",
                    "required": False,
                },
                "temperature": {
                    "type": "float",
                    "description": "Sampling temperature (default 0.0)",
                    "required": False,
                },
                "store": {
                    "type": "bool",
                    "description": "Let LM Studio retain this turn server-side (default False)",
                    "required": False,
                },
                "persist_tool_results": {
                    "type": "bool",
                    "description": (
                        "When True, raw MCP tool outputs are saved to "
                        "the artifact store and the artifact id is "
                        "embedded in each tool_calls entry "
                        "(output_artifact_id). Default False — responses "
                        "contain only tool-call metadata, not raw "
                        "output. Errors auto-escalate to persist "
                        "regardless of this flag."
                    ),
                    "required": False,
                },
            },
            callable=llm_with_tools,
            invokes=_llm_with_tools_invokes,  # unioned from tool_presets at build time
            # Retrying a failed local-LLM tool call wastes tokens, spams
            # consent prompts (the model re-invokes tools on each replay),
            # and is unlikely to succeed (model hang ≠ network hiccup).
            # Failures should surface to the caller, not go to the queue.
            auto_retry=False,
        ),
        Capability(
            name="claude_code_usage_scan",
            description=(
                "Scan Claude Code's local transcript JSONLs into the cost "
                "cache (~/.claude/projects/**/*.jsonl). Incremental by default. "
                "Use full_rebuild=true after a pricing or schema change."
            ),
            category="llm",
            search_aliases=[
                "claude usage",
                "claude code usage",
                "transcript scan",
                "ingest claude code activity",
                "rescan costs",
            ],
            parameters={
                "full_rebuild": {
                    "type": "bool",
                    "description": "Drop and rebuild the cache (default false).",
                    "required": False,
                },
            },
            callable=_claude_code_usage_scan,
            mutates_state=True,
            auto_retry=False,
        ),
        Capability(
            name="llm_costs_query",
            description=(
                "Aggregate LLM cost / usage across one or both data sources "
                "(work-buddy's per-call internal log + Claude Code transcripts). "
                "Smart parameters: time window (named or ISO range), group_by "
                "(project, model, session, day, tool), source filter, "
                "min_cost / project / model filters, and previous-window "
                "comparison. Single capability covering most cost questions."
            ),
            category="llm",
            search_aliases=[
                "llm costs",
                "llm spend",
                "how much have i spent",
                "claude api costs",
                "cost by project",
                "cost by model",
                "weekly spend",
                "monthly spend",
                "expensive sessions",
                "burn rate",
                "cost trend",
                "spend comparison",
            ],
            parameters={
                "window": {
                    "type": "str",
                    "description": (
                        "Time window. Named: 'today', 'yesterday', '7d', "
                        "'30d', '90d', 'month_to_date', 'all'. ISO range: "
                        "'YYYY-MM-DD..YYYY-MM-DD'. Single day: 'YYYY-MM-DD'. "
                        "Default '30d'."
                    ),
                    "required": False,
                },
                "group_by": {
                    "type": "str",
                    "description": (
                        "Optional breakdown: 'project', 'model', 'session', "
                        "'day', 'tool'. Omit for top-line totals only."
                    ),
                    "required": False,
                },
                "source": {
                    "type": "str",
                    "description": (
                        "'all' (default), 'internal' (work-buddy's runner "
                        "calls), or 'claude_code' (Claude Code transcripts)."
                    ),
                    "required": False,
                },
                "min_cost": {
                    "type": "float",
                    "description": "Filter rows where cost_usd < this. Default 0.",
                    "required": False,
                },
                "project": {
                    "type": "str",
                    "description": "Substring match on project name / cwd.",
                    "required": False,
                },
                "model": {
                    "type": "str",
                    "description": "Exact-match model filter (e.g. 'claude-sonnet-4-6').",
                    "required": False,
                },
                "top_n": {
                    "type": "int",
                    "description": "Cap on grouped rows. Default 10. Pass 0 for no cap.",
                    "required": False,
                },
                "include_local": {
                    "type": "bool",
                    "description": "Include local-LLM rows? Default true.",
                    "required": False,
                },
                "compare_to_previous": {
                    "type": "bool",
                    "description": (
                        "Include previous-equivalent-window comparison "
                        "(delta_pct_cost, delta_pct_calls). Default true."
                    ),
                    "required": False,
                },
            },
            callable=_llm_costs_query,
        ),
        Capability(
            name="escalation_recent",
            description=(
                "Recent LLM-escalation observability records. Each record is "
                "one logical job (one LLMRunner.call OR one adapter-level "
                "escalation chain across multiple calls) with its full per-tier "
                "attempt list, final outcome, and trace correlation."
            ),
            category="llm",
            search_aliases=[
                "tier escalation",
                "escalation log",
                "what tier answered",
                "did this escalate",
                "fallback chain",
                "model fallback",
                "validation failed",
                "tier observability",
            ],
            parameters={
                "limit": {
                    "type": "int",
                    "description": "Max records to return (newest first). Default 50.",
                    "required": False,
                },
                "trace_id": {
                    "type": "str",
                    "description": "Filter by exact trace_id correlation token.",
                    "required": False,
                },
                "final_outcome": {
                    "type": "str",
                    "description": (
                        "Filter by final_outcome ('success', 'backend_error', "
                        "'validation_failed', 'empty_content', 'exhausted')."
                    ),
                    "required": False,
                },
                "source": {
                    "type": "str",
                    "description": (
                        "Filter by source ('llm_runner', 'journal_segmenter', "
                        "'verdict_call')."
                    ),
                    "required": False,
                },
                "summary": {
                    "type": "bool",
                    "description": (
                        "When true, return aggregate counts (by source / outcome / "
                        "final_tier, plus 'escalated_past_first') instead of records."
                    ),
                    "required": False,
                },
            },
            callable=_escalation_recent,
        ),
    ]


def _claude_code_usage_scan(*, full_rebuild: bool = False) -> dict[str, Any]:
    """Trigger the vendored Claude-Code-usage scanner."""
    from work_buddy.dashboard.costs_claude_code_usage import (
        rescan_claude_code_usage,
    )
    return rescan_claude_code_usage(full_rebuild=full_rebuild)


def _llm_costs_query(**kwargs: Any) -> dict[str, Any]:
    """Dispatch to the unified cost-query module."""
    from work_buddy.llm.cost_query import llm_costs_query
    return llm_costs_query(**kwargs)


def _escalation_recent(
    *,
    limit: int = 50,
    trace_id: str | None = None,
    final_outcome: str | None = None,
    source: str | None = None,
    summary: bool = False,
) -> dict[str, Any]:
    """Read recent records from the LLM-escalation log.

    See :mod:`work_buddy.llm.escalation_log` for the record shape.
    """
    from work_buddy.llm.escalation_log import (
        read_escalations,
        summarize_escalations,
    )
    if summary:
        return {
            "summary": summarize_escalations(limit=None),
            "applied_filters": {},
        }
    records = read_escalations(
        limit=limit,
        trace_id=trace_id,
        final_outcome=final_outcome,
        source=source,
    )
    return {
        "records": records,
        "count": len(records),
        "applied_filters": {
            "limit": limit,
            "trace_id": trace_id,
            "final_outcome": final_outcome,
            "source": source,
        },
    }


def _sidecar_capabilities() -> list[Capability]:
    """Sidecar status and service management capabilities.

    Status capabilities read sidecar_state.json.  ``service_restart``
    terminates a child service by PID; the sidecar supervisor detects
    the exit on its next health-check tick and auto-restarts it.
    """
    def _sidecar_status() -> dict:
        from work_buddy.sidecar.state import load_state
        state = load_state()
        if state is None:
            return {"running": False, "message": "Sidecar state file not found."}
        from work_buddy.sidecar.pid import check_existing_daemon
        alive = check_existing_daemon() is not None
        from dataclasses import asdict
        data = asdict(state)
        data["running"] = alive
        return data

    def _sidecar_jobs() -> dict:
        from work_buddy.sidecar.state import load_state
        state = load_state()
        if state is None:
            return {"jobs": [], "message": "Sidecar not running."}
        from dataclasses import asdict
        return {
            "jobs": [asdict(j) for j in state.jobs],
            "exclusion_active": state.exclusion_active,
        }

    def _service_restart(service: str) -> dict:
        """Kill a sidecar-managed service so the supervisor auto-restarts it.

        Reads the PID from sidecar_state.json and sends SIGTERM.  The
        sidecar's health-check loop detects the dead process within one
        tick (~30s) and starts a fresh instance — picking up any code
        changes made since the last launch.
        """
        import os
        import signal

        from work_buddy.sidecar.state import load_state

        state = load_state()
        if state is None:
            return {"success": False, "error": "Sidecar state file not found."}

        svc = state.services.get(service)
        if svc is None:
            available = list(state.services.keys())
            return {
                "success": False,
                "error": f"Unknown service '{service}'.",
                "available_services": available,
            }

        pid = svc.pid
        if not pid:
            return {
                "success": False,
                "error": f"Service '{service}' has no PID (status: {svc.status}).",
            }

        try:
            os.kill(pid, signal.SIGTERM)
            return {
                "success": True,
                "service": service,
                "killed_pid": pid,
                "message": (
                    f"Sent SIGTERM to {service} (pid {pid}). "
                    f"Sidecar will auto-restart it on next health check."
                ),
            }
        except ProcessLookupError:
            return {
                "success": True,
                "service": service,
                "message": f"Process {pid} already dead. Sidecar will restart it.",
            }
        except PermissionError:
            return {
                "success": False,
                "error": f"Permission denied killing pid {pid}.",
            }

    return [
        Capability(
            name="sidecar_status",
            description=(
                "Check if the sidecar daemon is running and get its current "
                "state: supervised services health, scheduler status, and "
                "upcoming job schedule."
            ),
            category="status",
            parameters={},
            callable=_sidecar_status,
            search_aliases=["daemon", "sidecar", "process supervisor", "services health"],
        ),
        Capability(
            name="sidecar_jobs",
            description=(
                "List all scheduled sidecar jobs with their next fire time, "
                "heartbeat status, and whether exclusion windows are active."
            ),
            category="status",
            parameters={},
            callable=_sidecar_jobs,
            search_aliases=["cron", "scheduled jobs", "heartbeat", "sidecar schedule"],
        ),
        # DISABLED: service_restart for mcp_gateway kills the MCP server
        # process, but Claude Code's MCP client does NOT auto-reconnect.
        # This leaves the gateway permanently disconnected until the user
        # manually restarts Claude Code or the MCP connection.
        # Non-gateway services (dashboard, telegram, etc.) could still be
        # restarted safely, but the capability doesn't distinguish.
        # Re-enable once Claude Code supports MCP server auto-reconnect,
        # or add a guard that refuses to kill the mcp_gateway service.
        #
        # Capability(
        #     name="service_restart",
        #     description=(
        #         "Restart a sidecar-managed service by killing its process. "
        #         "The sidecar supervisor auto-restarts it on the next health "
        #         "check (~30s), picking up any code changes. Use after editing "
        #         "dashboard, messaging, embedding, telegram, or mcp_gateway code."
        #     ),
        #     category="sidecar",
        #     parameters={
        #         "service": {
        #             "type": "str",
        #             "description": (
        #                 "Service name to restart (e.g. 'dashboard', 'messaging', "
        #                 "'embedding', 'telegram', 'mcp_gateway')."
        #             ),
        #             "required": True,
        #         },
        #     },
        #     callable=_service_restart,
        #     search_aliases=[
        #         "kill service", "restart service", "reload service",
        #         "refresh dashboard", "restart dashboard",
        #     ],
        #     mutates_state=True,
        # ),
    ]


def _remote_session_capabilities() -> list[Capability]:
    """Remote CLI session launcher capabilities.

    Launch visible, persistent Claude Code sessions in real terminal
    windows — for Remote Control (phone app) connection.
    """
    from work_buddy.session_launcher import (
        begin_session,
        list_resumable_sessions,
    )

    _REPO_ROOT = Path(__file__).parent.parent

    def _remote_begin(
        cwd: str | None = None,
        prompt: str | None = None,
        session_id: str | None = None,
        session_name: str | None = None,
        bypass_permissions: bool = True,
    ) -> dict:
        return begin_session(
            cwd=cwd, prompt=prompt,
            session_id=session_id, session_name=session_name,
            bypass_permissions=bypass_permissions,
        )

    def _session_resume(
        session_id: str,
        cwd: str | None = None,
        bypass_permissions: bool = True,
    ) -> dict:
        # Resume into a local terminal — no remote-control, no prompt.
        # cwd defaults to the session's own recorded cwd via begin_session.
        # Validate up-front: begin_session falls back to a bare `claude
        # --resume` if resolution fails, which spawns a useless terminal.
        from work_buddy.sessions.inspector import resolve_session_id
        try:
            resolved = resolve_session_id(session_id)
        except FileNotFoundError as exc:
            return {"status": "error", "error": str(exc)}
        return begin_session(
            cwd=cwd,
            session_id=resolved,
            bypass_permissions=bypass_permissions,
            remote_control=False,
        )

    def _remote_list(cwd: str | None = None) -> dict:
        sessions = list_resumable_sessions(cwd=cwd or str(_REPO_ROOT))
        return {
            "sessions": sessions[:20],  # Cap at 20
            "total": len(sessions),
            "cwd_filter": cwd,
        }

    return [
        Capability(
            name="remote_session_begin",
            description=(
                "Launch or resume a visible Claude Code session in a real "
                "terminal window. If session_id or session_name is provided, "
                "resumes that session; otherwise starts a new one. Designed "
                "for Remote Control (phone app) connection."
            ),
            category="sidecar",
            parameters={
                "cwd": {
                    "type": "str",
                    "description": "Working directory. Defaults to repo root.",
                    "required": False,
                },
                "prompt": {
                    "type": "str",
                    "description": "Initial prompt for a new session. Ignored when resuming.",
                    "required": False,
                },
                "session_id": {
                    "type": "str",
                    "description": "Session ID to resume. Triggers resume mode.",
                    "required": False,
                },
                "session_name": {
                    "type": "str",
                    "description": "Session name to look up for resume. Triggers resume mode.",
                    "required": False,
                },
                "bypass_permissions": {
                    "type": "bool",
                    "description": "Add --dangerously-skip-permissions so the session operates without interactive permission prompts. Default: True.",
                    "required": False,
                },
            },
            callable=_remote_begin,
            search_aliases=[
                "remote session", "launch terminal", "start claude",
                "remote control", "visible session", "phone session",
                "telegram", "remote launch", "resume session",
                "continue session", "reconnect",
            ],
            mutates_state=True,
            retry_policy="manual",
        ),
        Capability(
            name="session_resume",
            description=(
                "Resume an existing Claude Code session in a new local "
                "terminal window. No prompt is sent and remote-control is "
                "off — the terminal opens directly into the conversation, "
                "ready for the user to type. cwd is auto-derived from the "
                "session's recorded working directory."
            ),
            category="sidecar",
            parameters={
                "session_id": {
                    "type": "str",
                    "description": "Full or partial (>=8-char prefix) session UUID to resume.",
                    "required": True,
                },
                "cwd": {
                    "type": "str",
                    "description": "Override the working directory. Defaults to the session's own recorded cwd, falling back to repo root.",
                    "required": False,
                },
                "bypass_permissions": {
                    "type": "bool",
                    "description": "Add --dangerously-skip-permissions. Default: True.",
                    "required": False,
                },
            },
            callable=_session_resume,
            search_aliases=[
                "resume session", "continue session", "reopen chat",
                "pick up conversation", "claude --resume", "reattach",
                "open terminal to session",
            ],
            mutates_state=True,
            retry_policy="manual",
            consent_operations=["sidecar:remote_session_launch"],
        ),
        Capability(
            name="remote_session_list",
            description=(
                "List resumable Claude Code sessions from ~/.claude/sessions/. "
                "Shows session ID, name, cwd, and start time."
            ),
            category="sidecar",
            parameters={
                "cwd": {
                    "type": "str",
                    "description": "Filter to sessions started in this directory. Defaults to repo root.",
                    "required": False,
                },
            },
            callable=_remote_list,
            search_aliases=[
                "list sessions", "resumable sessions", "session picker",
                "active sessions", "find session",
            ],
        ),
    ]


def _ledger_capabilities() -> list[Capability]:
    """Session activity ledger — query what this session has done."""
    from work_buddy.mcp_server.activity_ledger import query_activity, query_session_summary

    return [
        Capability(
            name="session_activity",
            description=(
                "Query the session activity ledger — what this agent session "
                "has done through work-buddy. Filters by event type, capability, "
                "category, status. Returns last N matching entries (newest first)."
            ),
            category="status",
            parameters={
                "event_type": {
                    "type": "str",
                    "description": "Filter: capability_invoked, workflow_started, workflow_step_completed, search_performed",
                    "required": False,
                },
                "capability_name": {
                    "type": "str",
                    "description": "Filter to a specific capability name",
                    "required": False,
                },
                "category": {
                    "type": "str",
                    "description": "Filter by category (tasks, journal, context, etc.)",
                    "required": False,
                },
                "status": {
                    "type": "str",
                    "description": "Filter by status: ok, error, consent_required",
                    "required": False,
                },
                "last_n": {
                    "type": "int",
                    "description": "Return last N matching entries (default 20)",
                    "required": False,
                },
                "include_searches": {
                    "type": "bool",
                    "description": "Include wb_search events (default false)",
                    "required": False,
                },
            },
            callable=query_activity,
            search_aliases=[
                "what did I do", "session history", "activity log",
                "what happened", "session activity", "ledger",
            ],
        ),
        Capability(
            name="session_summary",
            description=(
                "Compact summary of what this agent session has done — "
                "counts by category/capability, errors, mutations, "
                "key artifacts created, workflow progress."
            ),
            category="status",
            parameters={},
            callable=query_session_summary,
            search_aliases=[
                "session overview", "what has happened", "session recap",
                "session digest", "activity summary",
            ],
        ),
    ]


def _consent_capabilities() -> list[Capability]:
    """Consent management capabilities.

    These let agents grant/revoke/list consent via the MCP gateway,
    which is critical for the cross-process consent flow: grants written
    by the MCP process are readable by the MCP process on retry.
    """
    from work_buddy.consent import (
        grant_consent, revoke_consent, list_consents,
        create_consent_request, resolve_consent_request, list_pending_requests,
    )

    return [
        Capability(
            name="consent_grant",
            description=(
                "LOW-LEVEL: Direct consent grant for deferred resolution ONLY. "
                "Do NOT use this to bypass the consent flow — use consent_request "
                "instead, which notifies the user and waits for their approval. "
                "This capability exists for: (1) manual resolution after a "
                "consent_request timeout when the user later approves out-of-band, "
                "(2) programmatic grants from surface callbacks. "
                "All grants are session-scoped. "
                "Modes: 'always' (24h), 'temporary' (TTL-based), 'once' (single-use)."
            ),
            category="consent",
            parameters={
                "operation": {
                    "type": "str",
                    "description": "Operation identifier from the consent_required response",
                    "required": True,
                },
                "mode": {
                    "type": "str",
                    "description": "Grant mode: 'always', 'temporary', or 'once'",
                    "required": True,
                },
                "ttl_minutes": {
                    "type": "int",
                    "description": "TTL in minutes (required for 'temporary' mode)",
                    "required": False,
                },
            },
            callable=grant_consent,
            search_aliases=[
                "consent", "permission", "approve", "allow",
                "grant consent", "give permission",
            ],
            mutates_state=True,
            retry_policy="manual",
        ),
        Capability(
            name="consent_revoke",
            description="Revoke a previously granted consent for an operation.",
            category="consent",
            parameters={
                "operation": {
                    "type": "str",
                    "description": "Operation identifier to revoke",
                    "required": True,
                },
            },
            callable=revoke_consent,
            search_aliases=["revoke", "deny", "remove consent", "block"],
            mutates_state=True,
            retry_policy="manual",
        ),
        Capability(
            name="consent_list",
            description=(
                "List all consent entries with their status (mode, tier, "
                "expiry for temporary grants)."
            ),
            category="consent",
            parameters={},
            callable=list_consents,
            search_aliases=[
                "list consents",
                "show permissions",
                "consent status",
                "what have I approved",
                "current grants",
                "session permissions",
                "consent grants",
            ],
        ),
        Capability(
            name="consent_request_resolve",
            description=(
                "Approve or deny a pending consent request. If approved, writes "
                "the grant and dispatches the callback (session resume or messaging)."
            ),
            category="consent",
            parameters={
                "request_id": {
                    "type": "str",
                    "description": "The request ID to resolve",
                    "required": True,
                },
                "approved": {
                    "type": "bool",
                    "description": "True to approve, False to deny",
                    "required": True,
                },
                "mode": {
                    "type": "str",
                    "description": "Grant mode if approved: 'always', 'temporary', or 'once'",
                    "required": False,
                },
                "ttl_minutes": {
                    "type": "int",
                    "description": "TTL in minutes (for 'temporary' mode)",
                    "required": False,
                },
            },
            callable=resolve_consent_request,
            search_aliases=[
                "approve consent",
                "deny consent",
                "resolve request",
                "handle pending consent",
                "grant or deny operation",
                "respond to consent request",
                "decide on permission",
            ],
            mutates_state=True,
            retry_policy="manual",
        ),
        Capability(
            name="consent_request_list",
            description="List all pending (unresolved) consent requests.",
            category="consent",
            parameters={},
            callable=list_pending_requests,
            search_aliases=[
                "pending requests",
                "waiting for approval",
                "consent queue",
                "what needs approval",
                "unresolved consent",
                "approval queue",
            ],
        ),
    ]


def _conversation_capabilities() -> list[Capability]:
    """Conversation capabilities — multi-turn agent-user dialogue.

    Conversations are a standalone subsystem backed by SQLite. The
    dashboard renders them in a sidebar chat panel.

    Renamed from ``_thread_capabilities`` in v5 Stage 1; the
    ``thread`` namespace is reserved for the v5 universal-entity
    primitive.
    """
    import os
    import time
    import urllib.request
    from work_buddy.conversations.store import (
        create_conversation as _create_conversation,
        get_conversation as _get_conversation,
        get_conversation_with_messages as _get_conv_msgs,
        add_message as _add_msg,
        get_pending_question as _get_pending,
        respond_to_conversation as _respond_conv,
        close_conversation as _close_conversation,
        list_conversations as _list_conversations,
    )

    def _notify_conversation_created(
        conversation_id: str, title: str, body: str = "",
    ) -> None:
        """Deliver a conversation_chat notification through the
        notification system.

        Creates a Notification record and delivers via SurfaceDispatcher.
        DashboardSurface.deliver() creates the workflow view, and the
        dashboard poll loop detects it and shows a toast.
        """
        try:
            from work_buddy.notifications.store import (
                create_notification as _create_notif,
                mark_delivered as _mark_delivered,
            )
            from work_buddy.notifications.models import Notification, ResponseType
            from work_buddy.notifications.dispatcher import SurfaceDispatcher

            n = Notification(
                notification_id=f"conversation-{conversation_id}",
                title=title,
                body=body[:100] if body else "New conversation",
                response_type=ResponseType.NONE.value,
                custom_template={
                    "type": "conversation_chat",
                    "conversation_id": conversation_id,
                },
                expandable=True,
            )
            created = _create_notif(n)
            dispatcher = SurfaceDispatcher.from_config()
            dispatcher.deliver(created, mark_delivered_fn=_mark_delivered)
        except Exception:
            pass  # Dashboard/notification system may not be running

    def conversation_create(
        title: str, message: str = "", source: str = "",
    ) -> dict:
        if not source:
            source = f"agent:{os.environ.get('WORK_BUDDY_SESSION_ID', 'unknown')}"
        conv = _create_conversation(title=title, source=source)
        result = {"conversation_id": conv.conversation_id, "status": "created"}

        if message:
            msg = _add_msg(conv.conversation_id, "agent", message)
            if msg:
                result["message_id"] = msg.message_id

        _notify_conversation_created(conv.conversation_id, title, message)
        return result

    def conversation_send(conversation_id: str, message: str) -> dict:
        msg = _add_msg(conversation_id, "agent", message)
        if msg is None:
            return {
                "error": f"Conversation not found or closed: {conversation_id}",
            }
        # Frontend polls /api/conversations/<id> for new messages
        return {
            "message_id": msg.message_id,
            "conversation_id": conversation_id,
        }

    def conversation_ask(
        conversation_id: str,
        question: str,
        response_type: str = "freeform",
        choices: list | None = None,
        timeout_seconds: int | None = None,
    ) -> dict:
        choice_dicts = None
        if choices:
            choice_dicts = []
            for c in choices:
                if isinstance(c, str):
                    choice_dicts.append({"key": c, "label": c})
                elif isinstance(c, dict):
                    choice_dicts.append(c)

        msg = _add_msg(
            conversation_id, "agent", question,
            message_type="question",
            response_type=response_type,
            choices=choice_dicts,
        )
        if msg is None:
            return {
                "error": f"Conversation not found or closed: {conversation_id}",
            }
        result = {
            "message_id": msg.message_id,
            "conversation_id": conversation_id,
            "status": "pending",
        }

        # Optional blocking poll
        if timeout_seconds is not None:
            timeout_seconds = min(timeout_seconds, 110)
            deadline = time.time() + timeout_seconds
            while time.time() < deadline:
                pending = _get_pending(conversation_id)
                if pending is None or pending.status == "answered":
                    data = _get_conv_msgs(conversation_id)
                    if data:
                        for m in reversed(data["messages"]):
                            if m.get("message_id") == msg.message_id:
                                result["status"] = "answered"
                                result["response"] = m.get("response")
                                return result
                    result["status"] = "answered"
                    return result
                time.sleep(3)
            result["status"] = "timeout"

        return result

    def conversation_poll(
        conversation_id: str,
        timeout_seconds: int | None = None,
    ) -> dict:
        pending = _get_pending(conversation_id)
        if pending is None:
            data = _get_conv_msgs(conversation_id)
            if not data:
                return {"error": f"Conversation not found: {conversation_id}"}
            answered = [m for m in data["messages"]
                        if m.get("status") == "answered"]
            if answered:
                last = answered[-1]
                return {
                    "status": "answered",
                    "message_id": last["message_id"],
                    "response": last.get("response"),
                }
            return {"status": "no_pending_question"}

        if timeout_seconds is None:
            return {
                "status": "pending",
                "message_id": pending.message_id,
                "question": pending.content,
            }

        timeout_seconds = min(timeout_seconds, 110)
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            p = _get_pending(conversation_id)
            if p is None:
                data = _get_conv_msgs(conversation_id)
                if data:
                    answered = [m for m in data["messages"]
                                if m.get("message_id") == pending.message_id]
                    if answered:
                        return {
                            "status": "answered",
                            "message_id": pending.message_id,
                            "response": answered[0].get("response"),
                        }
                return {"status": "answered", "message_id": pending.message_id}
            time.sleep(3)

        return {"status": "timeout", "waited_seconds": timeout_seconds}

    def conversation_close(conversation_id: str) -> dict:
        ok = _close_conversation(conversation_id)
        if not ok:
            return {"error": f"Conversation not found: {conversation_id}"}
        try:
            from work_buddy.notifications.store import cancel_notification
            cancel_notification(f"conversation-{conversation_id}")
        except Exception:
            pass
        try:
            req = urllib.request.Request(
                f"http://localhost:5127/api/workflow-views/conversation-{conversation_id}/dismiss",
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=3)
        except Exception:
            pass
        return {"closed": True, "conversation_id": conversation_id}

    def conversation_list(status: str = "open") -> dict:
        conversations = _list_conversations(
            status=status if status != "all" else None,
        )
        return {
            "conversations": conversations,
            "count": len(conversations),
        }

    return [
        Capability(
            name="conversation_create",
            description="Create a new conversation with the user. Opens a chat sidebar on the dashboard.",
            category="conversations",
            parameters={
                "title": {"type": "string", "description": "Conversation title", "required": True},
                "message": {"type": "string", "description": "Optional initial agent message"},
                "source": {"type": "string", "description": "Source identifier (auto-detected if omitted)"},
            },
            callable=conversation_create,
            search_aliases=["chat", "conversation", "follow up", "multi-turn", "side chat"],
        ),
        Capability(
            name="conversation_send",
            description="Send a message in an existing conversation (fire-and-forget, no response expected).",
            category="conversations",
            parameters={
                "conversation_id": {"type": "string", "description": "Conversation ID", "required": True},
                "message": {"type": "string", "description": "Message content", "required": True},
            },
            callable=conversation_send,
            search_aliases=[
                "chat message",
                "conversation message",
                "send chat message",
                "post in conversation",
                "speak in conversation",
            ],
        ),
        Capability(
            name="conversation_ask",
            description="Ask a question in a conversation and optionally wait for the user's response.",
            category="conversations",
            parameters={
                "conversation_id": {"type": "string", "description": "Conversation ID", "required": True},
                "question": {"type": "string", "description": "Question text", "required": True},
                "response_type": {"type": "string", "description": "freeform (default), boolean, or choice"},
                "choices": {"type": "array", "description": "For choice type: [{key, label}] or [str]"},
                "timeout_seconds": {"type": "integer", "description": "Block and wait for response (max 110s)"},
            },
            callable=conversation_ask,
            search_aliases=["chat question", "ask user", "conversation question", "follow up question"],
        ),
        Capability(
            name="conversation_poll",
            description="Check if the latest question in a conversation has been answered.",
            category="conversations",
            parameters={
                "conversation_id": {"type": "string", "description": "Conversation ID", "required": True},
                "timeout_seconds": {"type": "integer", "description": "Block and wait (max 110s)"},
            },
            callable=conversation_poll,
            search_aliases=[
                "check conversation",
                "conversation response",
                "poll chat",
                "has user answered",
                "conversation answered",
                "check for reply",
            ],
        ),
        Capability(
            name="conversation_close",
            description="Close a conversation.",
            category="conversations",
            parameters={
                "conversation_id": {"type": "string", "description": "Conversation ID", "required": True},
            },
            callable=conversation_close,
            search_aliases=[
                "end conversation",
                "close chat",
                "finish conversation",
                "wrap up conversation",
                "close dashboard chat",
            ],
        ),
        Capability(
            name="conversation_list",
            description="List conversations.",
            category="conversations",
            parameters={
                "status": {"type": "string", "description": "Filter: 'open' (default), 'closed', or 'all'"},
            },
            callable=conversation_list,
            search_aliases=[
                "list chats",
                "active conversations",
                "open conversations",
                "what conversations are active",
                "recent conversations",
                "conversation directory",
            ],
        ),
    ]


def _inline_capabilities() -> list[Capability]:
    """Inline Obsidian-command capabilities.

    Exposes the :mod:`work_buddy.inline` dispatcher, watcher store, and
    sync reconciler to MCP callers (sidecar jobs and the Obsidian plugin
    bridge both use these).
    """
    from work_buddy.inline import dispatcher as _disp
    from work_buddy.inline import registry as _ireg
    from work_buddy.inline import store as _istore
    from work_buddy.inline import sync as _isync

    def inline_invoke(command: str, surface: str, payload: dict | None = None) -> dict:
        merged = dict(payload or {})
        merged["command"] = command
        return _disp.dispatch_sync(surface, merged)

    def inline_list_commands(surface: str | None = None) -> dict:
        cmds = _ireg.list_for_surface(surface) if surface else _ireg.list_commands()
        return {"commands": [c.to_dict() for c in cmds]}

    def inline_menu_manifest() -> dict:
        items = []
        for c in _ireg.list_for_surface("menu"):
            items.append(
                {
                    "command": c.name,
                    "label": c.menu_label or c.name,
                    "description": c.description,
                }
            )
        return {"items": items}

    def inline_tag_removed(file_path: str, tag: str) -> dict:
        cleaned = tag.lstrip("#")
        removed = []
        for w in _istore.list_watchers(file_path=file_path):
            if w.tag == cleaned or w.tag == tag:
                if _istore.delete_watcher(w.watcher_id):
                    removed.append(w.watcher_id)
        return {"removed": removed, "count": len(removed)}

    def inline_list_watchers() -> dict:
        return {"watchers": [w.to_dict() for w in _istore.list_watchers()]}

    def inline_cancel_watcher(watcher_id: str) -> dict:
        ok = _istore.delete_watcher(watcher_id)
        return {"cancelled": ok, "watcher_id": watcher_id}

    def inline_sync() -> dict:
        return _isync.inline_sync()

    return [
        Capability(
            name="inline_invoke",
            description="Execute an inline command (menu or #wb/cmd/* tag surface).",
            category="inline",
            parameters={
                "command": {"type": "string", "description": "Registered command name (e.g. 'task/new')", "required": True},
                "surface": {"type": "string", "description": "'menu' or 'tag'", "required": True},
                "payload": {"type": "object", "description": "Surface-specific payload (file_path, selection, cursor_line, tag, tag_line, full_text, params)"},
            },
            callable=inline_invoke,
            mutates_state=True,
            search_aliases=[
                "inline command",
                "obsidian right-click",
                "wb/cmd tag",
                "run inline handler",
                "invoke from note",
            ],
        ),
        Capability(
            name="inline_list_commands",
            description="List registered inline commands, optionally filtered by surface.",
            category="inline",
            parameters={
                "surface": {"type": "string", "description": "Filter: 'menu' or 'tag'"},
            },
            callable=inline_list_commands,
            search_aliases=[
                "list inline commands",
                "inline handlers",
                "available wb/cmd tags",
            ],
        ),
        Capability(
            name="inline_menu_manifest",
            description="Manifest of inline commands that expose a right-click menu entry.",
            category="inline",
            parameters={},
            callable=inline_menu_manifest,
            search_aliases=[
                "right-click menu",
                "obsidian menu items",
                "inline menu manifest",
            ],
        ),
        Capability(
            name="inline_tag_removed",
            description="Cancel persistent watchers whose tag was removed from a note.",
            category="inline",
            parameters={
                "file_path": {"type": "string", "description": "Vault-relative path", "required": True},
                "tag": {"type": "string", "description": "Tag that was removed (with or without #)", "required": True},
            },
            callable=inline_tag_removed,
            mutates_state=True,
            search_aliases=[
                "cancel watcher on tag delete",
                "inline tag removed",
                "wb/cmd removed",
            ],
        ),
        Capability(
            name="inline_list_watchers",
            description="List all persistent inline watchers.",
            category="inline",
            parameters={},
            callable=inline_list_watchers,
            search_aliases=[
                "persistent watchers",
                "active inline watchers",
                "wb/cmd watchers",
            ],
        ),
        Capability(
            name="inline_cancel_watcher",
            description="Cancel a single persistent watcher by ID.",
            category="inline",
            parameters={
                "watcher_id": {"type": "string", "description": "Watcher identifier", "required": True},
            },
            callable=inline_cancel_watcher,
            mutates_state=True,
            search_aliases=["cancel watcher", "delete watcher"],
        ),
        Capability(
            name="inline_sync",
            description="Reconcile vault #wb/cmd/* tags with the persistent watcher store.",
            category="inline",
            parameters={},
            callable=inline_sync,
            mutates_state=True,
            search_aliases=[
                "sync inline watchers",
                "reconcile wb/cmd tags",
                "inline watcher drift",
            ],
        ),
    ]


def _notification_capabilities() -> list[Capability]:
    """Notification and request capabilities.

    Consolidated API:
      - notification_send: fire-and-forget notification
      - request_send: create + deliver + optionally poll (one call)
      - request_poll: check/wait on an existing request
      - consent_request: one-call consent flow with auto-resolve
      - notification_list_pending: list all pending items

    Lower-level capabilities (consent_grant/revoke/list, consent_request_resolve)
    remain in _consent_capabilities for direct manipulation and deferred flows.
    """
    import os
    import time
    from work_buddy.notifications.store import (
        create_notification as _create_notif,
        get_notification as _get_notif,
        respond_to_notification as _respond,
        mark_delivered as _mark_delivered,
        list_pending as _list_pending,
    )
    from work_buddy.notifications.models import (
        Notification, StandardResponse, ResponseType,
    )

    # MCP tool call timeout is ~120s. Document this so agents set safe values.
    _MAX_RECOMMENDED_TIMEOUT = 110  # seconds — leave buffer below MCP timeout

    # --- Helper: dispatcher (routes to all available surfaces) ---
    def _get_dispatcher():
        from work_buddy.notifications.dispatcher import SurfaceDispatcher
        return SurfaceDispatcher.from_config()

    def _deliver_to_surfaces(notification_id: str) -> tuple[bool, str]:
        """Deliver via dispatcher to all available surfaces.
        Returns (any_success, error_msg)."""
        notif = _get_notif(notification_id)
        if notif is None:
            return False, f"Notification not found: {notification_id}"
        dispatcher = _get_dispatcher()
        results = dispatcher.deliver(notif, mark_delivered_fn=_mark_delivered)
        any_ok = any(results.values())
        if not any_ok:
            failed = [k for k, v in results.items() if not v]
            if not results:
                return False, "No eligible surfaces available"
            return False, f"Delivery failed on: {', '.join(failed)}"
        return True, ""

    def _poll_surfaces(
        notification_id: str,
        timeout_seconds: int | None = None,
        interval_seconds: int = 3,
    ) -> dict:
        """Poll all delivered surfaces for a response."""
        notif = _get_notif(notification_id)
        if notif is None:
            return {"status": "error", "error": f"Notification not found: {notification_id}"}
        dispatcher = _get_dispatcher()
        response = dispatcher.poll_response(
            notif,
            timeout_seconds=timeout_seconds,
            interval_seconds=interval_seconds,
        )
        if response is None:
            if timeout_seconds is not None:
                return {"status": "timeout", "waited_seconds": timeout_seconds}
            return {"status": "pending"}

        # First-response-wins: dismiss on all other surfaces
        notif_fresh = _get_notif(notification_id)
        if notif_fresh and notif_fresh.delivered_surfaces:
            try:
                dispatcher.dismiss_others(
                    notification_id,
                    responding_surface=response.surface,
                    delivered_surfaces=notif_fresh.delivered_surfaces,
                )
            except Exception:
                pass  # best-effort — don't block the response

        return {
            "status": "responded",
            "value": response.value,
            "surface": response.surface,
            "raw": response.raw,
        }

    def _log_to_dashboard(notif):
        """Best-effort: log notification event to dashboard's notification log."""
        try:
            import json as _json
            from urllib.request import Request as _Req, urlopen as _urlopen
            entry = {
                "notification_id": notif.notification_id,
                "title": notif.title,
                "type": "request" if notif.is_request() else "note",
                "short_id": notif.short_id,
                "response_type": notif.response_type,
                "surfaces": notif.delivered_surfaces or [],
            }
            data = _json.dumps(entry).encode("utf-8")
            req = _Req(
                "http://127.0.0.1:5127/api/notification-log",
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            _urlopen(req, timeout=3)
        except Exception:
            pass

    # -----------------------------------------------------------------------
    # Capability functions
    # -----------------------------------------------------------------------

    def send_notification(
        title: str,
        body: str = "",
        priority: str = "normal",
        source: str = "agent",
        tags: list | None = None,
        surfaces: list | None = None,
        expandable: bool | None = None,
    ) -> dict:
        """Send a fire-and-forget notification (no response expected).
        Creates the record and delivers to all available surfaces.
        Optionally specify surfaces=["obsidian"] to target specific ones.
        expandable: None=auto-detect, True=rich/dashboard view, False=toast-only."""
        n = Notification(
            title=title, body=body, priority=priority,
            source=source, response_type=ResponseType.NONE.value,
            tags=tags or [],
            surfaces=surfaces,
            expandable=expandable,
        )
        created = _create_notif(n)
        nid = created.notification_id
        delivered, err = _deliver_to_surfaces(nid)
        # Re-read from store to capture updated delivered_surfaces
        fresh = _get_notif(nid) or created
        if delivered:
            _log_to_dashboard(fresh)
        result = fresh.to_dict()
        result["delivered"] = delivered
        if err:
            result["delivery_error"] = err
        return result

    def request_send(
        title: str,
        body: str = "",
        response_type: str = "choice",
        choices: list | None = None,
        number_range: dict | None = None,
        custom_template: dict | None = None,
        source: str = "agent",
        source_type: str = "agent",
        priority: str = "normal",
        callback: dict | None = None,
        callback_session_id: str | None = None,
        tags: list | None = None,
        surfaces: list | None = None,
        timeout_seconds: int | None = None,
        interval_seconds: int = 3,
        expandable: bool | None = None,
    ) -> dict:
        """Create a request, deliver to all available surfaces, and optionally poll.

        Without timeout_seconds: creates + delivers, returns immediately (non-blocking).
        With timeout_seconds: creates + delivers + polls until response or timeout.
        Optionally specify surfaces=["telegram"] to target specific ones.
        expandable: None=auto-detect, True=rich/dashboard view, False=toast-only."""
        # Auto-inject session ID for AgentIngest hook delivery
        if callback_session_id is None:
            callback_session_id = os.environ.get("WORK_BUDDY_SESSION_ID")

        n = Notification(
            title=title, body=body, priority=priority,
            source=source, source_type=source_type,
            response_type=response_type,
            choices=choices or [],
            number_range=number_range,
            custom_template=custom_template,
            callback=callback,
            callback_session_id=callback_session_id,
            tags=tags or [],
            surfaces=surfaces,
            expandable=expandable,
        )
        created = _create_notif(n)
        nid = created.notification_id

        # Deliver
        delivered, err = _deliver_to_surfaces(nid)
        # Re-read from store to capture updated delivered_surfaces
        fresh = _get_notif(nid) or created
        if delivered:
            _log_to_dashboard(fresh)
        result = fresh.to_dict()
        result["delivered"] = delivered
        if err:
            result["delivery_error"] = err
            return result

        # Optionally poll
        if timeout_seconds is not None:
            poll_result = _poll_surfaces(nid, timeout_seconds, interval_seconds)
            result["poll"] = poll_result

        return result

    def request_poll(
        notification_id: str,
        timeout_seconds: int | None = None,
        interval_seconds: int = 3,
    ) -> dict:
        """Check/wait for a response to a previously delivered request.

        Without timeout_seconds: single immediate check.
        With timeout_seconds: blocks and polls until response or timeout."""
        return _poll_surfaces(notification_id, timeout_seconds, interval_seconds)

    def consent_request(
        operation: str,
        reason: str,
        risk: str = "moderate",
        default_ttl: int = 5,
        requester: str = "unknown",
        context: dict | None = None,
        callback: dict | None = None,
        callback_session_id: str | None = None,
        timeout_seconds: int | None = None,
        interval_seconds: int = 3,
        surfaces: list[str] | None = None,
    ) -> dict:
        """One-call consent flow: create request, deliver to surfaces, poll, auto-resolve.

        Without timeout_seconds: creates + delivers, returns immediately (non-blocking).
          Agent can call request_poll later, then consent_request_resolve.
        With timeout_seconds: creates + delivers + polls + auto-resolves on response.
          On approval: grant is written automatically. On deny: no grant.
          On timeout: request stays pending for later resolution."""
        from work_buddy.consent import (
            create_consent_request,
            resolve_consent_request,
        )

        # Auto-inject session ID for AgentIngest hook delivery when not
        # explicitly provided.  This ensures the notification response
        # gets dispatched with session targeting so PostToolUse / Stop
        # hooks can surface it mid-turn.
        if callback_session_id is None:
            callback_session_id = os.environ.get("WORK_BUDDY_SESSION_ID")

        # 1. Create the consent request (uses notification substrate)
        record = create_consent_request(
            operation=operation, reason=reason, risk=risk,
            default_ttl=default_ttl, requester=requester,
            context=context, callback=callback,
            callback_session_id=callback_session_id,
            surfaces=surfaces,
        )
        nid = record["notification_id"]

        # 2. Deliver to surfaces
        delivered, err = _deliver_to_surfaces(nid)
        record["delivered"] = delivered
        if err:
            record["delivery_error"] = err
            return record

        # 3. Non-blocking if no timeout
        if timeout_seconds is None:
            record["status"] = "pending"
            return record

        # 4. Poll for response
        poll_result = _poll_surfaces(nid, timeout_seconds, interval_seconds)

        if poll_result.get("status") != "responded":
            record["status"] = "timeout"
            record["poll"] = poll_result
            return record

        # 5. Auto-resolve based on user's choice.
        # The response may have already been recorded by a surface handler
        # (e.g., Telegram's on_button called respond_to_notification directly).
        # In that case resolve_consent_request raises ValueError — handle gracefully.
        choice = poll_result["value"]
        # Dashboard returns {"phase": "generic", "value": "once"} — unwrap
        if isinstance(choice, dict) and "value" in choice:
            choice = choice["value"]
        try:
            if choice == "deny":
                resolve_consent_request(nid, approved=False)
                record["approved"] = False
                record["status"] = "denied"
            else:
                mode = choice  # "always", "temporary", or "once"
                ttl = default_ttl if mode == "temporary" else None
                resolve_consent_request(nid, approved=True, mode=mode, ttl_minutes=ttl)
                record["approved"] = True
                record["mode"] = mode
                record["status"] = "granted"
        except ValueError:
            # Already resolved by a surface handler — the response was
            # recorded but grant_consent was NOT called. Write the grant now.
            resolved = _get_notif(nid)
            if resolved and resolved.response:
                final_choice = resolved.response.get("value", choice)
                record["approved"] = final_choice != "deny"
                record["mode"] = final_choice if final_choice != "deny" else None
                if final_choice == "deny":
                    record["status"] = "denied"
                else:
                    # Write the grant that resolve_consent_request would have written
                    from work_buddy.consent import grant_consent as _grant
                    _ttl = default_ttl if final_choice == "temporary" else None
                    _grant(
                        operation, mode=final_choice,
                        ttl_minutes=_ttl,
                    )
                    record["status"] = "granted"
            else:
                record["status"] = "responded"
                record["approved"] = choice != "deny"

        return record

    def list_pending_notifications() -> list[dict]:
        """List all pending notifications/requests."""
        return [n.to_dict() for n in _list_pending()]

    return [
        Capability(
            name="notification_send",
            description=(
                "Send a fire-and-forget notification to the user via all "
                "available surfaces (Obsidian, Telegram if enabled). "
                "No response expected. Optionally target specific surfaces."
            ),
            category="notifications",
            parameters={
                "title": {"type": "str", "description": "Notification title", "required": True},
                "body": {"type": "str", "description": "Notification body", "required": False},
                "priority": {"type": "str", "description": "low, normal, high, urgent", "required": False},
                "source": {"type": "str", "description": "Who is sending", "required": False},
                "tags": {"type": "list", "description": "Tags for filtering", "required": False},
                "surfaces": {"type": "list", "description": "Target surfaces (e.g. ['telegram']). Default: all available.", "required": False},
                "expandable": {"type": "bool", "description": "None=auto-detect, True=rich dashboard view, False=toast-only.", "required": False},
            },
            callable=send_notification,
            search_aliases=["notify", "alert", "message user", "send notification"],
            mutates_state=True,
            retry_policy="manual",
        ),
        Capability(
            name="request_send",
            description=(
                "Create a request, deliver to all available surfaces, and optionally "
                "poll for the user's response. Supports choice, boolean, freeform, "
                "and range response types. Without timeout_seconds: non-blocking "
                "(returns immediately, use request_poll later). With timeout_seconds: "
                f"blocks until response or timeout (max recommended: {_MAX_RECOMMENDED_TIMEOUT}s "
                "to stay within MCP call limits)."
            ),
            category="notifications",
            parameters={
                "title": {"type": "str", "description": "Request title", "required": True},
                "body": {"type": "str", "description": "Request body/explanation", "required": False},
                "response_type": {"type": "str", "description": "choice, boolean, freeform, range, custom", "required": False},
                "choices": {"type": "list", "description": "For choice type: [{key, label, description}]", "required": False},
                "number_range": {"type": "dict", "description": "For range type: {min, max, step}", "required": False},
                "custom_template": {"type": "dict", "description": "For custom type: surface-specific rendering data (e.g., {type: 'triage_clarify', presentation: ...})", "required": False},
                "source": {"type": "str", "description": "Who is sending", "required": False},
                "source_type": {"type": "str", "description": "agent or programmatic", "required": False},
                "priority": {"type": "str", "description": "low, normal, high, urgent", "required": False},
                "callback": {"type": "dict", "description": "Dispatch on response: {capability, params}", "required": False},
                "callback_session_id": {"type": "str", "description": "Resume this session on response", "required": False},
                "tags": {"type": "list", "description": "Tags for filtering", "required": False},
                "surfaces": {"type": "list", "description": "Target surfaces (e.g. ['telegram']). Default: all available.", "required": False},
                "timeout_seconds": {"type": "int", "description": f"Poll timeout. Omit for non-blocking. Max recommended: {_MAX_RECOMMENDED_TIMEOUT}s", "required": False},
                "interval_seconds": {"type": "int", "description": "Seconds between polls (default: 3)", "required": False},
                "expandable": {"type": "bool", "description": "None=auto-detect, True=rich dashboard view, False=toast-only.", "required": False},
            },
            callable=request_send,
            search_aliases=["ask user", "prompt user", "request response", "user input", "show modal"],
            mutates_state=True,
            retry_policy="manual",
        ),
        Capability(
            name="request_poll",
            description=(
                "Check/wait for a response to a previously delivered request. "
                "Without timeout_seconds: single immediate check. "
                "With timeout_seconds: blocks until response or timeout "
                f"(max recommended: {_MAX_RECOMMENDED_TIMEOUT}s). "
                "Response is cleared from Obsidian after reading (one-shot)."
            ),
            category="notifications",
            parameters={
                "notification_id": {"type": "str", "description": "The request ID to poll", "required": True},
                "timeout_seconds": {"type": "int", "description": f"Poll timeout. Omit for immediate check. Max recommended: {_MAX_RECOMMENDED_TIMEOUT}s", "required": False},
                "interval_seconds": {"type": "int", "description": "Seconds between polls (default: 3)", "required": False},
            },
            callable=request_poll,
            search_aliases=["check response", "poll modal", "check obsidian", "wait for response"],
        ),
        Capability(
            name="consent_request",
            description=(
                "One-call consent flow: create a consent request, deliver to all "
                "available surfaces, and optionally poll + auto-resolve. The modal shows "
                "Allow always / Allow for N min / Allow once / Deny options. "
                "Without timeout_seconds: non-blocking (returns request_id for later "
                "polling via request_poll + consent_request_resolve). "
                "With timeout_seconds: blocks until user responds, then auto-resolves "
                "(writes the grant on approval, returns denial on deny). "
                f"Max recommended timeout: {_MAX_RECOMMENDED_TIMEOUT}s to stay within MCP limits."
            ),
            category="consent",
            parameters={
                "operation": {"type": "str", "description": "Operation identifier (same as @requires_consent keys)", "required": True},
                "reason": {"type": "str", "description": "Human-readable explanation", "required": True},
                "risk": {"type": "str", "description": "low, moderate, or high", "required": False},
                "default_ttl": {"type": "int", "description": "Default TTL in minutes for temporary grants", "required": False},
                "requester": {"type": "str", "description": "Who is requesting (e.g., sidecar:cron_cleanup)", "required": False},
                "context": {"type": "dict", "description": "Optional metadata shown in the modal", "required": False},
                "callback": {"type": "dict", "description": "Dispatch on approval: {capability, params}", "required": False},
                "callback_session_id": {"type": "str", "description": "Resume this session on approval", "required": False},
                "timeout_seconds": {"type": "int", "description": f"Poll timeout. Omit for non-blocking. Max recommended: {_MAX_RECOMMENDED_TIMEOUT}s", "required": False},
                "interval_seconds": {"type": "int", "description": "Seconds between polls (default: 3)", "required": False},
                "surfaces": {"type": "list[str]", "description": "Target surface names (e.g., ['dashboard']). Default: all available", "required": False},
            },
            callable=consent_request,
            search_aliases=[
                "consent", "permission", "approve operation",
                "ask consent", "request consent", "consent modal",
            ],
            mutates_state=True,
            retry_policy="manual",
        ),
        Capability(
            name="notification_list_pending",
            description="List all pending notifications and requests awaiting user response.",
            category="notifications",
            parameters={},
            callable=list_pending_notifications,
            search_aliases=[
                "pending notifications",
                "waiting requests",
                "notification queue",
                "what needs response",
                "awaiting user input",
                "unresolved notifications",
                "open requests",
            ],
        ),
    ]


def _build_slash_command_index() -> dict[str, str]:
    """Scan .claude/commands/wb-*.md for `workflow:` frontmatter.

    Returns mapping of registry entry name → slash command stem.
    E.g. {"update-journal": "wb-journal-update", "task_briefing": "wb-task-briefing"}
    """
    index: dict[str, str] = {}
    if not _SLASH_CMD_DIR.exists():
        return index
    for md_file in sorted(_SLASH_CMD_DIR.glob("wb-*.md")):
        try:
            meta, _ = parse_frontmatter(md_file)
            target = meta.get("workflow")
            if target and isinstance(target, str):
                index[target] = md_file.stem
        except Exception:
            continue
    return index


def _compute_workflow_requires(
    registry: dict[str, Capability | WorkflowDefinition],
) -> None:
    """Populate ``WorkflowDefinition.requires`` in-place.

    For each workflow, unions:
      - Every step's own ``requires`` (tool/component IDs).
      - The ``requires`` of every capability named in ``step.invokes``.

    Invoked capabilities that are not (yet) in the registry (e.g. filtered
    out by tool availability) are skipped silently — the workflow will
    show an incomplete dependency set, which is recoverable once the
    upstream component comes back.

    Transitive closure (capability A.invokes = [B], B.requires = [obsidian])
    is followed one hop. Multi-hop chains (A invokes B invokes C) are not
    resolved here; the control-graph resolver in
    ``work_buddy.control.capability_resolver`` handles the full closure on
    demand without bloating the workflow dataclass.
    """
    for entry in registry.values():
        if not isinstance(entry, WorkflowDefinition):
            continue
        seen: set[str] = set()
        for step in entry.steps:
            for t_id in step.requires:
                seen.add(t_id)
            for cap_name in step.invokes:
                cap = registry.get(cap_name)
                if isinstance(cap, Capability):
                    for t_id in cap.requires:
                        seen.add(t_id)
        entry.requires = sorted(seen)


def _discover_workflows_from_store() -> list[WorkflowDefinition]:
    """Load workflow definitions from the knowledge store.

    Replaces ``_discover_workflows()`` (file-based). The store's
    ``WorkflowUnit`` entries contain the full DAG structure, step
    instructions, auto_run specs, and execution policy — everything
    the conductor needs.
    """
    from work_buddy.knowledge.store import load_store
    from work_buddy.knowledge.model import WorkflowUnit

    store = load_store()
    workflows: list[WorkflowDefinition] = []

    for _path, unit in store.items():
        if not isinstance(unit, WorkflowUnit):
            continue
        if not unit.steps:
            continue

        wf_execution = unit.execution or "main"

        steps: list[WorkflowStep] = []
        for s in unit.steps:
            step_id = s.get("id", "")

            # Reconstruct AutoRun from dict
            auto_run_raw = s.get("auto_run")
            auto_run: AutoRun | None = None
            if isinstance(auto_run_raw, dict) and "callable" in auto_run_raw:
                auto_run = AutoRun(
                    callable=auto_run_raw["callable"],
                    kwargs=auto_run_raw.get("kwargs") or {},
                    input_map=auto_run_raw.get("input_map") or {},
                    timeout=auto_run_raw.get("timeout", 30),
                )

            # Reconstruct ResultVisibility from dict
            vis_raw = s.get("visibility")
            vis: ResultVisibility | None = None
            if isinstance(vis_raw, dict):
                vis = ResultVisibility(
                    mode=vis_raw.get("mode", "auto"),
                    include_keys=vis_raw.get("include_keys") or [],
                    exclude_keys=vis_raw.get("exclude_keys") or [],
                )

            steps.append(WorkflowStep(
                id=step_id,
                name=s.get("name", step_id),
                instruction=unit.step_instructions.get(step_id, ""),
                step_type=s.get("step_type", "reasoning"),
                depends_on=s.get("depends_on", []),
                execution=s.get("execution", wf_execution),
                workflow_file=s.get("workflow_ref"),
                optional=s.get("optional", False),
                requires=s.get("requires", []),
                invokes=s.get("invokes", []),
                auto_run=auto_run,
                result_schema=s.get("result_schema"),
                requires_individual_consent=s.get("requires_individual_consent", False),
                visibility=vis,
            ))

        # Extract context from content["full"] if present
        context = ""
        if unit.content and isinstance(unit.content, dict):
            context = unit.content.get("full", "")

        workflows.append(WorkflowDefinition(
            name=unit.workflow_name,
            description=unit.description,
            workflow_file=f"store:{_path}",  # provenance marker
            execution=wf_execution,
            allow_override=unit.allow_override,
            steps=steps,
            context=context,
            slash_command=unit.command,
        ))

    return workflows


def _dev_mode_toggle(enabled: bool | None = None) -> dict[str, Any]:
    """Toggle dev mode for the current session."""
    from work_buddy.agent_session import get_dev_mode, set_dev_mode

    previous = get_dev_mode()
    new_value = (not previous) if enabled is None else bool(enabled)
    set_dev_mode(new_value)
    return {"dev_mode": new_value, "previous": previous}


def _knowledge_capabilities() -> list[Capability]:
    """Unified agent self-documentation — search, navigate, rebuild, and validate."""
    from work_buddy.knowledge.query import (
        agent_docs, agent_docs_rebuild,
        knowledge, knowledge_personal,
        knowledge_index_rebuild, knowledge_index_status,
        # Legacy wrappers for backward compat during migration
        docs_query, docs_get, docs_index_build,
    )
    from work_buddy.knowledge.validate import docs_validate
    from work_buddy.knowledge.editor import (
        docs_create, docs_update, docs_delete, docs_move,
        workflow_create, workflow_update,
    )
    from work_buddy.knowledge.vault_editor import mint_personal_unit

    return [
        Capability(
            name="agent_docs",
            description=(
                "Search and navigate all agent documentation: directions, "
                "system docs, capabilities, and workflows. Supports exact "
                "path lookup, subtree browsing, and natural language search "
                "with hierarchical progressive disclosure."
            ),
            category="context",
            parameters={
                "query": {
                    "type": "str",
                    "description": (
                        "Natural language search. Empty + no path/scope = "
                        "full index."
                    ),
                    "required": False,
                },
                "path": {
                    "type": "str",
                    "description": (
                        "Exact unit path for direct lookup "
                        "(e.g. 'journal/running-notes', 'tasks/triage')"
                    ),
                    "required": False,
                },
                "scope": {
                    "type": "str",
                    "description": (
                        "Path prefix to filter to a subtree "
                        "(e.g. 'tasks/', 'obsidian/')"
                    ),
                    "required": False,
                },
                "kind": {
                    "type": "str",
                    "description": (
                        "Filter by kind: directions, system, capability, workflow"
                    ),
                    "required": False,
                },
                "depth": {
                    "type": "str",
                    "description": (
                        "Content depth: 'index' (navigation), "
                        "'summary' (default), 'full' (complete)"
                    ),
                    "required": False,
                },
                "top_n": {
                    "type": "int",
                    "description": "Max search results (default 8)",
                    "required": False,
                },
                "dev": {
                    "type": "bool",
                    "description": (
                        "Include dev_notes in full-depth results. "
                        "Auto-set when session dev mode is active."
                    ),
                    "required": False,
                },
            },
            callable=agent_docs,
            search_aliases=[
                "documentation", "knowledge", "docs", "how does",
                "what is", "help", "guide", "reference", "manual",
                "agent docs", "self documentation", "how to",
                "find capability", "what can I do",
            ],
        ),
        Capability(
            name="agent_docs_rebuild",
            description=(
                "Reload the knowledge store from disk. Use after editing "
                "store JSON files or after registry changes."
            ),
            category="context",
            parameters={
                "force": {
                    "type": "bool",
                    "description": "Force full reload (default false)",
                    "required": False,
                },
            },
            callable=agent_docs_rebuild,
            search_aliases=[
                "reload docs",
                "rebuild knowledge",
                "refresh store",
                "reload knowledge store",
                "pick up knowledge edits",
                "refresh agent docs",
                "reindex documentation",
            ],
        ),
        # Legacy compat — keep old names working during migration
        Capability(
            name="docs_query",
            description="[Legacy] Search knowledge units. Use agent_docs instead.",
            category="context",
            parameters={
                "query": {"type": "str", "required": False},
                "category": {"type": "str", "required": False},
                "depth": {"type": "str", "required": False},
                "top_n": {"type": "int", "required": False},
            },
            callable=docs_query,
            invokes=["agent_docs"],  # thin wrapper — calls agent_docs(...) directly
            search_aliases=[
                "legacy knowledge query",
                "old docs query",
                "legacy search knowledge",
                "deprecated knowledge query",
            ],
        ),
        Capability(
            name="docs_get",
            description="[Legacy] Get a knowledge unit by name. Use agent_docs instead.",
            category="context",
            parameters={
                "name": {"type": "str", "required": True},
                "depth": {"type": "str", "required": False},
            },
            callable=docs_get,
            invokes=["agent_docs"],  # thin wrapper — calls agent_docs(path=name, depth=depth)
            search_aliases=[
                "legacy knowledge get",
                "old docs get",
                "legacy unit lookup",
                "deprecated docs fetch",
            ],
        ),
        Capability(
            name="docs_index",
            description="[Legacy] Build IR index. Use agent_docs_rebuild instead.",
            category="context",
            parameters={
                "force": {"type": "bool", "required": False},
            },
            callable=docs_index_build,
            invokes=["agent_docs_rebuild"],  # thin wrapper — calls agent_docs_rebuild(force=force)
            search_aliases=[
                "legacy build index",
                "old index rebuild",
                "deprecated docs index",
                "legacy docs indexing",
                "old knowledge rebuild",
            ],
        ),
        Capability(
            name="docs_validate",
            description=(
                "Validate the knowledge store: DAG integrity, "
                "command-to-store mappings, thinned command format, "
                "required fields, kind-specific fields, and parent-child symmetry."
            ),
            category="context",
            parameters={
                "checks": {
                    "type": "str",
                    "description": (
                        "Comma-separated check names to run. Empty = all. "
                        "Available: dag_integrity, command_mapping, "
                        "thinned_commands, store_path_validity, "
                        "required_fields, directions_fields, "
                        "kind_specific_fields, parent_child_symmetry"
                    ),
                    "required": False,
                },
            },
            callable=docs_validate,
            search_aliases=[
                "validate store", "check knowledge", "store health",
                "integrity check", "knowledge validation",
            ],
        ),
        Capability(
            name="docs_create",
            description=(
                "Create a new unit in the knowledge store. Writes to the "
                "appropriate JSON file, updates parent children lists, "
                "and validates DAG integrity."
            ),
            category="context",
            parameters={
                "path": {"type": "str", "description": "Unique path ID (e.g. 'tasks/my-directions')", "required": True},
                "kind": {"type": "str", "description": "Unit type: directions, system, capability, workflow", "required": True},
                "name": {"type": "str", "description": "Human-readable name", "required": True},
                "description": {"type": "str", "description": "One-line summary", "required": True},
                "content_full": {"type": "str", "description": "Full content text (newlines preserved)", "required": False},
                "content_summary": {"type": "str", "description": "Short summary", "required": False},
                "trigger": {"type": "str", "description": "(directions) When to use this unit", "required": False},
                "command": {"type": "str", "description": "(directions/workflow) Slash command name", "required": False},
                "workflow": {"type": "str", "description": "(directions) Linked workflow path", "required": False},
                "capabilities": {"type": "str", "description": "(directions) Comma-separated MCP capability paths", "required": False},
                "parents": {"type": "str", "description": "Comma-separated parent paths", "required": False},
                "children": {"type": "str", "description": "Comma-separated child paths", "required": False},
                "tags": {"type": "str", "description": "Comma-separated search tags", "required": False},
                "aliases": {"type": "str", "description": "Comma-separated search aliases", "required": False},
                "dev_notes": {
                    "type": "str",
                    "description": (
                        "Development-facing notes surfaced only in dev mode "
                        "(set via dev_mode_toggle). Use for architectural "
                        "constraints, non-obvious dependencies, and "
                        "hard-won lessons future dev agents could clobber."
                    ),
                    "required": False,
                },
                "entry_points": {
                    "type": "str",
                    "description": (
                        "(system kind) Comma-separated dotted module paths "
                        "that implement this system, for navigation."
                    ),
                    "required": False,
                },
            },
            callable=docs_create,
            mutates_state=True,
            retry_policy="manual",
            search_aliases=[
                "create unit",
                "add knowledge",
                "new docs entry",
                "write new knowledge",
                "author docs entry",
                "add documentation unit",
                "new knowledge unit",
            ],
        ),
        Capability(
            name="docs_update",
            description=(
                "Update fields on an existing knowledge unit. "
                "Only provided fields are changed; omitted fields preserved."
            ),
            category="context",
            parameters={
                "path": {"type": "str", "description": "Path of unit to update", "required": True},
                "name": {"type": "str", "description": "New name", "required": False},
                "description": {"type": "str", "description": "New description", "required": False},
                "content_full": {"type": "str", "description": "New full content", "required": False},
                "content_summary": {"type": "str", "description": "New summary", "required": False},
                "trigger": {"type": "str", "description": "(directions) New trigger", "required": False},
                "command": {"type": "str", "description": "New slash command name", "required": False},
                "parents": {"type": "str", "description": "New comma-separated parents (replaces)", "required": False},
                "children": {"type": "str", "description": "New comma-separated children (replaces)", "required": False},
                "tags": {"type": "str", "description": "New comma-separated tags (replaces)", "required": False},
                "aliases": {"type": "str", "description": "New comma-separated aliases (replaces)", "required": False},
                "dev_notes": {
                    "type": "str",
                    "description": (
                        "New development-facing notes (surfaced only in "
                        "dev mode). Pass an empty string to clear."
                    ),
                    "required": False,
                },
                "entry_points": {
                    "type": "str",
                    "description": (
                        "New comma-separated dotted module paths "
                        "(replaces existing)."
                    ),
                    "required": False,
                },
            },
            callable=docs_update,
            mutates_state=True,
            retry_policy="manual",
            search_aliases=[
                "update unit",
                "edit knowledge",
                "modify docs",
                "change knowledge unit",
                "patch docs",
                "edit documentation field",
                "update docs entry",
            ],
        ),
        Capability(
            name="docs_delete",
            description=(
                "Delete a unit from the knowledge store. "
                "Cleans up parent/child references."
            ),
            category="context",
            parameters={
                "path": {"type": "str", "description": "Path of unit to delete", "required": True},
            },
            callable=docs_delete,
            mutates_state=True,
            retry_policy="manual",
            search_aliases=[
                "delete unit",
                "remove knowledge",
                "drop knowledge entry",
                "delete documentation",
                "erase knowledge unit",
                "remove docs unit",
            ],
        ),
        Capability(
            name="docs_move",
            description=(
                "Move a unit to a new path. Updates all parent/child "
                "references across the store."
            ),
            category="context",
            parameters={
                "old_path": {"type": "str", "description": "Current path", "required": True},
                "new_path": {"type": "str", "description": "New path", "required": True},
            },
            callable=docs_move,
            mutates_state=True,
            retry_policy="manual",
            search_aliases=[
                "move unit",
                "rename knowledge",
                "repath",
                "rename docs path",
                "relocate knowledge",
                "change unit path",
                "move documentation",
            ],
        ),
        Capability(
            name="workflow_create",
            description=(
                "Create a new workflow unit (DAG + step instructions). "
                "Use this instead of docs_create for kind='workflow' units — "
                "docs_create does not accept workflow-specific fields."
            ),
            category="context",
            parameters={
                "path": {"type": "str", "description": "Unique path ID (e.g. 'dev/dev-document')", "required": True},
                "name": {"type": "str", "description": "Human-readable name", "required": True},
                "description": {"type": "str", "description": "One-line summary", "required": True},
                "workflow_name": {"type": "str", "description": "Registry slug used with wb_run('<workflow_name>')", "required": True},
                "steps": {
                    "type": "str",
                    "description": (
                        "JSON array of step dicts. Each step requires at least "
                        "id, name, step_type ('reasoning' or 'code'), and "
                        "depends_on (list of prior step ids). Additional keys: "
                        "auto_run, visibility, result_schema, invokes, optional."
                    ),
                    "required": True,
                },
                "step_instructions": {
                    "type": "str",
                    "description": (
                        "JSON object mapping step_id -> instruction text. "
                        "Reasoning steps generally need this; pure auto_run "
                        "steps usually don't."
                    ),
                    "required": False,
                },
                "execution": {"type": "str", "description": "Default execution policy: 'main' or 'subagent' (default 'main')", "required": False},
                "allow_override": {"type": "bool", "description": "Allow per-step execution override (default false)", "required": False},
                "content_full": {"type": "str", "description": "Workflow-level context (philosophy, what-not-to-do). Surfaces at depth='full'.", "required": False},
                "content_summary": {"type": "str", "description": "One-paragraph summary.", "required": False},
                "command": {"type": "str", "description": "Slash command name (e.g. 'wb-dev-document')", "required": False},
                "parents": {"type": "str", "description": "Comma-separated parent paths (typical: domain, e.g. 'dev')", "required": False},
                "children": {"type": "str", "description": "Comma-separated child paths", "required": False},
                "tags": {"type": "str", "description": "Comma-separated search tags", "required": False},
                "aliases": {"type": "str", "description": "Comma-separated search aliases", "required": False},
                "dev_notes": {"type": "str", "description": "Dev-mode-only notes about the workflow's internals", "required": False},
            },
            callable=workflow_create,
            mutates_state=True,
            retry_policy="manual",
            search_aliases=[
                "create workflow",
                "new workflow",
                "author workflow DAG",
                "register workflow",
                "add workflow unit",
                "define workflow",
            ],
        ),
        Capability(
            name="workflow_update",
            description=(
                "Update an existing workflow unit. Only provided fields "
                "change; omitted fields preserved. 'steps' and "
                "'step_instructions' replace/merge rather than patch "
                "individual entries — read the current value, mutate, "
                "and pass the whole structure back."
            ),
            category="context",
            parameters={
                "path": {"type": "str", "description": "Path of workflow to update", "required": True},
                "name": {"type": "str", "description": "New human-readable name", "required": False},
                "description": {"type": "str", "description": "New one-line summary", "required": False},
                "workflow_name": {"type": "str", "description": "New registry slug", "required": False},
                "steps": {
                    "type": "str",
                    "description": (
                        "JSON array replacing the DAG. Callers should read the "
                        "current value via agent_docs, mutate, and pass back."
                    ),
                    "required": False,
                },
                "step_instructions": {
                    "type": "str",
                    "description": (
                        "JSON object merged into step_instructions. Keys "
                        "present in the new dict overwrite; keys absent are "
                        "preserved. Pass the whole dict to replace cleanly."
                    ),
                    "required": False,
                },
                "execution": {"type": "str", "description": "New default execution policy", "required": False},
                "allow_override": {"type": "bool", "description": "New allow_override flag", "required": False},
                "content_full": {"type": "str", "description": "New workflow-level content", "required": False},
                "content_summary": {"type": "str", "description": "New summary", "required": False},
                "command": {"type": "str", "description": "New slash command name", "required": False},
                "parents": {"type": "str", "description": "New comma-separated parent paths (replaces)", "required": False},
                "children": {"type": "str", "description": "New comma-separated child paths (replaces)", "required": False},
                "tags": {"type": "str", "description": "New comma-separated tags (replaces)", "required": False},
                "aliases": {"type": "str", "description": "New comma-separated aliases (replaces)", "required": False},
                "dev_notes": {"type": "str", "description": "New dev-mode-only notes. Pass an empty string to clear.", "required": False},
            },
            callable=workflow_update,
            mutates_state=True,
            retry_policy="manual",
            search_aliases=[
                "update workflow",
                "edit workflow DAG",
                "modify workflow steps",
                "change workflow",
                "patch workflow",
                "edit step instructions",
            ],
        ),
        # ----- Unified knowledge query surface -----
        Capability(
            name="knowledge",
            description=(
                "Search across both system documentation and personal "
                "knowledge from the Obsidian vault. Returns results tagged "
                "with their source scope (system or personal)."
            ),
            category="context",
            parameters={
                "query": {
                    "type": "str",
                    "description": "Natural language search.",
                    "required": False,
                },
                "path": {
                    "type": "str",
                    "description": "Exact unit path for direct lookup.",
                    "required": False,
                },
                "scope": {
                    "type": "str",
                    "description": "Path prefix to filter to a subtree.",
                    "required": False,
                },
                "kind": {
                    "type": "str",
                    "description": (
                        "Filter by kind: directions, system, capability, "
                        "workflow, personal."
                    ),
                    "required": False,
                },
                "category": {
                    "type": "str",
                    "description": (
                        "Filter personal units by category: work_pattern, "
                        "self_regulation, skill_gap, feedback, preference, reference."
                    ),
                    "required": False,
                },
                "severity": {
                    "type": "str",
                    "description": "Filter personal units by severity: HIGH, MODERATE, LOW.",
                    "required": False,
                },
                "depth": {
                    "type": "str",
                    "description": "Content depth: 'index', 'summary' (default), 'full'.",
                    "required": False,
                },
                "top_n": {
                    "type": "int",
                    "description": "Max search results (default 8).",
                    "required": False,
                },
                "dev": {
                    "type": "bool",
                    "description": (
                        "Include dev_notes in full-depth results. "
                        "Auto-set when session dev mode is active."
                    ),
                    "required": False,
                },
            },
            callable=knowledge,
            search_aliases=[
                "knowledge", "search everything", "find",
                "personal patterns", "blindspots", "metacognition",
                "system docs", "unified search",
            ],
        ),
        Capability(
            name="knowledge_personal",
            description=(
                "Search personal knowledge from the Obsidian vault. "
                "Includes minted insights, patterns, feedback, preferences. "
                "Supports filtering by category and severity."
            ),
            category="context",
            parameters={
                "query": {
                    "type": "str",
                    "description": "Natural language search.",
                    "required": False,
                },
                "path": {
                    "type": "str",
                    "description": "Exact unit path for direct lookup.",
                    "required": False,
                },
                "scope": {
                    "type": "str",
                    "description": "Path prefix (e.g. 'personal/metacognition/').",
                    "required": False,
                },
                "category": {
                    "type": "str",
                    "description": (
                        "Filter by category: work_pattern, self_regulation, "
                        "skill_gap, feedback, preference, reference."
                    ),
                    "required": False,
                },
                "severity": {
                    "type": "str",
                    "description": "Filter by severity: HIGH, MODERATE, LOW.",
                    "required": False,
                },
                "depth": {
                    "type": "str",
                    "description": "Content depth: 'index', 'summary' (default), 'full'.",
                    "required": False,
                },
                "top_n": {
                    "type": "int",
                    "description": "Max search results (default 8).",
                    "required": False,
                },
                "dev": {
                    "type": "bool",
                    "description": "Include dev_notes. Auto-set in dev mode.",
                    "required": False,
                },
            },
            callable=knowledge_personal,
            search_aliases=[
                "personal knowledge", "my patterns", "calibration",
                "metacognition patterns", "blindspot patterns",
                "feedback", "preferences", "vault knowledge",
            ],
        ),
        Capability(
            name="dev_mode_toggle",
            description=(
                "Toggle dev mode for the current session. When active, "
                "all knowledge queries automatically include dev_notes — "
                "development-facing documentation that operational agents "
                "don't need. Use True to enable, False to disable, or "
                "omit to toggle."
            ),
            category="context",
            parameters={
                "enabled": {
                    "type": "bool",
                    "description": (
                        "True=on, False=off, omit=toggle current state."
                    ),
                    "required": False,
                },
            },
            callable=_dev_mode_toggle,
            search_aliases=[
                "dev mode", "developer mode", "development mode",
                "toggle dev", "enable dev notes",
            ],
        ),
        Capability(
            name="knowledge_mint",
            description=(
                "Create or update a personal knowledge unit in the "
                "Obsidian vault. Generates a markdown file with YAML "
                "frontmatter. If the file already exists, appends "
                "new evidence."
            ),
            category="context",
            parameters={
                "name": {
                    "type": "str",
                    "description": "Human-readable name (e.g., 'Branch Explosion').",
                    "required": True,
                },
                "category": {
                    "type": "str",
                    "description": (
                        "Category: work_pattern, self_regulation, "
                        "skill_gap, feedback, preference, reference."
                    ),
                    "required": True,
                },
                "content_body": {
                    "type": "str",
                    "description": "Full markdown body. If empty, builds from structured fields.",
                    "required": False,
                },
                "severity": {
                    "type": "str",
                    "description": "HIGH, MODERATE, or LOW (optional).",
                    "required": False,
                },
                "tags": {
                    "type": "str",
                    "description": "Comma-separated tags.",
                    "required": False,
                },
                "context_before": {
                    "type": "str",
                    "description": "Comma-separated unit paths to chain before.",
                    "required": False,
                },
                "context_after": {
                    "type": "str",
                    "description": "Comma-separated unit paths to chain after.",
                    "required": False,
                },
                "evidence": {
                    "type": "str",
                    "description": "Initial evidence observation.",
                    "required": False,
                },
                "definition": {
                    "type": "str",
                    "description": "Pattern definition text.",
                    "required": False,
                },
                "triggers": {
                    "type": "str",
                    "description": "What typically triggers this pattern.",
                    "required": False,
                },
                "signals": {
                    "type": "str",
                    "description": "Observable signals.",
                    "required": False,
                },
                "default_response": {
                    "type": "str",
                    "description": "Agent's default response.",
                    "required": False,
                },
            },
            callable=mint_personal_unit,
            mutates_state=True,
            retry_policy="manual",
            search_aliases=[
                "mint", "create personal", "add pattern",
                "create insight", "new personal unit",
                "mint knowledge", "add observation",
            ],
        ),
        Capability(
            name="knowledge_index_rebuild",
            description=(
                "Rebuild the knowledge search index. Uses the persistent "
                "on-disk cache by default — unchanged units keep their "
                "cached vectors, so typical warm rebuilds are <1s. Pass "
                "force=true to purge the cache and re-embed everything "
                "(slow — 1-3 minutes for the full store)."
            ),
            category="context",
            parameters={
                "force": {
                    "type": "bool",
                    "description": (
                        "Purge the dense-vector cache before rebuilding. "
                        "Re-embeds every unit. Default: False."
                    ),
                    "required": False,
                },
            },
            callable=knowledge_index_rebuild,
            search_aliases=[
                "rebuild index", "reindex knowledge", "embedding index",
                "knowledge index", "rebuild search",
            ],
        ),
        Capability(
            name="knowledge_index_status",
            description=(
                "Check the knowledge search index status: whether it's built, "
                "unit count, and whether dense vectors are available."
            ),
            category="context",
            parameters={},
            callable=knowledge_index_status,
            search_aliases=[
                "index status",
                "knowledge index status",
                "is search index built",
                "dense vector status",
                "search index health",
                "knowledge index health",
                "cache hit rate",
            ],
        ),
    ]


# ---------------------------------------------------------------------------
# Artifact capabilities
# ---------------------------------------------------------------------------


def _artifact_capabilities() -> list[Capability]:

    def artifact_save(
        content: str,
        type: str,
        slug: str,
        ext: str = "json",
        tags: str = "",
        description: str = "",
        ttl_days: int | None = None,
        agent_session_id: str = "",
    ) -> dict:
        from work_buddy.artifacts import get_store

        tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
        sid = agent_session_id or None

        store = get_store()
        rec = store.save(
            content=content,
            type=type,
            slug=slug,
            ext=ext,
            tags=tag_list,
            description=description,
            session_id=sid,
            ttl_days=ttl_days,
        )
        return rec.to_dict()

    def artifact_list(
        type: str = "",
        since: str = "",
        tags: str = "",
        session_id: str = "",
        include_expired: bool = False,
        limit: int = 50,
    ) -> dict:
        from work_buddy.artifacts import get_store
        from datetime import datetime

        since_dt = datetime.fromisoformat(since) if since else None
        tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else None

        store = get_store()
        records = store.list(
            type=type or None,
            since=since_dt,
            tags=tag_list,
            session_id=session_id or None,
            include_expired=include_expired,
            limit=limit,
        )
        return {"count": len(records), "artifacts": [r.to_dict() for r in records]}

    def artifact_get(id: str) -> dict:
        from work_buddy.artifacts import get_store

        store = get_store()
        rec = store.get(id)
        result = rec.to_dict()
        # Include content inline if small enough (< 50KB)
        if rec.size_bytes < 50_000:
            try:
                result["content"] = rec.path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                result["content"] = "(binary content — use file path to read)"
        else:
            result["content"] = f"(large file: {rec.size_bytes} bytes — use file path to read)"
        return result

    def artifact_delete(id: str) -> dict:
        from work_buddy.artifacts import get_store

        store = get_store()
        found = store.delete(id)
        return {"deleted": found, "id": id}

    def artifact_cleanup(dry_run: bool = True) -> dict:
        from work_buddy.artifacts import get_store

        store = get_store()
        return store.cleanup(dry_run=dry_run)

    def commit_record(
        commit_hash: str,
        message: str,
        branch: str = "",
        files_changed: str = "",
        tests_run: str = "",
        tests_passed: int = 0,
        tests_failed: int = 0,
        knowledge_units_updated: str = "",
        summary: str = "",
        agent_session_id: str = "",
    ) -> dict:
        """Record structured commit metadata as an artifact."""
        import json
        from work_buddy.artifacts import get_store

        files_list = [f.strip() for f in files_changed.split(",") if f.strip()] if files_changed else []
        tests_list = [t.strip() for t in tests_run.split(",") if t.strip()] if tests_run else []
        ku_list = [k.strip() for k in knowledge_units_updated.split(",") if k.strip()] if knowledge_units_updated else []

        record = {
            "commit_hash": commit_hash,
            "message": message,
            "branch": branch,
            "files_changed": files_list,
            "tests": {
                "files_run": tests_list,
                "passed": tests_passed,
                "failed": tests_failed,
            },
            "knowledge_units_updated": ku_list,
            "summary": summary,
        }

        store = get_store()
        slug = f"commit-{commit_hash[:7]}"
        rec = store.save(
            content=json.dumps(record, indent=2),
            type="commit",
            slug=slug,
            ext="json",
            tags=["commit", branch] if branch else ["commit"],
            description=summary or message[:80],
            session_id=agent_session_id or None,
        )

        result = rec.to_dict()
        result["record"] = record
        return result

    return [
        Capability(
            name="artifact_save",
            description=(
                "Save an artifact (context bundle, export, report, snapshot, or scratch) "
                "to the centralized data store with metadata and TTL-based lifecycle."
            ),
            category="artifacts",
            parameters={
                "content": {"type": "str", "description": "Content to save (text)", "required": True},
                "type": {
                    "type": "str",
                    "description": (
                        "Artifact type: context (7d TTL), export (90d), "
                        "report (30d), snapshot (14d), scratch (3d)"
                    ),
                    "required": True,
                },
                "slug": {"type": "str", "description": "Short descriptive name (kebab-case, used in filename)", "required": True},
                "ext": {"type": "str", "description": "File extension (default: json)", "required": False},
                "tags": {"type": "str", "description": "Comma-separated tags for filtering", "required": False},
                "description": {"type": "str", "description": "Human-readable description", "required": False},
                "ttl_days": {"type": "int", "description": "Override default TTL in days", "required": False},
                "agent_session_id": {"type": "str", "description": "Session ID (auto-injected by gateway)", "required": False},
            },
            callable=artifact_save,
            mutates_state=True,
            retry_policy="replay",
            search_aliases=[
                "save artifact", "store output", "write artifact",
                "save bundle", "save export", "save report",
            ],
        ),
        Capability(
            name="artifact_list",
            description=(
                "List artifacts in the data store, filtered by type, recency, "
                "tags, or session. Sorted by creation time (newest first)."
            ),
            category="artifacts",
            parameters={
                "type": {"type": "str", "description": "Filter by type (context, export, report, snapshot, scratch)", "required": False},
                "since": {"type": "str", "description": "ISO datetime — only artifacts after this time", "required": False},
                "tags": {"type": "str", "description": "Comma-separated tags — artifact must have all", "required": False},
                "session_id": {"type": "str", "description": "Filter to artifacts from this session", "required": False},
                "include_expired": {"type": "bool", "description": "Include expired artifacts (default: false)", "required": False},
                "limit": {"type": "int", "description": "Max results (default: 50)", "required": False},
            },
            callable=artifact_list,
            search_aliases=[
                "list artifacts", "show artifacts", "find artifacts",
                "browse data", "artifact inventory",
            ],
        ),
        Capability(
            name="artifact_get",
            description=(
                "Retrieve an artifact by ID (filename stem). Returns metadata "
                "and content (inline if < 50KB, otherwise file path)."
            ),
            category="artifacts",
            parameters={
                "id": {"type": "str", "description": "Artifact ID (filename stem, e.g. '20260412-093000_weekly-review')", "required": True},
            },
            callable=artifact_get,
            search_aliases=[
                "get artifact",
                "read artifact",
                "fetch artifact",
                "retrieve artifact",
                "open artifact",
                "load artifact",
                "artifact contents",
            ],
        ),
        Capability(
            name="artifact_delete",
            description="Delete an artifact and its metadata by ID.",
            category="artifacts",
            parameters={
                "id": {"type": "str", "description": "Artifact ID to delete", "required": True},
            },
            callable=artifact_delete,
            mutates_state=True,
            retry_policy="manual",
            search_aliases=[
                "delete artifact",
                "remove artifact",
                "drop artifact",
                "erase artifact",
                "remove saved output",
                "clean up artifact",
                "delete report file",
            ],
        ),
        Capability(
            name="artifact_cleanup",
            description=(
                "Run TTL-based cleanup: delete all artifacts past their expiry. "
                "Use dry_run=true (default) to preview what would be deleted."
            ),
            category="artifacts",
            parameters={
                "dry_run": {"type": "bool", "description": "Preview only, don't delete (default: true)", "required": False},
            },
            callable=artifact_cleanup,
            mutates_state=True,
            retry_policy="manual",
            search_aliases=[
                "cleanup artifacts", "sweep expired", "artifact gc",
                "prune artifacts", "data cleanup",
            ],
        ),
        Capability(
            name="commit_record",
            description=(
                "Record structured commit metadata (hash, files, test results, "
                "knowledge units updated) as an artifact. Called after a successful "
                "git commit to enable enriched commit cards in the dashboard."
            ),
            category="artifacts",
            parameters={
                "commit_hash": {"type": "str", "description": "Git commit hash (7+ chars)", "required": True},
                "message": {"type": "str", "description": "Commit message", "required": True},
                "branch": {"type": "str", "description": "Branch name", "required": False},
                "files_changed": {"type": "str", "description": "Comma-separated file paths", "required": False},
                "tests_run": {"type": "str", "description": "Comma-separated test file names", "required": False},
                "tests_passed": {"type": "int", "description": "Number of tests passed", "required": False},
                "tests_failed": {"type": "int", "description": "Number of tests failed", "required": False},
                "knowledge_units_updated": {"type": "str", "description": "Comma-separated knowledge store paths updated", "required": False},
                "summary": {"type": "str", "description": "1-2 sentence summary of the commit", "required": False},
                "agent_session_id": {"type": "str", "description": "Session ID (auto-injected by gateway)", "required": False},
            },
            callable=commit_record,
            mutates_state=True,
            retry_policy="replay",
            search_aliases=[
                "record commit", "commit metadata", "save commit info",
                "log commit", "commit artifact",
            ],
        ),
    ]


def _email_capabilities() -> list[Capability]:
    """Capabilities exposed by the email/Thunderbird integration.

    All callables flow through ``work_buddy.email.provider.get_email_provider``,
    which currently returns the Thunderbird HTTP client. The ``thunderbird``
    tool probe gates these so they're filtered out of the registry when the
    bridge isn't reachable.
    """
    from work_buddy.email.capabilities import (
        email_accounts,
        email_display,
        email_get,
        email_health,
        email_triage_run,
    )

    return [
        Capability(
            name="email_health",
            description=(
                "Liveness probe for the email bridge. Returns the bridge's "
                "/health payload (port, version, allowed-account count). Use "
                "this when the user reports email features are missing — it "
                "distinguishes 'bridge down' from 'no accounts allowed'."
            ),
            category="email",
            parameters={},
            callable=email_health,
            requires=["thunderbird"],
            search_aliases=[
                "email health", "email bridge status",
                "thunderbird bridge running", "is email working",
                "email provider check",
            ],
        ),
        Capability(
            name="email_accounts",
            description=(
                "List the email accounts visible through the bridge — only "
                "accounts the user has explicitly allowed in the extension's "
                "options page are exposed (default-deny)."
            ),
            category="email",
            parameters={},
            callable=email_accounts,
            requires=["thunderbird"],
            search_aliases=[
                "list email accounts", "show mail accounts",
                "thunderbird accounts", "what email is connected",
            ],
        ),
        Capability(
            name="email_triage_run",
            description=(
                "Run one BackgroundTriageProducer pass over recent email and "
                "drop new messages into the triage Review pool as raw entries. "
                "Idempotent — re-running over an unchanged inbox is a no-op. "
                "Slice 1: verdict pass disabled; Sonnet/Opus classification of "
                "email is a follow-up."
            ),
            category="email",
            parameters={
                "days_back": {"type": "int", "description": "How far back to scan (default 2)", "required": False},
                "max_messages": {"type": "int", "description": "Hard cap on candidates per run (default 50)", "required": False},
                "unread_only": {"type": "bool", "description": "Only consider unread messages (default true)", "required": False},
                "folder_path": {"type": "str", "description": "Limit to a specific folder URI (e.g. an inbox)", "required": False},
                "account_id": {"type": "str", "description": "Limit to a specific account", "required": False},
                "include_body_chars": {"type": "int", "description": "Fetch body up to N chars per message and include in TriageItem.text. Defaults: 0 when triage.verdict_pass.enabled is false (headers-only is plenty for raw capture); 1500 when verdict pass is on (the LLM needs body content to discriminate newsletter vs action-required).", "required": False},
                "force": {"type": "bool", "description": "Bypass idempotence and per-item dedup", "required": False},
                "dry_run": {"type": "bool", "description": "Collect candidates but don't write to the pool", "required": False},
                "tier": {"type": "str", "description": "Override starting LLM tier for the verdict agent (only used when triage.verdict_pass.enabled is true). One of 'local_fast', 'frontier_balanced' (default), 'frontier_best'. call_for_verdict still escalates on failure.", "required": False},
            },
            callable=email_triage_run,
            requires=["thunderbird"],
            mutates_state=True,
            retry_policy="manual",
            search_aliases=[
                "triage email", "scan inbox", "email triage scan",
                "check unread mail", "process new email",
                "feed email into triage", "email backlog",
            ],
        ),
        Capability(
            name="email_get",
            description=(
                "Fetch one email message by its operational handle "
                "(provider_message_id + folder_path) — returns the body up to "
                "max_body_chars chars plus all summary fields."
            ),
            category="email",
            parameters={
                "provider_message_id": {"type": "str", "description": "RFC Message-ID (or backend handle) from a triage card", "required": True},
                "folder_path": {"type": "str", "description": "Backend folder URI (from the triage card)", "required": True},
                "max_body_chars": {"type": "int", "description": "Body truncation cap (default 8000)", "required": False},
            },
            callable=email_get,
            requires=["thunderbird"],
            search_aliases=[
                "read email", "open email", "get email body",
                "show message content", "fetch mail",
            ],
        ),
        Capability(
            name="email_display",
            description=(
                "Open a message in Thunderbird's UI. Useful when the user wants "
                "to read it themselves — does not modify anything."
            ),
            category="email",
            parameters={
                "provider_message_id": {"type": "str", "description": "RFC Message-ID from a triage card", "required": True},
                "folder_path": {"type": "str", "description": "Backend folder URI", "required": True},
                "mode": {"type": "str", "description": "'3pane' (default), 'tab', or 'window'", "required": False},
            },
            callable=email_display,
            requires=["thunderbird"],
            search_aliases=[
                "open in thunderbird", "show email in thunderbird",
                "display message", "open mail in client",
            ],
        ),
    ]
