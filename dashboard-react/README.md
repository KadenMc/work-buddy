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

In a development checkout, a full sidecar restart automatically fingerprints the
React inputs and the emitted payload before Flask starts. It rebuilds only when the
served bundle is stale, stages and validates the new output before swapping `dist/`,
and makes `/app` return a clear 503 if the build fails instead of silently serving an
older UI. Packaged installations use their shipped `dist/` and do not require npm at
runtime. A dashboard-only health restart does not invoke the build guard.

## Verification

```powershell
npm run typecheck
npm test
npm run build
npm run test:e2e
npm run test:e2e:live -- --app cowork
npm run test:e2e:live:interactive -- --app cowork
```

`npm test` runs Vitest component and contract tests. `npm run test:e2e` starts Vite and
runs Playwright against Chromium and Firefox; use `npm run test:e2e:ui` for the
interactive runner. Set `PLAYWRIGHT_PORT` if port `4173` is unavailable.
Ordinary browser artifacts stay under `test-results/e2e/`. The live runner owns
`test-results/live/`, with Playwright artifacts in its `playwright/` child, so starting
an ordinary suite preserves a running interactive harness's session metadata.

The live harness selects its seeder and specification with `--app cowork`. Automated
runs typecheck and build the production bundle. `:interactive` and `--dev` use Vite's
dev server, so source edits reload live; pass `--build` for production preview instead.
Both modes announce whether source changes need a restart. The harness owns a marked
temporary root with disposable Folders, data, and config, and proxies only to its
isolated Flask backend on a port other than `5127`. Use `--frontend-port` when a browser
launcher needs a fixed port. Interactive mode prints an authenticated URL and records
the mode, URLs, and nonce in `test-results/live/interactive-session.json`. Ctrl+C or
the timeout stops the services and removes the root; failed cleanup fails the run.

The isolated host injects deterministic folder and file pickers through the product's
picker seam. Open folder selects the seeded Reference Folder; Choose Location keeps
the active fixture folder. Import selects the declared source file when it belongs to
that active folder, and otherwise returns cancellation. Native picker adapters and
native picker child processes are refused, so browser exploration cannot open an OS
file or folder dialog on the user's desktop. The normal dashboard keeps its native
pickers.

See `dev/dashboard/verification-directions` through `agent_docs` for browser guidance
and the environment rule. Harness guard and seeding checks run with
`npm run test:live-harness` and, from the repository root,
`uv run pytest dashboard-react/tests/live/test_live_harness.py`.

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
