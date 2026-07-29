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
  coworkHeadingPath,
  coworkRangeBlockIds,
  coworkRangeQuote,
  coworkRangeSummary,
  type CoworkBlockRange,
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
  readonly range: CoworkBlockRange;
  readonly now?: Date;
}): CoworkDocumentTargetReference => {
  const quote = coworkRangeQuote(editor.state.doc, range);
  if (quote === null) {
    throw new Error("Select text before choosing Work on this");
  }
  const { fragment, mapping } = mappingFor(editor, document);
  // y-tiptap's conversion is defined for ProseMirror text positions. Store
  // positions just inside the first and last selected blocks, then restore the
  // intended block boundaries after resolution. Raw top-level node boundaries
  // can associate with the neighboring block.
  const startPosition = Math.min(range.to - 1, range.from + 1);
  const endPosition = Math.max(startPosition + 1, range.to - 1);
  const start = absolutePositionToRelativePosition(
    startPosition,
    fragment,
    mapping,
  ) as Y.RelativePosition;
  const end = absolutePositionToRelativePosition(
    endPosition,
    fragment,
    mapping,
  ) as Y.RelativePosition;
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
  readonly range: CoworkBlockRange;
  readonly resolution: Exclude<
    CoworkDocumentTargetResolution,
    "unresolved"
  >;
}

const resolveRelative = (
  editor: Editor,
  document: Y.Doc,
  reference: CoworkDocumentTargetReference,
): CoworkBlockRange | null => {
  try {
    const { fragment, mapping } = mappingFor(editor, document);
    const start = Y.decodeRelativePosition(
      decodeCoworkBytes(reference.relative.startBase64),
    );
    const end = Y.decodeRelativePosition(
      decodeCoworkBytes(reference.relative.endBase64),
    );
    const from = relativePositionToAbsolutePosition(
      document,
      fragment,
      start,
      mapping,
    );
    const to = relativePositionToAbsolutePosition(
      document,
      fragment,
      end,
      mapping,
    );
    if (from === null || to === null || to <= from) return null;
    return blockAlignCoworkRange(editor.state.doc, { from, to });
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
  const recovered = blockAlignCoworkRange(editor.state.doc, quote);
  return recovered === null
    ? null
    : { range: recovered, resolution: "quote" };
};
