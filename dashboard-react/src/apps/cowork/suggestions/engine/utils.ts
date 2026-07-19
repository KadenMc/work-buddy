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

import { type MarkType, type Schema } from "@tiptap/pm/model";

export interface SuggestionMarks {
  insertion: MarkType;
  deletion: MarkType;
  modification: MarkType;
}

/**
 * Get the suggestion mark types from a schema, with proper error handling.
 * Throws an error if any of the required marks are not found.
 */
export function getSuggestionMarks(schema: Schema): SuggestionMarks {
  const { insertion, deletion, modification } = schema.marks;

  if (!insertion) {
    throw new Error(
      "Failed to find insertion mark in schema. Did you forget to add it?",
    );
  }

  if (!deletion) {
    throw new Error(
      "Failed to find deletion mark in schema. Did you forget to add it?",
    );
  }

  if (!modification) {
    throw new Error(
      "Failed to find modification mark in schema. Did you forget to add it?",
    );
  }

  return { insertion, deletion, modification };
}
