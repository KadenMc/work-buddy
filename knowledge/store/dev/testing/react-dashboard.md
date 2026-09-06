---
name: React Dashboard Testing
kind: concept
description: The five test surfaces of dashboard-react, how each one stays off the user's real data, the authentication boundary an agent must not try to cross, and what a green continuous-integration run actually proves.
summary: 'dashboard-react has five independent test surfaces: a vitest component suite, a Playwright end-to-end suite, an isolated Co-work live harness, a document-kernel determinism check, and a separate projection-fidelity package. The end-to-end suite proxies to the developer''s real dashboard by default and avoids live data only through explicit fixture query parameters, so anything that writes belongs in the live harness.'
tags:
- dev
- developmental
- testing
- dashboard
- react
- vitest
- playwright
- isolation
- continuous-integration
aliases:
- dashboard-react tests
- Co-work live harness
- Playwright dashboard specs
- dashboard test isolation
- how to test the React dashboard
parents:
- dev/testing
- services/dashboard/react
---

`dashboard-react/` carries five test surfaces. They share a package root and nothing else: separate runners, separate configuration files, separate isolation stories, and separate coverage in continuous integration. Running one of them proves nothing about the other four, so a report on dashboard testing has to name which surface was exercised.

## The five surfaces

**Component tests.** `npm test` runs `vitest run` under `dashboard-react/vitest.config.ts`: a jsdom environment, the setup file `src/test/setup.ts`, and roughly 250 co-located `*.test.ts` and `*.test.tsx` files collected from `src/`. The setup file installs a no-op `ResizeObserver` that jsdom lacks, and exports `expectNoAccessibilityViolations(container)`, an axe wrapper that shared widgets are expected to call from both their ready and non-ready state tests instead of inventing local accessibility assertions.

**Browser end-to-end.** `npm run test:e2e` runs Playwright under `dashboard-react/playwright.config.ts`, which matches about twenty specs across `tests/e2e/` and `tests/performance/`. It starts its own front end: the `webServer` entry runs `npm run dev` bound to loopback on port 4173, overridable with `PLAYWRIGHT_PORT`, and reuses an already-listening server on that port outside continuous integration. Two projects are declared, chromium and firefox, and neither filters the spec set, so a full local run executes every spec twice.

**The Co-work live harness.** `npm run test:e2e:cowork-live` runs `tests/live/run-cowork-live.mjs`, a process orchestrator rather than a Playwright entry point. It creates a temporary root holding a `.cowork-live-harness` marker, draws two random loopback ports and redraws them until neither one is 5127, seeds a fixture folder tree, typechecks and builds the production bundle, starts a Flask backend from `tests/live/cowork_live_server.py` against an isolated config root and data root, serves the built app through `vite preview`, and only then invokes Playwright with `playwright.cowork-live.config.ts`. That config pins `workers: 1` and `fullyParallel: false`. Teardown removes the temporary root only after confirming both the expected path prefix and the marker file, and treats a failed cleanup as a failed run. The `--interactive` flag, wrapped as `npm run test:e2e:cowork-live:interactive`, builds the same isolated stack, skips Playwright entirely, prints a live URL into the isolated dashboard, and holds everything open for a bounded window until it expires or receives an interrupt.

**Document-kernel determinism.** `npm run test:document-kernel-build` runs `scripts/verify-document-kernel-determinism.mjs`, which builds the document-kernel worker twice and compares the sha256 of the emitted `worker.mjs`, failing when the two builds disagree.

**Projection fidelity.** `dashboard-react/tests/fidelity/` is a separate npm package, `@work-buddy/cowork-fidelity`, with its own `package.json`, lockfile, and vitest config. No root script reaches it, and the root vitest config collects only under `src/`, so `npm test` never runs it. Run `npm ci && npm test` from inside that directory. Its `.gitattributes` marks `corpus/** -text` because the corpus is a byte-fidelity fixture and a line-ending rewrite would invalidate the recorded digests.

## Isolation is a fixture choice, not a sandbox

This is the section to read before pointing any browser at the dashboard.

The Vite dev server that the end-to-end suite starts proxies `/api` to `WB_DASHBOARD_PROXY_TARGET`, which defaults to loopback port 5127. On a developer machine that is the real Flask dashboard, backed by the user's real notes, tasks, and Co-work Folders. Nothing about running Playwright puts a boundary between the specs and that data.

What keeps the specs off it is an explicit fixture selection in every navigation. Journal specs go through `openJournal` in `tests/e2e/helpers.ts`, which loads `/app/journal?provider=demo`. Co-work specs go through `openCowork` in `tests/e2e/cowork-helpers.ts`, which loads `/app/cowork?cowork_fixture=demo`. Both parameters swap in in-memory providers. A spec, or an agent, that navigates without one of them is driving the user's live dashboard.

