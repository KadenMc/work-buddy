import { Editor } from "@tiptap/core";
import * as Y from "yjs";

import { assertCanonicalCoworkEditorState } from "../editor/canonicalState";
import { buildEditorExtensions } from "../editor/extensions";
import { serializeCoworkEditorMarkdown } from "../editor/serializeCoworkMarkdown";
import { sha256Hex } from "../persistence/hashing";
import {
  RecoverableDecisionApplyError,
  type DecisionApplyBlocker,
} from "../rail/applyRecovery";
import { resolveQuoteAnchor } from "../suggestions/anchor";
import type {
  DecisionItem,
  ProposalInput,
  SittingDocumentCommit,
  SittingResponse,
} from "../suggestions/types";

export interface CoworkSittingPreflight {
  readonly expectedFileSha256: string;
  readonly expectedStructuredHeadSha256: string;
  readonly generation: number;
}

export interface PreparedCoworkSittingDocument {
  readonly commit: SittingDocumentCommit;
  readonly generation: number;
  dispose(): void;
}

/** Narrow editor-owned seam used by the two-phase sitting coordinator. */
export interface CoworkSittingWorkspace {
  synchronize(): Promise<CoworkSittingPreflight>;
  prepare(
    admittedItems: readonly DecisionItem[],
    generation: number,
  ): Promise<PreparedCoworkSittingDocument>;
  isCurrent(generation: number): boolean;
  refreshFromServer(response: SittingResponse, generation: number): Promise<void>;
}

/**
 * Build a complete canonical replacement snapshot on an isolated Y.Doc. This function
 * never mutates the live editor; after commit the workspace pulls the authoritative
 * server state. Proposals are a view-only projection, so materialization resolves the
 * admitted ledger inputs against this initial canonical clone and applies only confirmed
 * edits. No proposal mark or adapter state is consulted or persisted.
 */
