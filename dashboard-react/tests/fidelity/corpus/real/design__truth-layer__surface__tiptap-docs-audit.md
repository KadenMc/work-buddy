# Tiptap docs audit: the official docs vs the Knowledge-work surface design

- Source: sparse clone of `github.com/ueberdosis/tiptap-docs`, commit `5c80cd8` (upstream commit date 2026-07-16), vendored at `vendor/tiptap-docs/`. All citations are paths relative to `src/content/`.
- Audited against: [PRD.md](PRD.md), [architecture.md](architecture.md), [old-prd-distilled.md](old-prd-distilled.md).
- Date: 2026-07-17. Agent-authored, unreviewed. Input to the S0 spike suite and the contract freeze.
- Reading convention: "docs are silent" means the point was searched across the corpus and not found, not merely absent from one page.

## A. Glossed over: details the docs surface that our design must decide

Ordered by importance. Each item: what the docs establish, why it bites THIS design, and the decision it forces.

### A1. Where the one Markdown serializer runs (the materializer has no runtime)

- What: `@tiptap/markdown` is a JavaScript library. "The extension works identically in browser and server environments" means browser and Node (editor/markdown/index.mdx). `MarkdownManager` is a standalone JS class, constructible without an editor, with `registerExtension()`, `parse()`, `serialize()` (editor/markdown/api/markdown-manager.mdx). A fully headless editor also exists via `element: null` (guides/upgrade-tiptap-v2.mdx, "Server-Side Rendering").
- Why it bites: the design assigns materialization (structured doc to clean `.md`) to first-party server glue (architecture.md section 4, item 3), and v1 explicitly has no Node runtime (architecture.md section 6). The Flask/Python server cannot run the serializer in-process. Server-side flows the PRD promises (retire re-materialization, drift-diff regeneration) have no runtime to run on.
- Decision forced: pick one of (a) materialize in the dashboard client, the only JS runtime in v1, and POST rendered Markdown plus content hash to the server, accepting that re-materialization requires an open client, (b) add a small Node helper process invoked by the engine, (c) write a Python serializer, which creates the second serializer I14 forbids. Re-specify the server-only flows against the winner. Option (b) is small if chosen, per the standalone-manager and headless-editor docs above.

### A2. Custom marks need an explicit Markdown-projection contract, and unknown-mark serialization is undocumented

- What: every extension participates in serialization via its own `renderMarkdown` handler and in parsing via `parseMarkdown` plus optional `markdownTokenizer` (editor/markdown/getting-started/basic-usage.mdx "Extension Handlers"). Documented fallback exists only on the PARSE side, and only for `paragraph`, `heading`, `text`, and `html` tokens (editor/markdown/advanced-usage/custom-parsing.mdx "Fallback Parsing"). The behavior of serializing a mark that has NO markdown spec (dropped, children-only, error) is nowhere documented.
- Why it bites: our provenance and suggestion marks must project to NOTHING in clean Markdown (I12, I13). Relying on undocumented drop-through risks either leaked markup or thrown serialization.
- Decision forced: give every wb mark an explicit `renderMarkdown` that returns `helpers.renderChildren(node.content)` unchanged, the documented mark pattern minus the syntax wrapper (editor/markdown/advanced-usage/custom-serializing.mdx "Serializing Marks"), and add a fidelity-suite case proving open suggestion marks never appear in materialized output. For structural custom nodes we DO want in Markdown, use the official spec generators: Pandoc `:::name {attrs}` blocks and `[name attr="v"]content[/name]` inline shortcodes with attribute whitelists (`createBlockMarkdownSpec`, `createAtomBlockMarkdownSpec`, `createInlineMarkdownSpec`, importable from `@tiptap/core`, editor/markdown/api/utilities.mdx).

### A3. Paste is an attribution forgery vector

- What: a mark's `parseHTML` "will be used during paste events to parse the HTML content into a mark" (editor/extensions/custom-extensions/create-new/mark.mdx). HTML embedded in Markdown also parses through the same `parseHTML` rules on import (editor/markdown/getting-started/basic-usage.mdx "Handling HTML in Markdown").
- Why it bites: if the provenance mark parses `span[data-wb-provenance]` (or any attribute-carrying tag), pasting crafted clipboard HTML mints provenance-bearing marks with no ledger event, forging AI-approved or human-authored status. The same applies to suggestion marks and to out-of-band `.md` files re-imported with embedded HTML.
- Decision forced: provenance and suggestion display marks must not be reconstructible from paste or HTML import. Documented options: omit or restrict `parseHTML` on those marks, strip attributes in a `transformPastedHTML` hook (priority-ordered chains documented in editor/extensions/custom-extensions/create-new/extension.mdx), or treat parsed-in provenance attrs as untrusted and re-derive display state from the ledger, which I12 already implies. Note `enablePasteRules` and `enableInputRules` accept per-extension allowlists (editor/api/editor.mdx).

### A4. The mark schema flag matrix, and the docs contradict themselves on defaults

