---
name: Status CLI (shell-pollable consent/operation status)
kind: directions
description: Read-only shell command for polling consent-request and operation status from tooling that cannot speak MCP (the Monitor tool, bash loops, cron)
summary: '`bash /tmp/wb/status consent wait <request_id>` observes an ordinary timed-out consent request until approval/denial, then exits with a branchable code (0 granted, 1 denied, 2 timeout, 3 not found). Per-invocation exact-review timeouts require a fresh capability invocation instead of retry. Backed by python -m work_buddy.statusctl; read-only; generated per session by a SessionStart hook.'
trigger: an agent or shell watcher needs to wait for ordinary cacheable consent or poll operation completion without calling MCP
tags:
- consent
- cli
- monitor
- polling
- operations
- shell
aliases:
- wb status
- consent wait
- consent status
- op status
- /tmp/wb/status
- wait for consent
- poll consent
- statusctl
parents:
- operations
---

A read-only, shell-pollable window into two pieces of work-buddy state —
**consent-request status** and **operation status** — for tooling that
cannot speak the MCP gateway: the `Monitor` tool, `bash run_in_background`
loops, cron scripts, one-off diagnostics.

## Why this exists

When a `wb_run` call trips an ordinary cacheable consent gate, the gateway sends one bundled
notification, polls ~90s, then **times out and hands back** a result like
`{"status":"timeout","request_id":"req_…","operation_id":"op_…"}`. The user
can still approve on any surface afterward. The agent wants to *wait* for
that approval and then retry — but a shell watcher has no sanctioned way to
ask "is this consent granted yet?" without grovelling session-scoped consent
SQLite directly. This command is that sanctioned poll target.

Per-invocation exact-review consent is deliberately different. Once its
gateway call times out, a later approval cannot authorize the old operation,
an operation replay, or any future execution. This command may still observe
the durable request decision for audit, but the caller must invoke the
capability again to receive a fresh exact-review prompt.

## Commands

Generated per session at `/tmp/wb/status` (the session id is baked in, so
consent queries are automatically session-scoped). Backed by
`python -m work_buddy.statusctl`.

```
bash /tmp/wb/status consent wait   <request_id> [--timeout 600]
bash /tmp/wb/status consent status <request_id>          # one-shot
bash /tmp/wb/status op      wait   <operation_id> [--timeout 600]
bash /tmp/wb/status op      status <operation_id>         # one-shot
bash /tmp/wb/status --help
```

The verb may be omitted — `consent <id>` is shorthand for
`consent status <id>`. Add `--json` for the full status dict on stdout.

### `wait` vs `status`

- **`wait`** blocks in a single process, polling internally on a tiered
  cadence (2s for the first 30s, 5s to a few minutes, then 15s), and exits
  the moment the state resolves or `--timeout` (default 600s) elapses.
  `--timeout 0` checks exactly once; a negative timeout waits indefinitely.
  Prefer `wait` for a `Monitor` loop — it pays Python startup **once** for
  the whole wait instead of re-spawning the interpreter every poll.
- **`status`** prints the current state and exits 0 (state in the body).
  Use `wait --timeout 0` instead when you want a single check *with* the
  full exit-code vocabulary.

### Exit codes (branch on `$?`)

| code | meaning |
|---|---|
| 0 | granted / operation completed |
| 1 | denied / operation failed |
| 2 | timed out — no decision within the deadline, or the request expired |
| 3 | not found (unknown id) |
| 4 | internal error (e.g. no work-buddy interpreter found) |
| 130 | interrupted (SIGINT) |

## The wait-for-consent pattern (ordinary cacheable consent)

The loop the gateway timeout hands off to:

1. A `wb_run` call returns `{"status":"timeout","request_id":…,"operation_id":…}`.
2. First do anything else you can safely do now — then arm `Monitor` (or a bash loop) on `bash /tmp/wb/status consent wait <request_id> --timeout -1`.
3. On exit 0 (granted), complete the sanctioned retry — `wb_run("retry", {"operation_id": …})` (or `obsidian_retry` for bridge ops). On exit 1 (denied) abort; on 2 (expired) re-prompt or escalate.

Do not use this retry pattern for `grant_policy="per_invocation"`. A timeout
has already destroyed that invocation's opportunity to receive ephemeral
authority; start a fresh capability call instead.

