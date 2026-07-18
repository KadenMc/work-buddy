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

import { type Node } from "@tiptap/pm/model";
import { type EditorState, type Transaction } from "@tiptap/pm/state";
import {
  type AddMarkStep,
  type ReplaceStep,
  type Step,
  replaceStep,
} from "@tiptap/pm/transform";

import { applySuggestionsToRange } from "./commands";
import { suggestReplaceStep } from "./replaceStep";
import { type SuggestionId } from "./generateId";

/**
 * Transform an add mark step into its equivalent tracked steps.
 *
 * Add mark steps are treated as replace steps in this model. An
 * equivalent replace step will be generated, and then processed via
 * trackReplaceStep().
 */
export function trackAddMarkStep(
  trackedTransaction: Transaction,
  state: EditorState,
  doc: Node,
  step: AddMarkStep,
  prevSteps: Step[],
  suggestionId: SuggestionId,
) {
  const applied = step.apply(doc).doc;
  if (!applied) return false;
  const slice = applySuggestionsToRange(applied, step.from, step.to);
  const replace = replaceStep(doc, step.from, step.to, slice);
  if (!replace) return false;

  return suggestReplaceStep(
    trackedTransaction,
    state,
    doc,
    replace as ReplaceStep,
    prevSteps,
    suggestionId,
  );
}