- What: marks carry schema flags our design never assigns: `inclusive`, `spanning`, `keepOnSplit`, `exitable`, `excludes`, `code`, `clearable`. The core schema page documents the ProseMirror-correct semantics: "If you don't want the mark to be active when the cursor is at its end, set inclusive to `false`" (the Link mark does), and "By default marks can span multiple nodes... Set `spanning: false`" (editor/core-concepts/schema.mdx "The mark schema"). The Mark API page asserts the OPPOSITE defaults: "By default, marks are not inclusive", "By default, marks do not span multiple nodes" (editor/extensions/custom-extensions/create-new/mark.mdx).
- Why it bites: an inclusive AI-provenance mark grows as the human types at its edge, silently attributing human keystrokes to the AI span. This is a direct I11 violation waiting in a default.
- Decision forced: a per-mark flag matrix in the extension bundle spec, verified at spike against the schema page's semantics (which match upstream ProseMirror), not the Mark API page's wording. Suggested start: provenance and suggestion marks `inclusive: false`, `keepOnSplit: true`, `clearable: false` (protects them from `unsetAllMarks`, an explicitly documented protection, create-new/mark.mdx "clearable"), plus explicit `excludes` relations between suggestion mark variants.

### A5. Suggestion marks cannot exist in code blocks or on atom nodes

- What: Tiptap code blocks disallow marks, and the paid Tracked Changes product ships a patch helper for exactly this: "By default, code blocks don't support marks. The extension provides a helper to patch any code block extension" (tracked-changes/guides/editor-setup.mdx "Enable code block tracking", tracked-changes/usage/advanced-usage.mdx "Code block compatibility"). Atom nodes cannot carry marks at all, so the paid product tracks them via node attributes: `suggestionId`, `suggestionType`, `suggestionUserId`, `suggestionCreatedAt`, `suggestionUserMetadata` (usage/advanced-usage.mdx "Atom node tracking"). Node views additionally need suggestion attrs forwarded explicitly via a `getSuggestionHTMLAttributes()` helper (tracked-changes/guides/nodeview-support.mdx).
- Why it bites: our documents are Markdown-shaped and WILL contain code blocks and horizontal rules. An agent proposal touching a code block is a normal case, not an edge.
- Decision forced: the S0 bake-off must test agent edits inside code blocks and on hr/image nodes for every candidate engine, and the adapter contract must state v1 support: schema `marks` override on code_block, attribute-based fallback for atoms, or scoping such edits to whole-block replace proposals.

### A6. Schema strictness, contentError policy, and what the check does not cover

- What: the schema is "very strict", content that does not fit is silently thrown away, on paste and on load (editor/core-concepts/schema.mdx). `enableContentCheck: true` validates only the INITIAL `content` and emits `contentError`. `emitContentError: true` reports while still accepting invalid content. Checking is "100% accurate on JSON content types" but HTML-sourced marks "can be missed in certain situations" (schema.mdx "Invalid Schema Handling", guides/invalid-schema.mdx). The documented collaborative recovery: call `disableCollaboration()` from the error payload, then `editor.setEditable(false, false)` so invalid content never syncs. v3 also ships a `rewriteUnknownContent` helper in `@tiptap/core` (resources/whats-new.mdx).
- Why it bites: a persisted Y.Doc that no longer matches the current extension bundle (after a wb upgrade) is exactly the version-skew scenario the guide describes. Silent stripping would violate I13's never-silently-absorbed rule from the inside.
- Decision forced: wire `enableContentCheck` + `onContentError` into the surface's read-only degrade path (I18 host states), define the recovery flow for bundle-mismatch on open, and version the extension bundle in the document profile so mismatch is detectable server-side before the editor ever mounts.

### A7. Transaction interception now has TWO documented layers, and engine plus provenance must pick theirs

- What: Tiptap v3 documents an extension-level `dispatchTransaction` middleware chain: priority-ordered, each hook receives `{ transaction, next }`, not calling `next` blocks the transaction, and the chain terminates in "the editor's base dispatch function (or your custom `editorProps.dispatchTransaction` if defined)" (editor/extensions/custom-extensions/create-new/extension.mdx "dispatchTransaction"). A global `enableExtensionDispatchTransaction` option, default `true`, disables the whole mechanism (editor/api/editor.mdx).
- Why it bites: the engine candidate `prosemirror-suggest-changes` decorates `editorProps.dispatchTransaction`, which the Tiptap chain wraps AROUND. Our provenance meta stamping wants a deterministic position relative to suggestion interception, and two uncoordinated layers is how double-handling bugs happen.
- Decision forced: which layer hosts (a) suggestion-mode interception and (b) provenance stamping, and their priorities. The middleware layer is the Tiptap-native seam for `WbTrackedChangesAdapter` and makes the provenance tap an ordinary prioritized extension. Also lock a rule that no wb extension blocks transactions silently, since a blocked transaction is indistinguishable from a no-op to the user.

### A8. UniqueID is not turnkey: configuration and runtime placement

- What: `types` defaults to `[]`, so NOTHING gets an ID until types are enumerated or `'all'` is passed (which covers everything except `doc` and `text`). The attribute renders prefixed as `data-*`. With Collaboration you must pass `filterTransaction: (tr) => !isChangeOrigin(tr)` so remote transactions do not mutate IDs. Read-only surfaces should set `updateDocument: false`. Mounting before the Y.Doc has synced "can lead to unintended document state (e.g., persistent empty paragraphs), which the UniqueID extension will then preserve" (editor/extensions/functionality/uniqueid.mdx, cross-referenced from editor/extensions/functionality/collaboration.mdx "Usage with UniqueID"). A server-side `generateUniqueIds(doc, extensions)` utility exists but is a Node API (uniqueid.mdx "Server side Unique ID utility").
- Why it bites: PRD section 5 treats `node_id` via UniqueID as settled. The unconfigured default assigns no IDs, the wrong mount order corrupts documents in a way UniqueID then PRESERVES, and our Python import pipeline cannot call the Node ID-minting utility directly.
- Decision forced: the `types` list (recommendation: `'all'` minus inline atoms, decided at spike), the ID-assignment step's location in registration/import (client-side on first open, or the A1 Node helper), and a hard load-order contract: initialize Y.Doc from persistence, await readiness, then mount, seeding initial content only when the fragment is empty.

