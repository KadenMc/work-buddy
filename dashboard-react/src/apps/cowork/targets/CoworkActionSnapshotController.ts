import type { Editor } from "@tiptap/core";
import * as Y from "yjs";

import { assertCanonicalCoworkEditorState } from "../editor/canonicalState";
import { sha256Hex } from "../persistence/hashing";
import type {
  CoworkActionCapturePersistence,
  CoworkActionSnapshotController,
  CoworkActionSnapshotControllerState,
  CoworkActionTargetChoice,
  CoworkCapturedActionSnapshot,
  CoworkDocumentTargetReference,
  CoworkResolvedActionTarget,
} from "./contracts";
import { CoworkDocumentTargetStore } from "./documentTargetStore";
import {
  createCoworkCursorBoundaryReference,
  createCoworkDocumentTargetReference,
  encodeCoworkBytes,
  resolveCoworkCursorBoundaryReference,
  resolveCoworkDocumentTargetReference,
  type CoworkCursorBoundaryReference,
} from "./relativeEndpoints";
import {
  coworkCurrentSectionRange,
  coworkExactRange,
  coworkProjectionTarget,
  coworkRangeQuote,
  coworkRangeSummary,
  coworkSelectionRange,
  coworkWholeDocumentSummary,
  coworkWordCount,
  type CoworkIndexedRange,
} from "./selection";

const CAPTURE_STABILITY_ATTEMPTS = 2;

export class CoworkActionCaptureChangedError extends Error {
  readonly code = "cowork_action_capture_changed";

  constructor() {
    super(
      "The document changed while Co-work was preparing this exact version. Try Run Verify again.",
    );
    this.name = "CoworkActionCaptureChangedError";
  }
}

export class CoworkActionTargetUnavailableError extends Error {
  readonly code = "cowork_action_target_unavailable";

  constructor(message = "This document target needs attention before it can be used.") {
    super(message);
    this.name = "CoworkActionTargetUnavailableError";
  }
}

interface CapturePlan {
  readonly choice: CoworkActionTargetChoice;
  readonly reference: CoworkDocumentTargetReference | null;
}

const loadingState = (): CoworkActionSnapshotControllerState => ({
  phase: "loading",
  selection: null,
  currentSection: null,
  workingTarget: {
    kind: "document",
    label: "Whole document",
    wordCount: 0,
    range: null,
  },
});

const randomCaptureId = (): string =>
  globalThis.crypto?.randomUUID?.() ??
  `${Date.now().toString(36)}${Math.random().toString(36).slice(2)}`;

/**
 * Editor-owned adapter for reusable document targets and one-action immutable
 * captures. The persistence controller remains private; only this narrow
 * behavior is lifted through the bridge.
 */
