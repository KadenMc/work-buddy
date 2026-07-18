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

import { type MarkType, type ResolvedPos } from "@tiptap/pm/model";

export function findSuggestionMarkEnd($pos: ResolvedPos, markType: MarkType) {
  const initialDeletionMark = ($pos.nodeAfter ?? $pos.nodeBefore)?.marks.find(
    (mark) => mark.type === markType,
  );
  if (!initialDeletionMark) {
    return $pos.pos;
  }
  let afterPos = $pos.pos + ($pos.nodeAfter?.nodeSize ?? 0);
  // We always return from this while loop

  // eslint-disable-next-line @typescript-eslint/no-unnecessary-condition
  while (true) {
    const $afterPos = $pos.doc.resolve(afterPos);
    if (
      $afterPos.depth < 1 ||
      ($afterPos.nodeAfter && !markType.isInSet($afterPos.marks()))
    ) {
      return $afterPos.pos;
    }

    // We're at the end of a node. We need to check
    // whether there's a matching deletion at the beginning
    // of the next node
    let afterParentPos = $afterPos.after();
    let $afterParentPos = $pos.doc.resolve(afterParentPos);
    let nextParent = $afterParentPos.nodeAfter;
    while ($afterParentPos.depth > 0 && !nextParent) {
      afterParentPos = $afterPos.after($afterParentPos.depth);
      $afterParentPos = $pos.doc.resolve(afterParentPos);
      nextParent = $afterParentPos.nodeAfter;
    }

    let cousinStartPos = afterParentPos + 1;
    let cousin = nextParent?.firstChild;
    while (cousin && !cousin.isLeaf && !markType.isInSet(cousin.marks)) {
      cousin = cousin.firstChild;
      cousinStartPos++;
    }

    const deletionMark = cousin?.marks.find((mark) => mark.type === markType);

    if (
      !cousin ||
      !deletionMark ||
      deletionMark.attrs["id"] !== initialDeletionMark.attrs["id"]
    ) {
      return $afterPos.pos;
    }

    const $cousinStartPos = $pos.doc.resolve(cousinStartPos);
    afterPos = $cousinStartPos.pos + ($cousinStartPos.nodeAfter?.nodeSize ?? 0);
  }
}
