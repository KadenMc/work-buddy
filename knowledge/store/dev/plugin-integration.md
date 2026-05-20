---
name: Obsidian Plugin Integration
kind: directions
description: Build a new Obsidian plugin integration for work-buddy — probe, wrap, package, and optionally collect
summary: Step-by-step methodology for building a new Obsidian plugin integration in work-buddy. Covers runtime probing via eval_js (object graph walking, method discovery, side-effect testing), Python wrapper patterns, JS snippet rules, package structure, and optional collector integration.
trigger: When the user invokes /wb-dev-plugin-integration or asks to integrate a new Obsidian plugin into work-buddy
command: wb-dev-plugin-integration
tags:
- dev
- developmental
- obsidian
- plugin
- eval_js
- reverse-engineering
- collectors
aliases:
- obsidian integration
- plugin integration
- new obsidian plugin
- reverse engineer plugin
- eval_js probe
- plugin wrapper
- obsidian bridge integration
parents:
- dev
- dev
---

Build a new Obsidian plugin integration for work-buddy.

## Essential reading (in this order)

1. `work_buddy/obsidian/smart/README.md` — read the **Reverse-Engineering Methodology** section. It explains how to probe bundled Obsidian plugins at runtime via eval_js (object graph walking, prototype method discovery, function source extraction, capability probing). Use these exact techniques.

2. `work_buddy/obsidian/smart/env.py` — the **template pattern**: JS snippets in `_js/` directory, `_run_js()` helper, Python wrapper functions with docstrings. Follow this pattern exactly.

3. `work_buddy/obsidian/bridge.py` — the eval_js transport layer. `bridge.eval_js(code, timeout)` executes JS inside Obsidian with access to the `app` object. **`bridge.eval_js` is gated by `@requires_consent('obsidian.eval_js', risk='high')`** — direct calls trigger a consent prompt. Read-only wrappers that execute fixed JS snippets must declare themselves safe invokers via `@reduces_risk_for('obsidian.eval_js', 'low')` so the inner gate auto-passes. Mutations that already establish their own outer `@requires_consent` gate (e.g. `calendar.create_event`) need no `@reduces_risk_for` — the outer consent context subsumes the inner check. See `notifications/consent` for the full mechanism and `work_buddy/calendar/env.py` for a reference implementation.

4. `work_buddy/calendar/` — a simple, complete reference integration (Google Calendar). Read `env.py`, `_js/`, and the collector to see the full pattern end-to-end.

5. `work_buddy/obsidian/tasks/` — a more complex integration (Obsidian Tasks plugin). Shows mutations, intelligence layer, and task-specific patterns.

## Finding the plugin

The target plugin lives under `<vault-root>\.obsidian\plugins\<plugin-id>\`. The `main.js` is bundled/minified — you **cannot** read the source on disk. Use runtime probing via eval_js instead.

Check the manifest:
```bash
cat "<vault-root>/.obsidian/plugins/<plugin-id>/manifest.json"
```

## Probe approach

Use **dedicated JS probe files** for complex exploration. Write a `.js` file in the `_js/` directory and execute it:

```python
from work_buddy.obsidian import bridge
from pathlib import Path

