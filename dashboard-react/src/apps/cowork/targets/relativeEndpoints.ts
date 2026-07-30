import type { Editor } from "@tiptap/core";
import {
  absolutePositionToRelativePosition,
  relativePositionToAbsolutePosition,
  ySyncPluginKey,
} from "@tiptap/y-tiptap";
import * as Y from "yjs";

import { resolveQuoteAnchor } from "../suggestions/anchor";
import type {
  CoworkDocumentTargetReference,
  CoworkDocumentTargetResolution,
} from "./contracts";
import {
  blockAlignCoworkRange,
  coworkExactRange,
  coworkHeadingPath,
  coworkRangeBlockIds,
  coworkRangeQuote,
  coworkRangeSummary,
  type CoworkIndexedRange,
} from "./selection";

type ProsemirrorMapping = Parameters<
  typeof absolutePositionToRelativePosition
>[2];

interface CoworkYSyncState {
  readonly type?: Y.XmlFragment;
  readonly binding?: {
    readonly mapping?: ProsemirrorMapping;
  };
}

const mappingFor = (
  editor: Editor,
  document: Y.Doc,
): {
  readonly fragment: Y.XmlFragment;
  readonly mapping: ProsemirrorMapping;
} => {
  const state = ySyncPluginKey.getState(editor.state) as
    | CoworkYSyncState
    | undefined;
  const fragment = state?.type ?? document.getXmlFragment("default");
  const mapping = state?.binding?.mapping;
  if (mapping === undefined) {
    throw new Error("The document target is not ready until the editor finishes loading");
  }
  return { fragment, mapping };
};

export const encodeCoworkBytes = (bytes: Uint8Array): string => {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return globalThis.btoa(binary);
};

export const decodeCoworkBytes = (base64: string): Uint8Array => {
  const binary = globalThis.atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index);
  }
  return bytes;
};

const relativeAt = (
  editor: Editor,
  document: Y.Doc,
  position: number,
  association: -1 | 1,
): Y.RelativePosition => {
  const { fragment, mapping } = mappingFor(editor, document);
  const leftAssociated = absolutePositionToRelativePosition(
    position,
    fragment,
    mapping,
  ) as Y.RelativePosition;
  if (association < 0) return leftAssociated;
  const absolute = Y.createAbsolutePositionFromRelativePosition(
    leftAssociated,
    document,
  );
  if (absolute === null) {
    throw new Error("The document target boundary could not be encoded");
  }
  return Y.createRelativePositionFromTypeIndex(
    absolute.type,
    absolute.index,
    association,
  );
};

export const createCoworkDocumentTargetReference = ({
  editor,
  document,
  storeId,
  documentId,
  range,
  now = new Date(),
}: {
  readonly editor: Editor;
  readonly document: Y.Doc;
  readonly storeId: string;
  readonly documentId: string;
  readonly range: CoworkIndexedRange;
  readonly now?: Date;
}): CoworkDocumentTargetReference => {
  const quote = coworkRangeQuote(editor.state.doc, range);
  if (quote === null) {
    throw new Error("Select text before setting Working on");
  }
  // Character targets keep their literal endpoints. Start associates with
  // content to its right and end with content to its left, so inserts exactly
  // at either outer boundary do not silently expand the target. Block targets
  // retain their interior-position representation for v1 compatibility.
  const startPosition =
    range.granularity === "character"
      ? range.from
      : Math.min(range.to - 1, range.from + 1);
  const endPosition =
    range.granularity === "character"
      ? range.to
      : Math.max(startPosition + 1, range.to - 1);
  const start = relativeAt(
    editor,
    document,
    startPosition,
    range.granularity === "character" ? 1 : -1,
  );
  const end = relativeAt(editor, document, endPosition, -1);
  const summary = coworkRangeSummary(
    editor.state.doc,
    range,
    "Selected passage",
  );
  const blockIds = coworkRangeBlockIds(editor.state.doc, range);
  const timestamp = now.toISOString();
  return {
    schema: "wb.cowork.document-target/v1",
    storeId,
    documentId,
    kind: "text_range",
    granularity: range.granularity,
    relative: {
      startBase64: encodeCoworkBytes(Y.encodeRelativePosition(start)),
      endBase64: encodeCoworkBytes(Y.encodeRelativePosition(end)),
    },
    quote,
    label: summary.label,
    headingPath: coworkHeadingPath(
      editor.state.doc,
      range.startBlockIndex,
    ),
    ...blockIds,
    createdAt: timestamp,
    updatedAt: timestamp,
  };
};

export interface CoworkResolvedDocumentTarget {
  readonly range: CoworkIndexedRange;
  readonly resolution: Exclude<
    CoworkDocumentTargetResolution,
    "unresolved"
  >;
}

