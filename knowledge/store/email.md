---
name: Email integration (Thunderbird bridge)
kind: integration
description: How work-buddy reads email via the thunderbird-work-buddy companion extension and feeds it into the triage Review pool
entry_points:
- work_buddy.email
- work_buddy.email.provider
- work_buddy.email.providers.thunderbird
- work_buddy.email.triage_adapter
- work_buddy.email.capabilities
tags:
- email
- thunderbird
- triage
- bridge
- companion-plugin
aliases:
- email integration
- thunderbird integration
- mail triage
- email bridge
- work-buddy email
dev_notes: '**Don''t reuse the `tools.obsidian.bridge_port` strategy here.** The Thunderbird bridge dynamically picks a port and writes a connection file at `<tmpdir>/thunderbird-work-buddy/connection.json`; the Python client discovers it. A fixed port collides badly when users run multiple Thunderbird profiles. **Don''t add mutating routes to the extension without consent gates.** The v1 read-only surface is asserted by `tests/routes.test.cjs` in the extension repo. Any compose / move / delete capability needs `@requires_consent` on the Python side AND an explicit feature flag in the extension''s options page. **The `thunderbird` tool probe is intentionally cheap.** It does a TCP probe + one authenticated `/health` round-trip; do not extend it to walk folders or fetch messages. **Verdict pass body-cap interaction with local models.** `_DEFAULT_VERDICT_BODY_CHARS=800` was tuned empirically against Qwen 14B''s 4096-token window plus the system prompt + active-tasks context block. Bumping the cap requires either a larger-context model or trimming the prompt. The classifier fix (work_buddy/llm/runner_v2.py + backends/_errors.py 2026-04) means context-exceeded errors auto-escalate now, but you''ll burn cycles on a wasted local call before the escalation fires.'
---

## Architecture

```
work-buddy agents
   │  MCP
   ▼
work-buddy capabilities (email_*)
   │  EmailProvider protocol
   ▼
ThunderbirdEmailProvider (HTTP client)
   │  Authorization: Bearer …  on 127.0.0.1:<dynamic_port>
   ▼
thunderbird-work-buddy companion extension (separate repo)
   │
   ▼
Thunderbird Mail API
```

The companion extension is **vendored separately** at `KadenMc/thunderbird-work-buddy`. It does nothing on its own — it exposes a narrow, authenticated, read-only HTTP bridge so work-buddy Python can read mail data. The agent-facing surface lives entirely inside this repo as MCP capabilities.

## Bridge surface (read-only v1)

The extension exposes a small REST API on a localhost port discovered via a connection file at `<tmpdir>/thunderbird-work-buddy/connection.json`:

| Method | Path                | Use |
|--------|---------------------|-----|
| GET    | `/health`           | liveness probe + plugin metadata |
| GET    | `/accounts`         | enumerate accounts; default-deny allowlist |
| POST   | `/folders`          | folder tree under an account |
| POST   | `/messages/recent`  | recent unread/all summaries |
| POST   | `/messages/search`  | token search across header fields |
| POST   | `/messages/get`     | fetch one message including body |
| POST   | `/messages/display` | open a message in Thunderbird UI |
| POST   | `/messages/exists`  | quarantine probe — "is this still at this folder URI?" |

No compose / send / move / delete / contacts / calendar in v1. Adding any of those requires extension-side trust changes plus capability-side `@requires_consent` gating.

## Provider abstraction

`work_buddy.email.provider.EmailProvider` is a Protocol; backends live under `work_buddy.email.providers.*`. The factory `get_email_provider()` reads `email.provider` from config (default: `"thunderbird"`). `"fake"` selects an in-memory provider used by tests and dry runs. This abstraction keeps the door open for Gmail API / Microsoft Graph / IMAP backends without touching capabilities or the triage pipeline.

Key provider methods:
- `recent_messages` / `search_messages` — header summaries
- `get_message` — body fetch
- `display_message` — open in UI
- `message_exists` — three-state liveness probe (`True` / `False` / `None` for ambiguous; used by the Threads cleanup runner)

## Stable keys vs operational handles

- **Stable key** — derived from RFC 822 Message-ID where available, hash of `(from, date, subject)` otherwise. Survives Thunderbird restarts and folder moves. This is the durable identifier we put on `TriageItem.id` (after hashing) and key dedup off of.
- **Operational handle** (`EmailMessageHandle`) — the bridge's transient id (Thunderbird's `messageId`) plus the folder URI. Used for follow-up calls (display, get-body, exists). Carried in `TriageItem.metadata` rather than the id.

