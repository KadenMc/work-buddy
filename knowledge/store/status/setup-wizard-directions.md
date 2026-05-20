---
name: Setup Wizard Directions
kind: directions
description: How to run the setup wizard — modes, feature preferences, requirements, guided setup
summary: 'Four modes: status (quick overview), guided (interactive walkthrough), diagnose (deep diagnostic for one component), preferences (view/edit wanted features). Default is status.'
trigger: user runs /wb-setup, asks to configure features, or wants to know what's set up
command: wb-setup
capabilities:
- status/setup_wizard
tags:
- status
- setup
- wizard
- preferences
- requirements
- directions
aliases:
- setup wizard
- configure work-buddy
- feature preferences
- onboarding
- first-time setup
parents:
- status
---

Run mcp__work-buddy__wb_run("setup_wizard", {"mode": "<mode>", ...}).

Modes:

1. **status** (default, no args): Quick overview of bootstrap requirements, component health, and requirement validation for wanted components. Present: bootstrap pass/fail, then per-component health + any requirement failures with fix hints.

2. **guided** (first-time setup): Returns structured steps. Walk the user through:
   - Step 1: Bootstrap — fix any core/config failures first
   - Step 2: Features — walk each DOMAIN (Journal, Notifications, Knowledge & Retrieval, Browser Integration, Calendar, Runtime, System Prerequisites). For each domain, ask which of its components the user wants. The domain-grouped view comes from the control graph (`steps[1]['domains']`); `steps[1]['components']` still carries the legacy implementation-category grouping (external/integration/service/plugin) for older callers — prefer the domain view when talking to the user
   - Step 3: Requirements — show failures for wanted features with fix hints
   - Step 4: Health — check running services
   Save preferences with: wb_run("setup_wizard", {"mode": "preferences", "updates": {"hindsight": {"wanted": false}}})

3. **diagnose** (targeted): Requires component param. Deep diagnostic combining requirements + health + check sequences. Lead with the fix.
   Example: wb_run("setup_wizard", {"mode": "diagnose", "component": "hindsight"})

4. **preferences** (view/edit): Show or update feature preferences. Each component entry carries a `domains` list from the control graph — use it when presenting components to the user so they see user-facing groupings (e.g. "Obsidian (Notifications)") rather than implementation categories.
   View: wb_run("setup_wizard", {"mode": "preferences"})
   Update: wb_run("setup_wizard", {"mode": "preferences", "updates": {"telegram": {"wanted": false, "reason": "..."}, "hindsight": {"wanted": true}}})

Key principles:
- If a feature is wanted:false, don't suggest it or show it as broken.
- If a user asks 'why isn't X working?', check preferences first — they may have opted out.
- Bootstrap failures block everything — fix those first.
- Requirements are configuration issues (fix once). Health checks are runtime issues (may need restart).
- Prefer control-graph domain names over implementation categories when talking to the user.
