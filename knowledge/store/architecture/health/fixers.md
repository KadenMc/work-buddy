---
name: Fixers layer
kind: system
description: 'Per-requirement repair functions: clicking ''Fix'' on a failed requirement runs the registered fixer to bring it back to passing.'
tags:
- health
- fixers
- fix
- repair
- programmatic
- input-required
- agent-handoff
- settings
- architecture
aliases:
- fixers
- fix_kind
- fix_fn
- fix_params
- fix_agent_brief
- fix_preview
- programmatic fix
- input required fix
- agent handoff fix
- fix system
- click to fix
- settings fix button
parents:
- architecture/health
- architecture/health
dev_notes: 'When adding a fixer: pick the lowest fix_kind that fits. Most filesystem creators are programmatic. Anything needing user-supplied secrets or paths is input_required. Reach for agent_handoff only when there''s UI navigation or branching logic that resists scripting — the agent-spawn cost is non-trivial. Always test idempotency: run the fixer once, confirm green; run again, confirm the second run reports ''already exists'' rather than creating duplicates. Fixers must not raise; if you find yourself wrapping in try/except just to swallow exceptions, the underlying call should return its own ok/detail and you should be propagating that. Secret-type fixers persist via `_set_env_var`, which writes the repo `.env` AND updates the live `os.environ` in-process. The matching *consumer* must read the value via `work_buddy.secret_env.read_secret_env(name)` (env first, then a repo-`.env` line-scan), not a bare `os.environ.get` — `.env` is not auto-loaded by the MCP server / sidecar / dashboard, and the fixer runs in the dashboard process, so a consumer in a different process (e.g. an MCP capability) only sees a freshly-set key via the `.env` fallback. Reuse `read_secret_env` rather than hand-rolling another `.env` scan.'
---

Per-requirement repair functions. Each `RequirementDef` may opt into a fixer that runs when the user clicks Fix in the Settings tab. Fixers attach to **requirements**, not to components — runtime probe failures need a different kind of remediation (restart the service, fix upstream).

## The four `fix_kind` values

* **`none`** *(default)* — no automated fix. The requirement's `fix_hint` text is the only guidance; the user must follow it manually.
* **`programmatic`** — one click runs `fix_fn()` with no input. Use when everything the fix needs is already known (e.g., create a directory whose path comes from config).
* **`input_required`** — click pops a form rendered from `fix_params`; user fills it; `fix_fn(**form_values)` runs. Use when the fix needs information only the user can supply (e.g., a timezone string, a path, a secret).
* **`agent_handoff`** — click spawns a Claude Code session with `fix_agent_brief` as prompt. Use for setups too clicky or context-dependent for programmatic capture (e.g., installing an Obsidian community plugin).

## Fixer return shape

```
{
  "ok": bool,
  "detail": str,
  "side_effects": list[str]   # optional; surfaced in the UI
}
```

## Conventions

* **Idempotent.** Running the fixer twice produces the same end state. The dispatcher re-runs the requirement check after the fix — a non-idempotent fixer that creates duplicates breaks this.
* **Specific in `detail`.** Say what was created or changed: `"Created <vault>/journal/"` beats `"Done"`.
* **Honest about partial failure.** Return `ok=False` if anything blocks completion. Never raise — the dispatcher converts exceptions to `{ok: False, detail: "..."}` for endpoint consistency, but a fixer that handles its own failures gives better detail strings.

## `fix_params` schema (for `input_required`)

```
fix_params = {
  "<field_name>": {
    "type": "str" | "path" | "secret",
    "label": "<UI label>",
    "hint": "<placeholder / format hint>",
    "default": <Any>,        # optional
    "required": True | False,
    "secret": True | False,  # optional; redacts in UI
  },
}
```

The Settings tab renders one form input per field; on submit, values are passed as keyword args to `fix_fn`.

## `fix_agent_brief` (for `agent_handoff`)

A string prompt that's handed to a freshly spawned Claude Code session. Should explain: what the user is trying to fix, what the agent is empowered to do, the steps to walk the user through, how to verify completion (often: ask the user to refresh the dashboard Settings tab). The spawn flow is desktop-only (non-remote) — the user must be at the machine.

## Dispatch

Fixers are invoked by the dashboard endpoint `POST /api/control/fix/<req_id>`. The endpoint validates that the requirement opts in to a fix, gates on consent, runs the fixer, re-runs the requirement check, busts the control-graph cache, and returns `{ok, detail, side_effects, recheck, spawned}`. Full endpoint surface and Settings-tab UI: [architecture/control-graph](architecture/control-graph).

## See also

* [architecture/health](architecture/health) — the four-layer overview.
* [architecture/health/requirements](architecture/health/requirements) — the layer fixers attach to.
* [architecture/control-graph](architecture/control-graph) — dispatcher endpoint, Settings-tab UI, post-fix recheck flow.
