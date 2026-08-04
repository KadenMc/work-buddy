---
name: React Dashboard
kind: concept
description: Incrementally migrated React dashboard shell, routing, provider selection, and same-origin API boundary at /app.
summary: The React dashboard is served at /app by the dashboard Flask service and receives standardized view contributions through ViewHost while unmigrated root-dashboard surfaces remain available at /.
tags:
- dashboard
- react
- frontend
- routing
- view-host
- same-origin
aliases:
- React dashboard frontend
- dashboard-react
- /app dashboard
- ViewHost
parents:
- services/dashboard
entry_points:
- dashboard-react/src/app/DashboardApp.tsx
- dashboard-react/src/dashboard/views/ViewHost.tsx
dev_notes: |-
  The package root is `dashboard-react/`. `DashboardApp` owns shell routing and registry/provider assembly; standard view modules contribute definitions rather than mounting unrelated application roots. Development fixture routes are explicitly registered and are not production fallback behavior.

  The production build is emitted to `dashboard-react/dist` and served by Flask. Tests inject fixture providers so component behavior does not require a live Work Buddy process. Keep grid, calendar, and other dependency-specific objects behind Work Buddy adapters.

  In a source checkout, a full sidecar boot is also the React activation boundary: the sidecar fingerprints build inputs, compares them with the versioned full-output marker in `dist`, and runs the bounded npm build only when stale. This preflight runs once per daemon boot, not on dashboard health restarts. Packaged payloads contain no authoring tree and use their validated shipped `dist` without requiring Node.js.

  This `services/dashboard/react` subtree is an explicit migration namespace. After the Python-generated root dashboard at `/` is fully retired, collapse these units into `services/dashboard/*` so React stops being an architectural qualifier: merge this parent into `services/dashboard`, move children such as `services/dashboard/react/widget-platform` up one level, and repair cross-references in the same documentation change.
---

The React dashboard is the incrementally migrated desktop surface served at `/app`. The Python-generated dashboard at `/` remains available for operational surfaces that have not moved. They share one Flask service, one origin, and the same backend authority; they are distinct frontends rather than aliases.

## Route and hosting contract

Registered `/app/<view>` routes host standardized dashboard views. `/app/settings/...` hosts the React Settings projection backed by the same-origin Settings authority. Explicit development routes host fixture laboratories. Unknown routes do not silently become convincing sample data.

Flask serves the production index, content-hashed assets, manifest, icons, and safe history fallbacks for registered routes. Optional PWA metadata changes presentation and install identity; it does not bootstrap Work Buddy services.

## Development build activation

A full development sidecar restart checks whether the served React bundle represents the current source contents. An unchanged source/output fingerprint takes the fast path. A changed or missing marker triggers one bounded build while independent sidecar services start normally. The build writes into a locked sibling staging directory, validates and fingerprints the complete emitted payload, then swaps it into `dist`; a failed, timed-out, interrupted, or racing build preserves the last-known-good directory.

The supervised dashboard child receives the preflight state directly. When preparation fails, Flask and its APIs remain available, but `/app` returns a no-store 503 instead of silently serving the last-known-good UI as though it represented current source. Detailed compiler output stays in sidecar logs. Dashboard health recovery does not retry builds; fix the reported source/dependency problem and perform an explicit full sidecar restart.

Packaged installations are a separate fast path: they ship `dashboard-react/dist` without the React authoring tree, validate the shipped shell and its local references, and do not require npm at runtime.

## View composition

Shareable views normally enter through the contribution registry and render through `ViewHost`. A view contribution declares identity, chrome, widget placements, and a provider. The host validates the contribution before rendering it.

Providers own view definitions, widget input, and declared intent handling. A provider that already implements the dashboard contract is not an adapter. An adapter is justified only when translating an incompatible source, such as a root-dashboard Journal payload, into that contract.

Widget renderers receive typed presentation input and emit declared intents. They do not collect global context, connect directly to external providers, own credentials, or choose transport endpoints. App and System layers retain those authorities.

## Network and event boundary

Browser code uses same-origin `/api/...` routes only. It never calls sibling localhost service ports, because those addresses fail over remote access and move service authority into the browser. Real-time UI reconciliation consumes the dashboard's lossy SSE projection at `/api/events`; durable reactions belong to the durable Events backbone.

## Migration contract

The root dashboard remains in service while React coverage grows. Moving one view does not imply that all root tabs, mutations, or integrations have moved. Compatibility providers state whether they are read-only or write-capable, and a failed live source remains visibly failed instead of falling back to demo fixtures.

See `services/dashboard/react/widget-platform` for widget composition, `services/dashboard/react/appearance` for visual compatibility, `services/dashboard/react/calendar-surface` for the temporal presentation adapter, `settings` for configuration authority, and `services/dashboard/frontend` for the Python-generated root frontend.
