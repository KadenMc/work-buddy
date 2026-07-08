# dashboard-react

The React frontend for the work-buddy dashboard, migrating over from the
Python-generated frontend one view at a time (Journal first). Today it is a
shell: header with the sidecar and live indicators, a clock, and an empty
Journal tab.

## Build (required before Flask can serve it)

The build output (`dist/`) is gitignored, so serve-from-Flask needs a local
build first:

```
cd dashboard-react
npm install
npm run build
```

The Flask dashboard then serves the app same-origin at
`http://127.0.0.1:5127/app` (no separate server, no extra port). `GET /app`
returns the built `index.html` with no-store headers; the hashed build
output under `dist/assets/` is served from `/app/assets/<file>` with an
immutable one-year cache policy, so rebuilds cache-bust automatically
through the changed filenames.

## Dev mode

```
npm run dev
```

Runs the Vite dev server with HMR. `/api` (and `/favicon.svg`) are proxied
to the local dashboard at `http://127.0.0.1:5127`, so the header indicators
work against real endpoints. The proxy is a dev convenience only: the
production app is served same-origin by Flask and never touches a sibling
localhost port.

## Header indicator wiring

Same sources as the legacy header:

- sidecar: `GET /api/state`, `status === "running"` plus the `read_only`
  flag, refreshed on load and when the tab returns to the foreground
- live: an `EventSource` on `/api/events`, "live" while open,
  "reconnecting" after an error (the browser retries on its own)