### A9. Collaboration field/fragment naming is part of the document contract

- What: `Collaboration.configure` takes `document` plus `field` (default `'default'`) or a raw `fragment`. The documented emptiness check on load reads `doc.getXmlFragment('default')` (editor/extensions/functionality/collaboration.mdx "Settings", editor/extensions/functionality/uniqueid.mdx "Correct setup").
- Why it bites: our persistence layer, a future Hocuspocus `onLoadDocument`, and any server-side inspection all reference this name. A drifted fragment name forks content invisibly: the editor binds an empty fragment while the real content sits in another.
- Decision forced: fix the fragment name (keep `'default'`) in the document-surface profile, and standardize the seeding-on-empty rule per the documented pattern.

### A10. Round-trip normalization is real and specific: byte preservation cannot come from the serializer

- What: documented normalizations that rewrite untouched regions on full-document serialization: block separators appended (`'# Hello World\n\n'`, editor/markdown/guides/integrate-markdown-in-your-extension.mdx test examples), consecutive empty paragraphs round-trip via `&nbsp;` markers after the first in a run (editor/markdown/api/markdown-manager.mdx "parse", integrate-markdown-in-your-extension.mdx "Preserve Empty Paragraphs"), list and code indentation regenerated from the `indentation` config (editor/markdown/getting-started/installation.mdx), Markdown tables accept only one child node per cell, and comments are dropped entirely (editor/markdown/index.mdx "Limitations").
- Why it bites: success criterion 2 promises unedited regions byte-preserved. A whole-document serialize pass cannot deliver that. The block-splice materializer is load-bearing, not an optimization.
- Decision forced: specify the splice unit (top-level block keyed by `node_id`), the fidelity suite's normalization-tolerance rules for EDITED blocks, and a policy for constructs MarkedJS reads differently than the vault corpus expects (setext headings, lazy list continuation), discovered at spike.

### A11. `contentType: 'markdown'` must be threaded through every content entry point

- What: content is treated as JSON or HTML unless `contentType: 'markdown'` is passed, on the constructor and on `setContent`, `insertContent`, `insertContentAt` alike. An autodetect fallback exists "if formats don't match", and the docs still say "Always use `contentType`" (editor/markdown/getting-started/basic-usage.mdx "Best Practices", editor/markdown/api/editor.mdx). One page references a `contentAsMarkdown` option its own examples never use, an internal inconsistency to resolve in favor of `contentType` (editor/markdown/api/editor.mdx "Editor.content" note).
- Why it bites: the import path, proposal ingestion, and any programmatic insertion each independently risk mis-typed content becoming literal text or mis-parsed HTML.
- Decision forced: declare per call site whether agent hunks arrive as Markdown strings or Tiptap JSON. Recommendation: convert quote-anchored Markdown to JSON once at the ingest boundary so `contentType` handling lives in exactly one place.

### A12. React rendering policy for an editor plus a live review rail

- What: v3 changed the default: `shouldRerenderOnTransaction` is `false`, the editor component does NOT re-render per transaction (resources/whats-new.mdx "Breaking Changes"). Reactive UI reads state via `useEditorState` selectors with deep-compare (guides/performance.mdx). The editor must be isolated in its own component, away from unrelated state. React state updates inside editor callbacks need `queueMicrotask` to avoid the `flushSync` warning. React node views render synchronously, with known extra wrapper divs, and React context reaches node views only if the provider wraps `EditorContent` (guides/performance.mdx, guides/faq.mdx "React context is not working with NodeViews", "Why are there extra divs").
- Why it bites: the review rail, mark bar, and margin cards are exactly the "things that depend on the editor" the performance guide warns about, and I1 makes jank a hard invariant.
- Decision forced: rail state flows exclusively through `useEditorState` selectors over our plugin state, margin-card geometry comes from DOM measurement not React render cycles, and the Theme Contract provider wraps `EditorContent`.

### A13. React 19 support is not doc-backed, and editor StrictMode behavior is undocumented

- What: the corpus's only React 19 statement is a warning that UI Components "work best with React 18" while React 19 support is in progress (ui-components/getting-started/overview.mdx). Nothing documents `@tiptap/react` core on React 19, and nothing documents `useEditor` under StrictMode double-mount. The only StrictMode-safe claim anywhere is for `@hocuspocus/provider-react` (v4, `useSyncExternalStore` based, hocuspocus/provider/react.mdx).
- Why it bites: the dashboard is React 19 (I18 context). architecture.md's React 19 claim is npm-metadata-sourced, and the double-mount failure mode intersects A8's document-corruption warning.
- Decision forced: S0 runs the editor under React 19 StrictMode with Collaboration plus persistence attached, watching for double-init of the Y.Doc binding. Do not lift Tiptap UI Components code into the React 19 dashboard until upstream declares support.

### A14. Undo facts to bake into the spike

