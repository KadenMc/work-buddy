# React dashboard architecture

This document records the implemented Journal scaffold, not a future SDK promise. The
public seams are deliberately small and JSON-compatible so fixture, in-memory, legacy,
and future App-backed providers can drive the same dashboard composition.

## Runtime shape

```text
/app/journal
  -> DashboardApp (router, Header, registry-generated navigation)
  -> contribution-projected lazy standard view module
  -> Dashboard Core mount (selects module-provided provider/repository/chrome)
  -> ViewHost (view session + personal composition)
       -> desktop: ReactGridLayoutAdapter
       -> mobile: canonical one-column document flow
       -> provider.loadWidget per visible instance
       -> WidgetHost per hydrated instance
            -> host frame/state/error boundary
            -> lazy reusable renderer with bound input + emit(intent)
```

`ThemeProvider`, the singleton `DashboardEventProvider`, and the host-level
`DashboardAnnouncer` wrap the router. Header and every view therefore share one theme,
one `/api/events` connection, and one accessibility announcement surface.

## Boundaries and dependency direction

The dashboard uses these names precisely:

- **Dashboard Contribution API** is the metadata/module-discovery umbrella:
  `AppContribution`, `ViewDefinition`, `WidgetRoleContract`, `WidgetDefinition`, and
  `WidgetModule`.
- **Dashboard View API** is the App-to-dashboard snapshot and intent contract.
  `ViewProvider` is an implementation of that API, not a second public service layer.
- **Adapter** is reserved for a provider that translates an incompatible source. The
  `LegacyFlaskViewAdapter` earns the name; fixture, in-memory, and future native
  providers do not automatically do so.
- **Widget Renderer Contract** is the local host-to-renderer boundary. It supplies
  already-bound input, `emit(intent)`, identity, measured size mode, edit state, and a
  resolved theme summary. It excludes URLs, SSE, arbitrary resource discovery, and
  direct App/System operations.
- A future **App API** sits behind an App-owned `ViewProvider` and may aggregate future
  systems, workflows, capabilities, and managed artifacts. Those domain runtimes are not
  implemented by this UI scaffold.

The runtime data path is intentionally one-way:

```text
App/domain data -> ViewProvider -> ViewSnapshot -> bound widget input
widget UI intent -> ViewProvider.dispatch -> accepted revision -> reconcile -> snapshot
```

Cross-widget effects occur only when the provider publishes a new snapshot revision.
A renderer never mutates a sibling or receives the provider itself.

## Contribution registry

`src/app/dashboardRegistry.ts` registers reusable library Apps before the Journal App.
Registration is atomic: a contribution is not visible if any role, renderer module,
view module, trust provenance, layout, route, size, identity, theme declaration, or
default selection is invalid. Standard views use a 24-column grid.

The registry enforces, among other invariants:

- lowercase namespaced App/view/type/module IDs and versioned role IDs;
- unique Apps, views, routes, widget types, roles, modules, slots, and default instances;
- lazy renderer bindings for every registered widget type;
- a structured `standard-widget-view` runtime module for every shareable view;
- rejection of arbitrary React page roots by the standard registry;
- JSON-compatible settings and bindings;
- default layouts within the grid and each widget's min/max size contract;
- non-overlapping defaults and complete reading/mobile orders;
- role-compatible default and replacement widget types; and
- exact role input schemas, all required output-intent schemas, and Theme Contract v1
  declarations for standard widgets.

Navigation and lazy page modules are both projected from registered contributions.
A standard view module may construct only a provider, personalization repository,
provider label, and optional chrome renderer; Dashboard Core always constructs
`ViewHost`. The separate `developer-root` contract is only a modeled future escape
hatch: the standard registry rejects it and no runtime currently evaluates it.

Trust is explicit provenance (`native`, `verified`, `personal`, `developer`, or
`unverified`), not an inference from an `wb.*`-looking ID. Built-in contributions are
registered as native; spoofed namespaces remain unverified.

## Identity, ownership, and replacement

Three identities must never be collapsed:

