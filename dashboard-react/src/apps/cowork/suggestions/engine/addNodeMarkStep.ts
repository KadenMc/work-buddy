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

import { type Mark, type Node } from "@tiptap/pm/model";
import { type EditorState, type Transaction } from "@tiptap/pm/state";
import { type AddNodeMarkStep, type Step } from "@tiptap/pm/transform";

import { rebasePos } from "./rebasePos";
import { getSuggestionMarks } from "./utils";
import { type SuggestionId } from "./generateId";

/**
 * Transform an add node mark step into its equivalent tracked steps.
 *
 * Add node mark steps are processed normally, and then a modification
 * mark is added to the node as well, to track the change.
 */
export function trackAddNodeMarkStep(
  trackedTransaction: Transaction,
  state: EditorState,
  _doc: Node,
  step: AddNodeMarkStep,
  prevSteps: Step[],
  suggestionId: SuggestionId,
) {
  const { modification } = getSuggestionMarks(state.schema);

  const rebasedPos = rebasePos(step.pos, prevSteps, trackedTransaction.steps);
  const $pos = trackedTransaction.doc.resolve(rebasedPos);
  const node = $pos.nodeAfter;
  let marks = node?.marks ?? [];
  const existingMods = marks.filter(
    (mark) =>
      mark.type === modification &&
      mark.attrs["type"] === "mark" &&
      step.mark.type.excludes(
        state.schema.markFromJSON(mark.attrs["newValue"]).type,
      ),
  );
  existingMods.forEach((mark) => {
    marks = mark.removeFromSet(marks);
  });
  let newMarks = step.mark.addToSet(marks);
  let previousValue: Mark | null = null;
  for (const mark of marks) {
    if (!newMarks.some((m) => m.eq(mark))) {
      previousValue = mark;
      break;
    }
  }
  newMarks = modification
    .create({
      id: suggestionId,
      type: "mark",
      // eslint-disable-next-line @typescript-eslint/no-unsafe-assignment
      previousValue: previousValue?.toJSON(),
      // eslint-disable-next-line @typescript-eslint/no-unsafe-assignment
      newValue: step.mark.toJSON(),
    })
    .addToSet(newMarks);
  trackedTransaction.setNodeMarkup(rebasedPos, null, null, newMarks);
  return true;
}