- What: Collaboration "comes with its own history extension" plus `undo()`/`redo()` commands and shortcuts, with StarterKit's UndoRedo disabled via `StarterKit.configure({ undoRedo: false })` (editor/extensions/functionality/collaboration.mdx "Commands", starterkit.mdx, guides/upgrade-tiptap-v2.mdx). The `depth` and `newGroupDelay` knobs belong to the PM UndoRedo extension only (editor/extensions/functionality/undo-redo.mdx), no grouping knobs are documented for the Collaboration undo. The paid product's events note suggestions can be "restored via undo/collaboration" (tracked-changes/api-reference/events.mdx).
- Why it bites: undo can resurrect suggestion marks for proposals the ledger already closed, and undo grouping granularity is untunable by any documented means.
- Decision forced: the planned Yjs-undo spike gains two named cases: undo-after-accept (mark returns while ledger says applied, who wins) and grouping granularity of typing bursts under the collab undo manager.

### A15. Decorations are raw ProseMirror, and the new delete event helps anchor upkeep

- What: Tiptap has no decoration API. The official linting example says so ("There is no decoration API in Tiptap, that's why this is a lot of ProseMirror work", examples/experiments/linting.mdx), and a Decorations API is listed as post-3.0 roadmap (resources/whats-new.mdx "What's next"). Node views do not re-render when only decorations or positions change (editor/extensions/custom-extensions/node-views/react.mdx "Tracking node position"). v3 adds a `delete` event carrying `{ type, deletedRange, newRange, partial, node, mark }` (editor/api/events.mdx).
- Why it bites: the 300-live-decorations perf budget rides hand-rolled `DecorationSet` code in `addProseMirrorPlugins` (editor/extensions/custom-extensions/create-new/extension.mdx), and proposal-anchor invalidation otherwise re-anchors by quote every transaction.
- Decision forced: whether the anchor-maintenance plugin consumes the `delete` event as its invalidation fast path, and the decoration batching strategy for margin-alignment measurement.

### A16. Diffing agent output into suggestions must ignore identity attributes

- What: the paid AI Toolkit's tracked-changes reconstruction exposes its diff knobs: `ignoreAttributes` defaults to `['id', 'data-thread-id', '_hash']`, `ignoreMarks` defaults to `['inlineThread']`, plus `simplifyChanges`, `changeMergeDistance`, `mode: 'inline' | 'block' | 'smart'`, `groupInlineChanges`, `expandBlockChanges: ['listItem']` (ai/ai-toolkit/api-reference/review-options.mdx).
- Why it bites: with UniqueID active, node IDs differ between an agent-produced fragment and the live doc. Naive structural diffing marks everything changed. Their first default exists precisely because of this trap.
- Decision forced: the adapter's diff layer ignores `node_id` and wb provenance attrs, and the defaults above are the starting configuration for hunk merging in the bake-off scoring.

### A17. Mark views are new, minimal, and their interactivity limits are unstated

- What: MarkViews are new in v3 (resources/whats-new.mdx). Documented API: `addMarkView()` returning a vanilla `{ dom, contentDOM }` factory or `ReactMarkViewRenderer(Component)`, `MarkViewContent`, `updateAttributes`, attrs via `mark.attrs`, and mark views are "unrelated to the HTML output by design" (editor/extensions/custom-extensions/mark-views/index.mdx, react.mdx, javascript.mdx). Unlike node views: no props table, no `getPos`, no `selected`, no lifecycle or `ignoreMutation` hooks, no performance notes.
- Why it bites: if suggestion or provenance chips render via mark views, we would be building interactive UI on an API whose event and update semantics the docs do not specify.
- Decision forced: spike what a MarkView can intercept before committing any rail affordance to it. Default posture: decorations plus a single inspector for dense inline UI, mark views reserved for low-frequency interactive marks.

## B. Lock-ins: doc-stated facts the design can commit to

### Markdown extension

- Official, public npm, plain `npm install @tiptap/markdown`, no registry auth (editor/markdown/getting-started/installation.mdx). Beta, "early release and can be subject to change or may have edge cases" (editor/markdown/index.mdx warning). Every markdown page carries the beta tag. Exact-pin policy stands.
- Pipeline: MarkedJS lexer to tokens to per-extension parse handlers to Tiptap JSON, and extension render handlers back to strings. CommonMark compliant, GFM via `markedOptions: { gfm: true }`, custom `marked` instance injectable, `indentation` style/size configurable (editor/markdown/index.mdx "Architecture", installation.mdx, basic-usage.mdx).
- Extension spec surface: `parseMarkdown(token, helpers)`, `renderMarkdown(node, helpers, context)`, `markdownTokenName`, `markdownTokenizer` (its type requires a `tokenize` function, editor/markdown/api/types.mdx), `markdownOptions.indentsContent` (editor/markdown/api/editor.mdx "Extension Spec"). One editor-page tokenizer example names the function `tokenizer` instead of `tokenize`, contradicted by the type definition and every guide, resolve toward `tokenize` (api/editor.mdx vs api/types.mdx and guides/create-a-highlight-mark.mdx).
- Helpers, parse side: `parseInline`, `parseChildren`, `parseBlockChildren` (empty-paragraph preserving), `createTextNode`, `createNode`, `applyMark(markType, content, attrs)`. Render side: `renderChildren(nodes, separator)`, `indent`, `wrapInBlock`, with `RenderContext` carrying `index`, `level`, `parentType`, `meta`, `previousNode` (editor/markdown/api/utilities.mdx, api/types.mdx, api/markdown-manager.mdx, guides/integrate-markdown-in-your-extension.mdx).
- `editor.getMarkdown()`, `editor.markdown` manager access, standalone `parse`/`serialize`/`renderNodeToMarkdown`/`renderNodes` (editor/markdown/api/editor.mdx, api/markdown-manager.mdx).
- Hard limitations to design around: comments unsupported and can be LOST when Markdown content replaces a comment-bearing doc, table cells single-child (editor/markdown/index.mdx "Limitations").
- The complete worked mark example (tokenizer, parse, render for `==highlight==`) is a template for any wb mark that DOES get Markdown syntax (editor/markdown/guides/create-a-highlight-mark.mdx).

