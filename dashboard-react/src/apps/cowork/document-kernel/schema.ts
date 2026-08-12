import type { AnyExtension } from "@tiptap/core";
import StarterKit from "@tiptap/starter-kit";
import { TableKit } from "@tiptap/extension-table";
import { TaskItem, TaskList } from "@tiptap/extension-list";
import Image from "@tiptap/extension-image";
import { UniqueID } from "@tiptap/extension-unique-id";

import { WbExpressionMark, WbProvenanceTint } from "../editor/marks";

/** Versioned structured schema shared by browser and headless runtimes. */
export const COWORK_DOCUMENT_SCHEMA = "cowork-yjs/v1";
export const COWORK_FRAGMENT_FIELD = "default";

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

export const COWORK_LINK_OPTIONS = {
  autolink: false,
  openOnClick: false,
  linkOnPaste: false,
  defaultProtocol: "https",
  protocols: ["http", "https", "mailto", "wb-truth"],
};

/**
 * Canonical, DOM-free schema bundle. UI plugins, menus, decorations, node
 * views, and browser-only commands deliberately do not belong here.
 *
 * UniqueID is included because it contributes the canonical block `id`
 * attribute. Its ProseMirror plugin is inert in MarkdownManager/getSchema;
 * the browser editor activates the same extension to mint IDs for new blocks.
 */
export const buildDocumentSchemaExtensions = (): AnyExtension[] => [
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
  UniqueID.configure({ types: [...COWORK_UNIQUE_ID_TYPES] }),
];
