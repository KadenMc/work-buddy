---
name: Account-backed Agent Execution
kind: concept
description: Provider-neutral execution of long-running agents through locally authenticated Claude Code and Codex hosts, with server-authoritative model discovery and conversation-scoped provenance.
tags:
- agents
- execution
- providers
- models
- claude-code
- codex
- conversations
- security
aliases:
- agent execution
- execution profile
- provider picker
- model picker
- account-backed agents
parents:
- architecture
dev_notes: |-
  This subsystem is not `LLMRunner`. `LLMRunner` serves internal, usually one-shot inference calls through configured API or local-model tiers. `work_buddy.agent_execution` starts a long-running agent host that uses the user's existing Claude Code or ChatGPT sign-in.

  Provider and model IDs are untrusted until the server registry re-probes and validates the exact pair. Persist only the registry-authored labels returned by validation. Provider discovery failures must remain isolated and user-safe; never surface raw SDK, CLI, path, account, subprocess, or authentication diagnostics in the catalog.

  A Co-work execution process owns the exact entropy-first `<generation>-cowork` MCP session. The gateway replaces caller-asserted session identity with its transport identity and applies the non-overridable Co-work ACL. Every read, write, inbox operation, and assistant message is fenced to the lease's exact store, document, conversation, consumer, generation, and execution snapshot.

  Process ownership is `(pid, generation-owner-token)`, not PID alone. Natural-exit cleanup removes only the exact registered handle, failed termination retains ownership for a later retry, and cleanup must never kill an unowned or recycled PID.

  Content egress uses `work_buddy.agent_execution.disclosure`: create a run manifest, grant and reserve the exact Source representation/boundary, write `possibly_sent` before provider invocation, then mark `sent` and bind output to the ordered manifest digest. Raw source bytes never belong in manifest arguments or rows. A `possibly_sent` handoff is not automatically replayable.

  Claude runs against a per-run clean `CLAUDE_CONFIG_DIR`; user/project/local settings are absent rather than merely overlaid. On Windows and Linux it receives a private `0600` `.credentials.json` projection containing only `claudeAiOauth`; unrelated `mcpOAuth` credentials are never exposed. On macOS it starts empty because Claude Code reads the account credential from Keychain. Operating-system or organization-managed policy remains part of the host administrator trust boundary and cannot be bypassed by a child process. Do not describe the worker as suppressing that managed layer.

  WorkerDisclosureBoundary owns source-bound tool input accounting and output-manifest binding independently of hosts. Co-work retains only its document-specific source-origin adapter. Form agents reuse the neutral boundary; no second manifest or provider authority is introduced.
---

# Account-backed agent execution

`work_buddy.agent_execution` is the provider-neutral boundary for starting a
long-running agent through a locally installed, already authenticated agent
host. It lets a surface select one atomic provider/model pair without teaching
the conversation store or reusable Chat UI how either vendor's runtime works.

The first providers are:

- **Claude Code**, using the user's signed-in Claude account and the supported
  Sonnet or Opus aliases; and
- **Codex**, using the official `openai-codex` SDK/App Server and the user's
  ChatGPT-managed account. Its available model catalog is discovered from the
  installed runtime rather than hard-coded.

These routes use the user's existing product subscription. A future direct API
route must be a separate, plainly named provider with its own authentication
and cost semantics; an API key must never silently replace an account-backed
selection.

## Registry and catalog

`ProviderRegistry` is the server authority for provider availability, model
discovery, selection validation, and detached startup. Provider probes are
bounded, cached, and isolated: one unavailable runtime does not prevent other
providers from appearing. Catalog responses contain redacted, user-safe
availability states and descriptions only.

A selection is one indivisible `{provider_id, model_id}` pair. The server
re-probes and validates the pair before both persistence and launch. A provider
or model that disappeared after the catalog was shown therefore fails closed
instead of falling back to a different runtime or model.

## Dashboard defaults

System → Dashboard AI owns one profile-level default for new/unbound dashboard
chats. Reads resolve it lazily without probing providers; prepared conversations
keep their pinned selection independently of later default changes or failures.
The provider-only catalog can be read without resolving a global default.
Internal API/local inference tiers are never converted into account-backed
model selections.