const resolveRelative = (
  editor: Editor,
  document: Y.Doc,
  reference: CoworkDocumentTargetReference,
): CoworkIndexedRange | null => {
  try {
    const { fragment, mapping } = mappingFor(editor, document);
    const start = Y.decodeRelativePosition(
      decodeCoworkBytes(reference.relative.startBase64),
    );
    const end = Y.decodeRelativePosition(
      decodeCoworkBytes(reference.relative.endBase64),
    );
    const directlyResolvedFrom = relativePositionToAbsolutePosition(
      document,
      fragment,
      start,
      mapping,
    );
    let usedPositionOneFallback = false;
    const from =
      directlyResolvedFrom ??
      (() => {
        if (reference.granularity !== "character") return null;

        // y-tiptap deliberately rejects a right-associated Yjs item that
        // translates to ProseMirror position 1: after a collaborative block
        // reorder, an old item could otherwise appear to belong to the first
        // block. New character targets preserve that +1 association so text
        // inserted exactly at the outer start stays excluded. Translate the
        // item's current Yjs coordinate through a temporary left-associated
        // copy, but accept it only at position 1 and only after the complete
        // range proves both stored block identities below.
        const current = Y.createAbsolutePositionFromRelativePosition(
          start,
          document,
        );
        if (current === null) return null;
        const translated = relativePositionToAbsolutePosition(
          document,
          fragment,
          Y.createRelativePositionFromTypeIndex(
            current.type,
            current.index,
            -1,
          ),
          mapping,
        );
        if (translated !== 1) return null;
        usedPositionOneFallback = true;
        return translated;
      })();
    const to = relativePositionToAbsolutePosition(
      document,
      fragment,
      end,
      mapping,
    );
    if (from === null || to === null || to <= from) return null;
    const range =
      reference.granularity === "character"
        ? coworkExactRange(editor.state.doc, { from, to })
        : blockAlignCoworkRange(editor.state.doc, { from, to });
    if (range === null || !usedPositionOneFallback) return range;

    // The fallback above intentionally bypasses one stale-position guard, so
    // it needs stronger structural proof than ordinary relative resolution.
    // New exact references carry stable IDs for both containing blocks.
    // Legacy/malformed references without that proof continue to quote repair
    // (or fail closed) instead of being guessed into the document start.
    const ids = coworkRangeBlockIds(editor.state.doc, range);
    if (
      reference.startBlockId === undefined ||
      reference.endBlockId === undefined ||
      ids.startBlockId !== reference.startBlockId ||
      ids.endBlockId !== reference.endBlockId
    ) {
      return null;
    }
    return range;
  } catch {
    return null;
  }
};

export const resolveCoworkDocumentTargetReference = (
  editor: Editor,
  document: Y.Doc,
  reference: CoworkDocumentTargetReference,
): CoworkResolvedDocumentTarget | null => {
  const relative = resolveRelative(editor, document, reference);
  if (relative !== null) {
    const ids = coworkRangeBlockIds(editor.state.doc, relative);
    const startMatches =
      reference.startBlockId === undefined ||
      ids.startBlockId === reference.startBlockId;
    const endMatches =
      reference.endBlockId === undefined ||
      ids.endBlockId === reference.endBlockId;
    if (startMatches && endMatches) {
      return { range: relative, resolution: "relative" };
    }
  }
  const quote = resolveQuoteAnchor(editor.state.doc, reference.quote);
  if (quote === null) return null;
  const recovered =
    reference.granularity === "character"
      ? coworkExactRange(editor.state.doc, quote)
      : blockAlignCoworkRange(editor.state.doc, quote);
  return recovered === null
    ? null
    : { range: recovered, resolution: "quote" };
};

/** Ephemeral exact cursor identity used while a two-step range is incomplete. */
export interface CoworkCursorBoundaryReference {
  readonly positionBase64: string;
}

export const createCoworkCursorBoundaryReference = (
  editor: Editor,
  document: Y.Doc,
  position = editor.state.selection.head,
): CoworkCursorBoundaryReference => ({
  positionBase64: encodeCoworkBytes(
    Y.encodeRelativePosition(relativeAt(editor, document, position, 1)),
  ),
});

export const resolveCoworkCursorBoundaryReference = (
  editor: Editor,
  document: Y.Doc,
  reference: CoworkCursorBoundaryReference,
): number | null => {
  try {
    const { fragment, mapping } = mappingFor(editor, document);
    const relative = Y.decodeRelativePosition(
      decodeCoworkBytes(reference.positionBase64),
    );
    const resolved = relativePositionToAbsolutePosition(
      document,
      fragment,
      relative,
      mapping,
    );
    if (resolved !== null) return resolved;

    // y-tiptap intentionally rejects item-associated text positions that
    // resolve to ProseMirror position 1 because an old relative position can
    // misresolve there after a collaborative block reorder. This ephemeral
    // boundary was created against the current editor, so decode its tracked
    // Yjs identity first, then use a left-associated copy only to translate
    // that current coordinate through y-tiptap. The stored +1 association
    // remains authoritative and continues to exclude inserts at the start.
    const absolute = Y.createAbsolutePositionFromRelativePosition(
      relative,
      document,
    );
    if (absolute === null) return null;
    return relativePositionToAbsolutePosition(
      document,
      fragment,
      Y.createRelativePositionFromTypeIndex(
        absolute.type,
        absolute.index,
        -1,
      ),
      mapping,
    );
  } catch {
    return null;
  }
};
