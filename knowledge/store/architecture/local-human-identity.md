---
name: Local human identity boundary
kind: concept
description: Persistent installation-qualified actor, loopback session, Origin/CSRF, and exact one-use gesture boundary for protected local dashboard writes.
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

  v1 is direct-loopback only. Reverse-proxied/Tailscale requests cannot make human-authority writes until a separate authenticated remote principal provider exists. Do not weaken this by treating same-origin alone as authentication.
---

# Local human identity boundary

The authority stores a stable installation issuer, tenant scope, and enrolled
local actor in SQLite. A trusted host launch mints a short-lived one-use
bootstrap grant and places it in the URL fragment so it does not enter HTTP
request logs or referrers. The browser removes the fragment synchronously and
redeems it for an opaque HttpOnly Strict session cookie plus an in-memory CSRF
token.

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
