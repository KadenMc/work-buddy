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

import { type Step } from "@tiptap/pm/transform";

/**
 * Rebase a position onto a new lineage of steps
 *
 * @param pos The position to rebase
 * @param back The old steps to undo, in the order they were originally applied
 * @param forth The new steps to map through
 */
export function rebasePos(pos: number, back: Step[], forth: Step[]) {
  const reset = back
    .slice()
    .reverse()
    .reduce((acc, step) => step.getMap().invert().map(acc), pos);
  return forth.reduce((acc, step) => step.getMap().map(acc), reset);
}