export const prepareCoworkSittingDocument = async (
  liveDocument: Y.Doc,
  admittedItems: readonly DecisionItem[],
  proposalCatalog: readonly ProposalInput[],
  generation: number,
): Promise<PreparedCoworkSittingDocument> => {
  const clone = new Y.Doc();
  Y.applyUpdate(clone, Y.encodeStateAsUpdate(liveDocument));
  const editor = new Editor({
    extensions: buildEditorExtensions(clone),
    editable: false,
  });

  try {
    assertCanonicalCoworkEditorState(editor);

    const proposalsById = new Map<string, ProposalInput>();
    for (const proposal of proposalCatalog) {
      if (proposalsById.has(proposal.proposal_id)) {
        throw new Error(
          `The proposal catalog contains duplicate id ${proposal.proposal_id}.`,
        );
      }
      proposalsById.set(proposal.proposal_id, proposal);
    }

    const blockers = new Map<string, DecisionApplyBlocker>();
    const resolved = admittedItems.flatMap((item) => {
      const proposal = proposalsById.get(item.proposal_id);
      if (proposal === undefined) {
        blockers.set(item.proposal_id, {
          proposalId: item.proposal_id,
          reason: "proposal_unavailable",
          relatedProposalIds: [],
          message: "This suggestion is no longer available in the current review.",
        });
        return [];
      }
      if (proposal.canonical_sha256 !== item.canonical_sha256) {
        blockers.set(item.proposal_id, {
          proposalId: item.proposal_id,
          reason: "proposal_changed",
          relatedProposalIds: [],
          message: "This suggestion changed since you made the decision.",
        });
        return [];
      }

      let replacement: string | null = null;
      if (item.verb === "confirm") {
        if (proposal.kind !== "edit" || proposal.replacement === null) {
          blockers.set(item.proposal_id, {
            proposalId: item.proposal_id,
            reason: "not_editable",
            relatedProposalIds: [],
            message: "This suggestion is not an editable text replacement.",
          });
          return [];
        }
        replacement = proposal.replacement;
      } else if (item.verb === "edit_confirm") {
        if (proposal.kind !== "edit" || proposal.replacement === null) {
          blockers.set(item.proposal_id, {
            proposalId: item.proposal_id,
            reason: "not_editable",
            relatedProposalIds: [],
            message: "This suggestion is not an editable text replacement.",
          });
          return [];
        }
        if (item.amend_content === undefined) {
          throw new Error(
            `edit_confirm on ${item.proposal_id} requires amend_content.`,
          );
        }
        replacement = item.amend_content;
      }

      const range =
        replacement === null
          ? null
          : resolveQuoteAnchor(editor.state.doc, proposal.quoteAnchor);
      if (replacement !== null && range === null) {
        blockers.set(item.proposal_id, {
          proposalId: item.proposal_id,
          reason: "passage_unavailable",
          relatedProposalIds: [],
          message: "The original passage could not be found in the current document.",
        });
        return [];
      }

      return [{ item, range, replacement }];
    });

    const materialized = resolved.filter(
      (
        candidate,
      ): candidate is typeof candidate & {
        readonly replacement: string;
        readonly range: { readonly from: number; readonly to: number };
      } => candidate.replacement !== null && candidate.range !== null,
    );
    for (let left = 0; left < materialized.length; left += 1) {
      for (let right = left + 1; right < materialized.length; right += 1) {
        const a = materialized[left].range;
        const b = materialized[right].range;
        if (a.from < b.to && b.from < a.to) {
          const leftId = materialized[left].item.proposal_id;
          const rightId = materialized[right].item.proposal_id;
          const addConflict = (proposalId: string, relatedProposalId: string) => {
            const existing = blockers.get(proposalId);
            const relatedProposalIds = new Set(
              existing?.reason === "conflicts_with_selected_edit"
                ? existing.relatedProposalIds
                : [],
            );
            relatedProposalIds.add(relatedProposalId);
            blockers.set(proposalId, {
              proposalId,
              reason: "conflicts_with_selected_edit",
              relatedProposalIds: [...relatedProposalIds],
              message: "This edit overlaps another selected edit.",
            });
          };
          addConflict(leftId, rightId);
          addConflict(rightId, leftId);
        }
      }
    }

    if (blockers.size > 0) {
      const availableProposalIds = admittedItems
        .map((item) => item.proposal_id)
        .filter((proposalId) => !blockers.has(proposalId));
      const orderedBlockers = admittedItems.flatMap((item) => {
        const blocker = blockers.get(item.proposal_id);
        return blocker === undefined ? [] : [blocker];
      });
      throw new RecoverableDecisionApplyError(
        orderedBlockers.some(
          (blocker) => blocker.reason === "conflicts_with_selected_edit",
        )
          ? "Some selected edits overlap and cannot be applied together."
          : "Some selected decisions no longer match the current document.",
        { availableProposalIds, blockers: orderedBlockers },
      );
    }

    const transaction = editor.state.tr;
    for (const candidate of [...materialized].sort(
      (left, right) => right.range.from - left.range.from,
    )) {
      if (candidate.replacement.length === 0) {
        transaction.delete(candidate.range.from, candidate.range.to);
      } else {
        transaction.replaceWith(
          candidate.range.from,
          candidate.range.to,
          editor.state.schema.text(candidate.replacement),
        );
      }
    }
    if (transaction.docChanged) editor.view.dispatch(transaction);
    assertCanonicalCoworkEditorState(editor);

    const renderedMarkdown = serializeCoworkEditorMarkdown(editor, clone);
    const renderedSha256 = await sha256Hex(
      new TextEncoder().encode(renderedMarkdown),
    );
    // The exact projection now represented by this snapshot is the next fidelity baseline.
    clone.getMap<unknown>("wb-cowork:fidelity").set("source_sha256", renderedSha256);
    const snapshot = Y.encodeStateAsUpdate(clone);
    const snapshotSha256 = await sha256Hex(snapshot);
    let disposed = false;

    const dispose = (): void => {
      if (disposed) return;
      disposed = true;
      editor.destroy();
      clone.destroy();
    };

    return {
      generation,
      commit: {
        snapshot,
        snapshot_sha256: snapshotSha256,
        rendered_markdown: renderedMarkdown,
        rendered_sha256: renderedSha256,
      },
      dispose,
    };
  } catch (error) {
    editor.destroy();
    clone.destroy();
    throw error;
  }
};
