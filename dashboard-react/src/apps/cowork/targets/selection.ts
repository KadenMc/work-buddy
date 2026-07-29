import type { Editor, JSONContent } from "@tiptap/core";
import type { Node } from "@tiptap/pm/model";
import type * as Y from "yjs";

import { quoteAnchorFromRange } from "../feedback/feedbackAnchor";
import {
  createCoworkMarkdownManager,
} from "../editor/extensions";
import {
  normalizeCoworkMarkdownNewlines,
  serializeCoworkEditorMarkdownProjection,
} from "../editor/serializeCoworkMarkdown";
import type {
  CoworkCanonicalTargetSelector,
  CoworkProseMirrorRange,
  CoworkRangeQuote,
  CoworkTargetSummary,
} from "./contracts";

interface TopLevelBlock {
  readonly node: Node;
  readonly index: number;
  readonly from: number;
  readonly to: number;
}

export interface CoworkBlockRange extends CoworkProseMirrorRange {
  readonly startBlockIndex: number;
  readonly endBlockIndex: number;
}

export interface CoworkProjectionTarget {
  readonly selector: CoworkCanonicalTargetSelector;
  readonly markdownText: string;
}

const blocksIn = (doc: Node): readonly TopLevelBlock[] => {
  const blocks: TopLevelBlock[] = [];
  doc.forEach((node, offset, index) => {
    blocks.push({
      node,
      index,
      from: offset,
      to: offset + node.nodeSize,
    });
  });
  return blocks;
};

export const coworkWordCount = (
  doc: Node,
  range: CoworkProseMirrorRange,
): number => {
  const text = doc.textBetween(range.from, range.to, " ", " ").trim();
  return text.length === 0 ? 0 : text.split(/\s+/u).length;
};

/**
 * Expand a non-empty text selection to complete top-level blocks. The initial
 * direct-manipulation target is deliberately contiguous and block-aligned.
 */
export const blockAlignCoworkRange = (
  doc: Node,
  range: CoworkProseMirrorRange,
): CoworkBlockRange | null => {
  if (range.to <= range.from) return null;
  const blocks = blocksIn(doc);
  const selected = blocks.filter(
    (block) => range.from < block.to && range.to > block.from,
  );
  const first = selected[0];
  const last = selected[selected.length - 1];
  if (first === undefined || last === undefined) return null;
  return {
    from: first.from,
    to: last.to,
    startBlockIndex: first.index,
    endBlockIndex: last.index + 1,
  };
};

export const coworkSelectionRange = (
  editor: Editor,
): CoworkBlockRange | null => {
  const { from, to, empty } = editor.state.selection;
  if (empty) return null;
  return blockAlignCoworkRange(editor.state.doc, { from, to });
};

/** The single top-level block containing the caret/selection head. */
export const coworkCurrentBlockRange = (
  editor: Editor,
): CoworkBlockRange | null => {
  const blocks = blocksIn(editor.state.doc);
  if (blocks.length === 0) return null;
  const position = editor.state.selection.head;
  const block =
    blocks.find(
      (candidate) =>
        position >= candidate.from && position <= candidate.to,
    ) ?? blocks[blocks.length - 1];
  if (block === undefined) return null;
  return {
    from: block.from,
    to: block.to,
    startBlockIndex: block.index,
    endBlockIndex: block.index + 1,
  };
};

/**
 * Resolve the heading section containing the caret. A section begins at the
 * closest preceding top-level heading and ends before the next heading of the
 * same or a higher level. Documents without headings expose the current block.
 */
export const coworkCurrentSectionRange = (
  editor: Editor,
): CoworkBlockRange | null => {
  const doc = editor.state.doc;
  const blocks = blocksIn(doc);
  if (blocks.length === 0) return null;
  const cursor = editor.state.selection.head;
  let containing =
    blocks.find((block) => cursor >= block.from && cursor <= block.to) ??
    blocks[blocks.length - 1];
  let heading: TopLevelBlock | undefined;
  for (const block of blocks) {
    if (block.from > cursor) break;
    if (block.node.type.name === "heading") heading = block;
  }
  if (heading === undefined) {
    containing ??= blocks[0];
    return {
      from: containing.from,
      to: containing.to,
      startBlockIndex: containing.index,
      endBlockIndex: containing.index + 1,
    };
  }
  const level = Number(heading.node.attrs.level ?? 1);
  const next = blocks.find(
    (block) =>
      block.index > heading.index &&
      block.node.type.name === "heading" &&
      Number(block.node.attrs.level ?? 1) <= level,
  );
  return {
    from: heading.from,
    to: next?.from ?? doc.content.size,
    startBlockIndex: heading.index,
    endBlockIndex: next?.index ?? blocks.length,
  };
};

export const coworkHeadingPath = (
  doc: Node,
  blockIndex: number,
): readonly string[] => {
  const path: { level: number; label: string }[] = [];
  for (const block of blocksIn(doc)) {
    if (block.index > blockIndex) break;
    if (block.node.type.name !== "heading") continue;
    const level = Number(block.node.attrs.level ?? 1);
    while (path.length > 0 && path[path.length - 1].level >= level) {
      path.pop();
    }
    path.push({
      level,
      label: block.node.textContent.trim() || "Untitled section",
    });
  }
  return path.map((entry) => entry.label);
};

