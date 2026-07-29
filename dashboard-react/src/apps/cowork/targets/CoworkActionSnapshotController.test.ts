import { Editor } from "@tiptap/core";
import { afterEach, describe, expect, it } from "vitest";
import * as Y from "yjs";

import { bootstrapCoworkYdoc } from "../documents/bootstrapCoworkYdoc";
import { buildEditorExtensions } from "../editor/extensions";
import { sha256Hex } from "../persistence/hashing";
import { resolveQuoteAnchor } from "../suggestions/anchor";
import type {
  CoworkActionCapturePersistence,
} from "./contracts";
import {
  CoworkActionCaptureChangedError,
  DefaultCoworkActionSnapshotController,
} from "./CoworkActionSnapshotController";
import { decodeCoworkBytes } from "./relativeEndpoints";

class FakeCapturePersistence implements CoworkActionCapturePersistence {
  lastError: unknown = null;
  pendingBatchCount = 0;
  docSha256 = "head-0";
  ydocGeneration = "generation-a";
  compactCalls = 0;
  mutateAfterEveryCompaction = false;
  mutateAfterFirstCompaction: (() => void) | null = null;
  readonly #document: Y.Doc;

  constructor(document: Y.Doc) {
    this.#document = document;
  }

  async retry(): Promise<void> {}

  async flush(): Promise<void> {}

