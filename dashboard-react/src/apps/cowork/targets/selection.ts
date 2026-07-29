import type { Editor, JSONContent } from "@tiptap/core";
import type { Node } from "@tiptap/pm/model";
import type * as Y from "yjs";

import { quoteAnchorFromRange } from "../feedback/feedbackAnchor";
import { createCoworkMarkdownManager } from "../editor/extensions";
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

export interface CoworkIndexedRange extends CoworkProseMirrorRange {
  readonly startBlockIndex: number;
  readonly endBlockIndex: number;
  readonly granularity: "character" | "block";
}

export interface CoworkBlockRange extends CoworkIndexedRange {
  readonly granularity: "block";
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
 * Expand a non-empty range to complete top-level blocks. Section targets and
 * stored v1 references without an explicit granularity use this behavior.
 */
export const blockAlignCoworkRange = (
  doc: Node,
  range: CoworkProseMirrorRange,
): CoworkBlockRange | null => {
  if (
    range.from < 0 ||
    range.to > doc.content.size ||
    range.to <= range.from
  ) {
    return null;
  }
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
    granularity: "block",
  };
};

/**
 * Preserve exact ProseMirror character endpoints while recording only the
 * containing top-level blocks needed for labels, repair hints, and Markdown
 * projection. This helper never widens the supplied range.
 */
export const coworkExactRange = (
  doc: Node,
  range: CoworkProseMirrorRange,
): CoworkIndexedRange | null => {
  if (
    range.from < 0 ||
    range.to > doc.content.size ||
    range.to <= range.from
  ) {
    return null;
  }
  const blocks = blocksIn(doc);
  const selected = blocks.filter(
    (block) => range.from < block.to && range.to > block.from,
  );
  const first = selected[0];
  const last = selected[selected.length - 1];
  if (first === undefined || last === undefined) return null;
  return {
    from: range.from,
    to: range.to,
    startBlockIndex: first.index,
    endBlockIndex: last.index + 1,
    granularity: "character",
  };
};

export const coworkSelectionRange = (
  editor: Editor,
): CoworkIndexedRange | null => {
  const { from, to, empty } = editor.state.selection;
  if (empty) return null;
  return coworkExactRange(editor.state.doc, { from, to });
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
    granularity: "block",
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
      granularity: "block",
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
    granularity: "block",
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
  range: CoworkIndexedRange,
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
  range: CoworkIndexedRange,
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
  range: CoworkIndexedRange,
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

const markerPair = (
  body: string,
): { readonly start: string; readonly end: string } => {
  const stem = "WBCOWORKEXACTTARGETBOUNDARY";
  let attempt = 0;
  for (;;) {
    const suffix = attempt.toString(36).toUpperCase();
    const start = `${stem}START${suffix}`;
    const end = `${stem}END${suffix}`;
    if (!body.includes(start) && !body.includes(end)) return { start, end };
    attempt += 1;
  }
};

const exactProjectionOffsets = (
  editor: Editor,
  range: CoworkIndexedRange,
  body: string,
  newlineStyle: Parameters<typeof normalizeCoworkMarkdownNewlines>[1],
): { readonly start: number; readonly end: number } => {
  const markers = markerPair(body);
  const startPosition = editor.state.doc.resolve(range.from);
  const endPosition = editor.state.doc.resolve(range.to);
  const startMarks =
    startPosition.nodeAfter?.isText === true
      ? startPosition.nodeAfter.marks
      : startPosition.marks();
  const endMarks =
    endPosition.nodeBefore?.isText === true
      ? endPosition.nodeBefore.marks
      : endPosition.marks();
  let transaction = editor.state.tr.insert(
    range.to,
    editor.state.schema.text(markers.end, endMarks),
  );
  transaction = transaction.insert(
    range.from,
    editor.state.schema.text(markers.start, startMarks),
  );
  const marked = normalizeCoworkMarkdownNewlines(
    createCoworkMarkdownManager().serialize(transaction.doc.toJSON()),
    newlineStyle,
  );
  const start = marked.indexOf(markers.start);
  const end = marked.indexOf(markers.end);
  const markersAreUnique =
    start >= 0 &&
    end > start &&
    marked.indexOf(markers.start, start + markers.start.length) < 0 &&
    marked.indexOf(markers.end, end + markers.end.length) < 0;
  if (!markersAreUnique) {
    throw new Error(
      "Co-work could not map these exact character boundaries into Markdown",
    );
  }

  const withoutMarkers =
    marked.slice(0, start) +
    marked.slice(start + markers.start.length, end) +
    marked.slice(end + markers.end.length);
  if (withoutMarkers !== body) {
    throw new Error(
      "Co-work could not verify these exact character boundaries without changing Markdown",
    );
  }
  return {
    start,
    end: end - markers.start.length,
  };
};

const blockProjectionOffsets = (
  editor: Editor,
  range: CoworkIndexedRange,
  body: string,
  newlineStyle: Parameters<typeof normalizeCoworkMarkdownNewlines>[1],
): { readonly start: number; readonly end: number } => {
  const content = editor.getJSON().content ?? [];
  const manager = createCoworkMarkdownManager();
  const prefix = normalizeCoworkMarkdownNewlines(
    manager.serialize(asDoc(content.slice(0, range.startBlockIndex))),
    newlineStyle,
  );
  const selected = normalizeCoworkMarkdownNewlines(
    manager.serialize(
      asDoc(content.slice(range.startBlockIndex, range.endBlockIndex)),
    ),
    newlineStyle,
  );
  if (selected.length === 0) {
    throw new Error("The selected document target has no Markdown representation");
  }
  if (!body.startsWith(prefix)) {
    throw new Error(
      "Co-work could not verify the Markdown prefix for this document target",
    );
  }

  // Top-level renderers may place blank-line separators between the prefix and
  // selected slice. Search forward from the exact rendered-prefix boundary and
  // reject any non-whitespace gap instead of guessing at another occurrence.
  const start = body.indexOf(selected, prefix.length);
  if (start < 0) {
    throw new Error(
      "Co-work could not map this document target into the Markdown projection",
    );
  }
  if (/\S/u.test(body.slice(prefix.length, start))) {
    throw new Error(
      "Co-work found an ambiguous Markdown boundary for this document target",
    );
  }
  return { start, end: start + selected.length };
};

/**
 * Translate one ProseMirror range into Unicode code-point offsets in the exact
 * canonical Markdown projection. Character ranges are mapped by inserting
 * sentinel text into an undispatched transaction, serializing that detached
 * document, and proving that removing the sentinels reproduces the canonical
 * body byte-for-byte. A serializer shape that cannot preserve both endpoints
 * fails closed instead of widening to blocks.
 */
export const coworkProjectionTarget = (
  editor: Editor,
  document: Y.Doc,
  range: CoworkIndexedRange | null,
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

  const offsets =
    range.granularity === "character"
      ? exactProjectionOffsets(
          editor,
          range,
          projection.body,
          projection.fidelity.newlineStyle,
        )
      : blockProjectionOffsets(
          editor,
          range,
          projection.body,
          projection.fidelity.newlineStyle,
        );
  if (offsets.end <= offsets.start) {
    throw new Error("The selected document target has no Markdown representation");
  }
  const startUtf16 = projection.bodyStart + offsets.start;
  const endUtf16 = projection.bodyStart + offsets.end;
  if (
    projection.markdown.slice(startUtf16, endUtf16) !==
    projection.body.slice(offsets.start, offsets.end)
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
