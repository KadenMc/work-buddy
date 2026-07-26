import { Editor } from "@tiptap/core";
import * as Y from "yjs";

import { applyForeignUpdate } from "../editor/applyOrigin";
import { buildEditorExtensions } from "../editor/extensions";
import { serializeCoworkEditorMarkdown } from "../editor/serializeCoworkMarkdown";
import { sha256Hex } from "../persistence/hashing";
import { createWbTrackedChangesAdapter } from "../suggestions/adapter";
import type { DecisionItem, SittingDocumentCommit } from "../suggestions/types";

export interface CoworkSittingPreflight {
  readonly expectedFileSha256: string;
  readonly expectedStructuredHeadSha256: string;
  readonly generation: number;
}

export interface PreparedCoworkSittingDocument {
  readonly commit: SittingDocumentCommit;
  readonly generation: number;
  adopt(): void;
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
  refreshFromServer(): Promise<void>;
}

/**
 * Build a complete canonical replacement snapshot on an isolated Y.Doc. The live editor is
 * untouched until adopt(), and all still-open proposal decorations are reverted out of the
 * server snapshot so ephemeral review marks can never become durable document state.
 */
export const prepareCoworkSittingDocument = async (
  liveDocument: Y.Doc,
  admittedItems: readonly DecisionItem[],
  generation: number,
  onAdopt?: () => void,
): Promise<PreparedCoworkSittingDocument> => {
  const clone = new Y.Doc();
  Y.applyUpdate(clone, Y.encodeStateAsUpdate(liveDocument));
  const editor = new Editor({
    extensions: buildEditorExtensions(clone),
    editable: false,
  });
  const adapter = createWbTrackedChangesAdapter({ doc: clone });
  adapter.attach(editor);

  for (const item of admittedItems) adapter.applyDecision(item);

  for (const proposalId of adapter.listOpen()) {
    adapter.applyDecision({
      proposal_id: proposalId,
      verb: "reject_plain",
      canonical_sha256: "",
    });
  }

  const renderedMarkdown = serializeCoworkEditorMarkdown(editor, clone);
  const renderedSha256 = await sha256Hex(new TextEncoder().encode(renderedMarkdown));
  // The exact projection now represented by this snapshot is the next fidelity baseline.
  clone.getMap<unknown>("wb-cowork:fidelity").set("source_sha256", renderedSha256);
  const snapshot = Y.encodeStateAsUpdate(clone);
  const snapshotSha256 = await sha256Hex(snapshot);
  const updateForLive = new Uint8Array(snapshot);
  let disposed = false;

  const dispose = (): void => {
    if (disposed) return;
    disposed = true;
    adapter.detach();
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
    adopt: () => {
      if (disposed) throw new Error("The prepared Co-work sitting is no longer available.");
      applyForeignUpdate(liveDocument, updateForLive);
      onAdopt?.();
    },
    dispose,
  };
};
