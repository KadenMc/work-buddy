import type { Editor } from "@tiptap/core";
import type { Command, Transaction } from "@tiptap/pm/state";
import type * as Y from "yjs";

import { applyForeignUpdate, applyWithOrigin } from "../editor/applyOrigin";
import {
  applySuggestion,
  getSuggestionMarks,
  revertSuggestion,
  transformToSuggestionTransaction,
} from "./engine";
import { resolveQuoteAnchor } from "./anchor";
import { stampAttribution } from "./attribution";
import {
  acceptAtomSuggestion,
  listOpenAtomSuggestions,
  revertAtomSuggestion,
  suggestAtomDeletion,
  suggestAtomInsertion,
} from "./atomTracking";
import type { AtomSuggestionKind, AtomSuggestionSpec } from "./atomTracking";
import { AdapterEventBus } from "./events";
import type {
  AdapterEvents,
  DecisionItem,
  ProposalInput,
  WbTrackedChangesAdapter,
} from "./types";

export interface WbTrackedChangesAdapterOptions {
  /**
   * The live Y.Doc bound to the editor's Collaboration extension. Required for the
   * apply-origin discipline (ingestion, accepts) and for applyServerUpdate. It MUST be
   * the same doc the editor is bound to, so a nested Yjs transaction keeps the
   * apply-origin tag. Omitted only for non-collaborative engine-behavior tests.
   */
  readonly doc?: Y.Doc;
}

/**
 * The WbTrackedChangesAdapter (C1 surface section 3), the seam between the ledger and the
 * vendored suggest-changes engine. The ledger is truth and the marks are a projection, so
 * a proposal projects into the LOCAL editor as an ephemeral suggestion layer that never
 * reaches the server Y.Doc (surface section 1.4). Every mutation this adapter makes is
 * tagged through the applyOrigin helpers, so it stays off the local undo stack and the
 * persistence layer never pushes it.
 *
 * Ingestion is transaction-level (transformToSuggestionTransaction with generateId set to
 * the kernel proposal_id), decisions are collected per id for the R5 sitting, and drift is
 * handled by re-anchoring on the quote. The adapter never mints a gesture, the R5 route
 * does.
 */
export class WbTrackedChangesAdapterImpl implements WbTrackedChangesAdapter {
  readonly #events = new AdapterEventBus();
  readonly #staged = new Map<string, DecisionItem>();
  readonly #ingested = new Map<string, ProposalInput>();
  readonly #doc: Y.Doc | undefined;
  #editor: Editor | null = null;

  constructor(options: WbTrackedChangesAdapterOptions = {}) {
    this.#doc = options.doc;
  }

  attach(editor: Editor): void {
    this.#editor = editor;
  }

  detach(): void {
    this.#editor = null;
    this.#staged.clear();
    this.#ingested.clear();
    this.#events.clear();
  }

  ingestProposal(p: ProposalInput): { anchored: boolean } {
    const editor = this.#editor;
    if (editor === null) return { anchored: false };

    const range = resolveQuoteAnchor(editor.state.doc, p.quoteAnchor);
    if (range === null) {
      this.#ingested.set(p.proposal_id, p);
      this.#events.emit("anchor:lost", { proposal_id: p.proposal_id });
      return { anchored: false };
    }

    this.#ingested.set(p.proposal_id, p);

    // A flag carries no replacement, so it anchors a span for commentary without minting
    // any tracked-change mark. The Review rail renders flags from R2, not from marks.
    if (p.kind === "flag" || p.replacement === null) {
      return { anchored: true };
    }

    const plain = editor.state.tr;
    if (p.replacement.length > 0) {
      plain.replaceWith(range.from, range.to, editor.state.schema.text(p.replacement));
    } else {
      plain.delete(range.from, range.to);
    }

    const tracked = transformToSuggestionTransaction(plain, editor.state, () => p.proposal_id);
    stampAttribution(tracked, p.proposal_id, p.attrs.producer, p.attrs.epistemic);
    this.#dispatchApplyOrigin(tracked);
    this.#events.emit("proposals:changed", { open: this.listOpen() });
    return { anchored: true };
  }

