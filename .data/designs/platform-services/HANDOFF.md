# Platform services wayfinder handoff

## Purpose

This handoff orients a locally operating agent to the current platform-architecture effort. The goal is **not** to start implementing a platform immediately. The current phase is a Wayfinder planning effort: resolve architectural decisions until the route is clear, then derive the implementation/PR plan from those decisions.

Canonical map: https://github.com/KadenMc/work-buddy/issues/291

## Destination

Reach a decision-complete architecture and implementation plan for turning work-buddy’s existing reusable subsystems into a coherent local application platform: user Apps and agent Skills can discover, acquire, and compose stable platform services without reaching into stores, provider internals, dashboard-only seams, or duplicate legacy implementations.

The intended product direction is that a user can describe a small application, dashboard view, workflow, or personal tool and a work-buddy agent can build it rapidly by composing already-existing platform primitives rather than recreating search, embeddings, inference, tasks, calendar access, events, persistence, UI hosting, etc.

The likely architectural bias is:

- local-first;
- modular monolith where practical;
- a small number of justified long-running processes rather than microservices for symmetry;
- explicit public module/service boundaries;
- ports/adapters and provider-neutral interfaces where multiple implementations exist;
- self-service, discoverable platform interfaces for Apps and agents;
- architecture fitness tests so generated code cannot quietly bypass public boundaries.

Do not treat these as irrevocably decided where a Wayfinder ticket exists to decide them.

## Why this effort exists

An audit found that work-buddy is already surprisingly close to a composable application platform, but unevenly so.

Strong examples include:

- `TaskApplicationService` as the only supported task mutation boundary;
- `EmailProvider` and `CalendarProvider` as provider-neutral ports;
- `ContextSource` plus one `ContextRequest` shape across callers;
- `work_buddy.websearch` with an explicit frozen public surface;
- `work_buddy.events` as a durable event backbone;
- `work_buddy/index/` as a domain-agnostic consolidated search/index engine;
- the React Dashboard Contribution/View APIs and contribution registry.

The main problem is global irregularity: there is no single architectural contract answering **what reusable work-buddy services exist, how Apps declare/use them, what makes a service stable, and which internals are forbidden to consumers**.

Examples of current debt:

- the current agent-facing `Capability` term collides with broader architectural use of “capability”; migration to `Skill` is under consideration;
- Projects lacks the equivalent of the Tasks application-service boundary and consumers reach into `projects.store`, including private `_db_path` access;
- the new consolidated index is designed to subsume `knowledge/index.py`, `vault_index/`, and `ir/`, but is still flag-gated/inert while old hot paths remain authoritative;
- domain code still imports `work_buddy.dashboard.events`, even though durable domain events and lossy UI invalidations are conceptually distinct;
- multiple extension registries depend on process-global or import-side-effect registration, which is acceptable for first-party code but weak for agent/user-authored extensions;
- many domains independently reimplement SQLite connection/migration policy;
- the Dashboard already has a strong App/view/widget host layer, but its own architecture explicitly says the future App API/domain runtime beneath `ViewProvider` is not yet implemented.

## Terminology under active consideration

Do not casually cement these names in implementation until the vocabulary ticket is resolved.

Current working distinction:

- **Operation**: stable executable primitive, currently `op.wb.*`.
- **Skill**: likely future name for today's agent-discoverable `Capability` concept; agent-facing action/procedure semantics, searchability, consent, retries, invocation policy.
- **Workflow**: multi-step orchestration; whether it remains a peer concept or becomes a Skill subtype is unresolved.
- **Service**: reusable domain/platform interface that internal code, Skills, workflows, and Apps can depend on.
- **App**: user-facing composition of domain logic/state plus dashboard/UI contributions, backed by platform services.

The important distinction is that a Skill is **not** the same thing as a Service. A calendar Skill may invoke the Calendar service; a Calendar App may use the same Calendar service without going through the agent-facing Skill layer.

## Wayfinder operating rules

The planning method comes from `mattpocock/skills` → `/wayfinder`.

Follow the actual skill if installed locally. If not, read:

https://github.com/mattpocock/skills/blob/main/skills/engineering/wayfinder/SKILL.md

Critical rules:

- This phase **plans; it does not implement the destination**.
- Each Wayfinder child issue is a **decision ticket**: its job is to resolve one question, not deliver a slice of the build.
- Never resolve more than one non-research decision ticket in one agent session.
- Research tickets may be run in parallel.
- Before work, claim the ticket using the tracker's available assignment mechanism so concurrent agents skip it.
- Record the answer on the ticket, close it, and append only a one-line gist/link to the map’s `Decisions so far`.
- As decisions clear fog, create newly specifiable decision tickets and remove the corresponding item from `Not yet specified`.
- Do not pre-slice implementation PRs while architectural decisions still determine their shape.

GitHub-native parent/child/blocking relationships were not available through the connector that created the map. **First local-agent housekeeping task:** inspect issue #291 and issues #292–#302, attach them as sub-issues where appropriate, and wire native blocking relationships according to the true decision dependencies. Do not invent dependencies merely to create a neat DAG.

## Current Wayfinder map

Map:

- #291 — **Wayfinder map: composable work-buddy platform services for Apps and Skills**

Current decision tickets:

- #292 — **Decide platform vocabulary and layer boundaries** (`wayfinder:grilling`)
- #293 — **Decide the canonical platform service contract and catalog** (`wayfinder:grilling`)
- #294 — **Decide App API and service acquisition semantics** (`wayfinder:grilling`)
- #295 — **Research which existing subsystems qualify as initial platform services** (`wayfinder:research`)
- #296 — **Decide how platform services are exposed across process boundaries** (`wayfinder:grilling`)
- #297 — **Decide Projects domain boundary and migration path** (`wayfinder:grilling`)
- #298 — **Decide consolidated indexing cutover and service boundary** (`wayfinder:grilling`)
- #299 — **Decide event publication boundary for platform consumers** (`wayfinder:grilling`)
- #300 — **Decide extension registration and namespacing rules** (`wayfinder:grilling`)
- #301 — **Decide App trust, permissions, and isolation model** (`wayfinder:grilling`)
- #302 — **Decide platform persistence primitives without centralizing domain data** (`wayfinder:grilling`)

Read each ticket body before acting; the body contains the exact decision question.

## Recommended first sessions

Start breadth-first rather than by jumping into implementation.

A sensible initial sequence is:

1. **#295 — subsystem service-readiness research.** This is AFK and can begin immediately. Re-audit the current repository rather than trusting earlier conclusions, because the codebase is moving quickly. Produce a precise service-readiness matrix and explicit recommendations for initial admission vs remediation/deferral.
2. **#292 — vocabulary/layer boundaries.** This should be HITL. Use domain modeling and grilling with the user. Resolve `Operation` / `Skill` / `Workflow` / `Service` / `App` before code and docs begin proliferating new meanings.
3. **#293 — service contract/catalog.** Use findings from #295 and the vocabulary decision from #292. Decide the minimum cross-service registration/acquisition contract without erasing domain-specific typed APIs.
4. **#294 — App API.** Once the service contract exists, define how Apps declare dependencies, acquire services, own state, receive events, and project into the existing Dashboard View API.

Several other tickets can be investigated in parallel once obvious blockers are wired. In particular #297, #298, #299, and #302 are concrete architecture-remediation decisions that should feed the eventual implementation plan.

## Repository areas to inspect early

At minimum inspect:

- `docs/architecture.md`
- `CLAUDE.md`
- `work_buddy/mcp_server/registry.py`
- `work_buddy/mcp_server/op_registry.py`
- `work_buddy/mcp_server/ops/`
- `work_buddy/tasks/service.py`
- `work_buddy/tasks/store.py`
- `work_buddy/projects/`
- `work_buddy/context/`
- `work_buddy/calendar/`
- `work_buddy/email/`
- `work_buddy/websearch/`
- `work_buddy/events/`
- `work_buddy/embedding/`
- `work_buddy/ir/`
- `work_buddy/index/`
- `work_buddy/settings/`
- `work_buddy/artifacts/`
- `work_buddy/document_kernel/`
- `work_buddy/messaging/`
- `dashboard-react/ARCHITECTURE.md`
- `dashboard-react/COMPONENTS.md`
- `dashboard-react/src/app/dashboardRegistry.ts`

Also search for direct imports of `*.store`, private symbols such as `_db_path`, direct `sqlite3.connect`, provider implementation imports, import-time registration, direct dashboard event imports from non-dashboard domains, and parallel search/index implementations.

