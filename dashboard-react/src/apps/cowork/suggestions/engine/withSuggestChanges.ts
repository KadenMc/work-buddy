/**
 * Vendored from @handlewithcare/prosemirror-suggest-changes v0.1.8 (MIT).
 * Upstream https://github.com/handlewithcarecollective/prosemirror-suggest-changes
 * See the LICENSE and PROVENANCE.md files alongside this source.
 *
 * Modifications in this file:
 * 1. Import specifiers rewritten to @tiptap/pm/* and relative .js extensions dropped.
 * 2. Remote-Yjs guard completion (SP-1 fork delta, C1 surface section 3). The upstream
 *    guard read the raw "y-sync$" transaction meta by string, which does not match the
 *    Tiptap Collaboration plugin key. The guard now consults the canonical
 *    isChangeOrigin predicate from @tiptap/extension-collaboration, which is true for
 *    every ySync-applied transaction, so a remote batch, a Yjs undo/redo, AND a local
 *    apply-origin mutation (the Co-work accept path) all pass through untracked. The
 *    running editor keeps suggest mode disabled and ingests proposals programmatically,
 *    so this guard is the defensive floor rather than the live-typing path.
 * 3. One inline comment had its em-dash normalized to satisfy the house style rule.
 */

import { type Schema, type Node } from "@tiptap/pm/model";
import { type EditorState, type Transaction } from "@tiptap/pm/state";
import { isChangeOrigin } from "@tiptap/extension-collaboration";
import {
  AddMarkStep,
  AddNodeMarkStep,
  AttrStep,
  RemoveMarkStep,
  RemoveNodeMarkStep,
  ReplaceAroundStep,
  ReplaceStep,
  type Step,
} from "@tiptap/pm/transform";

import { trackAddMarkStep } from "./addMarkStep";
import { trackAddNodeMarkStep } from "./addNodeMarkStep";
import { trackAttrStep } from "./attrStep";
import { suggestRemoveMarkStep } from "./removeMarkStep";
import { suggestRemoveNodeMarkStep } from "./removeNodeMarkStep";
import { suggestReplaceAroundStep } from "./replaceAroundStep";
import { suggestReplaceStep } from "./replaceStep";
import { type EditorView } from "@tiptap/pm/view";
import { isSuggestChangesEnabled, suggestChangesKey } from "./plugin";
import { generateNextNumberId, type SuggestionId } from "./generateId";
import { getSuggestionMarks } from "./utils";

type StepHandler<S extends Step> = (
  trackedTransaction: Transaction,
  state: EditorState,
  doc: Node,
  step: S,
  prevSteps: Step[],
  suggestionId: SuggestionId,
) => boolean;

function getStepHandler<S extends Step>(step: S): StepHandler<S> {
  if (step instanceof ReplaceStep) {
    return suggestReplaceStep as unknown as StepHandler<S>;
  }
  if (step instanceof ReplaceAroundStep) {
    return suggestReplaceAroundStep as unknown as StepHandler<S>;
  }
  if (step instanceof AddMarkStep) {
    return trackAddMarkStep as unknown as StepHandler<S>;
  }
  if (step instanceof RemoveMarkStep) {
    return suggestRemoveMarkStep as unknown as StepHandler<S>;
  }
  if (step instanceof AddNodeMarkStep) {
    return trackAddNodeMarkStep as unknown as StepHandler<S>;
  }
  if (step instanceof RemoveNodeMarkStep) {
    return suggestRemoveNodeMarkStep as unknown as StepHandler<S>;
  }
  if (step instanceof AttrStep) {
    return trackAttrStep as unknown as StepHandler<S>;
  }

  // Default handler, simply rebase the step onto the
  // tracked transaction and apply it.
  return (
    trackedTransaction: Transaction,
    _state: EditorState,
    _doc: Node,
    step: S,
    prevSteps: Step[],
  ) => {
    const reset = prevSteps
      .slice()
      .reverse()
      .reduce<Step | null>(
        (acc, step) => acc?.map(step.getMap().invert()) ?? null,
        step,
      );

    const rebased = trackedTransaction.steps.reduce(
      (acc, step) => acc?.map(step.getMap()) ?? null,
      reset,
    );

    if (rebased) {
      trackedTransaction.step(rebased);
    }
    return false;
  };
}

