import { Editor } from "@tiptap/core";
import StarterKit from "@tiptap/starter-kit";
import { Collaboration } from "@tiptap/extension-collaboration";
import { afterEach, describe, expect, it } from "vitest";
import * as Y from "yjs";

import { WbTrackedChangesAdapterImpl } from "../adapter";
import { buildSuggestionSchemaExtensions } from "../index";
import { CoworkSuggestChanges } from "../pluginExtension";
import {
  COWORK_APPLY_ORIGIN,
  isLocalHumanOrigin,
} from "../../editor/applyOrigin";
import { buildEditorExtensions } from "../../editor/extensions";
import { editProposal } from "./support";

/**
 * Isolated-engine coverage for apply-origin tagging. These tests deliberately mutate
 * disposable Y.Docs to retain migration and transform behavior. Production pending
 * proposals are ProseMirror decorations and accepted decisions materialize in a clean
 * canonical clone. Origin filtering alone is not safe isolation for a live Y.Doc.
 */

let editor: Editor | undefined;

afterEach(() => {
  editor?.destroy();
  editor = undefined;
});

const mountCollab = (
  doc: Y.Doc,
): { editor: Editor; adapter: WbTrackedChangesAdapterImpl } => {
  const ed = new Editor({
    element: document.createElement("div"),
    extensions: [
      StarterKit.configure({ undoRedo: false }),
      Collaboration.configure({ document: doc, field: "default" }),
      ...buildSuggestionSchemaExtensions(),
      CoworkSuggestChanges,
    ],
  });
  const adapter = new WbTrackedChangesAdapterImpl({ doc });
  adapter.attach(ed);
  return { editor: ed, adapter };
};

const mountFullCoworkEditor = (
  doc: Y.Doc,
): { editor: Editor; adapter: WbTrackedChangesAdapterImpl } => {
  const ed = new Editor({
    element: document.createElement("div"),
    extensions: buildEditorExtensions(doc),
  });
  const adapter = new WbTrackedChangesAdapterImpl({ doc });
  adapter.attach(ed);
  return { editor: ed, adapter };
};

describe("apply-origin discipline", () => {
  it("tags isolated proposal ingestion with the apply-origin origin", () => {
    const doc = new Y.Doc();
    const mounted = mountCollab(doc);
    editor = mounted.editor;

    // Seed content as a human edit, then observe only the ingestion updates.
    editor.commands.setContent("<p>The quick brown fox</p>");

    const origins: unknown[] = [];
    doc.on("update", (_update: Uint8Array, origin: unknown) => {
      origins.push(origin);
    });

    mounted.adapter.ingestProposal(
      editProposal("prop-1", "quick", "slow", { prefix: "The ", suffix: " brown" }),
    );

    expect(origins.length).toBeGreaterThan(0);
    expect(origins.every((origin) => origin === COWORK_APPLY_ORIGIN)).toBe(true);
    // This classification is useful in an isolated transform, but is not permission to
    // perform the same mutation against the live collaborative document.
    expect(origins.every((origin) => !isLocalHumanOrigin(origin))).toBe(true);
  });

  it("keeps isolated projection non-human with the complete extension stack", () => {
    const doc = new Y.Doc();
    const mounted = mountFullCoworkEditor(doc);
    editor = mounted.editor;
    editor.commands.setContent("<p>The quick brown fox</p>");

    const origins: unknown[] = [];
    doc.on("update", (_update: Uint8Array, origin: unknown) => {
      origins.push(origin);
    });

    mounted.adapter.ingestProposal(
      editProposal("prop-full", "quick", "slow", {
        prefix: "The ",
        suffix: " brown",
      }),
    );

    expect(origins.length).toBeGreaterThan(0);
    expect(origins.every((origin) => origin === COWORK_APPLY_ORIGIN)).toBe(true);
    expect(origins.every((origin) => !isLocalHumanOrigin(origin))).toBe(true);
  });

  it("tags an isolated accepted edit with the apply-origin origin", () => {
    const doc = new Y.Doc();
    const mounted = mountCollab(doc);
    editor = mounted.editor;
    editor.commands.setContent("<p>The quick brown fox</p>");
    mounted.adapter.ingestProposal(
      editProposal("prop-1", "quick", "slow", { prefix: "The ", suffix: " brown" }),
    );

    const origins: unknown[] = [];
    doc.on("update", (_update: Uint8Array, origin: unknown) => {
      origins.push(origin);
    });

    mounted.adapter.applyDecision({
      proposal_id: "prop-1",
      verb: "confirm",
      canonical_sha256: "canonical-prop-1",
    });

    expect(origins.length).toBeGreaterThan(0);
    expect(origins.every((origin) => origin === COWORK_APPLY_ORIGIN)).toBe(true);
    expect(editor.getText()).toContain("slow");
  });

  it("tags an isolated amended deletion with apply-origin", () => {
    const doc = new Y.Doc();
    const mounted = mountCollab(doc);
    editor = mounted.editor;
    editor.commands.setContent("<p>The quick brown fox</p>");
    mounted.adapter.ingestProposal(
      editProposal("prop-1", "quick ", "slow ", { prefix: "The " }),
    );

    const origins: unknown[] = [];
    doc.on("update", (_update: Uint8Array, origin: unknown) => {
      origins.push(origin);
    });

    mounted.adapter.applyDecision({
      proposal_id: "prop-1",
      verb: "edit_confirm",
      canonical_sha256: "canonical-prop-1",
      amend_content: "",
    });

    expect(origins.length).toBeGreaterThan(0);
    expect(origins.every((origin) => origin === COWORK_APPLY_ORIGIN)).toBe(true);
    expect(origins.every((origin) => !isLocalHumanOrigin(origin))).toBe(true);
    expect(editor.getText()).toBe("The brown fox");
  });

  it("tags isolated projection retraction with apply-origin", () => {
    const doc = new Y.Doc();
    const mounted = mountCollab(doc);
    editor = mounted.editor;
    editor.commands.setContent("<p>The quick brown fox</p>");
    mounted.adapter.ingestProposal(
      editProposal("prop-1", "quick", "slow", { prefix: "The ", suffix: " brown" }),
    );

    const origins: unknown[] = [];
    doc.on("update", (_update: Uint8Array, origin: unknown) => {
      origins.push(origin);
    });

    mounted.adapter.retractProposalProjection("prop-1");

    expect(origins.length).toBeGreaterThan(0);
    expect(origins.every((origin) => origin === COWORK_APPLY_ORIGIN)).toBe(true);
    expect(origins.every((origin) => !isLocalHumanOrigin(origin))).toBe(true);
    expect(editor.getText()).toBe("The quick brown fox");
  });

  it("applies a foreign server update through the apply-origin helper", () => {
    const source = new Y.Doc();
    const sourceText = source.getText("probe");
    sourceText.insert(0, "hello");
    const update = Y.encodeStateAsUpdate(source);

    const doc = new Y.Doc();
    const mounted = mountCollab(doc);
    editor = mounted.editor;

    const origins: unknown[] = [];
    doc.on("update", (_update: Uint8Array, origin: unknown) => {
      origins.push(origin);
    });

    mounted.adapter.applyServerUpdate(update);
    expect(origins).toEqual([COWORK_APPLY_ORIGIN]);
    expect(doc.getText("probe").toString()).toBe("hello");
  });
});
