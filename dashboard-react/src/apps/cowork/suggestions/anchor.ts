import type { Node } from "@tiptap/pm/model";

import { createCoworkMarkdownManager } from "../editor/extensions";
import type { QuoteAnchor } from "./types";

/**
 * Client-side quote-anchor resolution (I12, C1 surface section 3). The ledger is truth
 * and the marks are a projection, so a proposal locates by quote plus context rather than
 * by a node id (SP-2 point 8, node_id is ephemeral). The kernel anchors.py resolves the
 * same shape server-side, and this is the browser-side realization the adapter uses to
 * project a proposal into the live doc and to re-anchor on drift.
 *
 * The algorithm walks the doc into a flat text index that maps each character to its
 * ProseMirror position, finds every occurrence of the exact quote, and disambiguates by
 * the prefix and suffix context. A quote that occurs once resolves. A quote that occurs
 * several times resolves only when exactly one occurrence matches the surrounding context,
 * otherwise the anchor is reported lost so the proposal expires toward re-review, never
 * acceptance.
 */

interface TextIndex {
  readonly flat: string;
  /** charPositions[k] is the ProseMirror position of the k-th flat character. */
  readonly charPositions: readonly number[];
}

/**
 * Build a flat text index over the doc. Adjacent text nodes inside one block are
 * contiguous, so their characters concatenate directly. A gap between one text run and
 * the next marks a block boundary and inserts a single newline whose position is the
 * boundary, so a multi-block quote can still match on its newline.
 */
export const buildTextIndex = (doc: Node): TextIndex => {
  const segments: { text: string; from: number }[] = [];
  doc.descendants((node, pos) => {
    if (node.isText && typeof node.text === "string") {
      segments.push({ text: node.text, from: pos });
    }
    return true;
  });

  let flat = "";
  const charPositions: number[] = [];
  let prevEnd: number | null = null;
  for (const segment of segments) {
    if (prevEnd !== null && segment.from > prevEnd) {
      flat += "\n";
      charPositions.push(prevEnd);
    }
    for (let i = 0; i < segment.text.length; i++) {
      flat += segment.text[i];
      charPositions.push(segment.from + i);
    }
    prevEnd = segment.from + segment.text.length;
  }

  return { flat, charPositions };
};

interface TextMatch {
  readonly offset: number;
  readonly length: number;
}

/** Every occurrence of `needle` in `haystack` (overlapping matches included). */
const exactOccurrences = (haystack: string, needle: string): TextMatch[] => {
  const found: TextMatch[] = [];
  if (needle.length === 0) return found;
  let from = 0;
  for (;;) {
    const at = haystack.indexOf(needle, from);
    if (at === -1) return found;
    found.push({ offset: at, length: needle.length });
    from = at + 1;
  }
};

const escapeRegExp = (value: string): string =>
  value.replace(/[.*+?^${}()|[\]\\]/gu, "\\$&");

/** Exact-first, then whitespace-tolerant, matching the server anchor firewall. */
const textOccurrences = (haystack: string, needle: string): TextMatch[] => {
  const exact = exactOccurrences(haystack, needle);
  if (exact.length > 0) return exact;
  const parts = needle.split(/\s+/u).filter((part) => part.length > 0);
  if (parts.length === 0) return [];
  const pattern = new RegExp(parts.map(escapeRegExp).join("\\s+"), "gu");
  return Array.from(haystack.matchAll(pattern), (match) => ({
    offset: match.index,
    length: match[0].length,
  }));
};

/**
 * Proposal selectors describe canonical Markdown, while ProseMirror exposes
 * visible text. Parse a Markdown fragment through Co-work's one canonical
 * parser so blockquote prefixes, emphasis delimiters, links, and hard-break
 * syntax become the same visible-text index as the live editor.
 */
const visibleMarkdownText = (doc: Node, markdown: string): string => {
  if (markdown.length === 0) return "";
  try {
    const parsed = createCoworkMarkdownManager().parse(markdown);
    const parsedDoc = doc.type.schema.nodeFromJSON(parsed);
    return buildTextIndex(parsedDoc).flat;
  } catch {
    return markdown;
  }
};

const contextPattern = (context: string): string | null => {
  const parts = context.split(/\s+/u).filter((part) => part.length > 0);
  return parts.length === 0 ? null : parts.map(escapeRegExp).join("\\s+");
};

