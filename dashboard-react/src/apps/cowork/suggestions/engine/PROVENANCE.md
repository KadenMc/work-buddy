# Vendored engine provenance

This directory is a vendored copy of the tracked-change engine that backs
`WbTrackedChangesAdapter`. It is source, not a published dependency, and it compiles
against whatever `@tiptap/pm` resolves in this tree (C1 surface section 3, adjudication 8).

## Upstream

- Package: `@handlewithcare/prosemirror-suggest-changes`
- Version: `0.1.8`
- Repository: https://github.com/handlewithcarecollective/prosemirror-suggest-changes
- Tag fetched: `v0.1.8`
- License: MIT (see `LICENSE` in this directory, retained verbatim)
- Copyright: 2025 Handle with Care Collective

Only the library source under `src/` was vendored. The upstream test suite, demo,
build tooling, and package metadata were not copied.

## Why vendored, not forked or depended on

Ratified by Kaden at the C1 freeze (FLAG-B, adjudication 8): 138 commits upstream at a
low cadence did not justify a fork burden, and a published dependency would reintroduce
the SP-1 `prosemirror-model` identity dedupe blocker. Vendoring the source removes the
separate published `prosemirror-model` and lets the engine share the one hoisted
ProseMirror instance. There is no folder named `vendor` by ruling (this is the natural
home, the Graphiti-prompts house precedent).

## Patch list

Every vendored file carries a per-file attribution header naming its modifications.
The complete list:

1. Import specifiers, every file. Bare `prosemirror-model` / `prosemirror-state` /
   `prosemirror-transform` / `prosemirror-view` imports rewritten to the matching
   `@tiptap/pm/*` subpaths (the single hoisted ProseMirror instance). Relative `.js`
   import extensions dropped to match the dashboard-react bundler module resolution.
2. `schema.ts`. Attribution attrs `producer` and `epistemic` added to the `deletion`,
   `insertion`, and `modification` mark specs, so provenance survives acceptance
   (SP-1 fork delta 2). The engine grouping key stays `id`, which the adapter injects
   with the kernel `proposal_id` through `generateId`, so the mark id is the proposal
   id. Every suggestion mark spec has its `parseDOM` stripped to `[]` (paste-forgery
   hardening, SP-1 fork delta 3, gate condition 2).
3. `withSuggestChanges.ts`. The suggest-mode dispatch guard now consults the canonical
   `isChangeOrigin` predicate from `@tiptap/extension-collaboration` instead of reading
   the raw `y-sync$` transaction meta by string, so remote batches, Yjs undo/redo, and
   local apply-origin mutations all pass through untracked (remote-Yjs guard completion,
   SP-1 fork delta).

## Adapter-level companions (not vendored, authored in the parent directory)

The Tiptap `Mark` wrappers, the suggest-changes plugin extension, the code-block schema
patch, the quote anchor resolver, the attribution stamping, the adapter, and the sitting
client live one level up in `../` as authored Co-work code. They consume this engine
through its `index.ts` re-export and are the running-schema source of truth for the
suggestion marks. The raw `schema.ts` `MarkSpec` objects are retained for upstream
parity.
