/**
 * Vendored from @handlewithcare/prosemirror-suggest-changes v0.1.8 (MIT).
 * Upstream https://github.com/handlewithcarecollective/prosemirror-suggest-changes
 * See the LICENSE and PROVENANCE.md files alongside this source.
 *
 * Modifications in this file: import specifiers only. Bare prosemirror-* imports were
 * rewritten to the @tiptap/pm/* subpaths that resolve the single hoisted ProseMirror
 * instance in this tree, and relative .js extensions were dropped to match the
 * dashboard-react bundler module resolution.
 */

import { type Node, type Schema } from "@tiptap/pm/model";
import { getSuggestionMarks } from "./utils";

export type SuggestionId = string | number;

export const suggestionIdValidate = "number|string";

export function parseSuggestionId(id: string): SuggestionId {
  const parsed = parseInt(id, 10);
  if (isNaN(parsed)) {
    return id;
  }
  return parsed;
}

export function generateNextNumberId(schema: Schema, doc?: Node) {
  const { deletion, insertion, modification } = getSuggestionMarks(schema);
  // Find the highest change id in the document so far,
  // and use that as the starting point for new changes
  let suggestionId = 0;
  doc?.descendants((node) => {
    const mark = node.marks.find(
      (mark) =>
        mark.type === insertion ||
        mark.type === deletion ||
        mark.type === modification,
    );
    if (mark) {
      suggestionId = Math.max(suggestionId, mark.attrs["id"] as number);
      return false;
    }
    return true;
  });
  return suggestionId + 1;
}