```bash
bash /tmp/wb/status consent wait "$REQUEST_ID" --timeout -1
case $? in
  0) echo "granted — retry now" ;;
  1) echo "denied — abort" ;;
  2) echo "expired — re-prompt or escalate" ;;
  3) echo "unknown request id" ;;
  *) echo "error" ;;
esac
```

### How long to wait, and what it costs

Waiting is **free** and **self-bounding**, so prefer a generous (or indefinite) timeout over a short one:

- **Free:** run the `wait` in the background (the `Monitor` tool or `run_in_background` bash). Billing is per-token, not per-wall-clock-time — while the watcher sleeps between polls the model isn't invoked, so a wait of minutes or hours costs **zero tokens** until it resolves and re-invokes you. (Keep it silent — do not pass `--verbose` under the `Monitor` tool, or each progress line becomes a billed turn.)
- **Self-bounding:** `--timeout -1` does not actually hang forever — a consent request carries a ~2h TTL, and `expired` is a terminal state, so the wait exits (code 2) when the request expires. So `-1` means "until the user decides or the request lapses," not "until the heat death of the universe."
- Because it's free and self-bounding, the only reason to keep working instead of waiting is that you have other useful work in hand. Do that first, then wait.
- **When a finite `--timeout` is the better choice:** `-1`'s free/self-bounding properties depend on running in the **background** (Monitor / `run_in_background`), where the model idles. Use a bounded `--timeout <seconds>` if you're running it in the **foreground** (where `-1` would tie up the turn for up to the full TTL), if you have your own deadline/SLA, or if you'd rather poll briefly and re-check later (the reschedule pattern) — e.g. escalate to another approver after N minutes.

## Guarantees and limits

- **Strictly read-only.** It observes consent/operation state; it never
  mints, caches, or consumes a grant. A `granted` verdict only means the
  user approved. For ordinary cacheable consent, the actual retry goes back
  through the gateway, whose `@requires_consent` gate re-checks the grant
  against the live principal. If the grant has since expired, the gate
  re-prompts. This preserves the invariant that grants do not time-travel
  through the retry queue.
- **Exact-review decisions are observational only after timeout.** A reported
  approval for a timed-out per-invocation request writes no reusable grant and
  cannot authorize replay. Only a fresh prompt on a fresh capability
  invocation can create the matching ephemeral authority.
- **Session-scoped grant reads.** Consent grants live in a per-session
  `consent.db`; the command resolves against the baked-in session id, so it
  reads the agent's own grants, not another session's. Out-of-band grants
  (approved on another surface) are detected even if the request record has
  not yet flipped to `responded`.
- **Operation records are global**, keyed by `operation_id` (not
  session-scoped); a `running` record whose execution lease has lapsed is
  reported as `stale` but a `wait` keeps polling until its own timeout.
- **Startup cost.** Each invocation pays interpreter + package import
  (hundreds of ms on some platforms, dominated by work-buddy's version
  lookup at import). The blocking `wait` amortizes this across the whole
  wait — another reason to prefer `wait` over re-spawning `status` in a
  tight loop.
- **Interpreter resolution.** The command finds a Python that can import
  work-buddy via `$WORK_BUDDY_PYTHON` → the project `.venv` created by
  `uv sync` → a `python`/`python3` on PATH. Set `WORK_BUDDY_PYTHON` if your
  interpreter is elsewhere.

## Implementation

- CLI: `work_buddy/statusctl/` (`cli.py` — argparse, tiered wait loop, exit
  codes; lazy domain imports keep `op` queries off the consent stack).
- Consent composer: `work_buddy/consent_status.py` — fuses
  `consent.get_consent_request` (request lifecycle) and
  `consent.list_consents` (the grant) into one verdict.
- Operation reader: `work_buddy/operations_read.py` — mirrors the gateway's
  on-disk operation layout without importing the gateway.
- Shell wrapper template: `work_buddy/statusctl/bin/status.sh`, materialized
  into `/tmp/wb/status` by `work_buddy/statusctl/install_commands.sh` on
  SessionStart (registered in `config/global_settings.json`, mirroring the
  messaging `check_messages.sh` hook).