js = Path('work_buddy/obsidian/<plugin>/_js/_probe_<thing>.js').read_text()
result = bridge.eval_js(js, timeout=15)
```

This avoids quote escaping hell (PowerShell → Python → JS), is readable/editable, and can be version controlled. Delete probe files after documenting findings — prefix with `_probe_` to mark them as temporary.

### What to probe (in this order)

1. **Check if plugin is loaded:**
   ```javascript
   app.plugins.plugins["<plugin-id>"]
   ```

2. **Walk top-level keys:** `Object.keys(plugin)` — find methods, API objects, settings, cache.

3. **Check for a public API:** Look for `plugin.api`, `plugin.apiV1`, etc. Walk methods and get sources. Public APIs are stable; internal methods may break on update.

4. **Explore stateful objects:** Plugins often have a `cache`, `store`, or `state` object holding live data. Walk its keys, check if it has getter methods, count items.

5. **Get method signatures:** `fn.toString()` gives minified but readable source. Look for parameter names and return structures.

6. **Probe prototypes:** `Object.getPrototypeOf(obj)` + `Object.getOwnPropertyNames(proto)` to find inherited methods including those from parent classes.

7. **Check registered commands:** `app.commands.commands["<plugin-id>:*"]` — these may have `editorCheckCallback` or `callback` functions with useful logic. Read their source.

8. **Test calls — verify side effects:** Before assuming a method is pure, check whether it mutates in-memory objects or writes to disk. Read the file before and after calling. Example:
   ```javascript
   const contentBefore = await app.vault.read(file);
   const result = task.handleNewStatus(newStatus);  // call the method
   const contentAfter = await app.vault.read(file);
   return {file_changed: contentBefore !== contentAfter, ...};
   ```

9. **Test serialization:** Task/note objects often have circular references (parent<->children). You can't return them raw from eval_js. Instead, call serialization methods (`toString()`, `toFileLineString()`) inside JS and return the plain string.

**Important:** Plugins use `window.moment` for dates (Obsidian bundles Moment.js), not native `Date` objects.

## Package structure

Decide where the integration lives based on scope:
- **Obsidian-specific** (reading plugin runtime) → `work_buddy/obsidian/<plugin>/`
- **Cross-cutting capability** (calendar, email, etc.) → `work_buddy/<capability>/`

Create:
```
work_buddy/<location>/
├── __init__.py          # Public API re-exports
├── env.py               # Python wrappers (_run_js pattern from smart/env.py)
├── README.md            # Integration docs + runtime surface + stale warnings
└── _js/                 # JavaScript snippets
    ├── check_ready.js   # Always include a readiness check
    └── ...              # One snippet per capability
```

## JS snippet rules

- Wrap in async IIFE: `return (async () => { ... })()`
- Use `__PLACEHOLDER__` for parameter injection (replaced by Python before execution)
- Return `{error: "message"}` on failure, data on success
- Handle missing plugins gracefully: `if (!plugin) return {error: "Plugin not found"}`
- Keep snippets focused — one operation per file

## Python wrapper rules

- Follow `work_buddy/calendar/env.py` or `work_buddy/obsidian/tasks/env.py` exactly
- `_load_js(name)` + `_run_js(snippet, replacements, timeout)` helpers
- `check_ready()` function always first
- Docstrings on every public function with return type descriptions
- Use `_escape_js()` for user-provided text going into JS strings
- **Consent decorators on read wrappers:** every public read function that flows into `bridge.eval_js` (directly or via `_run_js`) must carry `@reduces_risk_for('obsidian.eval_js', 'low')`. Without it, direct `wb_run` calls of low-level reads surface a high-risk eval_js consent prompt because the read-only wrapper is unaware that the JS body is a fixed snippet, not caller-supplied. Mutations are unaffected: their own `@requires_consent` establishes a consent context that subsumes the inner gate.

## Context collection (if applicable)

If the integration produces data useful for context bundles:
1. Create `work_buddy/collectors/<name>_collector.py` following `calendar_collector.py`
2. Add to `COLLECTORS` set and import in `work_buddy/collect.py`
3. Write output as `<name>_summary.md` in the bundle directory
4. Always degrade gracefully (return unavailable report, don't crash)

## Running Python

```bash
powershell.exe -Command "cd <vault-root>\repos\work-buddy; conda activate work-buddy; <command>"
```

Set `WORK_BUDDY_SESSION_ID` env var before any `work_buddy` imports (consent system needs it).

## Testing

Write a temporary `_test_<name>.py` script that exercises every function. Run it, verify output, then delete it. Test:
1. `check_ready()` — plugin found and functional
2. Each query function — returns expected structure
3. Collector output — markdown is clean and informative
4. Graceful degradation — what happens when Obsidian is closed?

## Checklist

- [ ] Plugin probed, runtime surface documented in README
- [ ] `check_ready.js` + readiness function
- [ ] Core query/read functions working
- [ ] `__init__.py` with clean re-exports
- [ ] README with stale/maintenance warnings if applicable
- [ ] Collector integrated into context bundle system (if applicable)
- [ ] Read wrappers carry `@reduces_risk_for('obsidian.eval_js', 'low')`; mutations carry `@requires_consent` with appropriate risk
- [ ] End-to-end test passing
- [ ] Temp files cleaned up
- [ ] CLAUDE.md repo structure updated (if new top-level package)
