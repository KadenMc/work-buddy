---
name: Settings
kind: system
description: "Registry-driven Work Buddy settings, authority boundaries, App/System placement, and persistence."
summary: Settings definitions are owned once, placed into Apps-based pages and sections, validated by their authority, and projected through the React dashboard without conflating configuration with system status.
tags:
- settings
- configuration
- registry
- authority
- dashboard
aliases:
- app settings
- settings registry
- settings broker
entry_points:
- work_buddy.settings
- dashboard-react/src/settings
dev_notes: |-
  Server/profile values live in `db/settings/settings.db`. The store runs the Settings migration ladder once per resolved database path. Definitions, pages, sections, and placements are separate records in the registry; stored values key by setting identity and scope, not navigation path.

  The broker owns typed validation, authority checks, optimistic revision matching, preview, mutation, reset, immediate values, policy transitions, and event publication. React code consumes same-origin endpoints and keeps page-local lexical search controls mounted while filtering.

  Dashboard chat defaults use the shared execution-profile control and GET /api/settings/execution-catalog. Provider catalog reads do not resolve global defaults. Keep revisions/errors under Settings authority; do not treat a swallowed mutation failure as a successful model selection.
---

Settings is the authority and information architecture for configurable Work Buddy behavior.

## Navigation

Application-owned settings are organized under **Apps**, separated by provenance such as Built-in and Community. Journal and Co-work each appear once under `Apps -> Built-in`; Co-work owns its atomic Review shortcut map on that page. View- and System-related groups are sections inside the owning App page rather than duplicate global View entries.

Cross-dashboard model behavior belongs under **System → Dashboard AI** at
`/app/settings/system/dashboard-ai`, not Apps. The former
`/app/settings/apps/dashboard` route redirects while preserving navigation
state, query and fragment. Existing opt-in identity/value is unchanged.

The canonical Journal route is `/app/settings/apps/journal`. Contextual settings launchers navigate directly to the owning page. Compatibility routes may redirect there while preserving navigation state; they do not create a second setting identity.

## Registry model

The registry separates:

- a **definition**, which owns identity, type, validation, default, authority, and provenance;
- a **page**, which owns navigation identity;
- a **section**, which groups controls on a page; and
- a **placement**, which places one definition into one section.

A definition may have several placements without duplicating its stored value.

## Authority

Authority is declared per setting. Device-local settings cover presentation and accessibility behavior such as typography. Server/profile settings cover shared domain meaning, such as the Journal day boundary. Native and community contributions can use the same registry shape while receiving different trust and permission grants.

## Dashboard AI defaults

`wb.dashboard.assistance` permits explicit form-agent starts; it does not itself
send content or launch a model. `wb.dashboard.chat-execution-default` stores one
atomic provider/model pair for new or unbound dashboard chats. Each conversation
pins its own selection, so changing this default neither changes nor restarts
existing chats. The old assistance-tier preference is retained dormant, not
reinterpreted as an account-backed choice.

The default bootstraps once from the configured account-backed default and uses
normal Settings revision/reset semantics. Selection and reset validate the
exact pair with the execution provider registry. The shared model picker is
also used here through a Settings-backed adapter. Local inference profiles are
not registered interactive agent providers; contextual help explains the
limitation, and no cloud fallback is selected on their behalf.

## Broker behavior

The same-origin Settings API exposes registry, values, preview, mutation, and reset operations. Mutations use typed validation, revision checks, authority enforcement, and the dashboard read-only gate. Successful changes publish `settings.changed` to the live UI projection.

Registry-backed select controls use the same authoritative values path as the
Journal time control. The reusable keybinding-map control also uses that path:
one setting owns a declared command set and one atomic command-to-chord object,
so capture, collision validation, persistence, reset, runtime resolution, and
rendered shortcut hints share the same source of truth across Apps. Immediate values take effect in the mutation response;
only settings explicitly declared `next-boundary` enter the Journal transition
path. A stored default is frozen for its declared `value_version`, so changing
a definition's default or representation requires a deliberate Settings-store
migration rather than silently reinterpreting an existing profile. Co-work's
legacy inverted/Vim Review-navigation value is therefore explicitly migrated
to the complete shortcut map instead of being treated as the new representation.

Page search is immediate lexical filtering and keeps controls mounted so draft values are not destroyed. Global semantic Settings search is a separate integration boundary and is not supplied by browser calls to the embedding service.

`/app/settings/status` is a projection of the component dependency/control graph. It reports whether Work Buddy can operate and how requirements can be repaired; it is not a bag of ordinary editable settings.

See `services/dashboard/react`, `architecture/control-graph`, and `journal/day-lifecycle`.
