---
name: React Dashboard Testing
kind: concept
description: Dashboard test runners, isolated environments, authentication, and the evidence each verification surface provides.
summary: Component, browser, isolated live, document-kernel determinism, and projection-fidelity tests are independent surfaces. Persisted writes use the disposable live harness.
parents:
- dev/testing
- services/dashboard/react
tags:
- dev
- testing
- dashboard
- react
- vitest
- playwright
- isolation
aliases:
- dashboard-react tests
- dashboard live harness
- Playwright dashboard specs
dev_notes: |-
  The truth-panel scenario bootstraps the Y.Doc snapshot and fidelity metadata
  through the production document kernel from the fixture's Markdown paragraph
  sequence and requires a lossless projection. Its manifest truth_panel object
  records the store, document, named claim IDs, passage quotes, and app_path.
  Re-seeding preserves record identities and later review or prose edits.
  Seed decisions consume the isolated local authority's exact gestures; the
  seed session is revoked before completion. Reject unmarked roots and the
  normal dashboard port. Receipt files and supported file: locators remain
  inside the marked throwaway root.
---

`dashboard-react/` has five independent verification surfaces. Name the runner and environment in each report. A passing result covers only the behavior that runner exercised.

## Test surfaces

| Surface | Command from the repository root | Coverage |
|---|---|---|
| Component | `npm --prefix dashboard-react test` | Vitest with jsdom and co-located `src/**/*.test.ts(x)`. Shared widget tests use `expectNoAccessibilityViolations` from `src/test/setup.ts`. |
| Browser end-to-end | `npm --prefix dashboard-react run test:e2e -- --workers=1` | Chromium and Firefox under `playwright.config.ts`, including ordinary e2e and performance specs. Fixtures select in-memory providers. |
| Isolated live harness | `npm --prefix dashboard-react run test:e2e:live -- --app cowork` | Real Flask application, disposable roots, domain-seeded data, production bundle, and persistence/authority regression under `playwright.live.config.ts`. |
| Document-kernel determinism | `npm --prefix dashboard-react run test:document-kernel-build` | Two builds of the document worker, compared by sha256. |
| Projection fidelity | `npm --prefix dashboard-react/tests/fidelity ci`, then `npm --prefix dashboard-react/tests/fidelity test` | Separate package and Vitest config with byte-fidelity corpus. Root `npm test` does not collect it. |

The ordinary Playwright configuration can run in parallel, but use `--workers=1` on machines with limited browser capacity and establish a main-branch baseline before attributing a failure. The live configuration is always serial with one worker and retains a Firefox smoke project dependent on the Chromium project.

## Isolation is chosen when opening the app

The ordinary Playwright Vite server defaults to port 4173 and proxies `/api` to `WB_DASHBOARD_PROXY_TARGET`, which defaults to the user's dashboard on port 5127. Running Playwright does not isolate the data. Journal's `openJournal` helper appends `?provider=demo`; Co-work's `openCowork` appends `?cowork_fixture=demo`. Those in-memory providers are the presentation fixture boundary.

Use demo fixture routes for layout, theme, accessibility, keyboard, and responsive exploration. Use the isolated live harness for every persisted create, import, edit, save, remove, review, recovery, or authority check. An explicit isolated proxy target also protects requests outside fixture providers. Read `dev/dashboard/verification-directions` before opening a browser.

Real-data reads are a last resort when fixtures cannot answer the question. Use the separate opt-in `--read-only` dashboard process on an explicit non-service port only after the isolated GET survey is clean, as specified in `services/dashboard`. Review per-route failures and coverage gaps; a successful survey exit alone is insufficient. Record authoritative counts or revisions before and after the browser read and a refused non-safe request according to `dev/live-testing-directions`. Never mint a session or use real items as fixtures, and report unavailable external-provider panes.

## Shared live harness

`tests/live/run-live.mjs --app cowork` owns the entire disposable application stack. It creates a temporary root with a `.wb-live-harness` marker, allocates non-5127 loopback ports, redirects `WORK_BUDDY_DATA_DIR` and `WORK_BUDDY_CONFIG_DIR`, and seeds through production domain code in `tests/live/seeds/cowork.py`. The seeder is idempotent. Co-work is the supported App seeder.

The Co-work seeder accepts `--scenario lifecycle` and `--scenario truth-panel`.
Lifecycle is the default. Truth-panel supplies one ready throwaway document
with five claims and six connected passages: proposed with a human-authored
receipt, proposed without evidence, confirmed, a claim expressed in two
separated passages, and confirmed with a needs-review overlay. Receipts refer
to actual throwaway text files. Context paragraphs separate connected passages
by more than one editor viewport for navigation checks. Use this scenario for
Truth state, evidence, correction, decision, and passage-navigation behavior.

`tests/live/live_server.py` imports the real `work_buddy.dashboard.service.app`. It refuses a missing marker, roots outside the harness root, and port 5127. It supplies nonce-gated `/api/_live/` host controls and skips normal sidecar pollers. Teardown rechecks the path and marker before deletion and treats failed cleanup as a failed run.