  async compact(): Promise<{
    readonly snapshotSha256: string;
    readonly structuredHeadSha256: string;
  }> {
    this.compactCalls += 1;
    const snapshotSha256 = await sha256Hex(
      Y.encodeStateAsUpdate(this.#document),
    );
    this.docSha256 = `head-${this.compactCalls}`;
    const receipt = {
      snapshotSha256,
      structuredHeadSha256: this.docSha256,
    };
    if (
      this.mutateAfterEveryCompaction ||
      (this.compactCalls === 1 && this.mutateAfterFirstCompaction !== null)
    ) {
      this.mutateAfterFirstCompaction?.();
    }
    return receipt;
  }
}

let editor: Editor | null = null;
let document: Y.Doc | null = null;

const open = async (): Promise<{
  readonly editor: Editor;
  readonly document: Y.Doc;
}> => {
  const initialized = await bootstrapCoworkYdoc(
    new TextEncoder().encode(
      "# Intro\n\nOpening.\n\n# Risks\n\nRisk one.\n\n# Next\n\nDone.",
    ),
  );
  if (!initialized.ok) throw new Error(initialized.message);
  document = new Y.Doc();
  Y.applyUpdate(document, initialized.snapshot);
  editor = new Editor({
    element: window.document.createElement("div"),
    extensions: buildEditorExtensions(document),
  });
  return { editor, document };
};

afterEach(() => {
  editor?.destroy();
  document?.destroy();
  editor = null;
  document = null;
  window.localStorage.clear();
});

describe("DefaultCoworkActionSnapshotController", () => {
  it("retries a capture changed during compaction, then returns one frozen exact payload", async () => {
    const opened = await open();
    const risk = resolveQuoteAnchor(opened.editor.state.doc, {
      exact: "Risk one.",
      prefix: "",
      suffix: "",
    });
    if (risk === null) throw new Error("fixture did not resolve");
    opened.editor.commands.setTextSelection(risk);
    let generation = 0;
    const persistence = new FakeCapturePersistence(opened.document);
    persistence.mutateAfterFirstCompaction = () => {
      opened.editor.commands.insertContentAt(
        opened.editor.state.doc.content.size,
        {
          type: "paragraph",
          content: [{ type: "text", text: "Concurrent edit." }],
        },
      );
      generation += 1;
    };
    const controller = new DefaultCoworkActionSnapshotController({
      document: opened.document,
      documentId: "doc-a",
      storeId: "store-a",
      persistence,
      getEditGeneration: () => generation,
      storage: window.localStorage,
    });
    controller.attach(opened.editor);

    const capture = await controller.capture("current_section");

    expect(persistence.compactCalls).toBe(2);
    expect(capture.editGeneration).toBe(1);
    expect(capture.structuredHeadSha256).toBe("head-2");
    expect(
      await sha256Hex(decodeCoworkBytes(capture.snapshotBase64)),
    ).toBe(capture.snapshotSha256);
    expect(Object.isFrozen(capture)).toBe(true);
    expect(Object.isFrozen(capture.target)).toBe(true);
    expect(capture.target.selector.kind).toBe("text_quote");
    expect(capture.target.targetReference).toBeDefined();
    if (capture.target.selector.kind === "text_quote") {
      expect(
        Array.from(capture.projectionMarkdown)
          .slice(
            capture.target.selector.start,
            capture.target.selector.end,
          )
          .join(""),
      ).toBe(capture.target.selector.exact);
      expect(capture.target.selector.exact).toContain("# Risks");
      expect(capture.target.selector.exact).not.toContain("# Next");
    }

    const frozenMarkdown = capture.projectionMarkdown;
    opened.editor.commands.insertContent(" Later.");
    expect(capture.projectionMarkdown).toBe(frozenMarkdown);
    const targetReference = capture.target.targetReference;
    if (targetReference === undefined) {
      throw new Error("scoped capture omitted its durable target reference");
    }
    const recheck = await controller.captureReference(
      "current_section",
      targetReference,
    );
    expect(recheck.target.targetReference).toEqual(targetReference);
    expect(recheck.target.source).toBe("current_section");
    controller.detach();
  });

  it("fails explicitly instead of binding a head to a moving document", async () => {
    const opened = await open();
    let generation = 0;
    const persistence = new FakeCapturePersistence(opened.document);
    persistence.mutateAfterEveryCompaction = true;
    persistence.mutateAfterFirstCompaction = () => {
      opened.editor.commands.insertContentAt(
        opened.editor.state.doc.content.size,
        {
          type: "paragraph",
          content: [{ type: "text", text: `Edit ${generation}.` }],
        },
      );
      generation += 1;
    };
    const controller = new DefaultCoworkActionSnapshotController({
      document: opened.document,
      documentId: "doc-a",
      storeId: "store-a",
      persistence,
      getEditGeneration: () => generation,
      storage: window.localStorage,
    });
    controller.attach(opened.editor);

    await expect(controller.capture("whole_document")).rejects.toBeInstanceOf(
      CoworkActionCaptureChangedError,
    );
    expect(persistence.compactCalls).toBe(2);
    controller.detach();
  });

  it("captures an accessible start/end custom range without changing Working on", async () => {
    const opened = await open();
    const risk = resolveQuoteAnchor(opened.editor.state.doc, {
      exact: "Risk one.",
      prefix: "",
      suffix: "",
    });
    const done = resolveQuoteAnchor(opened.editor.state.doc, {
      exact: "Done.",
      prefix: "",
      suffix: "",
    });
    if (risk === null || done === null) throw new Error("fixture did not resolve");
    const persistence = new FakeCapturePersistence(opened.document);
    const controller = new DefaultCoworkActionSnapshotController({
      document: opened.document,
      documentId: "doc-a",
      storeId: "store-a",
      persistence,
      getEditGeneration: () => 0,
      storage: window.localStorage,
    });
    controller.attach(opened.editor);

    opened.editor.commands.setTextSelection(risk.from);
    controller.setCustomRangeStartHere();
    expect(controller.getSnapshot().customRangeStart).not.toBeNull();
    expect(controller.getSnapshot().workingTarget.kind).toBe("document");

    opened.editor.commands.setTextSelection(done.from);
    controller.setCustomRangeEndHere();
    const prepared = controller.getSnapshot().customRange;
    expect(prepared?.kind).toBe("text_range");

    const capture = await controller.capture("custom_range");
    expect(capture.target.source).toBe("custom_range");
    expect(capture.target.selector.kind).toBe("text_quote");
    if (capture.target.selector.kind === "text_quote") {
      expect(capture.target.selector.exact).toContain("Risk one.");
      expect(capture.target.selector.exact).toContain("Done.");
      expect(capture.target.selector.exact).not.toContain("Opening.");
    }
    expect(controller.getSnapshot().workingTarget.kind).toBe("document");
    controller.detach();
  });

  it("preserves Working on as a whole-document source when no range was set", async () => {
    const opened = await open();
    const persistence = new FakeCapturePersistence(opened.document);
    const controller = new DefaultCoworkActionSnapshotController({
      document: opened.document,
      documentId: "doc-a",
      storeId: "store-a",
      persistence,
      getEditGeneration: () => 0,
      storage: window.localStorage,
    });
    controller.attach(opened.editor);

    const capture = await controller.captureReference(
      "working_target",
      null,
    );

    expect(capture.target.source).toBe("working_target");
    expect(capture.target.selector).toEqual({ kind: "document" });
    expect(capture.target.targetReference).toBeUndefined();
    controller.detach();
  });
});
