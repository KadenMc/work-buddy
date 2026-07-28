import type { Editor } from "@tiptap/core";

/**
 * Proposal review is view state. A tracked suggestion mark or atom inside a
 * canonical editor means a legacy or accidental projection path contaminated
 * structured document state.
 */
export const assertCanonicalCoworkEditorState = (editor: Editor): void => {
  let suggestionId: string | null = null;
  editor.state.doc.descendants((node) => {
    if (node.attrs["wbSuggestion"] !== null && node.attrs["wbSuggestion"] !== undefined) {
      suggestionId = "atom";
      return false;
    }
    const suggestion = node.marks.find((mark) =>
      ["insertion", "deletion", "modification"].includes(mark.type.name),
    );
    if (suggestion !== undefined) {
      suggestionId = String(suggestion.attrs["id"] ?? "unknown");
      return false;
    }
    return true;
  });
  if (suggestionId !== null) {
    throw new Error(
      `Co-work refused to save noncanonical proposal projection ${suggestionId}. Reload the document and try again.`,
    );
  }
};
