---
name: Local human identity boundary
kind: concept
description: Persistent installation-qualified actor, loopback session, Origin/CSRF, and exact one-use gesture boundary for protected local dashboard actions.
summary: Work Buddy can prove that an enrolled local profile submitted a specific request through a bound loopback dashboard session. This is stronger than caller headers but deliberately does not prove physical presence, sole composition, or authorship.
entry_points:
- work_buddy.security.local_identity
- work_buddy.dashboard.local_identity_api
- work_buddy.dashboard.local_identity_launch
- dashboard-react/src/security/localIdentity.ts
tags:
- identity
- local-session
- csrf
- gestures
- actors
- security
aliases:
- enrolled local actor
- local identity
- human gesture boundary
parents:
- architecture
requires:
- architecture/source-foundation
dev_notes: |-
  Protected routes must call the server verifier with the exact action, subject, and canonical context SHA-256. They derive the canonical actor from the consumed session/gesture and ignore caller-selected actor fields and legacy `X-WB-User-Ref`.

  A browser endpoint that consumes human authority must not use GET or HEAD, even when its domain result is a read. Chromium normally omits `Origin` on same-origin safe-method requests, so the exact-Origin verifier cannot authenticate them. The shared request gate rejects safe methods before consuming a gesture. Use a non-safe POST with the exact JSON request context bound into the gesture, validate any route/body target IDs for equality, and return `Cache-Control: no-store` for protected staged bytes.

  v1 is direct-loopback only. Reverse-proxied/Tailscale requests cannot perform human-authority actions until a separate authenticated remote principal provider exists. Do not weaken this by treating same-origin alone as authentication.
---

# Local human identity boundary

The authority stores a stable installation issuer, tenant scope, and enrolled
local actor in SQLite. A trusted host launch mints a short-lived one-use
bootstrap grant and places it in the URL fragment so it does not enter HTTP
request logs or referrers. The browser removes the fragment synchronously and
redeems it for an opaque HttpOnly Strict session cookie plus an in-memory CSRF
token.

Deliberate CLI and tray start/restart operations also use that trusted host
boundary to recover dashboard tabs that survived a sidecar restart. After the
dashboard is ready, the host mints a fresh one-use bootstrap and asks the
browser integration to update an existing app tab in place while preserving
its current route and query. Request/response nonces ensure that an unrelated
tab export cannot be mistaken for a successful identity handoff. If no
existing tab can be confirmed, including when a pre-update extension worker
does not support that mutation, recovery uses a fresh grant for the normal
trusted browser-launch path rather than replaying a possibly consumed grant or
calling the older navigation mutation. This leaves any open Co-work document
route intact while the new same-origin session becomes available to it.

An open tab consumes a newly delivered bootstrap on hash change. On window
focus or return to visible state it always recovers the exact-Origin cookie
session and refreshes the in-memory CSRF token, including when the tab still
looks authenticated with a stale token. A protected gesture that receives
401/403 performs the same recovery once before retrying, then publishes an
unauthenticated state if authority is still absent. These recovery paths do not
mint authority in HTTP, extend the server's hard session or gesture TTLs, or
weaken the direct-loopback, Origin, audience, CSRF, and exact-action checks.

An informational actor-binding read may continue to identify the enrolled
actor when an otherwise valid session has reached its rotation boundary. That
read neither rotates the session nor authorizes a mutation. The next protected
gesture request still receives the typed rotation requirement, rotates through
the CSRF-protected session endpoint, and retries before the exact bound write
can proceed. Expired, revoked, wrong-Origin, or otherwise invalid sessions
remain unavailable to both reads and writes.

Before a protected click, the browser requests a gesture for the exact action,
subject, and canonical context digest. The domain mutation sends that one-use
gesture and CSRF token; the backend consumes it and constructs the
`HumanAuthorityContext`. Replays, wrong origins, changed payloads, expired or
rotated sessions, direct HTTP actor injection, and non-loopback access fail
closed.

The assurance means “this enrolled local session submitted this exact bound
request.” Input mode and separate authorship/review attestations carry any
stronger statement. Paste and import in particular must never be labeled as
human-authored merely because a human initiated the operation.
