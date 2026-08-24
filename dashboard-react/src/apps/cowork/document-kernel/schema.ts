import { Extension, type AnyExtension } from "@tiptap/core";
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

const COWORK_LOCAL_FILE_URI =
  /^wb-local-file:([A-Za-z0-9][A-Za-z0-9_-]{15,127})$/;
const COWORK_TRUTH_URI =
  /^wb-truth:\/\/[A-Za-z0-9._~-]+\/[A-Za-z0-9._~!$&'()*+,;=:@/-]+$/;
const EXPLICIT_URI_SCHEME = /^([A-Za-z][A-Za-z0-9+.-]*):/;

/**
 * Tiptap's `protocols` option extends its built-in protocol set; it is not a
 * security allowlist by itself. Keep the stored Link mark strict as well as
 * keeping activation disabled. A local-file URI contains one opaque ID only:
 * paths, query strings, fragments, authority components, and percent escapes
 * are never admitted.
 */
export const isAllowedCoworkLinkUri = (value: string): boolean => {
  if (
    typeof value !== "string" ||
    value.length === 0 ||
    value !== value.trim() ||
    /[\u0000-\u001f\u007f]/.test(value)
  ) {
    return false;
  }
  if (COWORK_LOCAL_FILE_URI.test(value) || COWORK_TRUTH_URI.test(value)) {
    return true;
  }
  const scheme = EXPLICIT_URI_SCHEME.exec(value)?.[1]?.toLowerCase();
  if (scheme === "http" || scheme === "https") {
    try {
      const parsed = new URL(value);
      return parsed.protocol === `${scheme}:` && parsed.hostname.length > 0;
    } catch {
      return false;
    }
  }
  if (scheme === "mailto") {
    return /^mailto:[^\s@]+@[^\s@]+$/i.test(value);
  }
  if (scheme !== undefined) return false;
  // Existing Markdown may contain relative or fragment links. They remain
  // inert (`openOnClick: false`) and must resolve through normal same-origin
  // application navigation if a future explicit handler admits them.
  return !value.startsWith("//") && !value.includes("\\");
};

export const parseCoworkLocalFileHref = (value: string): string | null =>
  COWORK_LOCAL_FILE_URI.exec(value)?.[1] ?? null;

/**
 * The stock Link mark validates commands and DOM parsing, but its Markdown
 * parser applies every token without consulting `isAllowedUri`. This
 * higher-priority Markdown-only handler preserves unsafe link text as plain
 * content, so an imported `file:`/device/path link never enters Yjs as a Link
 * mark. The normal Link extension remains the schema/render authority.
 */
export const CoworkSafeLinkMarkdown = Extension.create({
  name: "coworkSafeLinkMarkdown",
  priority: 1_100,
  markdownTokenName: "link",
  parseMarkdown: (token, helpers) => {
    const content = helpers.parseInline(token.tokens ?? []);
    return isAllowedCoworkLinkUri(token.href)
      ? helpers.applyMark("link", content, {
          href: token.href,
          title: token.title || null,
        })
      : content;
  },
});

export const COWORK_LINK_OPTIONS = {
  autolink: false,
  openOnClick: false,
  linkOnPaste: false,
  defaultProtocol: "https",
  protocols: ["http", "https", "mailto", "wb-truth", "wb-local-file"],
  isAllowedUri: isAllowedCoworkLinkUri,
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
  CoworkSafeLinkMarkdown,
  UniqueID.configure({ types: [...COWORK_UNIQUE_ID_TYPES] }),
];
