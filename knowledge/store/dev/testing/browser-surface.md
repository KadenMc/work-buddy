---
name: Dashboard Browser Surface
kind: concept
description: Authentication and tool selection for interactive dashboard exploration on each agent harness.
parents:
- dev/testing
tags:
- dashboard
- browser
- harness
- testing
---

Explore with an interactive browser, then encode known behavior in Playwright regression tests. Name the environment before the first navigation: demo fixture routes, isolated live harness, or the user's live dashboard. Every persisted write belongs in the isolated harness. Read `dev/dashboard/verification-directions` for the complete environment and evidence policy.

## Authentication boundary

The gate is a session cookie, not agent detection. On startup `initializeLocalIdentity` redeems a `#wb-bootstrap=<token>` fragment if present, otherwise it tries to recover an existing cookie. No fragment and no cookie means unauthenticated. A host-launched user browser works because its launch URL carried a fragment once and the cookie persisted.

Production has no mint route, by design. Never attempt to mint against the real dashboard. Its absence is the security boundary, not a gap.

The harness mint route `/api/_live/identity-bootstrap` is sanctioned. It is nonce-gated, requires an exact match of requested origin and observed Origin header, and writes its authority database inside the disposable root. Use it in the isolated harness without hesitation.

The gesture layer binds an operation, not a presence. `/api/local-identity/gestures` mints a single-use challenge for one action, subject, and context hash, requiring the cookie and CSRF token. It does not inspect input events. Human authority means an enrolled session approved exactly that operation.

A missing session can surface only as text inside a dialog, with no server-side request to diagnose. Check authentication before treating a disabled mutation as a product defect.

The `claudecode`, `codexcli`, and `default` children carry tool-specific recipes. `dev/dashboard/verification-directions` selects one with the harness placeholder. Keep browser profiles isolated and avoid persistent profiles whose cookies survive unrelated harness runs.
