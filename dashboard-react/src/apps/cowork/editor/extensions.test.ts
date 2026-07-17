import { Editor } from "@tiptap/core";
import { afterEach, describe, expect, it } from "vitest";
import * as Y from "yjs";

import {
  COWORK_FRAGMENT_FIELD,
  COWORK_UNIQUE_ID_TYPES,
  buildEditorExtensions,
  createCoworkMarkdownManager,
  isFragmentEmpty,
} from "./extensions";

let editor: Editor | undefined;

afterEach(() => {
  editor?.destroy();
  editor = undefined;
});

describe("editor extension bundle", () => {
  it("fixes the collaboration fragment field to the frozen default", () => {
    expect(COWORK_FRAGMENT_FIELD).toBe("default");
  });

  it("uses the frozen block-level UniqueID allowlist and never bare all", () => {
    expect([...COWORK_UNIQUE_ID_TYPES]).toEqual([
      "paragraph",
      "heading",
      "blockquote",
      "codeBlock",
      "listItem",
      "bulletList",
      "orderedList",
      "horizontalRule",
    ]);
    // hardBreak is an inline atom and must NOT be in the allowlist (SP-2 F2.2).
    expect([...COWORK_UNIQUE_ID_TYPES]).not.toContain("hardBreak");
  });

  it("reports an empty fragment for a brand-new Y.Doc", () => {
    expect(isFragmentEmpty(new Y.Doc())).toBe(true);
  });

  it("binds the editor to the local Y.Doc on the default fragment and mints block ids", () => {
    const doc = new Y.Doc();
    editor = new Editor({
      element: document.createElement("div"),
      extensions: buildEditorExtensions(doc),
    });
    expect(isFragmentEmpty(doc)).toBe(true);

    editor.commands.setContent("<h1>Hello</h1><p>World</p>");

    // Collaboration flushed the content into the `default` fragment.
    expect(doc.getXmlFragment(COWORK_FRAGMENT_FIELD).length).toBeGreaterThan(0);
    expect(isFragmentEmpty(doc)).toBe(false);
    // UniqueID minted an id for the allowlisted heading block.
    expect(editor.state.doc.firstChild?.attrs.id).toBeTruthy();
  });

  it("registers the fidelity-minimum nodes and both wb marks in the schema", () => {
    const doc = new Y.Doc();
    editor = new Editor({
      element: document.createElement("div"),
      extensions: buildEditorExtensions(doc),
    });
    expect(editor.schema.nodes.table).toBeDefined();
    expect(editor.schema.nodes.taskList).toBeDefined();
    expect(editor.schema.nodes.image).toBeDefined();
    expect(editor.schema.marks.wbProvenanceTint).toBeDefined();
    expect(editor.schema.marks.wbExpressionMark).toBeDefined();
  });
});

describe("standalone MarkdownManager", () => {
  it("parses the fidelity-minimum constructs DOM-free", () => {
    const manager = createCoworkMarkdownManager();
    const json = manager.parse(
      "# Title\n\n- [ ] task\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n",
    );
    const serialized = JSON.stringify(json);
    expect(serialized).toContain("heading");
    expect(serialized).toContain("taskList");
    expect(serialized).toContain("table");
  });

  it("round-trips a parsed body to a stable serialized form", () => {
    const manager = createCoworkMarkdownManager();
    const body = "# Heading\n\nSome text with a list:\n\n- a\n- b\n";
    const once = manager.serialize(manager.parse(body));
    const twice = manager.serialize(manager.parse(once));
    expect(twice).toBe(once);
  });
});
