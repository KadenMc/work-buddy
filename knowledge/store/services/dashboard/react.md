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

  The production build is emitted to `dashboard-react/dist` and served by Flask. Tests inject fixture providers so component behavior does not require a live Work Buddy process. Keep grid-library objects and other dependency-specific state behind Work Buddy adapters.

  This `services/dashboard/react` subtree is an explicit migration namespace. After the Python-generated root dashboard at `/` is fully retired, collapse these units into `services/dashboard/*` so React stops being an architectural qualifier: merge this parent into `services/dashboard`, move children such as `services/dashboard/react/widget-platform` up one level, and repair cross-references in the same documentation change.
---

The React dashboard is the incrementally migrated desktop surface served at `/app`. The Python-generated dashboard at `/` remains available for operational surfaces that have not moved. They share one Flask service, one origin, and the same backend authority; they are distinct frontends rather than aliases.

## Route and hosting contract

Registered `/app/<view>` routes host standardized dashboard views. Explicit development routes host fixture laboratories. Unknown routes do not silently become convincing sample data.

Flask serves the production index, content-hashed assets, manifest, icons, and safe history fallbacks for registered routes. Optional PWA metadata changes presentation and install identity; it does not bootstrap Work Buddy services.

## View composition

Shareable views normally enter through the contribution registry and render through `ViewHost`. A view contribution declares identity, chrome, widget placements, and a provider. The host validates the contribution before rendering it.

Providers own view definitions, widget input, and declared intent handling. A provider that already implements the dashboard contract is not an adapter. An adapter is justified only when translating an incompatible source, such as a root-dashboard Journal payload, into that contract.

Widget renderers receive typed presentation input and emit declared intents. They do not collect global context, connect directly to external providers, own credentials, or choose transport endpoints. App and System layers retain those authorities.

## Network and event boundary

Browser code uses same-origin `/api/...` routes only. It never calls sibling localhost service ports, because those addresses fail over remote access and move service authority into the browser. Real-time UI reconciliation consumes the dashboard's lossy SSE projection at `/api/events`; durable reactions belong to the durable Events backbone.

## Migration contract

The root dashboard remains in service while React coverage grows. Moving one view does not imply that all root tabs, mutations, or integrations have moved. Compatibility providers state whether they are read-only or write-capable, and a failed live source remains visibly failed instead of falling back to demo fixtures.

See `services/dashboard/react/widget-platform` for widget composition and `services/dashboard/frontend` for the Python-generated root frontend.