Gmail's labels-as-folders model surfaces the same RFC Message-ID under multiple folder URIs (INBOX, [Gmail]/All Mail, [Gmail]/Important). The triage path dedups by stable_key within a single run, preferring inbox > archive > junk > trash via `_FOLDER_TYPE_PRIORITY`.

## Default-deny account access

The extension's options page presents a checkbox per account; an empty allow-list means **no accounts exposed** (intentionally different from upstream `TKasperczyk/thunderbird-mcp`'s "empty = all"). The `email_accounts` capability shows the user which accounts are currently visible.

## Triage integration

Email triage runs through the unified source pipeline at `work_buddy.pipelines.email.EmailTriagePipeline`, dispatched via the `run_source_pipeline` capability with `source='email_triage'`. The pipeline:

1. **Collects** recent unread mail via `collect_email_candidates` (the existing email_triage_adapter).
2. **Annotates** each item with synthesised tags (sender domain, folder type, flagged/read, message tags). No per-message LLM call — emails carry rich metadata already.
3. **Preclusters** algorithmically on subject + sender + tag overlap (no proximity weighting since email items don't have a spatial axis like Chrome tabs).
4. **Refines** cluster boundaries via the shared `refine_clusters` LLM step. Tier chain is local-first (`triage.refine_clusters.tier_chain` defaults to `local_tool_calling → local_fast → frontier_fast → frontier_balanced`). On full chain exhaustion, falls back to the algorithmic clusters with no proposed actions.
5. **Spawns** an umbrella Thread + N group children. Each child carries its emails as ContextItems and the LLM-proposed action from the email action library.

Email action library (`pipelines/email.py:EMAIL_ACTIONS`):
- **`email_close`** — advisory: dismisses the cluster Thread without touching the underlying mailbox. Routes through `thread_dismiss`.
- **`email_create_tasks`** — one task per email; subject → task text, sender + date → linked summary note.
- **`email_create_umbrella_task`** — one task for the whole cluster; cluster label → task text, bullet list of emails → summary note.
- **`email_record_into_task`** — file the cluster as a context section on an existing task's linked note. Use when the cluster is *context for ongoing work* (replies on an active deliverable, PR-review notifications about a task you're already tracking) rather than a new task. The user picks the target task at approval time.

The universal action library (`thread_dismiss`, `thread_defer`, `thread_rename`) layers on top.

Why advisory-only? The Thunderbird bridge is read-first in v1. Mutating actions (archive, move, delete, send) require extension-side permission changes plus capability-side `@requires_consent` gating; until then `email_close` is the closest defensible thing — the Thread is dismissed so the cluster stops appearing as work, but the mail is left alone.

## Trigger surfaces

- **Manual:** the `wb-email-triage` slash command (`.claude/commands/wb-email-triage.md`) → `email/email-triage` workflow → `run_source_pipeline(source='email_triage')`.
- **Scheduled:** `sidecar_jobs/email-triage-scan.md` cron, hourly at :23, **disabled by default**. Flip `enabled: true` in the frontmatter once the bridge is set up.

Both paths run the same pipeline; the cron version takes the same params (days_back, max_messages, unread_only) via the cron file's frontmatter.

## Source-stale handling

When an email referenced by a Thread's ContextItem disappears from its folder (deleted, moved out, account access revoked), the Threads cleanup runner (`work_buddy/threads/cleanup_runner.py`) detects this on its sweep via `provider.message_exists(handle)` and quarantines the Thread. Bridge ambiguity returns `None` and **never** quarantines — the sweep must not punish a brief outage.

## Setup — installing the companion extension from inside Thunderbird

Thunderbird-side install. The extension lives at `KadenMc/thunderbird-work-buddy` and ships pre-built. Two paths depending on what you're doing:

### Persistent install (the normal path)

Use this for day-to-day operation. Survives Thunderbird restarts.

