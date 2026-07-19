import * as Y from "yjs";
import { isChangeOrigin } from "@tiptap/extension-collaboration";
import type { Transaction } from "@tiptap/pm/state";

/**
 * The dedicated non-undo apply-origin tag (SP-2 load-order point 6, C3 v1 realization).
 *
 * Every mutation of the Y.Doc that is NOT a live human keystroke carries this origin:
 * proposal ingestion (local-only, never persisted), an accepted edit applied as the
 * client's own local transaction, and update batches pulled from the server. Because
 * the Collaboration undo manager only tracks the editor's own origin, an apply-origin
 * mutation stays OUT of the local undo stack, and the ProseMirror transaction it
 * produces reads as `isChangeOrigin(tr) === true`, so UniqueID does not re-mint ids on
 * it. Display state re-derives from the ledger every render, so a stray undo that
 * resurrects a mark self-corrects on the next paint. Never let an accept ride the
 * local undo stack.
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
 * Run a local, ledger-derived mutation (an accepted edit, a projected suggestion) inside
 * one Yjs transaction tagged with the apply-origin origin. This is the v1 accept path
 * (C3): the client mutates its OWN Y.Doc, because the server mints no Yjs bytes.
 */
export const applyWithOrigin = (doc: Y.Doc, mutate: () => void): void => {
  doc.transact(mutate, COWORK_APPLY_ORIGIN);
};

/**
 * True when a Yjs update event originated from a live human keystroke rather than from
 * an apply-origin mutation. The persistence layer pushes ONLY these updates through R4
 * (human direct edits only, section 1.4), never ledger-derived or pulled content.
 */
export const isLocalHumanOrigin = (origin: unknown): boolean =>
  origin !== COWORK_APPLY_ORIGIN;

/**
 * Re-export of the Collaboration change-origin predicate, so consumers depend on this
 * apply-origin module rather than reaching into the extension directly. A change-origin
 * ProseMirror transaction is an applied Yjs change (foreign or apply-origin), never a
 * live local edit (SP-2 F6.1/F6.2).
 */
export const isAppliedTransaction = (tr: Transaction): boolean => isChangeOrigin(tr);