export class DefaultCoworkActionSnapshotController
  implements CoworkActionSnapshotController
{
  readonly #document: Y.Doc;
  readonly #documentId: string;
  readonly #storeId: string;
  readonly #persistence: CoworkActionCapturePersistence;
  readonly #getEditGeneration: () => number;
  readonly #targetStore: CoworkDocumentTargetStore;
  readonly #listeners = new Set<() => void>();
  #editor: Editor | null = null;
  #reference: CoworkDocumentTargetReference | null;
  #workingStartBoundary: CoworkCursorBoundaryReference | null = null;
  #customStartBoundary: CoworkCursorBoundaryReference | null = null;
  #customReference: CoworkDocumentTargetReference | null = null;
  #state: CoworkActionSnapshotControllerState = loadingState();
  #captureInFlight: Promise<CoworkCapturedActionSnapshot> | null = null;
  #captureInFlightKey: string | null = null;

  constructor({
    document,
    documentId,
    storeId,
    persistence,
    getEditGeneration,
    storage,
  }: {
    readonly document: Y.Doc;
    readonly documentId: string;
    readonly storeId: string;
    readonly persistence: CoworkActionCapturePersistence;
    readonly getEditGeneration: () => number;
    readonly storage?: Storage | null;
  }) {
    this.#document = document;
    this.#documentId = documentId;
    this.#storeId = storeId;
    this.#persistence = persistence;
    this.#getEditGeneration = getEditGeneration;
    this.#targetStore = new CoworkDocumentTargetStore({
      storeId,
      documentId,
      storage,
    });
    this.#reference = this.#targetStore.load();
  }

  readonly getSnapshot = (): CoworkActionSnapshotControllerState => this.#state;

  readonly subscribe = (listener: () => void): (() => void) => {
    this.#listeners.add(listener);
    return () => this.#listeners.delete(listener);
  };

  attach(editor: Editor): void {
    if (this.#editor === editor) return;
    this.detach();
    this.#editor = editor;
    editor.on("selectionUpdate", this.#onEditorStateChanged);
    editor.on("transaction", this.#onEditorStateChanged);
    this.#refresh();
  }

  detach(): void {
    const editor = this.#editor;
    if (editor !== null) {
      editor.off("selectionUpdate", this.#onEditorStateChanged);
      editor.off("transaction", this.#onEditorStateChanged);
    }
    this.#editor = null;
    this.#publish(loadingState());
  }

  setWorkingTargetFromSelection(): void {
    const editor = this.#requireEditor();
    const range = coworkSelectionRange(editor);
    if (range === null || coworkRangeQuote(editor.state.doc, range) === null) {
      throw new CoworkActionTargetUnavailableError(
        "Select some document text before choosing Set by selection.",
      );
    }
    coworkProjectionTarget(editor, this.#document, range);
    const next = createCoworkDocumentTargetReference({
      editor,
      document: this.#document,
      storeId: this.#storeId,
      documentId: this.#documentId,
      range,
    });
    this.#targetStore.save(next);
    this.#reference = next;
    this.#workingStartBoundary = null;
    this.#refresh();
  }

  setWorkingTargetStartHere(): void {
    const editor = this.#requireCollapsedCursor(
      "Place the cursor at the exact Working on start.",
    );
    this.#workingStartBoundary = createCoworkCursorBoundaryReference(
      editor,
      this.#document,
    );
    this.#refresh();
  }

  setWorkingTargetEndHere(): void {
    const editor = this.#requireCollapsedCursor(
      "Place the cursor at the exact Working on end.",
    );
    if (this.#workingStartBoundary === null) {
      throw new CoworkActionTargetUnavailableError(
        "Set the Working on start before setting its end.",
      );
    }
    const range = this.#exactRangeFromBoundary(
      editor,
      this.#workingStartBoundary,
      "Working on",
    );
    coworkProjectionTarget(editor, this.#document, range);
    const next = createCoworkDocumentTargetReference({
      editor,
      document: this.#document,
      storeId: this.#storeId,
      documentId: this.#documentId,
      range,
    });
    this.#targetStore.save(next);
    this.#reference = next;
    this.#workingStartBoundary = null;
    this.#refresh();
  }

  clearWorkingTargetDraft(): void {
    this.#workingStartBoundary = null;
    this.#refresh();
  }

  clearWorkingTarget(): void {
    this.#targetStore.clear();
    this.#reference = null;
    this.#workingStartBoundary = null;
    this.#refresh();
  }

  setCustomRangeStartHere(): void {
    const editor = this.#requireCollapsedCursor(
      "Place the cursor at the exact custom range start.",
    );
    this.#customStartBoundary = createCoworkCursorBoundaryReference(
      editor,
      this.#document,
    );
    this.#customReference = null;
    this.#refresh();
  }

  setCustomRangeEndHere(): void {
    const editor = this.#requireCollapsedCursor(
      "Place the cursor at the exact custom range end.",
    );
    if (this.#customStartBoundary === null) {
      throw new CoworkActionTargetUnavailableError(
        "Set the custom range start before setting its end.",
      );
    }
    const range = this.#exactRangeFromBoundary(
      editor,
      this.#customStartBoundary,
      "custom range",
    );
    coworkProjectionTarget(editor, this.#document, range);
    this.#customReference = createCoworkDocumentTargetReference({
      editor,
      document: this.#document,
      storeId: this.#storeId,
      documentId: this.#documentId,
      range,
    });
    this.#customStartBoundary = null;
    this.#refresh();
  }

  clearCustomRange(): void {
    this.#customStartBoundary = null;
    this.#customReference = null;
    this.#refresh();
  }

  capture(
    choice: CoworkActionTargetChoice,
  ): Promise<CoworkCapturedActionSnapshot> {
    let plan: CapturePlan;
    try {
      plan = this.#capturePlan(choice);
    } catch (error) {
      return Promise.reject(error);
    }
    return this.#beginCapture(plan);
  }

  captureReference(
    choice: CoworkActionTargetChoice,
    reference: CoworkDocumentTargetReference | null,
  ): Promise<CoworkCapturedActionSnapshot> {
    let plan: CapturePlan;
    try {
      if (choice === "whole_document") {
        if (reference !== null) {
          throw new CoworkActionTargetUnavailableError(
            "A whole-document recheck cannot carry a scoped target reference.",
          );
        }
        plan = { choice, reference: null };
      } else if (choice === "working_target" && reference === null) {
        // `Working on` defaults to the whole document. Preserve that source
        // identity without consulting a newer device-local Working on target.
        plan = { choice, reference: null };
      } else {
        if (
          reference === null ||
          reference.storeId !== this.#storeId ||
          reference.documentId !== this.#documentId
        ) {
          throw new CoworkActionTargetUnavailableError(
            "The original document target is unavailable.",
          );
        }
        const editor = this.#requireEditor();
        if (
          resolveCoworkDocumentTargetReference(
            editor,
            this.#document,
            reference,
          ) === null
        ) {
          throw new CoworkActionTargetUnavailableError(
            "The original document target could not be resolved in this version.",
          );
        }
        plan = { choice, reference };
      }
    } catch (error) {
      return Promise.reject(error);
    }
    return this.#beginCapture(plan);
  }

  #beginCapture(
    plan: CapturePlan,
  ): Promise<CoworkCapturedActionSnapshot> {
    const captureKey = JSON.stringify({
      choice: plan.choice,
      reference: plan.reference,
    });
    if (this.#captureInFlight !== null) {
      if (this.#captureInFlightKey === captureKey) {
        return this.#captureInFlight;
      }
      return Promise.reject(
        new Error("Co-work is already capturing another document target."),
      );
    }
    const run = this.#captureStable(plan);
    this.#captureInFlight = run;
    this.#captureInFlightKey = captureKey;
    void run.then(
      () => {
        if (this.#captureInFlight === run) {
          this.#captureInFlight = null;
          this.#captureInFlightKey = null;
        }
      },
      () => {
        if (this.#captureInFlight === run) {
          this.#captureInFlight = null;
          this.#captureInFlightKey = null;
        }
      },
    );
    return run;
  }

  readonly #onEditorStateChanged = (): void => {
    this.#refresh();
  };

  #capturePlan(choice: CoworkActionTargetChoice): CapturePlan {
    const editor = this.#requireEditor();
    if (choice === "whole_document") {
      return {
        choice,
        reference: null,
      };
    }
    if (choice === "working_target") {
      if (this.#reference !== null) {
        if (
          resolveCoworkDocumentTargetReference(
            editor,
            this.#document,
            this.#reference,
          ) === null
        ) {
          throw new CoworkActionTargetUnavailableError();
        }
      }
      return {
        choice,
        reference: this.#reference,
      };
    }
    if (choice === "current_selection") {
      const range = coworkSelectionRange(editor);
      if (range === null || coworkRangeQuote(editor.state.doc, range) === null) {
        throw new CoworkActionTargetUnavailableError(
          "Select some document text before using the current selection.",
        );
      }
      return {
        choice,
        reference: createCoworkDocumentTargetReference({
          editor,
          document: this.#document,
          storeId: this.#storeId,
          documentId: this.#documentId,
          range,
        }),
      };
    }
    if (choice === "custom_range") {
      if (
        this.#customReference === null ||
        resolveCoworkDocumentTargetReference(
          editor,
          this.#document,
          this.#customReference,
        ) === null
      ) {
        throw new CoworkActionTargetUnavailableError(
          "Set both custom range boundaries before using this target.",
        );
      }
      return {
        choice,
        reference: this.#customReference,
      };
    }
    const range = coworkCurrentSectionRange(editor);
    if (range === null || coworkRangeQuote(editor.state.doc, range) === null) {
      throw new CoworkActionTargetUnavailableError(
        "The current section has no text to evaluate.",
      );
    }
    return {
      choice,
      reference: createCoworkDocumentTargetReference({
        editor,
        document: this.#document,
        storeId: this.#storeId,
        documentId: this.#documentId,
        range,
      }),
    };
  }

  async #captureStable(
    plan: CapturePlan,
  ): Promise<CoworkCapturedActionSnapshot> {
    for (
      let stabilityAttempt = 0;
      stabilityAttempt < CAPTURE_STABILITY_ATTEMPTS;
      stabilityAttempt += 1
    ) {
      const editGeneration = this.#getEditGeneration();
      if (
        this.#persistence.lastError !== null ||
        this.#persistence.pendingBatchCount > 0
      ) {
        await this.#persistence.retry();
      }
      await this.#persistence.flush();
      const editor = this.#requireEditor();
      assertCanonicalCoworkEditorState(editor);
      const compacted = await this.#persistence.compact();

      // Everything below is captured from one synchronous editor/Y.Doc turn.
      // The generation/hash checks after asynchronous hashing prove it still
      // matches the compaction receipt before the immutable payload escapes.
      const range = this.#resolvePlanRange(editor, plan);
      const projection = coworkProjectionTarget(
        editor,
        this.#document,
        range,
      );
      const snapshot = Y.encodeStateAsUpdate(this.#document);
      const stateVector = Y.encodeStateVector(this.#document);
      const targetText = projection.target.markdownText;
      const [
        snapshotSha256,
        stateVectorSha256,
        projectionSha256,
        targetTextSha256,
      ] = await Promise.all([
        sha256Hex(snapshot),
        sha256Hex(stateVector),
        sha256Hex(new TextEncoder().encode(projection.markdown)),
        sha256Hex(new TextEncoder().encode(targetText)),
      ]);

      const stable =
        this.#getEditGeneration() === editGeneration &&
        snapshotSha256 === compacted.snapshotSha256 &&
        this.#persistence.docSha256 ===
          compacted.structuredHeadSha256;
      if (!stable) continue;
      if (this.#persistence.ydocGeneration.length === 0) {
        throw new Error("The document generation is unavailable");
      }

      const resolvedTarget: CoworkResolvedActionTarget = Object.freeze({
        source: plan.choice,
        label:
          range === null
            ? "Whole document"
            : coworkRangeSummary(
                editor.state.doc,
                range,
                plan.choice === "current_section"
                  ? "Current section"
                  : plan.choice === "current_selection"
                    ? "Current selection"
                    : plan.choice === "custom_range"
                      ? "Custom range"
                  : plan.reference?.label ?? "Selected passage",
              ).label,
        wordCount:
          range === null
            ? coworkWordCount(editor.state.doc, {
                from: 0,
                to: editor.state.doc.content.size,
              })
            : coworkWordCount(editor.state.doc, range),
        proseMirrorRange:
          range === null
            ? null
            : Object.freeze({ from: range.from, to: range.to }),
        selector: Object.freeze(projection.target.selector),
        targetTextSha256,
        ...(plan.reference === null
          ? {}
          : { targetReference: plan.reference }),
      });
      return Object.freeze({
        schema: "wb.cowork.action-snapshot/v1",
        captureId: randomCaptureId(),
        storeId: this.#storeId,
        documentId: this.#documentId,
        capturedAt: new Date().toISOString(),
        editGeneration,
        ydocGenerationSha256: this.#persistence.ydocGeneration,
        snapshotBase64: encodeCoworkBytes(snapshot),
        snapshotSha256,
        stateVectorBase64: encodeCoworkBytes(stateVector),
        stateVectorSha256,
        structuredHeadSha256: compacted.structuredHeadSha256,
        projectionMarkdown: projection.markdown,
        projectionSha256,
        target: resolvedTarget,
      });
    }
    throw new CoworkActionCaptureChangedError();
  }

  #resolvePlanRange(
    editor: Editor,
    plan: CapturePlan,
  ): CoworkIndexedRange | null {
    if (plan.reference === null) return null;
    const resolved = resolveCoworkDocumentTargetReference(
      editor,
      this.#document,
      plan.reference,
    );
    if (resolved === null) throw new CoworkActionTargetUnavailableError();
    return resolved.range;
  }

  #requireCollapsedCursor(message: string): Editor {
    const editor = this.#requireEditor();
    if (!editor.state.selection.empty) {
      throw new CoworkActionTargetUnavailableError(message);
    }
    return editor;
  }

  #exactRangeFromBoundary(
    editor: Editor,
    boundary: CoworkCursorBoundaryReference,
    label: string,
  ): CoworkIndexedRange {
    const from = resolveCoworkCursorBoundaryReference(
      editor,
      this.#document,
      boundary,
    );
    const to = editor.state.selection.head;
    if (from === null) {
      throw new CoworkActionTargetUnavailableError(
        `The ${label} start could not be resolved after the document changed.`,
      );
    }
    if (to <= from) {
      throw new CoworkActionTargetUnavailableError(
        `Set the ${label} end after its start.`,
      );
    }
    const range = coworkExactRange(editor.state.doc, { from, to });
    if (range === null || coworkRangeQuote(editor.state.doc, range) === null) {
      throw new CoworkActionTargetUnavailableError(
        `The ${label} boundaries do not contain document text.`,
      );
    }
    return range;
  }

  #requireEditor(): Editor {
    if (this.#editor === null || this.#editor.isDestroyed) {
      throw new Error("The document is still loading. Try again in a moment.");
    }
    return this.#editor;
  }

  #refresh(): void {
    const editor = this.#editor;
    if (editor === null || editor.isDestroyed) {
      this.#publish(loadingState());
      return;
    }
    const selectionRange = coworkSelectionRange(editor);
    const selection =
      selectionRange === null ||
      coworkRangeQuote(editor.state.doc, selectionRange) === null
        ? null
        : coworkRangeSummary(
            editor.state.doc,
            selectionRange,
            "Selected passage",
          );
    const sectionRange = coworkCurrentSectionRange(editor);
    const currentSection =
      sectionRange === null
        ? null
        : coworkRangeSummary(
            editor.state.doc,
            sectionRange,
            "Current section",
          );
    const resolved =
      this.#reference === null
        ? null
        : resolveCoworkDocumentTargetReference(
            editor,
            this.#document,
            this.#reference,
          );
    const resolvedWorkingStart =
      this.#workingStartBoundary === null
        ? null
        : resolveCoworkCursorBoundaryReference(
            editor,
            this.#document,
            this.#workingStartBoundary,
          );
    const resolvedCustomStart =
      this.#customStartBoundary === null
        ? null
        : resolveCoworkCursorBoundaryReference(
            editor,
            this.#document,
            this.#customStartBoundary,
          );
    const resolvedCustom =
      this.#customReference === null
        ? null
        : resolveCoworkDocumentTargetReference(
            editor,
            this.#document,
            this.#customReference,
          );
    this.#publish({
      phase: "ready",
      selection,
      currentSection,
      workingTargetStart:
        resolvedWorkingStart === null
          ? null
          : {
              position: resolvedWorkingStart,
              label: "Working on start",
            },
      customRangeStart:
        resolvedCustomStart === null
          ? null
          : {
              position: resolvedCustomStart,
              label: "Custom range start",
            },
      customRange:
        resolvedCustom === null
          ? null
          : coworkRangeSummary(
              editor.state.doc,
              resolvedCustom.range,
              "Custom range",
            ),
      workingTarget:
        this.#reference === null
          ? coworkWholeDocumentSummary(editor.state.doc)
          : resolved === null
            ? {
                kind: "unresolved",
                label: this.#reference.label,
                wordCount: 0,
                range: null,
                resolution: "unresolved",
              }
            : {
                ...coworkRangeSummary(
                  editor.state.doc,
                  resolved.range,
                  this.#reference.label,
                ),
                resolution: resolved.resolution,
              },
    });
  }

  #publish(next: CoworkActionSnapshotControllerState): void {
    this.#state = next;
    for (const listener of this.#listeners) listener();
  }
}
