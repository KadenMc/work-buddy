import type { AnyExtension } from "@tiptap/core";

import { CoworkSuggestChanges } from "./pluginExtension";
import {
  SuggestionDeletion,
  SuggestionInsertion,
  SuggestionModification,
} from "./marks";

/**
 * The registration seam the editor bundle consumes at the join (owned here so
 * src/apps/cowork/editor/** stays untouched, C1 build note). The join spreads these
 * extensions into the editor set and swaps StarterKit's code_block for CoworkCodeBlock
 * (StarterKit codeBlock: false) so the code-block schema admits suggestion marks.
 *
 * The suggestion marks are editor-runtime schema, NOT Markdown schema, so they are added
 * to the editor extensions only and never to the DOM-free MarkdownManager. They carry no
 * serializer, and accept / reject resolves them before materialization.
 */

/** The three live suggestion marks, for a schema that must resolve getSuggestionMarks. */
export const buildSuggestionSchemaExtensions = (): AnyExtension[] => [
  SuggestionInsertion,
  SuggestionDeletion,
  SuggestionModification,
];

/** The full suggestion extension set: the three marks plus the decoration plugin. */
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
export {
  WbTrackedChangesAdapterImpl,
  createWbTrackedChangesAdapter,
} from "./adapter";
export type { WbTrackedChangesAdapterOptions } from "./adapter";
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
  CoworkSittingRequest,
  CoworkSittingTransport,
  SubmitSittingParams,
} from "./sitting";
export type {
  AdapterEvents,
  DecisionItem,
  EpistemicState,
  MaterializePayload,
  ProposalInput,
  QuoteAnchor,
  SittingItemResult,
  SittingRequest,
  SittingResponse,
  SittingResultKind,
  SittingVerb,
  WbSuggestionAttrs,
  WbTrackedChangesAdapter,
} from "./types";