### Editor core and extension machinery

- Schema strictness with silent stripping, `enableContentCheck`, `emitContentError`, `contentError` payload including `disableCollaboration` (editor/core-concepts/schema.mdx).
- Event list: `beforeCreate`, `create`, `update`, `selectionUpdate`, `transaction`, `focus`, `blur`, `destroy`, `paste`, `drop`, `delete`, `contentError`, registerable via options, `editor.on`, or extension hooks (editor/api/events.mdx). `delete` is new in v3 (resources/whats-new.mdx).
- Extension `priority` default 100, higher loads earlier, controls plugin order, keymap precedence, and mark `renderHTML` nesting (editor/extensions/custom-extensions/create-new/extension.mdx "priority").
- Extension-level `dispatchTransaction` middleware chain, plus the `enableExtensionDispatchTransaction` kill switch, default `true` (create-new/extension.mdx, editor/api/editor.mdx).
- `editor.commands.setMeta(key, value)` stores transaction metadata, including the documented `preventUpdate` meta (editor/api/commands/set-meta.mdx). Provenance stamps ride this.
- `addGlobalAttributes` targets `['heading', 'paragraph']`, `'*'`, `'nodes'`, or `'marks'` wholesale (create-new/extension.mdx), one mechanism to add a provenance attribute across all block types without touching each extension.
- Input rules come from core extensions ("Tiptap uses input rules under the hood to provide many of its default shortcuts"), built with `markInputRule`/`nodeInputRule`, individually undoable (`undoable` option, `undoInputRule` command), and disableable globally or per-extension via `enableInputRules`/`enablePasteRules` (editor/api/input-rules.mdx, editor/api/editor.mdx, editor/api/commands/nodes-and-marks/undo-input-rule.mdx).
- `@tiptap/pm` re-exports pinned ProseMirror packages including `@tiptap/pm/changeset`, so the fallback engine primitive `prosemirror-changeset` is already in the tree version-matched. `prosemirror-collab` is NOT re-exported, consistent with its rejection (editor/core-concepts/prosemirror.mdx).
- Node views: `ReactNodeViewRenderer`, `NodeViewWrapper`/`NodeViewContent`, props (`editor`, `node`, `decorations`, `selected`, `extension`, `getPos`, `updateAttributes`, `deleteNode`), `getPos()` can return `undefined` in v3, `draggable: true` plus `data-drag-handle`, `contentDOMElementTag`/`as` options, `selectedOnTextSelection`, `trackNodeViewPosition` (re-renders per position shift, explicit perf warning) (editor/extensions/custom-extensions/node-views/react.mdx, resources/whats-new.mdx). The editor never exports rendered node-view DOM, `renderHTML` alone defines output (node-views/index.mdx "Markup").
- Atom nodes copy as empty unless `renderText` is defined, the documented Mention pattern (editor/core-concepts/schema.mdx "Atom").
- Inline node selection quirk at line edges has a documented CSS zero-width-space fix (schema.mdx "Inline").
- Static renderer `@tiptap/static-renderer` renders PM JSON to HTML, Markdown, or React elements with no editor instance, useful for read-only proposal previews in the rail. Its Markdown mode is a SECOND serializer, mapping-based, with an explicit "does not validate the markdown output" caveat, banned for projection under I14 (editor/api/utilities/static-renderer.mdx, guides/output-json-html.mdx "Markdown").
- v3 renames and moves: History is `UndoRedo`, CollaborationCursor is `CollaborationCaret`, utility extensions (Placeholder, CharacterCount, Focus, Selection, TrailingNode, Dropcursor, Gapcursor, UndoRedo) consolidated in `@tiptap/extensions` (resources/whats-new.mdx, guides/upgrade-tiptap-v2.mdx).
- StarterKit now bundles Link, ListKeymap, and Underline by default, so the schema baseline inherits them unless disabled (guides/upgrade-tiptap-v2.mdx "StarterKit Updates").
- Menus position via floating-ui, installed as `@floating-ui/dom`, an explicit dependency, replacing tippy.js (resources/whats-new.mdx, guides/upgrade-tiptap-v2.mdx "Migration Steps").
- v3 additions relevant later: node/mark attribute validation, `unmount()` as a reusable alternative to `destroy()`, framework-agnostic JSX for `renderHTML`, HTML parsing via `happy-dom-without-node` (resources/whats-new.mdx, editor/api/utilities/jsx.mdx).
- `editor.getCharacterCount()` is removed in v3, word and character counts read from `editor.storage.characterCount.characters()` with the CharacterCount extension active (resources/whats-new.mdx "Breaking Changes"), the API our status-bar count uses.
- Keyboard shortcuts: a predefined set is documented, and any extension can overwrite or add bindings via `addKeyboardShortcuts()` using `Mod-` syntax (editor/core-concepts/keyboard-shortcuts.mdx). The PRD's j/k walkthrough and configurable inverted binding are ordinary extension keymaps, with `priority` deciding precedence (create-new/extension.mdx "priority").
- Forced document structure via a custom Document extension (`content: 'heading block+'` style) is a documented pattern, relevant later for class-1 generated docs with locked layouts, with a named TrailingNode interaction caveat (examples/advanced/forced-content-structure.mdx, editor/core-concepts/schema.mdx "Content").
- `parseOptions: { preserveWhitespace: 'full' }` is the documented knob for whitespace handling when content is parsed in, one input to the A10 fidelity rules (editor/api/editor.mdx "parseOptions").
- `injectNonce` supports CSP-nonce'd style injection, and `injectCSS: false` disables Tiptap's injected styles entirely, both relevant to dashboard CSP posture and the Theme Contract (editor/api/editor.mdx).
- A declarative `<Tiptap>` composable React API exists as an alternative integration style (guides/react-composable-api.mdx, referenced from editor/getting-started/install/react.mdx). Our imperative `useEditor` plan stays valid, this is an option, not a migration pressure.
- `@tiptap/suggestion` details for the slash trigger: `char`, `allow`, `allowSpaces` (incompatible with `allowToIncludeChar`), `allowedPrefixes`, a documented dismissed-state model with `shouldResetDismissed`, async item loading, and the collab guard from B above (editor/api/utilities/suggestion.mdx).
- Accessibility baseline for I18: editor `role="textbox"`, `role="toolbar"`, menu roles, no-keyboard-trap, WCAG 2.1 mapping (guides/accessibility.mdx).
- A 200k-word official demo is the performance reference (examples/basics/long-texts.mdx), making the PRD's 50k-word targets conservative.
- An official Tiptap agent skill for AI coding agents exists (`npx skills add ueberdosis/tiptap`, SKILL.md in the main repo), worth installing for the S-wave dev sessions (resources/agent-skill.mdx).