/**
 * Given a standard transaction from ProseMirror, produce
 * a new transaction that tracks the changes from the original,
 * rather than applying them.
 *
 * For each type of step, we implement custom behavior to prevent
 * deletions from being removed from the document, instead adding
 * deletion marks, and ensuring that all insertions have insertion
 * marks.
 */
export function transformToSuggestionTransaction(
  originalTransaction: Transaction,
  state: EditorState,
  generateId?: (schema: Schema, doc?: Node) => SuggestionId,
) {
  getSuggestionMarks(state.schema);

  let suggestionId = generateId
    ? generateId(state.schema, originalTransaction.docs[0])
    : generateNextNumberId(state.schema, originalTransaction.docs[0]);
  // Create a new transaction from scratch. The original transaction
  // is going to be dropped in favor of this one.
  const trackedTransaction = state.tr;

  for (let i = 0; i < originalTransaction.steps.length; i++) {
    // eslint-disable-next-line @typescript-eslint/no-non-null-assertion
    const step = originalTransaction.steps[i]!;

    // eslint-disable-next-line @typescript-eslint/no-non-null-assertion
    const doc = originalTransaction.docs[i]!;

    const stepTracker = getStepHandler(step);
    if (
      stepTracker(
        trackedTransaction,
        state,
        doc,
        step,
        originalTransaction.steps.slice(0, i),
        suggestionId,
      ) &&
      i < originalTransaction.steps.length - 1
    ) {
      // If the suggestionId was used by one of the step handlers,
      // increment it so that it's not reused.
      if (generateId) {
        suggestionId = generateId(state.schema, trackedTransaction.doc);
      } else if (typeof suggestionId === "number") {
        suggestionId = suggestionId + 1;
      }
    }
    continue;
  }

  if (originalTransaction.selectionSet && !trackedTransaction.selectionSet) {
    // Map the original selection backwards through the original transaction,
    // and then forwards through the new one.

    const originalBaseDoc = originalTransaction.docs[0];
    const base = originalBaseDoc
      ? originalTransaction.selection.map(
          originalBaseDoc,
          originalTransaction.mapping.invert(),
        )
      : originalTransaction.selection;

    trackedTransaction.setSelection(
      base.map(trackedTransaction.doc, trackedTransaction.mapping),
    );
  }

  if (originalTransaction.scrolledIntoView) {
    trackedTransaction.scrollIntoView();
  }

  if (originalTransaction.storedMarksSet) {
    trackedTransaction.setStoredMarks(originalTransaction.storedMarks);
  }

  // @ts-expect-error Preserve original transaction meta exactly as-is
  // eslint-disable-next-line @typescript-eslint/no-unsafe-assignment
  trackedTransaction.meta = originalTransaction.meta;

  return trackedTransaction;
}

/**
 * A `dispatchTransaction` decorator. Wrap your existing `dispatchTransaction`
 * function with `withSuggestChanges`, or pass no arguments to use the default
 * implementation (`view.setState(view.state.apply(tr))`).
 *
 * The result is a `dispatchTransaction` function that will intercept
 * and modify incoming transactions when suggest changes is enabled.
 * These modified transactions will suggest changes instead of directly
 * applying them, e.g. by marking a range with the deletion mark rather
 * than removing it from the document.
 */
export function withSuggestChanges(
  dispatchTransaction?: EditorView["dispatch"],
  generateId?: (schema: Schema, doc?: Node) => SuggestionId,
): EditorView["dispatch"] {
  const dispatch =
    dispatchTransaction ??
    function (this: EditorView, tr: Transaction) {
      this.updateState(this.state.apply(tr));
    };

  return function dispatchTransaction(this: EditorView, tr: Transaction) {
    const transaction =
      isSuggestChangesEnabled(this.state) &&
      !tr.getMeta("history$") &&
      !tr.getMeta("collab$") &&
      !isChangeOrigin(tr) &&
      !("skip" in (tr.getMeta(suggestChangesKey) ?? {}))
        ? transformToSuggestionTransaction(tr, this.state, generateId)
        : tr;
    dispatch.call(this, transaction);
  };
}