The live host injects deterministic picker callbacks through the production folder blueprint. Open folder selects the seeded Reference Folder, Choose Location keeps the active contained fixture folder, and import selects the manifest's source only when it belongs to the active folder, otherwise returning cancellation. Native picker adapters and picker child processes are refused. Harness browser testing must never open native dialogs on the user's desktop; the normal dashboard's picker behavior is unchanged.

Regression builds and previews the production bundle before running the
scenario's registered spec: `tests/live/cowork.spec.ts` for lifecycle or
`tests/live/cowork-truth.spec.ts` for truth-panel. Interactive exploration uses:

```bash
npm --prefix dashboard-react run test:e2e:live:interactive -- --app cowork
```

For the Truth fixture, add `--scenario truth-panel`:

```bash
npm --prefix dashboard-react run test:e2e:live:interactive -- --app cowork --scenario truth-panel
```

Run its production-bundle regression with
`npm --prefix dashboard-react run test:e2e:live -- --app cowork --scenario truth-panel`.
The demo route also has an in-memory Truth provider for presentation and shared
menu checks; its local mutations synchronize rail details and editor marks.
It does not establish server authority or persisted behavior.

Interactive mode implies `--dev`, which starts Vite with the isolated backend as `WB_DASHBOARD_PROXY_TARGET`. Source edits reload live. `--build` uses the production bundle instead, so source edits require restart and rebuild. Both modes announce this in the banner and `interactive-session.json`. `--frontend-port` supports browser attachment on a chosen port. `--dev` without the interactive flag also hosts an exploration session. The host stays open for a bounded window and cleans up on expiry or interruption.

Open the complete authenticated URL the runner prints, including its `#wb-bootstrap=...` fragment. It records that URL as `frontend_url`, plus backend URL, nonce, roots, mode, and expiry in `dashboard-react/test-results/live/interactive-session.json`. The bootstrap is single-use and the resulting cookie survives reloads. A fresh browser context can mint another grant through the sanctioned harness route using the recorded nonce. See `dev/testing/browser-surface` and its selected child for the exact recipe.

The `.claude/launch.json` `dashboard-live-dev` entry starts this isolated dev harness. `wb-dashboard` is only a keepalive stub for the sidecar's existing 5127 process. It starts no dashboard server.

## Authentication and exploration

A session cookie and an operation-bound gesture authorize mutations. Neither detects human presence. Production deliberately has no HTTP bootstrap mint route. The disposable server's `/api/_live/identity-bootstrap` checks `X-WB-Live-Control` against the harness nonce and requires exact agreement between the body origin and the Origin header.

Missing authentication can appear as dialog text without a server request. Authenticate before diagnosing a mutation. Browser-local drafts are origin-scoped, so seeding IndexedDB on one origin and reading on another does not test recovery.

Explore with the interactive browser, then encode regression. Do not append probes to specs or relax existing assertions to reach a later test. The live spec shares state in a serial group. `WB_LIVE_PLAYWRIGHT_GREP` selects tests but does not reconstruct prerequisites, so it cannot reliably isolate a later scenario. Use the interactive harness for one-off exploration.

## Runner details

- `WB_LIVE_SKIP_BUILD=1` skips typecheck/build and serves existing assets. Use only while iterating on specs, never to judge a `src/` change. Dev mode is the source-edit path.
- `dashboard-react/test-results/` is gitignored. Save scoped evidence before artifacts are replaced by another run.
- `npm run build` emits the worker into `work_buddy/document_kernel/runtime_dist/`. A sidecar preflight can contend for the build lock, so coordinate builds and restarts.
- Co-work URLs use a unique identity prefix when possible. API comparisons must resolve it to the authoritative full identity.
- Confirm that a control exists in the current UI before treating a stale locator as a product regression. Automatic chat recovery, for example, does not imply a restart button.
- Vitest's `--reporter=verbose` is useful when test console output is needed.

## Continuous integration

The dashboard job runs the component suite, checks CI decisions with `npx playwright test --list --reporter=./scripts/check-spec-tags.mjs`, selects the exact `@ci` token with `--grep '(^|\s)@ci(?=\s|$)'`, and builds production assets. Each ordinary browser test declares exactly one of `@ci` or `@no-ci`, verified from Playwright's collected metadata. The guard inventories spec files throughout `tests/`, including files outside the configured collection directories. An uncollected spec must have an exact entry in the checker's separate-configuration registry with an existing configuration and an exclusion reason. The registered `tests/live/cowork.spec.ts` and `tests/live/cowork-truth.spec.ts` each declare an encompassing `@live` and `@no-ci` suite, verified from TypeScript syntax. Comments do not count as decisions, and an extra live spec needs its own declaration.

CI-selected specs cover Journal, shell routing, layout, themes, and mobile/settings accessibility. Co-work specs, visual regression, widget lab, calendar spike, and performance specs explicitly opt out with `@no-ci`. The live harness, fidelity package, and document-kernel determinism runner require separate local execution.

A green CI result does not establish persistence, recovery, live backend behavior, projection fidelity, or reproducible worker output. Report the surface that supports each claim. See `services/dashboard/react` for the hosting and provider contracts.