1. **Get the .xpi.** Clone `KadenMc/thunderbird-work-buddy` and either run the build (`node scripts/build-xpi.cjs`, requires Node 18+, no extra deps — outputs to `dist/thunderbird-work-buddy.xpi`) or grab a pre-built release. The repo also has `node scripts/install.cjs` which auto-locates the standard Thunderbird profile (Windows: `%APPDATA%\Thunderbird\Profiles\*.default-release`; macOS: `~/Library/Thunderbird/Profiles/*.default-release`; Linux: `~/.thunderbird/*.default-release`) and drops the .xpi in. If you have multiple profiles, install manually instead.
2. **Open Add-ons Manager.** In Thunderbird: menu **Tools → Add-ons and Themes** (or **Ctrl+Shift+A**).
3. **Click the gear icon** (⚙) at the top right of the Add-ons Manager page.
4. Choose **"Install Add-on From File…"** from the dropdown.
5. Navigate to the .xpi (or to `dist/thunderbird-work-buddy.xpi` in the cloned repo) and select it. Accept the permissions prompt.
6. **Configure accounts.** Tools → Add-ons and Themes → Work Buddy Bridge → ⚙ Options. By default NO accounts are exposed (default-deny). Tick at least one account, click Save. The Status panel should show `Bridge running` plus the dynamic port (typically 27127, falling through 27128/27129 if busy) and the connection file path.
7. **Verify** with `wb_run('email_health')` from a Claude Code session. Expect `ok: true` plus a non-zero `accessible_accounts`.

### Temporary install (dev iteration)

Use this when you're hacking on the extension's code. **Unloads on every Thunderbird restart** — repeat the steps after each restart.

1. Hamburger menu (≡, top-right) → **Add-ons and Themes**. (Or press **Alt** to reveal the classic menu → Tools → Add-ons and Themes.)
2. Make sure **Extensions** is selected in the left sidebar.
3. Click the **gear icon** at the top-right of the Extensions list.
4. Select **"Debug Add-ons"** from the dropdown. (Note: Thunderbird has no URL bar, so typing `about:debugging` won't work — this menu route is the only way in.)
5. **First time only:** if the persistent copy is also installed, uninstall it first (Add-ons Manager → Extensions → Work Buddy Bridge → Remove). Otherwise both copies race for the bridge port and the connection file points at whichever startup happened last.
6. On the Debug Add-ons page click **"Load Temporary Add-on…"** and pick `extension/manifest.json` from the cloned repo (the unpacked source — no .xpi build needed for the temp-load path).

Dev-iteration loop after first install: edit code → click "Reload" next to Work Buddy Bridge on the Debug Add-ons page. That re-reads the unpacked source from disk, re-runs `init()` in `background.js`, reissues the per-startup bearer token, and rotates the connection file. Roughly one second; no build, no profile copy, no toggle-and-pray.

### Wire into work-buddy

Once the extension is running:

1. In `work-buddy/config.local.yaml`:
   ```yaml
   tools:
     thunderbird:
       enabled: true
   ```
2. Restart the work-buddy MCP gateway so the `thunderbird` tool probe picks up the now-reachable bridge and unhides the `email_*` capabilities.
3. Verify:
   - `wb_run('email_health')` → expect `ok: true`.
   - `wb_run('email_accounts')` → confirms the accounts you ticked are visible.
   - `wb_run('run_source_pipeline', {source: 'email_triage', dry_run: True})` is NOT supported (the unified pipeline doesn't take dry_run); use a small `max_messages` for first verification instead.
4. (Optional) To enable the hourly cron, flip `enabled: true` in `sidecar_jobs/email-triage-scan.md`.

When Thunderbird is closed or the bridge is unreachable, the `thunderbird` tool probe fails and the `email_*` capabilities are filtered out of the live registry — `wb_run('feature_status')` shows them under disabled-capabilities with the bridge as the missing dependency.

## Capabilities

- `email_health` — bridge liveness probe.
- `email_accounts` — list bridge-visible accounts.
- `email_get` — fetch one message body via the operational handle.
- `email_display` — open a message in Thunderbird's UI (3pane / tab / window).
- `email_close` — per-cluster advisory dismiss (Thread mutation only).
- `email_create_tasks` — per-cluster: one task per email.
- `email_create_umbrella_task` — per-cluster: one task representing the cluster.
- `email_record_into_task` — per-cluster: file emails as a context section on an existing task's note.

Email triage flows through the unified source pipeline; there is no separate `email_triage_run` capability. Triage execution goes through `run_source_pipeline(source='email_triage', ...)` (or the `email/email-triage` workflow).

## Related

- `email/email-triage` — the workflow wrapping the pipeline call.
- `email/triage-directions` — how to invoke the triage flow from an agent session.
