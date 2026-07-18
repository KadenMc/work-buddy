/**
 * Build a quote anchor from an editor SELECTION range, the reverse of the
 * proposal-side resolveQuoteAnchor. R9 anchors span-authored feedback the same
 * way a proposal is anchored (I12): exact quote plus prefix and suffix context,
 * resolved server-side by kernel anchors.py. This produces that shape from a
 * ProseMirror (from, to) range so the captured span re-locates after edits.
 *
 * The exact quote, prefix, and suffix are sliced from the SAME flat text index
 * resolveQuoteAnchor consumes (suggestions/anchor.ts), so a span captured here
 * round-trips through the resolver the scroll-to seam uses: block boundaries are
 * a single newline, adjacent text runs concatenate, and the context windows are
 * plain flat-text slices around the selection.
 */

import type { Node } from "@tiptap/pm/model";

import { buildTextIndex } from "../suggestions/anchor";

/** The quote-anchor shape R9 sends as its span (exact plus bounded context). */
export interface RangeQuoteAnchor {
  readonly exact: string;
  readonly prefix: string;
  readonly suffix: string;
}

/** Characters of context captured on each side of the selection by default. */
export const DEFAULT_FEEDBACK_CONTEXT_CHARS = 32;

/**
 * Turn a ProseMirror selection range into a quote anchor, or null when the range
 * covers no text. `contextChars` bounds the prefix and suffix so a long document
 * does not inflate the anchor.
 */
export const quoteAnchorFromRange = (
  doc: Node,
  from: number,
  to: number,
  contextChars: number = DEFAULT_FEEDBACK_CONTEXT_CHARS,
): RangeQuoteAnchor | null => {
  if (to <= from) return null;

  const { flat, charPositions } = buildTextIndex(doc);
  if (charPositions.length === 0) return null;

  // First flat offset whose ProseMirror position is at or after the range start.
  let start = -1;
  for (let k = 0; k < charPositions.length; k++) {
    if (charPositions[k] >= from) {
      start = k;
      break;
    }
  }
  if (start === -1) return null;

  // Exclusive flat offset: one past the last flat char whose position is before
  // the range end (the selection end is exclusive in ProseMirror terms).
  let end = -1;
  for (let k = charPositions.length - 1; k >= 0; k--) {
    if (charPositions[k] < to) {
      end = k + 1;
      break;
    }
  }
  if (end <= start) return null;

  const exact = flat.slice(start, end);
  if (exact.length === 0) return null;

  const prefix = flat.slice(Math.max(0, start - contextChars), start);
  const suffix = flat.slice(end, Math.min(flat.length, end + contextChars));
  return { exact, prefix, suffix };
};
