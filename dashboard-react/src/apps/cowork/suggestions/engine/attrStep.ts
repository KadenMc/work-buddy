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
import { type AttrStep, type Step } from "@tiptap/pm/transform";

import { rebasePos } from "./rebasePos";
import { getSuggestionMarks } from "./utils";
import { type SuggestionId } from "./generateId";

/**
 * Transform an attr mark step into its equivalent tracked steps.
 *
 * Attr steps are processed normally, and then a modification
 * mark is added to the node as well, to track the change.
 */
export function trackAttrStep(
  trackedTransaction: Transaction,
  state: EditorState,
  _doc: Node,
  step: AttrStep,
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
      mark.attrs["type"] === "attr" &&
      mark.attrs["attrName"] === step.attr,
  );
  if (existingMod) {
    marks = existingMod.removeFromSet(marks);
  }
  marks = modification
    .create({
      id: suggestionId,
      type: "attr",
      attrName: step.attr,
      // eslint-disable-next-line @typescript-eslint/no-unsafe-assignment
      previousValue: node?.attrs[step.attr],
      // eslint-disable-next-line @typescript-eslint/no-unsafe-assignment
      newValue: step.value,
    })
    .addToSet(marks);
  trackedTransaction.setNodeMarkup(
    rebasedPos,
    null,
    // eslint-disable-next-line @typescript-eslint/no-unsafe-assignment
    { ...node?.attrs, [step.attr]: step.value },
    marks,
  );
  return true;
}
