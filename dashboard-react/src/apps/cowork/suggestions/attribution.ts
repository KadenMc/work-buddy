import type { Mark } from "@tiptap/pm/model";
import type { Transaction } from "@tiptap/pm/state";

import { getSuggestionMarks } from "./engine";
import type { EpistemicState, WbSuggestionAttrs } from "./types";

/**
 * The vendored engine's step handlers create suggestion marks carrying only the grouping
 * `id`, which the adapter injects with the kernel proposal_id through generateId. This
 * module threads the forked attribution attrs (producer, epistemic) onto that same
 * tracked range after ingestion, so provenance survives acceptance (I11, SP-1 delta 2)
 * without patching every mark-creation site inside the engine.
 */

/** Read the adapter-facing attribution view from a live suggestion mark. */
export const readSuggestionAttrs = (mark: Mark): WbSuggestionAttrs => ({
  proposal_id: String(mark.attrs["id"]),
  producer: mark.attrs["producer"] === null ? "" : String(mark.attrs["producer"]),
  epistemic: (mark.attrs["epistemic"] as EpistemicState) ?? "ai_proposed",
});

/**
 * Stamp producer and epistemic onto every insertion / deletion / modification mark on the
 * transaction's doc whose id matches proposalId. Mark steps are size-preserving, so the
 * positions collected during the walk stay valid while the steps apply. The mark specs
 * exclude their own type, so re-adding a fully attributed mark replaces the id-only mark
 * in place rather than layering a second one.
 */
export const stampAttribution = (
  tr: Transaction,
  proposalId: string,
  producer: string,
  epistemic: EpistemicState,
): void => {
  const { insertion, deletion, modification } = getSuggestionMarks(tr.doc.type.schema);
  const targets = [insertion, deletion, modification];

  const inlineOps: { from: number; to: number; mark: Mark }[] = [];
  const nodeOps: { pos: number; mark: Mark }[] = [];

  tr.doc.descendants((node, pos) => {
    for (const markType of targets) {
      const existing = node.marks.find(
        (mark) => mark.type === markType && String(mark.attrs["id"]) === proposalId,
      );
      if (existing === undefined) continue;
      if (existing.attrs["producer"] === producer && existing.attrs["epistemic"] === epistemic) {
        continue;
      }
      const stamped = markType.create({ ...existing.attrs, producer, epistemic });
      if (node.isInline) {
        inlineOps.push({ from: pos, to: pos + node.nodeSize, mark: stamped });
      } else {
        nodeOps.push({ pos, mark: stamped });
      }
    }
    return true;
  });

  for (const op of inlineOps) {
    tr.addMark(op.from, op.to, op.mark);
  }
  for (const op of nodeOps) {
    tr.addNodeMark(op.pos, op.mark);
  }
};
