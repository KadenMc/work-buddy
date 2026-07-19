/**
 * Dashboard-citizenship proof (PRD I18) for the in-editor suggestion decorations.
 * A live editor ingests one insertion and one deletion proposal through the
 * WbTrackedChangesAdapter, so the rendered `ins` / `del` decorations are the real
 * tracked-change marks, not a rail-side stand-in. The proof asserts axe is clean
 * and that each decoration carries a redundant non-colour signal (the semantic
 * element plus `data-wb-suggestion` and `data-epistemic`), so meaning survives
 * forced-colors where the tint is replaced by a system colour (SP-6 G3).
 */

import { Editor } from "@tiptap/core";
import StarterKit from "@tiptap/starter-kit";
import { afterEach, describe, expect, it } from "vitest";

import { expectNoAccessibilityViolations } from "../../../test/setup";
import {
  buildSuggestionSchemaExtensions,
  CoworkSuggestChanges,
  createWbTrackedChangesAdapter,
  type ProposalInput,
} from "../suggestions";

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

  it("renders tracked-change marks with a non-colour encoding and clears axe", async () => {
    const container = mountEditorWithSuggestions();

    const insertion = container.querySelector('[data-wb-suggestion="insertion"]');
    const deletion = container.querySelector('[data-wb-suggestion="deletion"]');
    expect(insertion).not.toBeNull();
    expect(deletion).not.toBeNull();

    // The semantic element carries the change type on its own (ins vs del), and the
    // epistemic state rides a data attribute, so neither depends on the tint colour.
    expect(insertion?.tagName.toLowerCase()).toBe("ins");
    expect(deletion?.tagName.toLowerCase()).toBe("del");
    expect(insertion?.getAttribute("data-epistemic")).toBe("ai_proposed");

    await expectNoAccessibilityViolations(container);
  });
});