## Architectural questions the final plan must answer

By the time the Wayfinder map is complete, an implementation agent should not need to improvise answers to the following:

- What exactly is a Service in work-buddy?
- How is a Service identified, versioned, registered, discovered, and acquired?
- What is the relationship between Services, Operations, Skills, Workflows, and Apps?
- Which dependencies are legal between these layers?
- How does an App declare service dependencies and permissions?
- What is the stable App-facing API beneath the Dashboard View/Contribution APIs?
- When does a service remain in-process versus expose a transport projection?
- How are errors, cancellation, timeouts, health, revisions, idempotency, consent, and events represented?
- Which existing subsystems enter the initial Service Catalog?
- Which need remediation first?
- What becomes the canonical search/index service?
- How are extension points registered safely and namespaced?
- How are user-/agent-authored Apps trusted, permissioned, and isolated?
- Which persistence mechanics become common infrastructure while schemas/data remain domain-owned?
- Which implementation paths become forbidden so future agents cannot bypass the public interface?
- What ADRs are required to preserve alternatives considered, tradeoffs, and rejected/deferred options that code/tests cannot communicate?

## Important engineering principles

### Prefer strong module boundaries over gratuitous microservices

A logical Service does not imply its own daemon. Keep a subsystem in-process unless process isolation, concurrency, resource lifetime, fault containment, or another concrete runtime concern justifies a process boundary.

### One supported public boundary per reusable subsystem

If an App, Skill, workflow, or another domain is expected to reuse a subsystem, it should have one supported semantic entry point. Stores, SQLite paths, provider implementations, transport details, and migration scaffolding should remain private unless explicitly part of the public contract.

### Do not erase useful domain types

The Service Catalog should standardize discovery/acquisition/metadata/lifecycle, not reduce every domain to `call(name, payload)`. Prefer typed contracts such as Calendar-, Tasks-, Search-, and Context-shaped APIs inside Python.

### Adapters belong at boundaries

Google Calendar, Obsidian compatibility, Jina/DDGS, local/remote inference providers, etc. should remain replaceable implementations beneath stable ports. Apps should depend on the work-buddy semantic service rather than a concrete provider.

### Migration duplication is not automatically bad

Compatibility adapters and strangler migrations are acceptable when there is one declared target authority and a deletion/cutover path. Flag indefinite dual authority and new consumers targeting legacy paths.

### Agentic development raises the bar for enforcement

An agent will often copy the shortest discoverable path. If private stores and official APIs are equally importable, documentation alone is not enough. The eventual implementation should consider import-boundary linting, architecture tests, public package surfaces, allowlists, or other fitness functions that make bypasses fail loudly.

### Keep decision history in ADRs

Do not rely on code/tests alone for system-wide rationale. Once decisions become durable, record alternatives considered, tradeoffs, and why alternatives were rejected or deferred in ADRs or equivalent concise architecture records. Avoid duplicating source-local contracts in prose.

## Definition of done for the Wayfinder phase

The Wayfinder phase is complete when:

- all decision tickets required to reach the destination are closed;
- `Not yet specified` on #291 is empty or contains only consciously deferred/out-of-scope material;
- the map’s `Decisions so far` points to every consequential decision;
- architectural decisions that need durable rationale have ADRs planned or written;
- there is no consequential design choice left for an implementation agent to invent;
- the final decisions can be converted into an executable, dependency-aware set of implementation tickets/PRs sized for local-agent sessions.

Only then create the implementation/PR map.

## What to do right now

If you are the first local agent receiving this handoff:

1. Read #291 and all open child decision tickets.
2. Read `/wayfinder` itself.
3. Inspect the repository’s current architecture and any newer work that may supersede this handoff.
4. Repair native sub-issue/blocking metadata if your GitHub tooling supports it.
5. Pick an unclaimed frontier ticket. Prefer #295 if it is still open and unclaimed because it is AFK research and informs the rest of the map.
6. Claim it before working.
7. Resolve **that decision only** according to its ticket type.
8. Record the resolution on the issue, close it, update #291’s one-line decision index, and graduate any newly visible fog into fresh decision tickets.
9. Stop. A new session should take the next frontier decision.

Do not begin implementing the platform merely because the intended direction seems obvious. The purpose of this phase is to make the later implementation boring, explicit, and parallelizable.