# Work Buddy React dashboard

This package contains the contribution-driven React dashboard. The first complete
vertical slice is the Journal view: a standard composition of reusable Capture,
Timeline, and Notes widgets running through the same registry, provider, host, layout,
personalization, event, and theme boundaries intended for later Apps.

See [ARCHITECTURE.md](./ARCHITECTURE.md) for the durable contract and ownership model.

## Run locally

Install the pinned dependency tree and start Vite:

```powershell
cd dashboard-react
npm ci
npm run dev
```

Vite serves the app with HMR and proxies `/api` and the favicon to the Flask dashboard
at `http://127.0.0.1:5127`. Production does not use that proxy: Flask serves the built
assets same-origin beneath `/app/`.

Available Journal entries:

- `/app/` redirects to the default registered view.
- `/app/journal` uses the deterministic interactive in-memory provider and is visibly
  labelled `Demo data`.
- `/app/journal?provider=legacy` uses the partial, read-only adapter for
  `GET /api/automation/today`. It never substitutes demo behavior after a live failure.

The provider query value must match an explicitly registered provider. An unknown value
is an error rather than a silent fallback.

Development builds also expose `/app/__widget-lab`. It renders the reusable widget
library across size, lifecycle, accessibility, and theme states, and accepts
`?count=50` for a real-host stress run. The route and its code are absent from the
production bundle.

## Build for Flask

```powershell
cd dashboard-react
npm run build
```

`build` runs TypeScript checking and Vite's production build. The generated `dist/`
directory is gitignored but is required in payloads that serve the React dashboard.
Flask serves history-fallback HTML with no-store headers and hashed assets from
`/app/assets/` with immutable caching.

## Verification

```powershell
npm run typecheck
npm test
npm run build
npm run test:e2e
```

`npm test` runs Vitest component and contract tests. `npm run test:e2e` starts Vite and
runs Playwright against Chromium and Firefox; use `npm run test:e2e:ui` for the
interactive runner. Set `PLAYWRIGHT_PORT` if port `4173` is unavailable.

Focused Flask, launcher, and packaging tests live in the repository-level Python test
suite and should be run through `uv run pytest ...` from the repository root.

## Contribution rules in brief

- A view owns stable purposes (slots), presence policy, default widget selections, and
  default layout. A widget publisher owns reusable roles, definitions, and lazy renderer
  modules. The user owns instances and personalization.
- Renderers receive already-bound input and emit typed UI intents. They must not fetch,
  open an EventSource, discover Work Buddy resources, or call App/System internals.
- Standard widgets declare Theme Contract v1 support for light, dark, forced-colors, and
  reduced-motion, and style through semantic `--wb-*` tokens or host primitives. They do
  not ship private light/dark palettes.
- Widget type ID, view slot ID, and widget instance ID have different lifecycles and
  must remain independent.
- Shareable skins replace an allowlisted set of semantic values; they cannot inject
  CSS, scripts, external assets, selectors, or layout rules. Standard widget manifests
  must declare the complete Theme Contract v1 accessibility matrix.