### Collaboration and Yjs (free layer)

- v1's provider-less shape is documented practice: `Collaboration.configure({ document: ydoc })` with a local persistence adapter and no network provider (guides/offline-support.mdx shows exactly this with y-indexeddb).
- v3 pairs `@tiptap/extension-collaboration` with `@tiptap/y-tiptap` (editor/extensions/functionality/collaboration.mdx "Install"), confirming the architecture table's binding.
- One history stack is doc-law: "The Collaboration extension comes with its own history extension. Make sure to disable the UndoRedo extension" (collaboration.mdx "Commands").
- `field` default `'default'`, raw `fragment` override (collaboration.mdx "Settings").
- Y.Doc binary is the only merge-capable persistence: "A simple JSON document is not enough to merge changes" (guides/output-json-html.mdx "Option 3: Y.js"), and "Do not be tempted to store the Y.Doc as JSON and recreate it as YJS binary... content will duplicate on new connections. The data must be stored as binary" (hocuspocus/guides/persistence.mdx FAQ). I2's Y.Doc-primary decision now has two direct citations.
- `isChangeOrigin(transaction)` from `@tiptap/extension-collaboration` identifies remote-synced transactions, used by UniqueID's `filterTransaction` and by suggestion-utility `shouldShow` guards so remote typing never opens local popups (editor/extensions/functionality/uniqueid.mdx, editor/api/utilities/suggestion.mdx).
- CollaborationCaret is free public npm but requires a provider, so presence is structurally collab-phase (editor/extensions/functionality/collaboration-caret.mdx).

### Hocuspocus (ratified collab-phase backend, MIT)

- Server hooks for our integration points: `onAuthenticate` (throw to reject), `onLoadDocument`, `onChange` (can fire multiple times per second, debounce recommended), `onStoreDocument` (debounced via `debounce`/`maxDebounce`, v4 fires for ALL change sources, retries on throw to avoid data loss, `Server.destroy()` flushes pending stores), `onTokenSync`, awareness hooks (hocuspocus/server/hooks.mdx).
- Escape hatches: `skipStoreHooks` on a local transaction origin, `SkipFurtherHooksError` from `@hocuspocus/common` (hooks.mdx "onStoreDocument").
- `@hocuspocus/extension-sqlite` uses `better-sqlite3` since v4, database files compatible across the migration (hocuspocus/server/extensions/sqlite.mdx). The generic `@hocuspocus/extension-database` is a `fetch`/`store` promise pair over `Uint8Array` with a warning to return the same bytes saved (extensions/database.mdx).
- Provider: `flushDelay` batches outgoing updates with `Y.mergeUpdates` in fixed windows, `sessionAwareness` requires a v4 server, `messageReconnectTimeout` default 30000 (hocuspocus/provider/configuration.mdx).
- React bindings since v4: `@hocuspocus/provider-react` with `HocuspocusProviderWebsocketComponent`, `HocuspocusRoom`, hooks on `useSyncExternalStore`, explicitly StrictMode-safe (hocuspocus/provider/react.mdx, hocuspocus/guides/collaborative-editing.mdx). Add to the collab-phase component table.
- Migration symmetry: self-hosted versus Cloud is a provider-class swap (`HocuspocusProvider` versus `TiptapCollabProvider`) with no other integration change (collaboration/getting-started/overview.mdx). Our no-cloud posture costs nothing architecturally.

### UniqueID

