---
name: Browser Setup Required
kind: concept
description: Initialize harness identity and establish an interactive browser surface before claiming dashboard verification.
parents:
- dev/testing/browser-surface
tags:
- browser
- harness
- testing
---

Initialize the work-buddy session with `wb_init`, supplying the native session identity and harness id, then reload these directions. An unknown or unsupported harness uses this fallback.

If the harness supports MCP, connect the pinned Playwright MCP browser with an isolated profile. If no interactive browser surface is available, start the isolated interactive harness and ask the user to execute the scoped scenario, or report the browser verification as untested. Do independent source and automated checks while browser access is resolved. No scripted exploration fallback is supplied. Never substitute production writes or probes in the serial suite for interactive verification.
