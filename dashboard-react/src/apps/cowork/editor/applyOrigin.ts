import * as Y from "yjs";
import { isChangeOrigin } from "@tiptap/extension-collaboration";
import { ySyncPluginKey } from "@tiptap/y-tiptap";
import type { Transaction } from "@tiptap/pm/state";

/**
 * The dedicated non-undo apply-origin tag (SP-2 load-order point 6, C3 v1 realization).
 *
 * Foreign updates applied to the live collaborative Y.Doc, plus mutations made inside
 * an isolated materialization or migration document, carry this origin. Because the
 * Collaboration undo manager only tracks the editor's own origin, those mutations stay
 * OUT of the local undo stack. Their ProseMirror transactions also read as
 * `isChangeOrigin(tr) === true`, so UniqueID does not re-mint ids on them.
 *
 * Origin filtering is not persistence isolation. A later human edit can causally depend
 * on any prior Yjs mutation even when that prior update was filtered from the outbox.
 * Pending review items therefore render as ProseMirror decorations and MUST NOT mutate
 * the live Y.Doc. Accepted decisions materialize only in a clean canonical clone.
 *
 * The constant is a unique frozen object so no other origin can collide with it.
 */
export const COWORK_APPLY_ORIGIN: unique symbol = Symbol("wb.cowork.apply-origin");

export type CoworkApplyOrigin = typeof COWORK_APPLY_ORIGIN;

/**
 * Apply an opaque foreign update (a pulled R3 batch or snapshot) to the Y.Doc under the
 * apply-origin tag, so it never enters the local undo stack and reads as change-origin.
 */
export const applyForeignUpdate = (doc: Y.Doc, update: Uint8Array): void => {
  Y.applyUpdate(doc, update, COWORK_APPLY_ORIGIN);
};

/**
 * Run a non-human mutation inside one Yjs transaction tagged with the apply-origin
 * origin. On Co-work's review path this helper is restricted to isolated materialization
 * and migration documents. It MUST NOT project pending review items into the live
 * collaborative Y.Doc.
 */
export const applyWithOrigin = (doc: Y.Doc, mutate: () => void): void => {
  doc.transact(mutate, COWORK_APPLY_ORIGIN);
};

/**
 * True when a Yjs update event originated from a live human keystroke. The y-tiptap
 * Collaboration binding syncs ProseMirror edits into the Y.Doc under the ySyncPluginKey
 * origin (its `_prosemirrorChanged` transaction), so that origin is the positive signal.
 * This is an allowlist rather than an exclusion: the apply-origin tag, a pulled foreign
 * update, and any bare `doc.transact(fn)` that omits an origin all read as non-human, so
 * only genuine human edits are pushed through R4 (human direct edits only, section 1.4).
 */
export const isLocalHumanOrigin = (origin: unknown): boolean =>
  origin === ySyncPluginKey;

/**
 * Re-export of the Collaboration change-origin predicate, so consumers depend on this
 * apply-origin module rather than reaching into the extension directly. A change-origin
 * ProseMirror transaction is an applied Yjs change (foreign or apply-origin), never a
 * live local edit (SP-2 F6.1/F6.2).
 */
export const isAppliedTransaction = (tr: Transaction): boolean => isChangeOrigin(tr);
