import { Editor } from "@tiptap/core";
import type { AnyExtension } from "@tiptap/core";
import StarterKit from "@tiptap/starter-kit";

import { CoworkCodeBlock } from "../codeBlock";
import { CoworkHorizontalRule, CoworkImage } from "../atomTracking";
import { CoworkSuggestChanges } from "../pluginExtension";
import { buildSuggestionSchemaExtensions } from "../index";
import type { ProposalInput, WbSuggestionAttrs } from "../types";

/**
 * Shared test scaffolding for the suggestion engine and adapter. A plain (non-collab)
 * Tiptap editor over StarterKit plus the three suggestion marks and the decoration plugin
 * exercises the engine's ProseMirror behavior deterministically, which is the layer SP-1
 * validated. The collaborative apply-origin path is exercised separately with a real
 * Y.Doc.
 */

export interface MakeEditorOptions {
  readonly content?: string;
  /** Swap StarterKit's code_block for the mark-admitting CoworkCodeBlock. */
  readonly coworkCodeBlock?: boolean;
  /** Swap StarterKit's horizontalRule for CoworkHorizontalRule and add CoworkImage. */
  readonly coworkAtoms?: boolean;
}

export const makeSuggestionEditor = (options: MakeEditorOptions = {}): Editor => {
  const extensions: AnyExtension[] = [
    StarterKit.configure({
      undoRedo: false,
      ...(options.coworkCodeBlock === true ? { codeBlock: false } : {}),
      ...(options.coworkAtoms === true ? { horizontalRule: false } : {}),
    }),
    ...(options.coworkCodeBlock === true ? [CoworkCodeBlock] : []),
    ...(options.coworkAtoms === true ? [CoworkHorizontalRule, CoworkImage] : []),
    ...buildSuggestionSchemaExtensions(),
    CoworkSuggestChanges,
  ];
  return new Editor({
    element: document.createElement("div"),
    extensions,
    content: options.content,
  });
};

export const defaultAttrs = (proposalId: string): WbSuggestionAttrs => ({
  proposal_id: proposalId,
  producer: "model-run-1",
  epistemic: "ai_proposed",
});

/** A quote-anchored edit proposal input, defaulting the attribution and hashes. */
export const editProposal = (
  proposalId: string,
  exact: string,
  replacement: string,
  context: { prefix?: string; suffix?: string } = {},
): ProposalInput => ({
  proposal_id: proposalId,
  kind: "edit",
  quoteAnchor: {
    exact,
    prefix: context.prefix ?? "",
    suffix: context.suffix ?? "",
  },
  replacement,
  attrs: defaultAttrs(proposalId),
  base_doc_sha256: "base-sha",
  canonical_sha256: `canonical-${proposalId}`,
});

/** Collect the distinct suggestion mark ids and their text in the editor doc. */
export const markSummary = (
  editor: Editor,
): { id: string; type: string; text: string }[] => {
  const out: { id: string; type: string; text: string }[] = [];
  editor.state.doc.descendants((node) => {
    if (!node.isText || node.text === undefined) return true;
    for (const mark of node.marks) {
      if (
        mark.type.name === "insertion" ||
        mark.type.name === "deletion" ||
        mark.type.name === "modification"
      ) {
        out.push({
          id: String(mark.attrs["id"]),
          type: mark.type.name,
          text: node.text,
        });
      }
    }
    return true;
  });
  return out;
};
