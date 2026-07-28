import type { AnyExtension } from "@tiptap/core";

import { CoworkSuggestChanges } from "./pluginExtension";
import {
  SuggestionDeletion,
  SuggestionInsertion,
  SuggestionModification,
} from "./marks";

/**
 * Compatibility schema retained for isolated transform tests and fail-closed recovery of
 * legacy documents. Live pending proposals render through CoworkLedgerDecorations and
 * never mint these marks in the collaborative Y.Doc.
 *
 * These marks are never part of Markdown serialization. Their presence in canonical
 * state is treated as contamination by explicit Save and sitting preparation.
 */

/** The three compatibility suggestion marks used by isolated engine transforms. */
export const buildSuggestionSchemaExtensions = (): AnyExtension[] => [
  SuggestionInsertion,
  SuggestionDeletion,
  SuggestionModification,
];

/** The compatibility transform extension set: three marks plus the engine plugin. */
export const buildSuggestionExtensions = (): AnyExtension[] => [
  ...buildSuggestionSchemaExtensions(),
  CoworkSuggestChanges,
];

export {
  SuggestionInsertion,
  SuggestionDeletion,
  SuggestionModification,
  suggestionMarks,
} from "./marks";
export { CoworkSuggestChanges } from "./pluginExtension";
export { CoworkCodeBlock } from "./codeBlock";
export {
  CoworkHorizontalRule,
  CoworkImage,
  WB_ATOM_SUGGESTION_ATTR,
  acceptAtomSuggestion,
  listOpenAtomSuggestions,
  revertAtomSuggestion,
  suggestAtomDeletion,
  suggestAtomInsertion,
} from "./atomTracking";
export type { AtomSuggestionKind, AtomSuggestionSpec } from "./atomTracking";
export { resolveQuoteAnchor, buildTextIndex } from "./anchor";
export { readSuggestionAttrs, stampAttribution } from "./attribution";
export {
  CoworkSittingClient,
  HttpCoworkSittingTransport,
  InMemoryCoworkSittingTransport,
  buildMaterializePayload,
  validateSitting,
} from "./sitting";
export type {
  CoworkSittingCommitRequest,
  CoworkSittingPrepareRequest,
  CoworkSittingTransport,
} from "./sitting";
export type {
  AdapterEvents,
  DecisionItem,
  EpistemicState,
  ProposalInput,
  QuoteAnchor,
  SittingDocumentCommit,
  SittingItemResult,
  SittingPrepareBody,
  SittingPrepared,
  SittingResponse,
  SittingResultKind,
  SittingVerb,
  WbSuggestionAttrs,
} from "./types";
