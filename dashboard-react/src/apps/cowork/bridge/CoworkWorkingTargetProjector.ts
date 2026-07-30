import type { Editor } from "@tiptap/core";

import {
  clearCoworkWorkingTarget,
  projectCoworkWorkingTarget,
  projectCoworkWorkingTargetStart,
} from "../editor/workingTargetDecorations";
import type { CoworkActionSnapshotController } from "../targets/contracts";

/**
 * Projects the controller's resolved Working on range into its dedicated
 * editor decoration channel. Controller state remains authoritative; the
 * plugin stores only disposable view geometry.
 */
export class CoworkWorkingTargetProjector {
  readonly #controller: CoworkActionSnapshotController;
  #editor: Editor | null = null;
  #unsubscribe: (() => void) | null = null;
  #fingerprint: string | null = null;

  constructor(controller: CoworkActionSnapshotController) {
    this.#controller = controller;
  }

  attach(editor: Editor): void {
    if (this.#editor === editor) return;
    this.detach();
    this.#editor = editor;
    this.#unsubscribe = this.#controller.subscribe(this.#sync);
    this.#sync();
  }

  detach(): void {
    this.#unsubscribe?.();
    this.#unsubscribe = null;
    const editor = this.#editor;
    this.#editor = null;
    this.#fingerprint = null;
    if (editor !== null && !editor.isDestroyed) {
      clearCoworkWorkingTarget(editor);
    }
  }

  dispose(): void {
    this.detach();
  }

  readonly #sync = (): void => {
    const editor = this.#editor;
    if (editor === null || editor.isDestroyed) return;
    const state = this.#controller.getSnapshot();
    const target = state.workingTarget;
    const provisionalStart =
      state.phase === "ready" ? state.workingTargetStart ?? null : null;
    const range =
      state.phase === "ready" &&
      provisionalStart === null &&
      target.kind === "text_range"
        ? target.range
        : null;
    const fingerprint =
      provisionalStart !== null
        ? `provisional-start:${String(provisionalStart.position)}:${provisionalStart.label}`
        : range === null
        ? "clear"
        : `${String(range.from)}:${String(range.to)}:${target.label}`;
    if (fingerprint === this.#fingerprint) return;

    // Set before dispatch: a decoration-only transaction causes the attached
    // controller to publish once more, and that reentrant notification must
    // observe the already-projected identity.
    this.#fingerprint = fingerprint;
    if (provisionalStart !== null) {
      projectCoworkWorkingTargetStart(editor, provisionalStart);
      return;
    }
    if (range === null) {
      clearCoworkWorkingTarget(editor);
      return;
    }
    projectCoworkWorkingTarget(editor, {
      from: range.from,
      to: range.to,
      label: target.label,
    });
  };
}
