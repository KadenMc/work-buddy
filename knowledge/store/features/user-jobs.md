---
name: User-authored Scheduled Jobs
kind: directions
description: How a user authors a personal scheduled cron job — file location, frontmatter schema, collision behavior, hot-reload.
trigger: user wants to schedule a personal cron task that should not be tracked in the work-buddy repo
capabilities:
- sidecar_jobs
- sidecar_status
tags:
- user-jobs
- sidecar
- scheduler
- cron
- directions
- personal
aliases:
- schedule personal job
- write a user job
- drop a user job
- custom cron task
- my own scheduled job
parents:
- features
- features
---

Drop a markdown file with cron frontmatter into `<paths.data_root>/user_jobs/` (default `.data/user_jobs/`). The sidecar's filesystem watcher picks it up within ~50ms (a 30s polling reload remains as a safety net) alongside the system jobs in `sidecar_jobs/`. The directory is gitignored, so personal jobs never end up in the shared repo.

## Three ways to author a job

1. **Drop a file directly** — author the markdown with frontmatter (schema below) under `<paths.data_root>/user_jobs/`. Power-user path; assumes you know cron syntax and the registry.
2. **Dashboard job form** — `/app/jobs` (legacy Jobs tab → `+ Add job` redirects here). The native widget exposes live cron preview, registry parameter guidance, and schedule-aware Jitter validation. Its human-authorized `POST /api/jobs/authoring` delegates the existing `user_job_create` capability with `overwrite: false`.
3. **Dashboard assisted form** — choose **AI help** in that same widget. After opt-in and provider/model disclosure, the shared conversation component suggests typed patches that fill the visible fields. User edits win conflicts; changes have receipts and conditional Undo. Only the user's **Create job** click submits. Chat, including a message saying "yes", cannot schedule or overwrite a job. See `services/dashboard/react/assisted-drafts`.

Jobs assistance can search the same registered capability/workflow authoring
catalog shown by the form: exact names, slash aliases, one-line descriptions and
reduced parameter schemas. This lets it recommend `web_search` or another exact
operation without guessing. The lookup is metadata-only and disclosure-accounted;
it cannot run the result, search the web, start a workflow or create the job.

The capability/manual/assisted paths converge on `work_buddy.sidecar.scheduler.jobs.create_user_job_file`. Non-overwriting creation uses exclusive file creation, so concurrent requests cannot replace each other. React's assistable fields are declared once in `work_buddy/dashboard/assistance/form_schemas.json`, consumed by Python and TypeScript; there is no new form bridge, scheduler store, or duplicate chat implementation. The normal domain validation remains authoritative. Direct file authoring still relies on scheduler load-time parsing.

## Validation at create time

``create_user_job_file`` validates capability and dashboard submissions before writing the file. Directly authored files instead pass through scheduler load-time parsing. Specifically:

* **Name** — must match ``[A-Za-z0-9][A-Za-z0-9_-]{0,63}`` (no spaces, no leading dash/underscore). Returns ``{success: false, error: '...'}``.
* **Schedule** — exactly 5 cron fields, each parseable by ``parse_cron_field`` (range-checked).
* **Capability / workflow names** — must exist in the MCP registry. The validator strips a leading ``/`` (so ``/morning-routine`` works), then prioritizes a slash-command-to-registry resolution: if the user typed ``wb-morning`` (or just ``morning`` for a slash command stem), the error names the underlying registry entry explicitly: *"`wb-morning` is the slash-command name; the underlying workflow is `morning-routine`"*. Falls back to a ``difflib`` close-match suggestion if no slash-command match.
* **Workflow params** — when ``job_type=workflow`` and the workflow declares a ``params_schema``, params are pre-validated for unknown keys and missing required keys. Mismatches surface immediately at create time instead of on first cron fire.
* **Jitter** — ``jitter_seconds`` must be a non-negative integer. Bad input (negative, non-numeric) returns ``{success: false, error: 'jitter_seconds must be a non-negative integer, ...'}`` from the create path; jobs already on disk with bad input log a WARN and fall back to ``0``. The React form and its authorized API both enforce the schedule-aware ceiling; the underlying file authoring function accepts any non-negative integer.

