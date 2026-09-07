---
name: Claude Code Dashboard Browser Recipe
kind: concept
description: Use the Claude Browser pane to explore the authenticated isolated dashboard dev server.
parents:
- dev/testing/browser-surface
tags:
- claude-code
- browser
- testing
---

Use the Claude Browser pane. Start the `dashboard-live-dev` launch configuration with `preview_start`, or start `npm --prefix dashboard-react run test:e2e:live:interactive -- --app cowork --frontend-port 5188` and attach to that frontend. Name the environment **isolated live harness**. Use `navigate` to open the complete authenticated `/app/cowork#wb-bootstrap=...` URL printed by the runner and recorded in `interactive-session.json`.

Wait with `find` for a known Co-work workspace or launcher element before `read_page`. The SPA can be empty immediately after navigation. Confirm the fragment was consumed and authenticated controls are available before writing. Use `form_input` and `computer` for interaction, `read_console_messages` and `read_network_requests` for errors, and `resize_window` plus screenshots for layout.

The fragment is removed before redemption completes, and an enabled New control does not prove authentication. In `javascript_tool`, fetch `/api/local-identity/session`, parse its JSON, and require `authenticated === true` and `principal.origin === location.origin`. Resolve a failed session check before any write.

The printed bootstrap is single-use and expires after a short window, while a page reload retains its cookie. If the grant expired, or a fresh browser context needs authentication after redemption, open the isolated frontend and run this in `javascript_tool` using the nonce from `interactive-session.json`:

```javascript
const response = await fetch('/api/_live/identity-bootstrap', {
  method: 'POST',
  headers: {'Content-Type': 'application/json', 'X-WB-Live-Control': '<nonce from interactive-session.json>'},
  body: JSON.stringify({origin: location.origin})
});
if (!response.ok) throw new Error(`Bootstrap refused: ${response.status}`);
await response.json();
```

Then `navigate` to `<frontend origin>/app/cowork#wb-bootstrap=<returned token>`, repeat the session check, wait for the known element, and confirm with `read_page`. Derive the frontend origin from `new URL(frontend_url).origin`; the session file's URL already includes an App path and fragment. Issue the mint from page context so the body origin matches the Origin header. Never mint against the user's dashboard. Keep nonce and token values out of source and review records.

Prefer the accessibility tree for text and structure. Poll for expected controls rather than fixed sleeps. Use screenshots to confirm layout. Do not explore by inserting probes into regression specifications.
