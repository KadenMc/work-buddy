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
import { type RemoveNodeMarkStep, type Step } from "@tiptap/pm/transform";

import { rebasePos } from "./rebasePos";
import { getSuggestionMarks } from "./utils";
import { type SuggestionId } from "./generateId";

/**
 * Transform a remove node mark step into its equivalent tracked steps.
 *
 * Remove node mark steps are processed normally, and then a modification
 * mark is added to the node as well, to track the change.
 */
export function suggestRemoveNodeMarkStep(
  trackedTransaction: Transaction,
  state: EditorState,
  _doc: Node,
  step: RemoveNodeMarkStep,
  prevSteps: Step[],
  suggestionId: SuggestionId,
) {
  const { modification } = getSuggestionMarks(state.schema);

  const rebasedPos = rebasePos(step.pos, prevSteps, trackedTransaction.steps);
  const $pos = trackedTransaction.doc.resolve(rebasedPos);
  const node = $pos.nodeAfter;
  let marks = node?.marks ?? [];
  const existingMod = marks.find(
    (mark) =>
      mark.type === modification &&
      mark.attrs["type"] === "mark" &&
      mark.attrs["newValue"] &&
      step.mark.eq(state.schema.markFromJSON(mark.attrs["newValue"])),
  );
  if (existingMod) {
    trackedTransaction.removeNodeMark(rebasedPos, existingMod);
    trackedTransaction.removeNodeMark(
      rebasedPos,
      state.schema.markFromJSON(existingMod.attrs["newValue"]),
    );
    return false;
  }
  marks = step.mark.removeFromSet(marks);
  marks = modification
    .create({
      id: suggestionId,
      type: "mark",
      // eslint-disable-next-line @typescript-eslint/no-unsafe-assignment
      previousValue: step.mark.toJSON(),
      newValue: null,
    })
    .addToSet(marks);
  trackedTransaction.setNodeMarkup(rebasedPos, null, null, marks);
  return true;
}
