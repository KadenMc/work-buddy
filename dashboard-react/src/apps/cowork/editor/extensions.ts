import type { AnyExtension, Editor } from "@tiptap/core";
import StarterKit from "@tiptap/starter-kit";
import { TableKit } from "@tiptap/extension-table";
import { TaskItem, TaskList } from "@tiptap/extension-list";
import Image from "@tiptap/extension-image";
import { Collaboration, isChangeOrigin } from "@tiptap/extension-collaboration";
import { UniqueID } from "@tiptap/extension-unique-id";
import { Markdown, MarkdownManager } from "@tiptap/markdown";
import { yUndoPluginKey } from "@tiptap/y-tiptap";
import type * as Y from "yjs";

import { WbExpressionMark, WbProvenanceTint } from "./marks";
import {
  CoworkCodeBlock,
  CoworkHorizontalRule,
  CoworkImage,
  buildSuggestionExtensions,
} from "../suggestions";

/**
 * The Collaboration fragment field is fixed to `default` in the document-surface
 * profile (SP-2 load-order point 5, audit A9). Every seed-on-empty check and every
 * fragment read standardizes on this one field name.
 */
export const COWORK_FRAGMENT_FIELD = "default";

/**
 * UniqueID block allowlist (section 6). Explicitly a block-level set, NOT bare `'all'`,
 * because `'all'` also ids `hardBreak`, an inline atom, and pulls it out of its own
 * anchor namespace (SP-2 F2.2). Inline atoms keep node identity separate from block
 * identity so `node_id` and `span_id` remain distinct namespaces (point 8).
 */
export const COWORK_UNIQUE_ID_TYPES = [
  "paragraph",
  "heading",
  "blockquote",
  "codeBlock",
  "listItem",
  "bulletList",
  "orderedList",
  "horizontalRule",
] as const;

/**
 * Link posture (section 6). Restricted so Markdown projection fidelity survives and
 * paste cannot smuggle a link: autolink is off (GFM autolink rewrites bare URLs, SP-3),
 * click-to-open is off inside the editor, paste does not auto-link, and the protocol
 * allowlist is exactly http / https / mailto / wb-truth.
 */
const COWORK_LINK_OPTIONS = {
  autolink: false,
  openOnClick: false,
  linkOnPaste: false,
  defaultProtocol: "https",
  protocols: ["http", "https", "mailto", "wb-truth"],
};

/**
 * The schema nodes and marks shared by the editor and the standalone MarkdownManager.
 * StarterKit ships with `undoRedo: false` because Collaboration owns history (SP-2
 * point 7), and its bundled Link is configured with the restricted posture above rather
 * than added as a second Link extension. Table, TaskList / TaskItem, and Image are the
 * fidelity minimum (SP-3 finding 3, gate condition 8): the corpus contains all three
 * and the suite fails if a construct lacks a schema node. The two wb marks carry the
 * paste-forgery-proof `parseHTML: () => []` posture.
 *
 * Collaboration and UniqueID are intentionally absent here: they are editor-runtime
 * concerns, not Markdown-schema concerns, and the standalone manager must stay DOM-free
 * (SP-3 case 5).
 */
export const buildSchemaExtensions = (): AnyExtension[] => [
  StarterKit.configure({
    undoRedo: false,
    link: COWORK_LINK_OPTIONS,
  }),
  TableKit,
  TaskList,
  TaskItem,
  Image,
  WbProvenanceTint,
  WbExpressionMark,
];

/**
 * The full editor extension set. Unlike the DOM-free MarkdownManager schema, the editor
 * admits the tracked-change layer, so it swaps StarterKit's code_block and horizontal_rule
 * for the cowork variants that carry suggestion marks and atom-suggestion attrs
 * (CoworkCodeBlock, CoworkHorizontalRule, CoworkImage, SP-1 fork deltas 4 and 5), and
 * spreads the three suggestion marks plus the decoration plugin (buildSuggestionExtensions).
 * The suggestion layer is display-only and never serialized, so it is added here and never
 * to the MarkdownManager (surface build note). On top it binds the single Collaboration
 * binding to the passed local Y.Doc on the `default` fragment (replacing history), the
 * Markdown storage extension (the one Markdown parse/serialize integration point), and
 * UniqueID with the block allowlist and `filterTransaction: (tr) => !isChangeOrigin(tr)` so
 * it never re-mints ids on an applied (apply-origin or foreign) transaction (SP-2 point 3).
 *
 * `updateDocument` is left at its default because the Co-work editor is read-write. The
 * `updateDocument: false` rule in the load-order contract applies to read-only surfaces
 * only.
 */
export const buildEditorExtensions = (document: Y.Doc): AnyExtension[] => [
  StarterKit.configure({
    undoRedo: false,
    link: COWORK_LINK_OPTIONS,
    codeBlock: false,
    horizontalRule: false,
  }),
  TableKit,
  TaskList,
  TaskItem,
  CoworkCodeBlock,
  CoworkHorizontalRule,
  CoworkImage,
  WbProvenanceTint,
  WbExpressionMark,
  ...buildSuggestionExtensions(),
  Markdown,
  Collaboration.configure({
    document,
    field: COWORK_FRAGMENT_FIELD,
  }),
  UniqueID.configure({
    types: [...COWORK_UNIQUE_ID_TYPES],
    filterTransaction: (transaction) => !isChangeOrigin(transaction),
  }),
];

/**
 * A standalone, DOM-free MarkdownManager over the shared schema (SP-3 case 5). This is
 * the ONE Markdown parse/serialize path used at the import boundary and by the
 * block-splice materializer, never editor commands (which need a DOM) and never a
 * Python serializer (I14).
 */
export const createCoworkMarkdownManager = (): MarkdownManager =>
  new MarkdownManager({ extensions: buildSchemaExtensions() });

/**
 * True when the Collaboration fragment is empty, so a brand-new document may be seeded
 * exactly once (SP-2 load-order points 2 and 5). Standardized on the `default` fragment.
 */
export const isFragmentEmpty = (document: Y.Doc): boolean =>
  document.getXmlFragment(COWORK_FRAGMENT_FIELD).length === 0;

/**
 * Stop the Collaboration undo manager from capturing, immediately after the load-time id
 * mint, so load-time identity never merges into a user-undo item (SP-2 load-order point
 * 4, F2.7). No-op if the undo plugin is not present on the editor state.
 */
export const stopCapturingLoadTimeIds = (editor: Editor): void => {
  const undoState = yUndoPluginKey.getState(editor.state) as
    | { readonly undoManager?: { stopCapturing?: () => void } }
    | undefined;
  undoState?.undoManager?.stopCapturing?.();
};
