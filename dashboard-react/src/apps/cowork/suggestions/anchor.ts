import type { Node } from "@tiptap/pm/model";

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
 * acceptance (AOV).
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

/** Every start offset of `needle` in `haystack` (overlapping matches included). */
const allOccurrences = (haystack: string, needle: string): number[] => {
  const found: number[] = [];
  if (needle.length === 0) return found;
  let from = 0;
  for (;;) {
    const at = haystack.indexOf(needle, from);
    if (at === -1) return found;
    found.push(at);
    from = at + 1;
  }
};

/** True when the min(len, available) characters ending at `idx` match the tail of prefix. */
const prefixMatches = (flat: string, idx: number, prefix: string): boolean => {
  if (prefix.length === 0) return true;
  const k = Math.min(prefix.length, idx);
  return flat.substring(idx - k, idx) === prefix.substring(prefix.length - k);
};

/** True when the min(len, available) characters starting at `idx` match the head of suffix. */
const suffixMatches = (flat: string, idx: number, suffix: string): boolean => {
  if (suffix.length === 0) return true;
  const available = flat.length - idx;
  const k = Math.min(suffix.length, available);
  return flat.substring(idx, idx + k) === suffix.substring(0, k);
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
export const resolveQuoteAnchor = (
  doc: Node,
  anchor: QuoteAnchor,
): { from: number; to: number } | null => {
  if (anchor.exact.length === 0) return null;

  const index = buildTextIndex(doc);
  const occurrences = allOccurrences(index.flat, anchor.exact);
  if (occurrences.length === 0) return null;
  if (occurrences.length === 1) {
    return rangeFor(index, occurrences[0], anchor.exact.length);
  }

  const contextual = occurrences.filter(
    (offset) =>
      prefixMatches(index.flat, offset, anchor.prefix) &&
      suffixMatches(index.flat, offset + anchor.exact.length, anchor.suffix),
  );
  if (contextual.length === 1) {
    return rangeFor(index, contextual[0], anchor.exact.length);
  }
  return null;
};
