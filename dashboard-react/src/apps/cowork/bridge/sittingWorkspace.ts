import { Editor } from "@tiptap/core";
import * as Y from "yjs";

import { assertCanonicalCoworkEditorState } from "../editor/canonicalState";
import { buildEditorExtensions } from "../editor/extensions";
import { serializeCoworkEditorMarkdown } from "../editor/serializeCoworkMarkdown";
import { sha256Hex } from "../persistence/hashing";
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

    const resolved = admittedItems.map((item) => {
      const proposal = proposalsById.get(item.proposal_id);
      if (proposal === undefined) {
        throw new Error(
          `The admitted proposal ${item.proposal_id} is missing from the authoritative catalog.`,
        );
      }
      if (proposal.canonical_sha256 !== item.canonical_sha256) {
        throw new Error(
          `The admitted proposal ${item.proposal_id} no longer matches the authoritative catalog.`,
        );
      }

      let replacement: string | null = null;
      if (item.verb === "confirm") {
        if (proposal.kind !== "edit" || proposal.replacement === null) {
          throw new Error(
            `The admitted proposal ${item.proposal_id} is not a materializable edit.`,
          );
        }
        replacement = proposal.replacement;
      } else if (item.verb === "edit_confirm") {
        if (proposal.kind !== "edit" || proposal.replacement === null) {
          throw new Error(
            `The admitted proposal ${item.proposal_id} is not a materializable edit.`,
          );
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
        throw new Error(
          `The admitted proposal ${item.proposal_id} could not be resolved in the canonical document.`,
        );
      }

      return { item, range, replacement };
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
          throw new Error(
            `The admitted proposals ${materialized[left].item.proposal_id} and ${materialized[right].item.proposal_id} overlap.`,
          );
        }
      }
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
