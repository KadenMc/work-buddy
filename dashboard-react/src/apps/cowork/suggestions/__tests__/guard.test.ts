import type { Editor } from "@tiptap/core";
import type { Transaction } from "@tiptap/pm/state";
import { ySyncPluginKey } from "@tiptap/y-tiptap";
import { afterEach, describe, expect, it } from "vitest";

import {
  enableSuggestChanges,
  suggestChangesKey,
  withSuggestChanges,
} from "../engine";
import { makeSuggestionEditor } from "./support";

/**
 * Remote-Yjs guard completion (SP-1 fork delta, C1 surface section 3). The patched
 * dispatch decorator consults the canonical isChangeOrigin predicate, so with suggest mode
 * enabled a local edit is transformed into a suggestion transaction while a change-origin
 * transaction (a remote batch, a Yjs undo, or a local apply-origin mutation) passes through
 * untransformed. A transformed transaction is a fresh object, so a strict-equality check
 * against the input distinguishes the two paths.
 */

let editor: Editor | undefined;

afterEach(() => {
  editor?.destroy();
  editor = undefined;
});

describe("suggest-changes dispatch guard", () => {
  const setup = () => {
    editor = makeSuggestionEditor({ content: "<p>hello world</p>" });
    enableSuggestChanges(editor.state, editor.view.dispatch);
    let received: Transaction | null = null;
    const decorated = withSuggestChanges(function (this: unknown, tr: Transaction) {
      received = tr;
    });
    return {
      editor,
      run: (tr: Transaction) => {
        decorated.call(editor?.view, tr);
        return received;
      },
    };
  };

  it("transforms a local edit into a suggestion transaction when suggest mode is on", () => {
    const { editor: ed, run } = setup();
    const local = ed.state.tr.insertText("X", 1);
    const received = run(local);
    expect(received).not.toBeNull();
    expect(received).not.toBe(local);
  });

  it("passes a change-origin transaction through untransformed", () => {
    const { editor: ed, run } = setup();
    const foreign = ed.state.tr.insertText("Y", 1).setMeta(ySyncPluginKey, {
      isChangeOrigin: true,
    });
    const received = run(foreign);
    expect(received).toBe(foreign);
  });

  it("passes a skip-tagged transaction through untransformed", () => {
    const { editor: ed, run } = setup();
    const skipped = ed.state.tr.insertText("Z", 1).setMeta(suggestChangesKey, { skip: true });
    const received = run(skipped);
    expect(received).toBe(skipped);
  });
});