Failures return a typed ``{success: false, error: str, errors_by_field: {field: msg}, suggestions: [str]}`` shape. The dashboard retains the draft, highlights offending inputs, and lets the user correct them manually or ask for another suggestion. Assistant patches are draft edits, not validated scheduling results.

## Where the file goes

- Default location: `<paths.data_root>/user_jobs/<your-job>.md` — resolves through `paths.data_dir("user_jobs")`, so it follows whatever you set for `paths.data_root`.
- Override: set `sidecar.user_jobs_dir` in `config.local.yaml` to any absolute or repo-relative path. Empty = use the default. Note: changing this at runtime requires a sidecar restart for the watcher to pick up the new path; the 30s poll continues to work in the meantime.
- The filename stem becomes the job's `name` (used in events, the dashboard, and `sidecar_jobs` output).

## Frontmatter schema

Same as system jobs. Required: `schedule`. Optional fields shown with defaults:

```
---
schedule: "*/15 * * * *"      # 5-field cron, evaluated in config.timezone
type: capability                # capability | workflow | prompt
capability: noop                # for type=capability
workflow: ""                    # for type=workflow
params: {}                      # for type=capability or type=workflow
recurring: true                 # false = one-shot, schedule cleared after firing
enabled: true
spawn_mode: ""                  # for type=prompt: headless_ephemeral | headless_persistent | interactive_persistent
jitter_seconds: 0               # see Jitter section below
---

Body text becomes the prompt for type=prompt jobs and the description otherwise.
```

## Jitter (the thundering-herd problem and its fix)

*Thundering herd*: phase-aligned cron schedules (``*/3``, ``*/5``, ``*/10``, ``*/30``) coincide at common minute boundaries (``:00``, ``:30``, hourly, etc.) and fire at the same second. The contention from those simultaneous starts — CPU spikes, disk thrash, lock waits, downstream API rate limits — is much worse than the same total work spread across the interval. Each job is fine in isolation; the synchronized burst is the problem.

Add ``jitter_seconds: <N>`` to delay firing by a deterministic per-job offset in ``[0, N]``. The same job always lands at the same offset across restarts; two jobs sharing a schedule land at *different* offsets and stop colliding.

### Schedule-aware ceiling

The Add-job form caps the value per schedule. Worked examples:

| Schedule       | Interval  | Max jitter |
|----------------|-----------|------------|
| `*/3 * * * *`  | 3 min     | 10 s       |
| `*/5 * * * *`  | 5 min     | 30 s       |
| `*/10 * * * *` | 10 min    | 60 s       |
| `*/15 * * * *` | 15 min    | 90 s       |
| `*/30 * * * *` | 30 min    | 180 s      |
| `0 * * * *`    | hourly    | 300 s (cap)|
| `0 9 * * *`    | daily     | 300 s (cap)|

The form pulls these from ``/api/cron/describe``, which returns ``interval_seconds`` + ``max_jitter_seconds`` alongside the human description. Out-of-range assistant suggestions remain visible and fail the same submit validation as manual input; they are not silently clamped or submitted.

The ceiling applies to both the React form and its API. `create_user_job_file` still accepts any non-negative integer, so users hand-editing a `.md` file or using the existing capability can deliberately exceed that UI policy.

### Tick-quantization caveat

The scheduler ticks every ``health_check_interval`` (default 30 s), so values < 30 are quantized away in practice. React authoring keeps a visible warning for nonzero jitter below 30 seconds; optional-delay and schedule-ceiling guidance lives in the field's Hover Help. Legacy editing retains its amber `⚠ Too small to take effect` warning and green `✓ Randomly delays firing…` hint. ``jitter_seconds: 0`` (the default) bypasses the pending-fire queue and fires inline on cron match.

### Dashboard surfacing

* The Jobs tab's **Next Run** column reads ``effective_at`` (next_at + offset, or queued pending due time) so the displayed time matches actual fire time, not the raw cron minute.
* A dedicated **Jitter** column shows the configured ``jitter_seconds`` (`+90s` for a jittered job, em-dash otherwise). Header tooltip explains what jitter is; per-cell tooltips name the offset window.
* The React authoring form has a numeric **Jitter** input next to Schedule. Its maximum updates live; narrowing the schedule surfaces an explicit validation error without silently rewriting an authored value. Legacy editing retains its existing clamping behavior.

