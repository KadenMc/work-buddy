---
name: Dashboard Verification Directions
kind: directions
description: Select a safe dashboard environment, explore through the harness-specific browser, encode regression, and record scoped evidence.
parents:
- dev/dashboard
tags:
- dashboard
- verification
- testing
- browser
trigger: Before browser-driven dashboard development or verification.
---

Read `dev/testing/react-dashboard` for runner commands and `dev/testing/browser-surface` for the authentication boundary. For changed user tasks, establish scenarios with `dev/dashboard/ux-directions` before deciding interaction details.

## Choose and name the environment

| Environment | Boundary and use |
|---|---|
| Demo fixture routes | `/app/journal?provider=demo` and `/app/cowork?cowork_fixture=demo` use in-memory providers. Use for presentation, theme, keyboard, accessibility, and responsive checks. |
| Isolated live harness | The real application on marker-guarded disposable roots and non-production ports. Use for every persisted create, import, edit, save, remove, review, recovery, gesture, or persistence check. |
| Separate read-only process on real data | Last-resort reads through the opt-in `--read-only` process on an explicit non-service port, only after the isolated GET survey is clean. Follow `services/dashboard` and `dev/live-testing-directions`; record authoritative counts or revisions before and after a rendered read and a refused non-safe request. Never mint a session or perform a persisted write. |

The way the app is opened determines isolation. Running Playwright does not establish it. The ordinary e2e Vite proxy defaults to the user's backend on port 5127, so explicit fixture navigation or an explicit isolated backend is required.

Start interactive exploration with:

```bash
npm --prefix dashboard-react run test:e2e:live:interactive -- --app cowork
```

Interactive mode implies the Vite dev server: source edits reload live. `--dev` also selects it explicitly. `--build` selects the production bundle, where source edits require a restart and rebuild. `--frontend-port` selects an explicit frontend port for browser attachment. Read the banner and session file before judging whether a source edit appeared.

Use the complete printed authenticated URL. The runner also records backend URL and nonce in `dashboard-react/test-results/live/interactive-session.json` for a fresh browser context. Preserve the root, marker, and port guards. Teardown failure is a failed run, and a seeder for an unsupported App must be supplied before testing that App's writes.

## Explore, then encode

Explore retained scenarios through the interactive browser first. Verify observable outcomes, visible feedback, and recovery through the interface. Then encode important known behavior in Playwright regression tests. Never append a probe to the serial live suite, relax an assertion to reach it, or treat a successful endpoint call as evidence of a discoverable user journey.

Navigate directly to `/app/<app>` and wait for a known element before reading the page. Use accessibility snapshots for text and structure. Poll for the expected element rather than fixed sleeps. Take screenshots to confirm layout. Check browser console and network errors and affected keyboard/responsive behavior.

## Browser recipe for this harness

<<wb:dev/testing/browser-surface --harness>>

## Report only what the evidence supports

Every finding and verification claim names its environment and carries one label: **live-verified** for execution through the actual interface, **source-inferred** for code inspection, **design-hypothesis** for expert learnability judgment, or **untested**. Automated regression results name the command and tested behavior without claiming human learnability. Source-inferred recipes remain so until exercised through the stated browser surface.

Use `dev/dashboard/ux-review` for a compact record of scenarios, changes, results, remaining gaps, and blocked outcomes. Pass its run id or artifact path as `ux_review_ref` to `dev-pr`. Reassess evidence when scope or implementation changes. A green component run does not prove persistence, and a green CI run covers only its selected specs.