| Identity | Example | Lifecycle |
|---|---|---|
| Widget type | `wb.capture.quick-text` | Reusable implementation published once and placeable across views |
| View slot | `wb.journal.main` + `capture` | Stable App-owned purpose that survives renderer replacement |
| Widget instance | `journal:capture` or `personal:<uuid>` | User-owned placement that survives move, resize, settings changes, and compatible replacement |

IDs do not encode provider targets or mutable implementation choices. Required presence
belongs to a slot's functional role, not its default type. Journal's required `capture`
slot currently selects `wb.capture.quick-text`, but a future compatible Capture type may
replace it while retaining the slot, instance, layout, and migratable settings.

Ownership is similarly independent:

- the Journal App owns `wb.journal.main`, its slots, presence policies, defaults,
  bindings, and domain intent semantics;
- the Capture, Timeline, and Notes library Apps own their role contracts, reusable
  WidgetDefinitions, and renderer modules;
- the user owns shown/hidden state, placement, settings, substitutions, and personal
  instances; and
- Dashboard Core owns validation, lifecycle, hosting, layout, and compatibility checks.

The initial reusable library is:

| Publisher | Role | Widget type | Library path |
|---|---|---|---|
| `wb.capture` | `wb.widget-role.capture@1` | `wb.capture.quick-text` | Capture / Quick Capture |
| `wb.timeline` | `wb.widget-role.day-timeline@1` | `wb.timeline.day` | Time / Timelines / Day Timeline |
| `wb.notes` | `wb.widget-role.running-notes@1` | `wb.notes.running` | Notes / Running Notes |

Journal only selects these library types; it does not own or fork their renderers.
Capture and Timeline slots are required with plain-language invariant reasons. Running
Notes is `default_on` and may be hidden without losing its stored configuration.

## Providers, sessions, and events

Every provider implements `loadView`, `loadWidget`, `dispatch`, and `reconcile` from
`src/dashboard/providers/ViewProvider.ts`; providers may also expose local
invalidations and an explicit per-view allowlist of addable widget types.
`useViewSession` rejects stale async requests, tracks pending intents, validates the
JSON-compatible view/widget snapshots and intent/reconcile results at the provider
boundary, reconciles accepted intent revisions, and treats snapshots as immutable
queryable truth.

`DashboardEventProvider` owns the only EventSource. It normalizes both the legacy event
envelope and a CloudEvents-shaped projection, ignores heartbeats/malformed frames, and
publishes invalidations to view sessions. Because the UI stream is lossy and
non-replaying, connection, reconnection, foreground return, and numeric revision gaps
request provider reconciliation. Widgets never parse transport events.

### Journal provider modes

- **Demo (default):** `InMemoryJournalProvider` starts from deterministic July 11
  fixtures. Exact capture text is committed immediately, smart annotations settle via a
  later revision, dumb capture has no per-entry processing, and the clock can advance
  without wall-clock waits. The UI labels this data as demo.
- **Legacy live:** `?provider=legacy` selects `LegacyFlaskViewAdapter`, which reads
  same-origin `GET /api/automation/today`. It maps only honest `now`, `work_hours`,
  generated plan, status, and error information into a partial read-only timeline.
  Capture, Running Notes, native Journal records/day boundary, calendar provenance,
  smart processing, writes, and native revisions remain unavailable rather than being
  fabricated.
- **Fixture:** `FixtureViewProvider` is available to component/contract tests for named
  immutable ready, empty, stale, offline, read-only, unavailable, error, and stress
  states. It is not a silent runtime fallback.

The future native Journal/App store can replace the provider without changing renderer
props or the personalization shape.

## View hosting, layout, and personalization

`ViewHost` resolves an App default plus a personal patch into effective instances. It
hydrates every shown instance independently through `provider.loadWidget`, so mixed
ready/stale/read-only/unavailable states and revisions are represented honestly. Each
renderer is then lazy-loaded by `WidgetHost`, wrapped in standard frame/state/error
handling, and receives only that validated widget snapshot's presentation input.

Desktop Customize mode uses the exactly pinned `react-grid-layout` `2.2.3` adapter with:

- 24 columns, per-widget min/max constraints, and a dedicated drag handle;
- no automatic compaction, no overlap, and collision rejection during normal edits;
- form/control/link cancellation selectors so content interactions do not start drags;
- drag/resize checkpoints plus non-drag move/resize menu commands;
- undo, redo, explicit Tidy, Reset, Cancel, and Done; and
- gaps preserved until the user explicitly chooses Tidy.

The Widgets drawer separates shown, hidden, unavailable/orphaned, and available types;
shows publisher/trust/version provenance; preserves hidden instances; and filters
replacement choices by slot role. It uses the native modal-dialog lifecycle, including
Escape and focus return. Available types are further restricted by the provider's
view-specific addability allowlist, and addition preflights `loadWidget` before changing
the layout. Replacement is planned atomically, and failed role or settings migration
leaves the old type intact. All current built-in widget types are single-per-view, so a
second copy is rejected rather than silently duplicated.

The initial `LocalStoragePersonalizationRepository` stores the portable versioned patch
under `work-buddy.dashboard.personalization.v1:<viewId>`. The payload contains Work Buddy
instances/layouts/settings/bindings and deliberately rejects RGL-only fields. Unknown
slot overrides are retained as opaque orphan customizations across definition changes;
an explicit Reset removes the stored patch instead of saving an empty one. A future
server-backed composition repository can implement the same interface.

Below 768 px, `ViewHost` does not mount RGL or editing controls. It renders ordinary
one-column compact DOM flow from the persisted canonical mobile order, so visual,
focus, and assistive-technology order agree. Desktop Customize mode exposes explicit
Earlier/Later controls for editing this mobile order without relying on drag geometry.

### Durable widgets

Most widgets re-hydrate from their latest snapshot whenever the grid remounts, which a
customize toggle or an interaction-recovery pass triggers. An app-owned **durable** widget
(`durable: true` on its `WidgetDefinition`) is the exception. A keep-alive host above the grid
keeps its one live element mounted across every such remount, and light placeholder cells
re-home that element as the grid rebuilds, so its live local state, real DOM nodes, and focus
survive intact. Like a single-surface view, a durable widget owns everything below its frame
and may read its own URL, listen to its own streams, and call its own routes directly, so the
Widget Renderer Contract's URL, SSE, and direct-call exclusions do not apply to it. Every
identity, trust, and theme invariant still holds, and validation requires a durable widget to
be `single_per_view` with no drafts. It is a generic capability keyed off `definition.durable`,
with no widget-specific branch in `ViewHost`, `WidgetHost`, layout, or personalization code. A
durable card can occupy a content-sized container on a narrow viewport, so its renderer must
tolerate one. The Co-work workspace is the first durable widget.

## Theme Contract v1

Theme is a two-axis choice:

```ts
type ThemePreference = {
  scheme: "system" | "light" | "dark";
  skinId: string;
};
```

**Scheme** controls contrast polarity and native control behavior. **Skin** supplies
values behind a closed semantic-token contract. `wb.default` is the product skin;
`wb.conformance-stress` is an intentionally adversarial fixture proving that components
consume meanings rather than the default palette.

A blocking script in `index.html` validates the small local preference mirror, resolves
`system`, and sets `data-wb-scheme`/`data-wb-skin` before React mounts. `ThemeProvider`
then synchronizes system and storage changes, updates `color-scheme` and browser
`theme-color`, exposes reversible preview state, and provides a small resolved canvas
snapshot for integrations that cannot inherit CSS variables.

Standard DOM widgets must:

- use the semantic tokens in `src/theme/tokens.css` for canvas/raised/overlay/inset
  surfaces; primary/secondary/muted/link/on-accent text; borders/focus/actions;
  success/warning/danger/info states; data series; radius, elevation, and motion;
- use host primitives where they fit, `currentColor` for inline SVG, and textual/shape
  encodings in addition to color;
- declare support for light, dark, forced-colors, and reduced-motion in their manifest;
  and