export const coworkRangeQuote = (
  doc: Node,
  range: CoworkProseMirrorRange,
): CoworkRangeQuote | null => quoteAnchorFromRange(doc, range.from, range.to);

const blockId = (block: TopLevelBlock | undefined): string | undefined => {
  const value = block?.node.attrs.id;
  return typeof value === "string" && value.length > 0 ? value : undefined;
};

export const coworkRangeBlockIds = (
  doc: Node,
  range: CoworkBlockRange,
): { readonly startBlockId?: string; readonly endBlockId?: string } => {
  const blocks = blocksIn(doc);
  return {
    ...(blockId(blocks[range.startBlockIndex]) === undefined
      ? {}
      : { startBlockId: blockId(blocks[range.startBlockIndex]) }),
    ...(blockId(blocks[range.endBlockIndex - 1]) === undefined
      ? {}
      : { endBlockId: blockId(blocks[range.endBlockIndex - 1]) }),
  };
};

const labelForRange = (
  doc: Node,
  range: CoworkBlockRange,
  fallback: string,
): string => {
  const first = blocksIn(doc)[range.startBlockIndex];
  if (first?.node.type.name === "heading") {
    return first.node.textContent.trim() || "Untitled section";
  }
  const path = coworkHeadingPath(doc, range.startBlockIndex);
  return path.length > 0 ? path[path.length - 1] : fallback;
};

export const coworkRangeSummary = (
  doc: Node,
  range: CoworkBlockRange,
  fallback: string,
): CoworkTargetSummary => ({
  kind: "text_range",
  label: labelForRange(doc, range, fallback),
  wordCount: coworkWordCount(doc, range),
  range: { from: range.from, to: range.to },
});

export const coworkWholeDocumentSummary = (doc: Node): CoworkTargetSummary => ({
  kind: "document",
  label: "Whole document",
  wordCount: coworkWordCount(doc, { from: 0, to: doc.content.size }),
  range: null,
});

const asDoc = (content: readonly JSONContent[]): JSONContent => ({
  type: "doc",
  content: [...content],
});

const codePointLength = (value: string): number => Array.from(value).length;

const projectionQuote = (
  markdown: string,
  startUtf16: number,
  endUtf16: number,
): CoworkProjectionTarget => {
  const characters = Array.from(markdown);
  const start = codePointLength(markdown.slice(0, startUtf16));
  const end = start + codePointLength(markdown.slice(startUtf16, endUtf16));
  const context = 32;
  return {
    selector: {
      kind: "text_quote",
      exact: characters.slice(start, end).join(""),
      prefix: characters.slice(Math.max(0, start - context), start).join(""),
      suffix: characters.slice(end, Math.min(characters.length, end + context)).join(""),
      start,
      end,
    },
    markdownText: markdown.slice(startUtf16, endUtf16),
  };
};

/**
 * Translate one block-aligned ProseMirror range into Unicode code-point
 * offsets in the exact canonical Markdown projection. Each top-level slice is
 * rendered through the same MarkdownManager, then located after the rendered
 * prefix. This makes the coordinate-system conversion explicit and testable.
 */
export const coworkProjectionTarget = (
  editor: Editor,
  document: Y.Doc,
  range: CoworkBlockRange | null,
): {
  readonly markdown: string;
  readonly target: CoworkProjectionTarget;
} => {
  const projection = serializeCoworkEditorMarkdownProjection(editor, document);
  if (range === null) {
    return {
      markdown: projection.markdown,
      target: {
        selector: { kind: "document" },
        markdownText: projection.markdown,
      },
    };
  }

  const json = editor.getJSON();
  const content = json.content ?? [];
  const manager = createCoworkMarkdownManager();
  const prefix = normalizeCoworkMarkdownNewlines(
    manager.serialize(asDoc(content.slice(0, range.startBlockIndex))),
    projection.fidelity.newlineStyle,
  );
  const selected = normalizeCoworkMarkdownNewlines(
    manager.serialize(
      asDoc(content.slice(range.startBlockIndex, range.endBlockIndex)),
    ),
    projection.fidelity.newlineStyle,
  );
  if (selected.length === 0) {
    throw new Error("The selected document target has no Markdown representation");
  }
  if (!projection.body.startsWith(prefix)) {
    throw new Error(
      "Co-work could not verify the Markdown prefix for this document target",
    );
  }

  // Top-level renderers may place blank-line separators between the prefix and
  // selected slice. Search forward from the exact rendered-prefix boundary and
  // reject any non-whitespace gap instead of guessing at another occurrence.
  const bodyIndex = projection.body.indexOf(selected, prefix.length);
  if (bodyIndex < 0) {
    throw new Error(
      "Co-work could not map this document target into the Markdown projection",
    );
  }
  if (/\S/u.test(projection.body.slice(prefix.length, bodyIndex))) {
    throw new Error(
      "Co-work found an ambiguous Markdown boundary for this document target",
    );
  }
  const startUtf16 = projection.bodyStart + bodyIndex;
  const endUtf16 = startUtf16 + selected.length;
  if (
    projection.markdown.slice(startUtf16, endUtf16) !== selected
  ) {
    throw new Error(
      "Co-work could not verify the Markdown boundary for this document target",
    );
  }
  return {
    markdown: projection.markdown,
    target: projectionQuote(projection.markdown, startUtf16, endUtf16),
  };
};