- Free public npm `@tiptap/extension-unique-id`, package in the open ueberdosis/tiptap repo (editor/extensions/functionality/uniqueid.mdx). The architecture table's "re-verify at spike" on its license can be closed now.
- Settings: `attributeName` (rendered data-prefixed), `types` (default `[]`, `'all'` supported), `generateID` (default uuidv4, receives `{ node, pos }`), `filterTransaction`, `updateDocument` (uniqueid.mdx).
- Claimed to survive split, merge, undo/redo, crop, and paste, with exact duplicate-on-paste semantics unstated, so the spike keeps a paste-duplication case (uniqueid.mdx intro).
- Mount-after-sync ordering rule with the empty-paragraph failure mode named, plus "Avoid reusing corrupted document names during development" (uniqueid.mdx "Usage with Collaboration").

## C. Corrections and nuances to our three design docs

1. architecture.md section 3.1 describes `@tiptap/markdown` as "Markdown input-rule FEEL + parse/serialize projection". The docs scope the extension to parsing and serialization only. The Markdown typing feel comes from input rules the core extensions register themselves ("Tiptap uses input rules under the hood to provide many of its default shortcuts", editor/api/input-rules.mdx). Both halves exist and are free, but the component-table row should split them, and the fidelity suite targets the serializer, not the input rules.
2. architecture.md section 3.1 records "official, shipped v3.7" for `@tiptap/markdown`. The docs state no version anywhere and tag every markdown page beta with an explicit early-release warning (editor/markdown/index.mdx). Keep the version as npm-sourced, and carry the beta warning as the doc-side maturity signal.
3. architecture.md section 3.1 says "React 19 core support verified (v3.28, 2026-07-17)". Not contradicted, but not doc-backed: the corpus's only React 19 statement is the UI Components React-18 warning (ui-components/getting-started/overview.mdx). Treat core-on-React-19 as externally verified plus spike-confirmed (A13), never doc-guaranteed.
4. PRD section 7 "Undo is Yjs undo, spiked against annotation survival" is right, with the sharpening that the Collaboration undo exposes NO documented grouping knobs (`depth`/`newGroupDelay` belong to the PM UndoRedo extension, editor/extensions/functionality/undo-redo.mdx), so "scope undo semantics explicitly" may trigger even without breakage (A14).
5. old-prd-distilled section 5 gotchas upgrade from conversation claims to cited fact: JSON persistence loses merge capability (hocuspocus/guides/persistence.mdx), Collaboration replaces history (collaboration.mdx), UniqueID mount ordering (uniqueid.mdx), Markdown beta dropping comments and limiting table cells (editor/markdown/index.mdx).
6. old-prd-distilled section 5 lists Tiptap events accurately but incompletely for v3: add `beforeCreate`, `create`, `focus`, `blur`, `destroy`, and the structured `delete` payload (editor/api/events.mdx).
7. architecture.md's drag-handle rejection stands on UX grounds (I2), but the fact base moved: an official DragHandle extension now lives in the OPEN repo (editor/extensions/functionality/drag-handle.mdx links `ueberdosis/tiptap/tree/main/packages/extension-drag-handle`). The rejection is taste-motivated, not license-motivated.
8. architecture.md section 2's HTTP snapshot-plus-update-batch transport is consistent with the docs' provider-less pattern (guides/offline-support.mdx), but no doc describes an HTTP transport. That glue is wholly first-party, governed only by the binary-persistence rule and `Y.mergeUpdates` semantics (hocuspocus/guides/persistence.mdx, hocuspocus/provider/configuration.mdx).
9. The distilled layer matrix's "Tiptap Suggestion utility = AI-command-UI helpers (NOT tracked changes)" is confirmed: `@tiptap/suggestion` is free trigger-character plumbing (editor/api/utilities/suggestion.mdx), and the official slash-commands page is an UNPUBLISHED experiment that tells you to copy its source (examples/experiments/slash-commands.mdx). The cmdk plan plus first-party trigger wiring is the doc-consistent route.
10. architecture.md section 3.1 lists `@floating-ui/*` as "1 (transitive), comes with Tiptap v3". The upgrade guide instructs installing `@floating-ui/dom` yourself for bubble and floating menus (guides/upgrade-tiptap-v2.mdx "Migration Steps"). Treat it as a direct pinned dependency in the deviation-1 audit, not a transitive one.

## D. Paid boundary map

Status legend: FREE = public npm, no account. ACCOUNT = Tiptap private registry (`registry.tiptap.dev`) with a personal token, per guides/pro-extensions.mdx. PAID = active subscription required (plan noted). CLOUD = hosted service. I3 forbids everything below the FREE rows at runtime, ACCOUNT included, since the registry token is a supply-chain dependency on Tiptap's private infrastructure.