- avoid private hardcoded scheme palettes, `getComputedStyle()` palette discovery,
  arbitrary theme CSS, external theme assets, selectors, or behavior changes.

Forced-colors and reduced-motion styles override skin values where accessibility
requires it. The registry validates the manifest proof obligation; first-party
conformance is additionally exercised through component, axe, browser, alternate-skin,
forced-colors, reduced-motion, and visual tests. A declaration is not a security sandbox
for same-realm executable code.

Validated community theme-pack installation, the DTCG-subset compiler, provenance UI,
and isolated `ThemeSnapshot@1` bridge are reserved. General reskinning means validated
replacement of allowlisted semantic values—not arbitrary CSS or layout override.

## Adding a standard contribution

1. Define or reuse a versioned functional role. Publish a namespaced WidgetDefinition
   and a lazy WidgetModule from the owning App contribution.
2. Keep renderer models presentation-oriented. Receive typed input and emit declared UI
   intents; place fetching, permission/consent decisions, and domain behavior in the
   owning ViewProvider/App boundary. Reach for a `durable` widget only when a renderer must
   keep live local state that no snapshot can restore, such as a collaborative editor
   session, accepting the keep-alive contract with its `single_per_view` and no-drafts
   constraints in return.
3. In the hosting ViewDefinition, add a stable local slot ID, required role, presence
   policy, default type/settings/bindings/layout, and explicit reading/mobile order.
4. Register publisher contributions before views that select their types. Do not add
   widget- or App-specific branches to `ViewHost`, `WidgetHost`, layout, or
   personalization code.
5. Add registry/provider/renderer conformance tests, all standard host states, compact
   and expanded sizes, keyboard alternatives, mobile flow, and the full Theme Contract
   matrix.

For development, use `/app/__widget-lab` to inspect all three reusable renderers across
their size modes and host states. `?count=50` mounts 50 real `WidgetHost` instances for
stress measurement. The route is guarded by `import.meta.env.DEV`, is omitted from
navigation, and is tree-shaken from production.

## Source map

```text
src/app/                         router and contribution registry composition
src/dashboard/contributions/     JSON-compatible contracts, validation, registry
src/dashboard/providers/         Dashboard View API implementations/helpers
src/dashboard/events/            singleton SSE normalization and reconciliation signals
src/dashboard/views/             provider session and generic view composition
src/dashboard/customize/         cross-view customize-mode controller and the navbar entry toggle
src/dashboard/widgets/           generic host, frame, states, menus, catalog/replacement
src/dashboard/widgets/durable/   keep-alive host, durable cell, and context for durable widgets
src/dashboard/layout/            Work Buddy layout model, operations, RGL adapter, mobile order
src/dashboard/personalization/   portable patches, reducer/edit history, repositories/migrations
src/dashboard/accessibility/     host announcer and focus helpers
src/theme/                        Theme Contract runtime, semantic tokens, accessibility overrides
src/widget-library/               reusable Capture, Timeline, and Notes publishers/renderers
src/apps/journal/                 Journal view policy, bindings, fixtures, chrome, providers
src/dev/widget-lab/               development-only widget/theme/state/performance matrix
tests/e2e/                        routing, interaction, accessibility, theme, and browser coverage
tests/performance/                synthetic dashboard performance coverage
```

## Current constraints and intentional non-goals

This slice does not implement the native Journal store/import pipeline, real planner
mutation or smart-capture authority, App/System/Artifact/Workflow runtime migration,
Tracker systems, community Pack installation/security/isolation, executable
Developer-App roots, or the full agentic widget/App creation UI.

It also intentionally keeps personalization in browser storage, desktop-only editing,
click-to-add catalog insertion, and a truthful partial legacy provider while the
server-backed composition store, installable Pack loader, settings-schema editor,
permission tiers, cross-App dispatch protocol, native Journal provider, theme-pack
compiler, and isolated renderer bridge are designed and implemented separately. The
current widget sizes are evidence-led Journal defaults (Capture 8x8, Running Notes 8x8
at y=8, Timeline 16x16), not a frozen SDK guarantee.