/**
 * Context is authored against canonical Markdown while `flat` is visible editor text.
 * Match it at the quote boundary with whitespace tolerance, including a trimmed block
 * boundary: fragment parsing legitimately turns `\r\n\r\nThat week` into `That week`,
 * while the live text index still has a newline before the paragraph.
 */
const prefixMatches = (flat: string, idx: number, prefix: string): boolean => {
  const pattern = contextPattern(prefix);
  if (pattern === null) return true;
  return new RegExp(`${pattern}\\s*$`, "u").test(flat.substring(0, idx));
};

const suffixMatches = (flat: string, idx: number, suffix: string): boolean => {
  const pattern = contextPattern(suffix);
  if (pattern === null) return true;
  return new RegExp(`^\\s*${pattern}`, "u").test(flat.substring(idx));
};

const rangeFor = (
  index: TextIndex,
  offset: number,
  length: number,
): { from: number; to: number } => {
  const from = index.charPositions[offset];
  const to = index.charPositions[offset + length - 1] + 1;
  return { from, to };
};

/**
 * Resolve a quote anchor to a ProseMirror (from, to) range, or null when it cannot be
 * located uniquely. A single occurrence resolves directly. Multiple occurrences resolve
 * only when exactly one satisfies both the prefix and suffix context.
 */
export type QuoteAnchorResolution =
  | { readonly state: "unique"; readonly from: number; readonly to: number }
  | { readonly state: "missing" | "ambiguous" };

export const resolveQuoteAnchorDetailed = (
  doc: Node,
  anchor: QuoteAnchor,
): QuoteAnchorResolution => {
  if (anchor.exact.length === 0) return { state: "missing" };

  const index = buildTextIndex(doc);
  let exact = anchor.exact;
  let prefix = anchor.prefix;
  let suffix = anchor.suffix;
  let occurrences = textOccurrences(index.flat, exact);
  if (occurrences.length === 0) {
    exact = visibleMarkdownText(doc, anchor.exact);
    prefix = visibleMarkdownText(doc, anchor.prefix);
    suffix = visibleMarkdownText(doc, anchor.suffix);
    occurrences = textOccurrences(index.flat, exact);
  }
  if (occurrences.length === 0) return { state: "missing" };
  if (occurrences.length === 1) {
    return {
      state: "unique",
      ...rangeFor(index, occurrences[0].offset, occurrences[0].length),
    };
  }

  const contextual = occurrences.filter(
    (match) =>
      prefixMatches(index.flat, match.offset, prefix) &&
      suffixMatches(index.flat, match.offset + match.length, suffix),
  );
  if (contextual.length === 1) {
    return {
      state: "unique",
      ...rangeFor(index, contextual[0].offset, contextual[0].length),
    };
  }
  return { state: "ambiguous" };
};

/**
 * Provenance reanchoring is stricter than legacy proposal placement: every
 * supplied exact/prefix/suffix component must still describe the adjacent
 * visible text, even when the exact quote occurs only once.
 */
export const resolveProvenanceQuoteAnchorDetailed = (
  doc: Node,
  anchor: QuoteAnchor,
): QuoteAnchorResolution => {
  if (anchor.exact.length === 0) return { state: "missing" };
  const index = buildTextIndex(doc);
  let exact = anchor.exact;
  let occurrences = textOccurrences(index.flat, exact);
  if (occurrences.length === 0) {
    exact = visibleMarkdownText(doc, anchor.exact);
    occurrences = textOccurrences(index.flat, exact);
  }
  if (occurrences.length === 0) return { state: "missing" };
  const prefix = visibleMarkdownText(doc, anchor.prefix);
  const suffix = visibleMarkdownText(doc, anchor.suffix);
  const contextual = occurrences.filter(
    (match) =>
      prefixMatches(index.flat, match.offset, prefix) &&
      suffixMatches(index.flat, match.offset + match.length, suffix),
  );
  if (contextual.length === 0) return { state: "missing" };
  if (contextual.length > 1) return { state: "ambiguous" };
  return {
    state: "unique",
    ...rangeFor(index, contextual[0].offset, contextual[0].length),
  };
};

export const resolveQuoteAnchor = (
  doc: Node,
  anchor: QuoteAnchor,
): { from: number; to: number } | null => {
  const result = resolveQuoteAnchorDetailed(doc, anchor);
  return result.state === "unique" ? { from: result.from, to: result.to } : null;
};
