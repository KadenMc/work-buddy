/**
 * Accessibility coverage for the isolated compatibility transform. The production
 * collaborative editor renders authoritative ledger state through
 * CoworkLedgerDecorations; it never attaches this adapter or stores these marks.
 * This proof keeps the legacy transform's semantic `ins` / `del` output accessible
 * for migration and recovery fixtures.
 */

import { Editor } from "@tiptap/core";
import StarterKit from "@tiptap/starter-kit";
import { afterEach, describe, expect, it } from "vitest";

import { expectNoAccessibilityViolations } from "../../../test/setup";
import {
  buildSuggestionSchemaExtensions,
  CoworkSuggestChanges,
  type ProposalInput,
} from "../suggestions";
import { createWbTrackedChangesAdapter } from "../suggestions/adapter";

const CONTENT = "<p>The quick brown fox jumps over the lazy dog.</p>";

function editProposal(
  proposalId: string,
  exact: string,
  replacement: string,
): ProposalInput {
  return {
    proposal_id: proposalId,
    kind: "edit",
    quoteAnchor: { exact, prefix: "", suffix: "" },
    replacement,
    attrs: { proposal_id: proposalId, producer: "research-agent", epistemic: "ai_proposed" },
    base_doc_sha256: "base-sha",
    canonical_sha256: `canonical-${proposalId}`,
  };
}

let editor: Editor | null = null;
let host: HTMLElement | null = null;

function mountEditorWithSuggestions(): HTMLElement {
  host = document.createElement("div");
  document.body.appendChild(host);
  editor = new Editor({
    element: host,
    content: CONTENT,
    extensions: [
      StarterKit.configure({ undoRedo: false }),
      ...buildSuggestionSchemaExtensions(),
      CoworkSuggestChanges,
    ],
    editorProps: {
      attributes: {
        class: "wb-cowork-editor__surface",
        "aria-label": "Document editor",
        role: "textbox",
        "aria-multiline": "true",
      },
    },
  });

  const adapter = createWbTrackedChangesAdapter();
  adapter.attach(editor);
  const inserted = adapter.ingestProposal(
    editProposal("ins-1", "quick brown", "swift brown"),
  );
  const deleted = adapter.ingestProposal(
    editProposal("del-1", "lazy ", ""),
  );
  expect(inserted.anchored).toBe(true);
  expect(deleted.anchored).toBe(true);
  return host;
}

describe("Co-work suggestion decorations accessibility", () => {
  afterEach(() => {
    editor?.destroy();
    editor = null;
    host?.remove();
    host = null;
  });

  it("renders tracked-change marks, including an empty replacement as a visible deletion", async () => {
    const container = mountEditorWithSuggestions();

    const insertion = container.querySelector('[data-wb-suggestion="insertion"]');
    const deletion = container.querySelector(
      '[data-wb-suggestion="deletion"][data-wb-anchor-id="del-1"]',
    );
    expect(insertion).not.toBeNull();
    expect(deletion).not.toBeNull();

    // The semantic element carries the change type on its own (ins vs del), and the
    // epistemic state rides a data attribute, so neither depends on the tint colour.
    expect(insertion?.tagName.toLowerCase()).toBe("ins");
    expect(deletion?.tagName.toLowerCase()).toBe("del");
    expect(insertion?.getAttribute("data-epistemic")).toBe("ai_proposed");
    expect(deletion).toHaveTextContent("lazy");
    expect(deletion).toHaveAttribute("data-wb-anchor-kind", "proposal");
    expect(deletion).toHaveAttribute("data-wb-anchor-id", "del-1");
    expect(deletion).not.toHaveClass("wb-cowork-flag-mark");

    await expectNoAccessibilityViolations(container);
  });
});