  reanchor(proposalId: string): { from: number; to: number } | null {
    const editor = this.#editor;
    const proposal = this.#ingested.get(proposalId);
    if (editor === null || proposal === undefined) return null;

    const range = resolveQuoteAnchor(editor.state.doc, proposal.quoteAnchor);
    if (range === null) {
      this.#events.emit("anchor:lost", { proposal_id: proposalId });
      return null;
    }
    this.#events.emit("anchor:reanchored", {
      proposal_id: proposalId,
      from: range.from,
      to: range.to,
    });
    return range;
  }

  stageDecision(item: DecisionItem): void {
    this.#staged.set(item.proposal_id, item);
    this.#events.emit("decision:staged", { item });
  }

  clearDecision(proposalId: string): void {
    this.#staged.delete(proposalId);
    this.#events.emit("decision:cleared", { proposal_id: proposalId });
  }

  collectSitting(): DecisionItem[] {
    return [...this.#staged.values()];
  }

  listOpen(): string[] {
    const editor = this.#editor;
    if (editor === null) return [];
    const { insertion, deletion, modification } = getSuggestionMarks(editor.state.schema);
    const ids = new Set<string>();
    editor.state.doc.descendants((node) => {
      for (const mark of node.marks) {
        if (mark.type === insertion || mark.type === deletion || mark.type === modification) {
          ids.add(String(mark.attrs["id"]));
        }
      }
      return true;
    });
    for (const id of listOpenAtomSuggestions(editor.state.doc)) {
      ids.add(id);
    }
    return [...ids];
  }

  applyServerUpdate(update: Uint8Array): void {
    if (this.#doc === undefined) {
      throw new Error("applyServerUpdate requires a Y.Doc bound to the adapter");
    }
    applyForeignUpdate(this.#doc, update);
  }

  on<K extends keyof AdapterEvents>(ev: K, cb: (payload: AdapterEvents[K]) => void): () => void {
    return this.#events.on(ev, cb);
  }

  /**
   * Commit-time application of one collected decision to the doc, run by the sitting
   * client just before block-splice and the R5 POST. Accept keeps the inserted text and
   * removes the marks, Reject removes the inserted text and restores the original, and the
   * routing verbs (redirect, defer, endorse) leave the marks in place so the proposal
   * stays open. edit_confirm accepts the tracked edit here, and its verbatim amend_content
   * is applied during materialization. The engine op runs under the apply-origin tag, so
   * the accepted content stays off the local undo stack (surface section 1.4, SP-2 6).
   */
  applyDecision(item: DecisionItem): void {
    const editor = this.#editor;
    if (editor === null) return;

    // Each accept / reject runs both the mark path and the atom node-attribute path for the
    // same id. A proposal is one or the other, so the path that finds nothing is a no-op.
    let commands: Command[] = [];
    switch (item.verb) {
      case "confirm":
      case "edit_confirm":
        commands = [applySuggestion(item.proposal_id), acceptAtomSuggestion(item.proposal_id)];
        break;
      case "reject_plain":
      case "reject_as_false":
      case "reject_as_preference":
      case "dismiss":
        commands = [revertSuggestion(item.proposal_id), revertAtomSuggestion(item.proposal_id)];
        break;
      case "redirect":
      case "defer":
      case "endorse":
        commands = [];
        break;
    }
    if (commands.length === 0) return;

    this.#applyCommandsApplyOrigin(commands);
    this.#events.emit("proposals:changed", { open: this.listOpen() });
  }

  /**
   * Project an atom (horizontal rule, image) suggestion at a known node position, tracked
   * as a node attribute rather than a mark (SP-1 fork delta 5). The caller supplies the
   * position because an atom carries no text to quote-anchor. Applied under the apply-origin
   * tag like every other projection.
   */
  ingestAtomSuggestion(
    pos: number,
    kind: AtomSuggestionKind,
    spec: AtomSuggestionSpec,
  ): { anchored: boolean } {
    const editor = this.#editor;
    if (editor === null) return { anchored: false };
    const command =
      kind === "deletion" ? suggestAtomDeletion(pos, spec) : suggestAtomInsertion(pos, spec);
    const anchored = command(editor.state, (tr) => {
      if (this.#doc !== undefined) {
        applyWithOrigin(this.#doc, () => {
          editor.view.dispatch(tr);
        });
      } else {
        editor.view.dispatch(tr);
      }
    });
    if (anchored) {
      this.#events.emit("proposals:changed", { open: this.listOpen() });
    }
    return { anchored };
  }

  /** Dispatch a prepared transaction through the editor under the apply-origin tag. */
  #dispatchApplyOrigin(tr: Transaction): void {
    const editor = this.#editor;
    if (editor === null) return;
    if (this.#doc !== undefined) {
      applyWithOrigin(this.#doc, () => {
        editor.view.dispatch(tr);
      });
    } else {
      editor.view.dispatch(tr);
    }
  }

  /** Run engine commands in sequence, all dispatched under one apply-origin transaction. */
  #applyCommandsApplyOrigin(commands: Command[]): void {
    const editor = this.#editor;
    if (editor === null) return;
    const run = (): void => {
      for (const command of commands) {
        command(editor.state, (tr) => {
          editor.view.dispatch(tr);
        });
      }
    };
    if (this.#doc !== undefined) {
      applyWithOrigin(this.#doc, run);
    } else {
      run();
    }
  }
}

/** Construct an adapter behind the frozen interface (the engine stays swappable). */
export const createWbTrackedChangesAdapter = (
  options?: WbTrackedChangesAdapterOptions,
): WbTrackedChangesAdapterImpl => new WbTrackedChangesAdapterImpl(options);
