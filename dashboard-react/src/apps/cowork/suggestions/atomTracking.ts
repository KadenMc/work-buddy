import HorizontalRule from "@tiptap/extension-horizontal-rule";
import Image from "@tiptap/extension-image";
import type { Node } from "@tiptap/pm/model";
import type { Command } from "@tiptap/pm/state";

import type { EpistemicState } from "./types";

/**
 * Atom node-attribute tracking (SP-1 fork delta 5). Atom nodes (horizontal rule, image)
 * cannot carry marks, so the vendored engine threw when a proposal targeted one. Marks are
 * the wrong tool for a leaf node, so a suggestion on an atom is tracked as a NODE ATTRIBUTE
 * instead, mirroring the paid product's suggestionId node attrs.
 *
 * Scope note. This module implements the tracking MECHANISM and the accept / reject
 * resolution, and it is exercised directly against a node position. Automatic routing from
 * a quote-anchored ProposalInput to this path is deferred to the S2 wave, because an atom
 * carries no text and so has no quote to anchor, which needs the node_id_hint or a
 * position-based anchor the ingestProposal text path does not have (C1 surface section 3
 * pre-routing note, gate condition 3). The adapter exposes ingestAtomSuggestion for a
 * caller that already holds the atom position.
 */

export const WB_ATOM_SUGGESTION_ATTR = "wbSuggestion";

export type AtomSuggestionKind = "insertion" | "deletion";

export interface AtomSuggestionSpec {
  readonly proposal_id: string;
  readonly producer: string;
  readonly epistemic: EpistemicState;
}

interface AtomSuggestionValue {
  readonly id: string;
  readonly type: AtomSuggestionKind;
  readonly producer: string;
  readonly epistemic: EpistemicState;
}

const readSuggestion = (node: Node): AtomSuggestionValue | null => {
  const raw = node.attrs[WB_ATOM_SUGGESTION_ATTR] as AtomSuggestionValue | null | undefined;
  return raw ?? null;
};

/**
 * The node attribute the cowork atom nodes carry. It never parses from the DOM, so a paste
 * cannot forge a suggestion (gate condition 2), and it renders as a data attribute for
 * styling only. Display re-derives from the ledger every render (I12).
 */
const suggestionNodeAttr = {
  [WB_ATOM_SUGGESTION_ATTR]: {
    default: null,
    rendered: true,
    parseHTML: (): null => null,
    renderHTML: (attributes: Record<string, unknown>) =>
      attributes[WB_ATOM_SUGGESTION_ATTR] === null ||
      attributes[WB_ATOM_SUGGESTION_ATTR] === undefined
        ? {}
        : { "data-wb-atom-suggestion": JSON.stringify(attributes[WB_ATOM_SUGGESTION_ATTR]) },
  },
};

/** Horizontal rule that can carry an atom suggestion (the cowork bundle seam). */
export const CoworkHorizontalRule = HorizontalRule.extend({
  addAttributes() {
    return { ...(this.parent?.() ?? {}), ...suggestionNodeAttr };
  },
});

/** Image that can carry an atom suggestion (the cowork bundle seam). */
export const CoworkImage = Image.extend({
  addAttributes() {
    return { ...(this.parent?.() ?? {}), ...suggestionNodeAttr };
  },
});

const setAtomSuggestion = (
  pos: number,
  type: AtomSuggestionKind,
  spec: AtomSuggestionSpec,
): Command => {
  return (state, dispatch) => {
    const node = state.doc.nodeAt(pos);
    if (node === null || !node.type.isAtom) return false;
    if (dispatch) {
      const value: AtomSuggestionValue = {
        id: spec.proposal_id,
        type,
        producer: spec.producer,
        epistemic: spec.epistemic,
      };
      dispatch(state.tr.setNodeAttribute(pos, WB_ATOM_SUGGESTION_ATTR, value));
    }
    return true;
  };
};

/** Track a proposed deletion of the atom at pos as a node attribute. */
export const suggestAtomDeletion = (pos: number, spec: AtomSuggestionSpec): Command =>
  setAtomSuggestion(pos, "deletion", spec);

/** Track a proposed insertion of the atom at pos as a node attribute. */
export const suggestAtomInsertion = (pos: number, spec: AtomSuggestionSpec): Command =>
  setAtomSuggestion(pos, "insertion", spec);

interface AtomTarget {
  readonly pos: number;
  readonly size: number;
  readonly type: AtomSuggestionKind;
}

const collectTargets = (doc: Node, id: string): AtomTarget[] => {
  const targets: AtomTarget[] = [];
  doc.descendants((node, pos) => {
    const suggestion = readSuggestion(node);
    if (suggestion !== null && suggestion.id === id) {
      targets.push({ pos, size: node.nodeSize, type: suggestion.type });
    }
    return true;
  });
  return targets;
};

/**
 * Accept every atom suggestion for id. A tracked deletion removes the node, a tracked
 * insertion clears the attribute and keeps the node. Targets apply in reverse document
 * order so a delete never shifts an earlier position.
 */
export const acceptAtomSuggestion = (id: string): Command => {
  return (state, dispatch) => {
    const targets = collectTargets(state.doc, id);
    if (targets.length === 0) return false;
    if (dispatch) {
      const tr = state.tr;
      for (const target of [...targets].reverse()) {
        if (target.type === "deletion") {
          tr.delete(target.pos, target.pos + target.size);
        } else {
          tr.setNodeAttribute(target.pos, WB_ATOM_SUGGESTION_ATTR, null);
        }
      }
      dispatch(tr);
    }
    return true;
  };
};

/**
 * Revert every atom suggestion for id. A tracked deletion clears the attribute and keeps
 * the node, a tracked insertion removes the node (it was never really there).
 */
export const revertAtomSuggestion = (id: string): Command => {
  return (state, dispatch) => {
    const targets = collectTargets(state.doc, id);
    if (targets.length === 0) return false;
    if (dispatch) {
      const tr = state.tr;
      for (const target of [...targets].reverse()) {
        if (target.type === "insertion") {
          tr.delete(target.pos, target.pos + target.size);
        } else {
          tr.setNodeAttribute(target.pos, WB_ATOM_SUGGESTION_ATTR, null);
        }
      }
      dispatch(tr);
    }
    return true;
  };
};

/** Distinct ids of atoms currently carrying a tracked suggestion. */
export const listOpenAtomSuggestions = (doc: Node): string[] => {
  const ids = new Set<string>();
  doc.descendants((node) => {
    const suggestion = readSuggestion(node);
    if (suggestion !== null) ids.add(suggestion.id);
    return true;
  });
  return [...ids];
};