| Component | Status | Citation |
|---|---|---|
| `@tiptap/core`, `@tiptap/react`, `@tiptap/pm`, `@tiptap/starter-kit` | FREE | editor/getting-started/install/react.mdx |
| `@tiptap/markdown` (our one serializer) | FREE, beta | editor/markdown/getting-started/installation.mdx |
| `@tiptap/extension-collaboration`, `@tiptap/y-tiptap`, `yjs` | FREE | editor/extensions/functionality/collaboration.mdx |
| `@tiptap/extension-collaboration-caret` (needs provider) | FREE | editor/extensions/functionality/collaboration-caret.mdx |
| `@tiptap/extension-unique-id` (+ `generateUniqueIds`) | FREE | editor/extensions/functionality/uniqueid.mdx |
| `@tiptap/extensions` (Placeholder, CharacterCount, Focus, Selection, TrailingNode, UndoRedo, cursors) | FREE | resources/whats-new.mdx |
| `@tiptap/static-renderer` (Markdown mode banned by I14) | FREE | editor/api/utilities/static-renderer.mdx |
| `@tiptap/suggestion` utility | FREE | editor/api/utilities/suggestion.mdx |
| Mathematics (KaTeX peer dep + CSS), Emoji, Details, Mention, TableOfContents, InvisibleCharacters, FileHandler, DragHandle | FREE (open repo) | editor/extensions/nodes/mathematics.mdx and sibling pages' repo links |
| Hocuspocus server, provider, provider-react, sqlite/database/webhook/redis extensions | FREE (MIT) | hocuspocus/getting-started/overview.mdx |
| Official slash-commands and linting examples | FREE source, unpublished experiments, unmaintained | examples/experiments/slash-commands.mdx, linting.mdx |
| UI Components over OSS extensions (incl. Simple Editor template) | FREE (MIT), React 18 targeted today | ui-components/getting-started/overview.mdx, templates/simple-editor.mdx |
| UI Components for cloud features | NOT open source | ui-components/getting-started/overview.mdx |
| Tracked Changes `@tiptap-pro/extension-tracked-changes` | PAID add-on (contact sales), ACCOUNT, alpha | tracked-changes/getting-started/install.mdx |
| Comments (threads, `setThread`/`resolveThread` command family) | PAID Start (REST/webhooks Team), ACCOUNT | comments/getting-started/install.mdx |
| Collaboration Cloud / on-prem (`@tiptap-pro/provider`, `TiptapCollabProvider`, REST, webhooks) | PAID plan, ACCOUNT, CLOUD | collaboration/getting-started/install.mdx |
| Snapshot (version history) | PAID Start, CLOUD | editor/extensions/functionality/snapshot.mdx frontmatter |
| Snapshot Compare | PAID Team, CLOUD | editor/extensions/functionality/snapshot-compare.mdx frontmatter |
| Content AI (`ai/basic`), AI Generation extension | PAID Start, CLOUD | editor/extensions/functionality/ai-generation.mdx frontmatter |
| AI Toolkit (client + server, reviewOptions, diff utility) | ACCOUNT, some features PAID | guides/pro-extensions.mdx, editor/extensions/functionality/ai-toolkit.mdx |
| PasteHandler `@tiptap-pro/extension-paste-handler` | PAID Team, ACCOUNT | editor/extensions/functionality/paste-handler.mdx |
| Pages extension | PAID Team | editor/extensions/functionality/pages.mdx frontmatter |
| Conversion REST APIs (Markdown, DOCX, ODT, PDF import/export) | PAID Start, CLOUD | conversion/import/markdown/rest-api.mdx |
| `@tiptap-pro/extension-export-markdown` (download wrapper, converts LOCALLY via free `@tiptap/markdown`, "no API calls or credentials") | PAID Start, ACCOUNT, unnecessary for us | conversion/export/markdown/editor-extension.mdx |
| Legacy import/export extensions | Deprecated, default endpoint no longer hosted | conversion/legacy/overview.mdx |

Reference-design harvest from the paid dirs (API shapes only, no dependency, per I3): suggestion mark taxonomy `add`/`delete`/`replaceDeletion`/`replaceInsertion`/`markChange` with per-mark user, ISO timestamps, and id, plus `text` versus `fullText` under nested cross-author suggestions (tracked-changes/api-reference/types.mdx). Styling via `data-suggestion-*` attributes on plain spans (api-reference/styling.mdx). Command surface: `enable/disable/toggleTrackedChanges`, `setTrackedChangesUser`, accept/reject by id, range, user, or all, and programmatic `addTrackedInsertion/Deletion/Replacement/Mark` plus `toggleTrackedMark`, each accepting a `reason` that flows to events (api-reference/commands.mdx, editor/extensions/functionality/tracked-changes.mdx). Behavioral rules worth porting: continuous edits merge into single suggestions, deletion merges keep the OLDEST mark's metadata, deleting mixed content removes pending insertions immediately while marking original text deleted, the engine self-ignores its own suggestion mark and comment marks to prevent recursion with an opt-out list for more, and remote Yjs transactions (`y-sync$` meta) are ignored (tracked-changes/usage/advanced-usage.mdx). Event model worth porting: a creation event that also fires on undo/collaboration restore, `suggestionChanged` as the single source of truth for syncing external state, and a positions-only moved event (api-reference/events.mdx). These are the behaviors the `WbTrackedChangesAdapter` bake-off scores candidates against.

From the paid Comments product, the thread lifecycle verbs are the reference shape for our flag-proposal margin cards: `setThread`, `removeThread(id, deleteThread)` where the flag distinguishes removing the thread REFERENCE from removing its mark from the Yjs document, `updateThread` for properties like seen status, `selectThread`/`unselectThread` for focus routing, and `resolveThread`/`unresolveThread` (comments/integrate/editor-commands.mdx). The split between in-document anchoring marks and externally stored thread data mirrors our I12 ledger-canonical rule exactly, with the ledger playing the role their cloud plays.

---

*Provenance: agent-authored audit, 2026-07-17, from a full read of the vendored tiptap-docs corpus at commit 5c80cd8. Zero em-dashes and zero prose semicolons by rule. Companion to PRD.md section 10 (S0) and architecture.md sections 3 and 4.*
