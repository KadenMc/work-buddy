/**
 * Vendored from @handlewithcare/prosemirror-suggest-changes v0.1.8 (MIT).
 * Upstream https://github.com/handlewithcarecollective/prosemirror-suggest-changes
 * See the LICENSE and PROVENANCE.md files alongside this source.
 *
 * Modifications in this file:
 * 1. Import specifiers. Bare prosemirror-* imports rewritten to @tiptap/pm/* subpaths
 *    and relative .js extensions dropped for the dashboard-react bundler resolution.
 * 2. getSuggestionMarks re-exported from ./utils, so the adapter can resolve the three
 *    compatibility suggestion mark types from an isolated transform schema.
 */

export {
  addSuggestionMarks,
  insertion,
  deletion,
  modification,
} from "./schema";

export { getSuggestionMarks } from "./utils";
export type { SuggestionMarks } from "./utils";

export {
  selectSuggestion,
  revertSuggestion,
  revertSuggestions,
  applySuggestion,
  applySuggestions,
  enableSuggestChanges,
  disableSuggestChanges,
  toggleSuggestChanges,
} from "./commands";

export {
  suggestChanges,
  suggestChangesKey,
  isSuggestChangesEnabled,
} from "./plugin";

export {
  withSuggestChanges,
  transformToSuggestionTransaction,
} from "./withSuggestChanges";
