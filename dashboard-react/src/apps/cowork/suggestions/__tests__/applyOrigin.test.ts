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
import { editProposal } from "./support";

/**
 * The apply-origin tagging path (C1 surface section 1.4, SP-2 point 6). A projected
 * proposal and an accepted edit must reach the Y.Doc under the apply-origin tag, so the
 * persistence layer never pushes them (human-direct edits only) and they stay off the
 * local undo stack. This exercises the real collaborative binding, where the adapter is
 * given the Y.Doc it wraps its dispatch against.
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

describe("apply-origin discipline", () => {
  it("tags proposal ingestion with the apply-origin origin, never a human origin", () => {
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
    // The persistence push filter would skip every one of these updates.
    expect(origins.every((origin) => !isLocalHumanOrigin(origin))).toBe(true);
  });

  it("tags an accepted edit with the apply-origin origin", () => {
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
