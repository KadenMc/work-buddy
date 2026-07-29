import { Editor } from "@tiptap/core";
import StarterKit from "@tiptap/starter-kit";
import { afterEach, describe, expect, it, vi } from "vitest";

import type {
  CoworkActionSnapshotController,
  CoworkActionSnapshotControllerState,
} from "../targets/contracts";
import {
  CoworkWorkingTargetDecorations,
  readCoworkWorkingTarget,
  readCoworkWorkingTargetStart,
} from "../editor/workingTargetDecorations";
import { CoworkWorkingTargetProjector } from "./CoworkWorkingTargetProjector";

let editor: Editor | null = null;
let host: HTMLElement | null = null;

afterEach(() => {
  editor?.destroy();
  editor = null;
  host?.remove();
  host = null;
});

describe("CoworkWorkingTargetProjector", () => {
  it("replaces a provisional cursor start with the resolved Working on range", () => {
    let state: CoworkActionSnapshotControllerState = {
      phase: "ready",
      selection: null,
      currentSection: null,
      workingTarget: {
        kind: "text_range",
        label: "Exact passage",
        wordCount: 2,
        range: { from: 7, to: 20 },
        resolution: "relative",
      },
    };
    const listeners = new Set<() => void>();
    const controller: CoworkActionSnapshotController = {
      getSnapshot: () => state,
      subscribe: (listener) => {
        listeners.add(listener);
        return () => listeners.delete(listener);
      },
      setWorkingTargetFromSelection: vi.fn(),
      clearWorkingTarget: vi.fn(),
      capture: vi.fn(),
    };
    host = document.createElement("div");
    document.body.append(host);
    editor = new Editor({
      element: host,
      content: "<p>Alpha Exact passage Omega.</p>",
      extensions: [
        StarterKit.configure({ undoRedo: false }),
        CoworkWorkingTargetDecorations,
      ],
    });
    const projector = new CoworkWorkingTargetProjector(controller);
    projector.attach(editor);

    expect(readCoworkWorkingTarget(editor)).toEqual({
      from: 7,
      to: 20,
      label: "Exact passage",
    });

    state = {
      ...state,
      workingTargetStart: {
        position: 13,
        label: "Working on start",
      },
    };
    for (const listener of listeners) listener();
    expect(readCoworkWorkingTargetStart(editor)).toEqual({
      position: 13,
      label: "Working on start",
    });
    expect(readCoworkWorkingTarget(editor)).toBeNull();

    state = {
      ...state,
      workingTargetStart: null,
      workingTarget: {
        kind: "text_range",
        label: "passage Omega",
        wordCount: 2,
        range: { from: 13, to: 26 },
        resolution: "relative",
      },
    };
    for (const listener of listeners) listener();
    expect(readCoworkWorkingTargetStart(editor)).toBeNull();
    expect(readCoworkWorkingTarget(editor)).toEqual({
      from: 13,
      to: 26,
      label: "passage Omega",
    });

    state = {
      ...state,
      workingTargetStart: {
        position: 7,
        label: "Working on start",
      },
    };
    for (const listener of listeners) listener();
    state = { ...state, workingTargetStart: null };
    for (const listener of listeners) listener();
    expect(readCoworkWorkingTargetStart(editor)).toBeNull();
    expect(readCoworkWorkingTarget(editor)).toEqual({
      from: 13,
      to: 26,
      label: "passage Omega",
    });

    projector.dispose();
    expect(listeners).toHaveLength(0);
  });
});
