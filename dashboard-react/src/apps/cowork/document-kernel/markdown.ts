import { getSchema, type JSONContent } from "@tiptap/core";
import { MarkdownManager } from "@tiptap/markdown";
import type { Node as ProseMirrorNode, Schema } from "@tiptap/pm/model";
import * as Y from "yjs";
import {
  initProseMirrorDoc,
  prosemirrorJSONToYDoc,
  updateYFragment,
} from "@tiptap/y-tiptap";

import { reattachFrontmatter, splitFrontmatter } from "../editor/frontmatter";
import {
  buildDocumentSchemaExtensions,
  COWORK_FRAGMENT_FIELD,
  COWORK_UNIQUE_ID_TYPES,
} from "./schema";

export type CoworkKernelNewlineStyle = "crlf" | "lf" | "cr" | "none";

export interface CoworkKernelFidelity {
  readonly newlineStyle: CoworkKernelNewlineStyle;
  readonly utf8Bom: boolean;
  readonly trailingNewlineCount?: number;
  readonly frontmatter: string | null;
}

export const createDocumentMarkdownManager = (): MarkdownManager =>
  new MarkdownManager({ extensions: buildDocumentSchemaExtensions() });

export const createDocumentSchema = (): Schema =>
  getSchema(buildDocumentSchemaExtensions());

export const normalizeDocumentNewlines = (
  markdown: string,
  newlineStyle: CoworkKernelNewlineStyle,
): string => {
  if (newlineStyle === "crlf") return markdown.replace(/\r\n|\r|\n/g, "\r\n");
  if (newlineStyle === "cr") return markdown.replace(/\r\n|\r|\n/g, "\r");
  if (newlineStyle === "lf") return markdown.replace(/\r\n|\r/g, "\n");
  return markdown;
};

export const restoreDocumentFidelity = (
  markdown: string,
  fidelity: Omit<CoworkKernelFidelity, "frontmatter">,
): string => {
  let restored = normalizeDocumentNewlines(markdown, fidelity.newlineStyle);
  if (fidelity.trailingNewlineCount !== undefined) {
    restored = restored.replace(/(?:\r\n|\r|\n)+$/g, "");
    const newline =
      fidelity.newlineStyle === "crlf"
        ? "\r\n"
        : fidelity.newlineStyle === "cr"
          ? "\r"
          : "\n";
    restored += newline.repeat(Math.max(0, fidelity.trailingNewlineCount));
  }
  if (fidelity.utf8Bom && !restored.startsWith("\ufeff")) restored = `\ufeff${restored}`;
  return restored;
};

export const fidelityFromYdoc = (document: Y.Doc): CoworkKernelFidelity => {
  const stored = document.getMap<unknown>("wb-cowork:fidelity");
  const lineEnding = stored.get("newline_style");
  const frontmatter = stored.get("frontmatter");
  return {
    newlineStyle:
      lineEnding === "crlf" || lineEnding === "cr" || lineEnding === "none"
        ? lineEnding
        : "lf",
    utf8Bom: stored.get("utf8_bom") === true,
    trailingNewlineCount:
      typeof stored.get("trailing_newline_count") === "number"
        ? Number(stored.get("trailing_newline_count"))
        : undefined,
    frontmatter: typeof frontmatter === "string" ? frontmatter : null,
  };
};

export const writeFidelityToYdoc = (
  document: Y.Doc,
  fidelity: CoworkKernelFidelity,
  metadata: Readonly<Record<string, unknown>> = {},
): void => {
  const stored = document.getMap<unknown>("wb-cowork:fidelity");
  stored.set("schema", "cowork-fidelity/v1");
  stored.set("newline_style", fidelity.newlineStyle);
  stored.set("utf8_bom", fidelity.utf8Bom);
  if (fidelity.trailingNewlineCount !== undefined) {
    stored.set("trailing_newline_count", fidelity.trailingNewlineCount);
  }
  stored.set("frontmatter", fidelity.frontmatter);
  for (const [key, value] of Object.entries(metadata)) stored.set(key, value);
};

export const serializeDocumentJson = (
  value: JSONContent,
  fidelity: CoworkKernelFidelity,
  manager: MarkdownManager = createDocumentMarkdownManager(),
): string => {
  const rawBody = manager.serialize(value);
  const frontmatter =
    fidelity.frontmatter === null
      ? null
      : normalizeDocumentNewlines(fidelity.frontmatter, fidelity.newlineStyle);
  const body = normalizeDocumentNewlines(rawBody, fidelity.newlineStyle);
  return restoreDocumentFidelity(reattachFrontmatter(frontmatter, body), fidelity);
};

export const projectYdocMarkdown = (
  document: Y.Doc,
  manager: MarkdownManager = createDocumentMarkdownManager(),
): string => {
  const schema = createDocumentSchema();
  const { doc } = initProseMirrorDoc(
    document.getXmlFragment(COWORK_FRAGMENT_FIELD),
    schema,
  );
  return serializeDocumentJson(doc.toJSON(), fidelityFromYdoc(document), manager);
};

const blockTypes = new Set<string>(COWORK_UNIQUE_ID_TYPES);

/** Fill canonical block IDs before content enters Yjs. */
export const assignDocumentBlockIds = (
  value: JSONContent,
  idForPath: (path: readonly number[], node: JSONContent) => string,
  path: readonly number[] = [],
): JSONContent => {
  const content = value.content?.map((child, index) =>
    assignDocumentBlockIds(child, idForPath, [...path, index]),
  );
  if (!value.type || !blockTypes.has(value.type)) {
    return { ...value, ...(content === undefined ? {} : { content }) };
  }
  return {
    ...value,
    attrs: {
      ...(value.attrs ?? {}),
      id:
        typeof value.attrs?.["id"] === "string" && value.attrs["id"].length > 0
          ? value.attrs["id"]
          : idForPath(path, value),
    },
    ...(content === undefined ? {} : { content }),
  };
};

export const bootstrapMarkdownYdoc = (
  markdown: string,
  fidelity: Omit<CoworkKernelFidelity, "frontmatter">,
  idForPath: (path: readonly number[], node: JSONContent) => string,
  metadata: Readonly<Record<string, unknown>> = {},
): Y.Doc => {
  const manager = createDocumentMarkdownManager();
  const split = splitFrontmatter(markdown);
  const parsed = manager.parse(split.body);
  const identified = assignDocumentBlockIds(parsed, idForPath);
  const document = prosemirrorJSONToYDoc(
    createDocumentSchema(),
    identified,
    COWORK_FRAGMENT_FIELD,
  );
  writeFidelityToYdoc(
    document,
    { ...fidelity, frontmatter: split.frontmatter },
    metadata,
  );
  return document;
};

/** Apply one ProseMirror result through the Yjs binding and return its update. */
export const updateYdocFromProseMirror = (
  document: Y.Doc,
  result: ProseMirrorNode,
): Uint8Array => {
  const stateVector = Y.encodeStateVector(document);
  const fragment = document.getXmlFragment(COWORK_FRAGMENT_FIELD);
  const initialized = initProseMirrorDoc(fragment, result.type.schema);
  updateYFragment(document, fragment, result, initialized.meta);
  return Y.encodeStateAsUpdate(document, stateVector);
};

export const importDocumentMarkdown = (
  source: string,
  manager: MarkdownManager = createDocumentMarkdownManager(),
): { readonly doc: JSONContent; readonly frontmatter: string | null } => {
  const { frontmatter, body } = splitFrontmatter(source);
  return { doc: manager.parse(body), frontmatter };
};