Observability under each job in ``sidecar_state.json``:

* ``next_at`` — raw cron eligibility instant (no jitter applied).
* ``effective_at`` — the actual planned fire time. Equals ``next_at + offset`` for not-yet-queued jobs, or the pending due timestamp once a fire has been queued.
* ``jitter_seconds`` — mirror of the configured value.

Jitter does not substitute for concurrency control or misfire policy — those are separate, and a long-running capability still blocks subsequent ticks.

## Edit and delete via the dashboard

Each user-job row in the Jobs table has pencil + trash icon buttons. Pencil → ``GET /api/user_jobs/<name>`` to fetch the parsed frontmatter, populates the Add-job form pre-filled, sets the form into edit mode (name disabled, submit becomes "Save changes"). On submit the form passes ``overwrite: true`` so ``create_user_job_file`` replaces in place. Trash → confirm → ``DELETE /api/user_jobs/<name>`` removes the file; the sidecar's filesystem watcher catches up within ~50ms.

Three pending-action banners (Created / Updated / Deleted) provide instant feedback on user actions; they live in a dedicated DOM slot decoupled from the table refresh, so they appear on action and clear when the change has actually landed (row appears/disappears for create/delete; ``cron.hot_reload`` event for edit).

## Workflow params

For `type: workflow` jobs, the `params` dict is forwarded to the workflow at start time. The conductor validates the params against the workflow's declared `params_schema` and exposes them to:

- **`auto_run` steps via `input_map`** — use the synthetic source key `__params__` (whole dict) or `__params__.foo` / `__params__.a.b` (dotted-key walk) to wire a param into a kwarg. Example workflow step:
  ```json
  {"id": "do-thing", "step_type": "code", "auto_run": {
      "callable": "work_buddy.something.run",
      "input_map": {"project_id": "__params__.project_id"}
  }}
  ```
- **Reasoning steps via the workflow response** — the first-step response includes an `initial_params` field alongside `workflow_context`. Agents can read it and let it shape what they do in the reasoning step.

Workflows that don't declare a `params_schema` reject any non-empty `params` at start. Workflows that do declare one reject calls with required keys missing or with unknown keys. Validation policy is strict on purpose — a typo silently doing nothing is worse than an upfront error.

Declare a schema by adding a `params_schema` block to the workflow unit's YAML frontmatter (edit the unit via the `docs_edit` workflow):

```yaml
params_schema:
  project_id:
    type: str
    description: Project slug
    required: true
  depth:
    type: int
    description: How many levels to walk
```

## Collision policy

If your file's stem matches a system job stem (e.g. you create `user_jobs/task-sync.md`), **the user version wins** and the system version is dropped from the schedule. The scheduler logs a WARN naming both files so you can see which one is in effect. Use this deliberately to override a shipped job without forking the repo; or rename your file to keep both.

## Hot-reload

The scheduler reloads on two triggers:

- **Filesystem watcher** (`watchdog`, kernel events): picks up create/modify/delete/move on any `.md` file under the watched directories within ~50ms.
- **30s polling interval**: safety net for filesystem-event drops (rare on local NTFS; can happen on NFS / Docker overlay filesystems).

Drop, edit, or delete a file and it takes effect on the next tick (typically within a second). If a freshly-added file does not appear within ~30s, check the sidecar service log for parse errors. Hot-reload also prunes any pending jittered fires that reference jobs that disappeared, became disabled, or lost their schedule.

## Verifying it loaded

```
mcp__work-buddy__wb_run("sidecar_jobs")
```

Look for your stem in the returned `jobs` list. Each job carries a `source` field (`"system"` or `"user"`), so a missing entry means parse failure or a path mismatch. The dashboard's Jobs tab shows the same data, with user jobs at the top and system jobs collapsed under a disclosure; the table refreshes automatically when the scheduler reloads (driven by the `cron.hot_reload` event on the dashboard bus).

## What NOT to put in user_jobs/

- Anything you would want every other work-buddy install to run — those belong in `sidecar_jobs/` (system) and get committed to the repo.
- Secrets in plaintext params — the file lives under `<data_root>` which is gitignored, but the cron-triggered execution still runs through normal capability dispatch; pass secrets through the same env-var/keyring path you would use anywhere else.