The rule that follows is simple: reads and local presentation state can run against the proxied dashboard with a demo parameter, and anything that must write goes through the live harness, which owns its own ports, config root, data root, and folder tree and deletes all of it on teardown. `services/dashboard/react` already states this constraint in its dev notes as a standing rule for browser specifications. The parameters and the harness above are the mechanisms that implement it.

## The authentication boundary

`work_buddy/dashboard/local_identity_api.py` states in its module docstring that there is intentionally no HTTP bootstrap-mint route. A trusted host launch path calls `LocalIdentityAuthority.mint_bootstrap` in process and places the one-time grant in a browser URL fragment. Nothing reachable over the network can ask the real dashboard for a session.

The live harness needs a browser session, so its throwaway server adds one that production does not have: `/api/_cowork-live/identity-bootstrap` in `tests/live/cowork_live_server.py`, gated on the harness nonce and on an exact match between the requested origin and the observed `Origin` header, minting into an authority database that lives under the harness temporary root and disappears with it.

Stated plainly, because it is the mistake worth preventing: do not attempt to mint a session against the real dashboard. The route is absent by design, and its absence is not a gap to route around. When browser-driven work needs an authenticated session, start the interactive harness and use the URL it prints.

The same boundary has a consequence inside the live suite. Creating a document, importing one, editing it, submitting feedback, reviewing a proposal, and removing a document each issue an exact human-authority gesture before their request, and the gesture is only mintable from the backend origin. A live test that exercises any of those has to open through the authenticated helper rather than the plain one. Getting this wrong is easy to misdiagnose: the request never leaves the browser, so nothing appears in the server log and the failure surfaces as a message inside the dialog reading that an authenticated local session is required. That reads as a broken feature when it is a test opening the wrong way. Only the tests about the unauthenticated surface itself, production-preview isolation and the launcher, should open plainly.

Browser-local writing is origin-scoped in the same way. A recovered draft lives in IndexedDB under whichever origin created it, so a test that seeds one and then reads it from a different origin finds nothing and reports an empty launcher rather than a seeding failure.

## Driving the browser pane

For read-only inspection of dashboard UI, open the demo fixture routes against whatever dev server is already running: `/app/journal?provider=demo` and `/app/cowork?cowork_fixture=demo`. Real components render against in-memory providers, so clicking through them cannot reach the user's data.

For anything that writes, meaning creating a Folder, saving a document, or exercising persistence, recovery, or the review loop, run `npm run test:e2e:cowork-live:interactive` and drive the URL it prints. That is the only browser surface where a write is both meaningful and safe.

The `wb-dashboard` entry in `.claude/launch.json` starts no dashboard. Its command is a Node process that does nothing but stay alive, a keepalive placeholder so the preview attaches to port 5127, where the sidecar-hosted dashboard the user already runs is listening. Starting that entry does not produce a server, and stopping it does not stop one.

## Gotchas

- The live specification is one `test.describe.serial` block of sixteen tests that share module-level store and document identifiers built up by earlier tests. A single failure skips every test after it, so only the first red result carries information. Narrowing to one test with `--grep`, exposed as `COWORK_LIVE_PLAYWRIGHT_GREP`, usually fails outright, because the prerequisites that create its Folder and its documents never ran.
- Pass `--reporter=verbose` to vitest when you need to read `console` output from a test. The default reporter is not a reliable place to look for it.
- `dashboard-react/test-results/` is gitignored. Harness logs, the run summary, traces, videos, and screenshots land there and cannot be recovered from version control, so read them in the same session that produced them.
- `npm run build` writes outside `dashboard-react/`. The document-kernel step emits into `work_buddy/document_kernel/runtime_dist/` with `emptyOutDir` set, so a dashboard build modifies a Python-package directory. That is intended, and that emitted worker is what the determinism check hashes.

## What a green continuous-integration run proves

The `dashboard-test` job in `.github/workflows/tests.yml` installs dependencies, runs `npm test`, installs chromium and firefox, runs an explicit allowlist of eleven Playwright specs, and builds the production assets.

The allowlist covers the Journal specs, shell routing, the layout contract, themes, and the mobile and settings accessibility specs. It omits the five `cowork-*.spec.ts` specs, `visual.spec.ts`, `widget-lab.spec.ts`, `calendar-spike.spec.ts`, and everything under `tests/performance/`. The live harness, the fidelity package, and the document-kernel determinism check appear in no workflow at all. Continuous integration also installs no pandoc, so the Python render tests that require it skip on every run.

So "continuous integration is green" means: the component suite passed, eleven browser specs passed in two browsers against demo providers, and the production bundle compiled. It does not mean Co-work works, that persistence or recovery hold, that the document kernel builds reproducibly, that Markdown projection is byte-faithful, or that any code was exercised against a real backend. Those claims require running the matching surface locally, and a report that makes one should say which surface was run.

See `services/dashboard/react` for the hosting, provider, and network contract these tests exercise, `dev/live-testing-directions` for user-in-the-loop verification across the running multi-process system, and the `cowork/` scope for the document lifecycle the live harness covers.
