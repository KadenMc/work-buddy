import { getChangedRanges } from "@tiptap/core";
import type { Node, Slice } from "@tiptap/pm/model";
import type { Transaction } from "@tiptap/pm/state";

import {
  quoteAnchorFromRange,
  type RangeQuoteAnchor,
} from "../feedback/feedbackAnchor";
import { buildTextIndex } from "../suggestions/anchor";
import type { CoworkProvenanceDetermination } from "./contracts";

/** Authoritative conflict code returned by the provenance-attestation route. */
export const COWORK_PROVENANCE_TARGET_CHANGED =
  "provenance_target_changed" as const;
/** The acting identity no longer matches a frozen current-user determination. */
export const COWORK_PROVENANCE_ACTOR_CHANGED =
  "provenance_actor_changed" as const;

/** Mirrors the provenance-attestation API's Unicode-character ceiling. */
export const COWORK_PROVENANCE_EXACT_MAX_CHARS = 1_000_000;

export const COWORK_PASTE_PASSAGE_EXCERPT_CHARS = 180;

export interface CoworkPasteRange {
  readonly from: number;
  readonly to: number;
}

export interface CoworkPasteCapture {
  readonly range: CoworkPasteRange;
  readonly anchor: RangeQuoteAnchor;
  readonly substantial: boolean;
}

/**
 * Narrow boundary between the editor-owned paste transaction and whichever
 * same-origin API adapter persists its span attestation.
 */
export interface CoworkPasteProvenanceRequest {
  readonly storeId: string;
  readonly documentId: string;
  readonly basisKind:
    | "automatic_short_text_attribution"
    | "user_attestation";
  readonly expectedStructuredHeadSha256: string;
  readonly anchor: RangeQuoteAnchor;
  readonly attestation: CoworkProvenanceDetermination;
  readonly idempotencyKey: string;
}

export type CoworkPasteProvenanceRecorder = (
  request: CoworkPasteProvenanceRequest,
) => Promise<void>;

export type CoworkPasteAnchorResolution =
  | {
      readonly kind: "unique";
      readonly from: number;
      readonly to: number;
    }
  | { readonly kind: "absent" | "ambiguous" };

/**
 * Count Unicode code points, matching Python's len(str) at the API boundary,
 * and stop as soon as the server ceiling is exceeded.
 */
export const coworkProvenanceExactWithinLimit = (exact: string): boolean => {
  let characters = 0;
  for (const _character of exact) {
    characters += 1;
    if (characters > COWORK_PROVENANCE_EXACT_MAX_CHARS) return false;
  }
  return true;
};

const occurrences = (text: string, exact: string): number[] => {
  const matches: number[] = [];
  let from = 0;
  while (exact.length > 0) {
    const found = text.indexOf(exact, from);
    if (found < 0) break;
    matches.push(found);
    from = found + 1;
  }
  return matches;
};

/**
 * Paste provenance is stricter than ordinary review decoration projection:
 * exact, prefix, and suffix must together identify one current passage.
 */
export const resolveCoworkPasteAnchor = (
  document: Node,
  anchor: RangeQuoteAnchor,
): CoworkPasteAnchorResolution => {
  if (anchor.exact.length === 0) return { kind: "absent" };
  const index = buildTextIndex(document);
  const exactMatches = occurrences(index.flat, anchor.exact);
  const contextual = exactMatches.filter((offset) => {
    const afterOffset = offset + anchor.exact.length;
    if (
      offset < anchor.prefix.length ||
      afterOffset + anchor.suffix.length > index.flat.length
    ) {
      return false;
    }
    return (
      index.flat.slice(offset - anchor.prefix.length, offset) ===
        anchor.prefix &&
      index.flat.slice(
        afterOffset,
        afterOffset + anchor.suffix.length,
      ) === anchor.suffix
    );
  });
  if (contextual.length === 0) return { kind: "absent" };
  if (contextual.length > 1) return { kind: "ambiguous" };
  const offset = contextual[0];
  const from = index.charPositions[offset];
  const to =
    index.charPositions[offset + anchor.exact.length - 1] + 1;
  return { kind: "unique", from, to };
};

export const coworkPastePassageExcerpt = (
  exact: string,
  maxChars = COWORK_PASTE_PASSAGE_EXCERPT_CHARS,
): string => {
  const normalized = exact.replace(/\s+/gu, " ").trim();
  if (normalized.length <= maxChars) return normalized;
  return `${normalized.slice(0, Math.max(0, maxChars - 1)).trimEnd()}…`;
};

const COMPLEX_BLOCKS = new Set([
  "bulletList",
  "orderedList",
  "taskList",
  "codeBlock",
  "blockquote",
  "table",
]);

export const SUBSTANTIAL_SINGLE_BLOCK_CHARS = 600;

/** Decide when a paste is large/structured enough to ask rather than assume. */
export const isSubstantialCoworkPaste = (
  slice: Slice,
  plainText: string,
): boolean => {
  if (slice.content.childCount > 1) return true;
  let complex = false;
  slice.content.descendants((node) => {
    if (COMPLEX_BLOCKS.has(node.type.name)) {
      complex = true;
      return false;
    }
    return undefined;
  });
  return complex || Array.from(plainText).length >= SUBSTANTIAL_SINGLE_BLOCK_CHARS;
};

/**
 * Derive the actual post-paste range from the transaction that ProseMirror
 * applied. This handles replacement selections and normalized rich clipboard
 * input; the usually-collapsed post-paste selection is not used.
 */
export const coworkPasteRangeFromTransaction = (
  transaction: Transaction,
): CoworkPasteRange | null => {
  if (transaction.getMeta("uiEvent") !== "paste") return null;
  const ranges = getChangedRanges(transaction)
    .map((item) => item.newRange)
    .filter((range) => range.to > range.from);
  if (ranges.length === 0) return null;
  return {
    from: Math.min(...ranges.map((range) => range.from)),
    to: Math.max(...ranges.map((range) => range.to)),
  };
};

/**
 * Capture the exact text the paste transaction inserted after ProseMirror has
 * normalized it. Anchoring and substantial-paste classification therefore see
 * the same structured document that persistence and the server will receive.
 */
export const coworkPasteCaptureFromTransaction = (
  transaction: Transaction,
  document: Node = transaction.doc,
): CoworkPasteCapture | null => {
  const range = coworkPasteRangeFromTransaction(transaction);
  if (range === null) return null;
  const anchor = quoteAnchorFromRange(document, range.from, range.to);
  if (anchor === null) return null;
  return {
    range,
    anchor,
    substantial:
      !coworkProvenanceExactWithinLimit(anchor.exact) ||
      isSubstantialCoworkPaste(
        document.slice(range.from, range.to),
        anchor.exact,
      ),
  };
};

/** True when a paste transaction would create an API-invalid exact selector. */
export const coworkPasteTransactionExceedsProvenanceLimit = (
  transaction: Transaction,
): boolean => {
  if (transaction.getMeta("uiEvent") !== "paste") return false;
  const capture = coworkPasteCaptureFromTransaction(
    transaction,
    transaction.doc,
  );
  return (
    capture !== null &&
    !coworkProvenanceExactWithinLimit(capture.anchor.exact)
  );
};