Current picker labels are a pure projection over its already-fetched catalog,
shared by Co-work and form assistance. That display operation does not resolve
defaults, probe runtimes, persist selections or rewrite historical producer
labels; retired entries retain their saved-label fallback.

## Conversation authority

For Co-work, the conversations database durably owns the selected execution
profile. Before a conversation is bound, a read projects the configured default
without writing. Preparing the Chat surface creates or reuses its canonical
conversation and atomically pins the validated pair returned to the picker,
without starting a model. An authored turn is the execution boundary that
wakes or starts the selected driver. Confirmed changes use an opaque revision
and compare-and-swap. Repeating the same pair is idempotent, while a stale
conflicting change returns the current authoritative snapshot.

Changing the pair while a document agent is active restarts only that driver.
The durable conversation, transcript, pending human draft, document binding,
and review state remain. Every assistant message stores producer provenance
copied from the exact active lease, so history continues to show which
provider/model produced an older turn after a later switch.

Corrupt execution metadata is not treated as an absent selection. Reads and
mutations fail with a typed error, while already-committed document feedback or
review gestures return their durable receipt with a read-only degraded
execution projection rather than pretending the human action failed.

Form assistance uses the same execution registry and canonical conversation
selection, but its launch policy is explicit Start after displayed disclosure.
Preparation starts no agent. A form-model switch fences the previous driver
and requires a fresh Start, preserving the form and conversation. See
`services/dashboard/react/assisted-drafts` for its bounded tools and lifecycle.

## Isolation and fencing

Hosted form agents use an exact `<generation>-assisted-draft` transport identity
and only bound form-context/patch plus conversation tools. The same gateway
identity and top-level ACL protections apply; this does not grant Co-work,
task/job creation, arbitrary file or general workflow authority.

Each Co-work driver receives a fresh, exact session identity and only the
document-scoped Work Buddy MCP surface. The gateway pins the transport's real
session identity and enforces an immutable Co-work ACL for the selected folder,
document, conversation, lease consumer, and generation. Global status,
workflow, result-retrieval, and unrelated document operations are unavailable.
A restarted or retired generation loses all inbox and mutation authority.

Claude begins with only its built-in ToolSearch, uses that bootstrap to load the
exact Work Buddy initialization and capability tools named by its execution
identity, and receives a strict MCP configuration containing only Work Buddy.
The private brief forbids loading other tools. This bootstrap is required for
the driver to initialize and consume its durable inbox; disabling every tool
would start a process that could never participate in the conversation.

Claude Code runs with an empty neutral working directory, no session
persistence, browser/IDE integration, project instruction files, auto-memory,
attachments, inherited Anthropic credential/routing variables, prompt/tool
telemetry, debug logs, account-synced skills/plugins, marketplace auto-install,
slash commands, or inherited Claude OAuth environment tokens. Claude receives
a per-run clean config directory seeded only with a private projection of the
`claudeAiOauth` account credential on Windows and Linux, or initially empty
while macOS uses Keychain. Unrelated MCP OAuth credentials and user, project,
and local settings are absent.
Any transient state Claude creates remains inside that directory and is
removed on exit. Credential removal is attempted first and transient file
locks receive bounded retries; a persistent cleanup failure fails the run
rather than being silently ignored. Administrator-managed policy remains a
host trust boundary. Codex runs an
ephemeral read-only thread with inherited MCP servers, plugins, skills,
instructions, shell snapshots, analytics, proxies, and network capabilities
disabled. The selected host folder is represented only by the constrained
server-side resource binding; it is not placed in either provider's prompt,
arguments, or working directory.

The private brief is delivered through bounded stdin. A natural-exit reaper is
armed before delivery; if delivery fails and termination is not immediately
confirmed, an exact-handle background retrier continues cleanup while ownership
remains registered.

## Relationship to LLMRunner

`architecture/llm-runner` remains the unified interface for internal inference
calls selected by semantic capability tier. Account-backed agent execution
instead owns interactive, multi-turn, tool-using agent processes selected by a
human-visible provider/model profile. The two systems intentionally do not
share provider IDs, model configuration, persistence, or fallback behavior.
