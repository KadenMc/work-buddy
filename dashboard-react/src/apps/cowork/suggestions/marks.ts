import { Mark, mergeAttributes } from "@tiptap/core";

/**
 * The three live tracked-change marks (insertion, deletion, modification), authored as
 * Tiptap Mark wrappers so the running schema exposes schema.marks.insertion and the
 * vendored engine's getSuggestionMarks resolves them (SP-1 fork delta 1). They mirror the
 * vendored MarkSpec shape but carry the forked attribution attrs and the paste-forgery
 * posture directly.
 *
 * The engine grouping key is `id`, which the adapter injects with the kernel proposal_id
 * through generateId, so the mark id IS the proposal_id. `producer` and `epistemic` are
 * the forked attribution attrs, stamped onto the tracked range after ingestion so
 * provenance survives acceptance (I11). Every mark declares parseHTML: () => [], so no
 * suggestion mark is reconstructible from clipboard or imported HTML (gate condition 2,
 * PRD risk A3), and all suggestion display re-derives from the ledger every render (I12).
 */

const idAttribute = {
  id: {
    default: null,
    rendered: true,
    renderHTML: (attributes: Record<string, unknown>) =>
      attributes["id"] === null || attributes["id"] === undefined
        ? {}
        : { "data-id": JSON.stringify(attributes["id"]) },
  },
};

const attributionAttributes = {
  producer: {
    default: null,
    rendered: true,
    renderHTML: (attributes: Record<string, unknown>) =>
      attributes["producer"] === null || attributes["producer"] === undefined
        ? {}
        : { "data-producer": String(attributes["producer"]) },
  },
  epistemic: {
    default: "ai_proposed",
    rendered: true,
    renderHTML: (attributes: Record<string, unknown>) =>
      attributes["epistemic"] === null || attributes["epistemic"] === undefined
        ? {}
        : { "data-epistemic": String(attributes["epistemic"]) },
  },
};

/** An inserted span proposed by an agent (PRD section 7 ai_proposed insertion). */
export const SuggestionInsertion = Mark.create({
  name: "insertion",
  inclusive: false,
  excludes: "deletion modification insertion",

  addAttributes() {
    return { ...idAttribute, ...attributionAttributes };
  },

  // Paste-forgery hardening: never reconstructible from clipboard or imported HTML.
  parseHTML() {
    return [];
  },

  renderHTML({ HTMLAttributes }) {
    return [
      "ins",
      mergeAttributes(HTMLAttributes, {
        class: "wb-cowork-suggestion wb-cowork-suggestion--insertion",
        "data-wb-suggestion": "insertion",
      }),
      0,
    ];
  },
});

/** A deleted span proposed by an agent, kept in the doc with a deletion mark. */
export const SuggestionDeletion = Mark.create({
  name: "deletion",
  inclusive: false,
  excludes: "insertion modification deletion",

  addAttributes() {
    return { ...idAttribute, ...attributionAttributes };
  },

  // Paste-forgery hardening: never reconstructible from clipboard or imported HTML.
  parseHTML() {
    return [];
  },

  renderHTML({ HTMLAttributes }) {
    return [
      "del",
      mergeAttributes(HTMLAttributes, {
        class: "wb-cowork-suggestion wb-cowork-suggestion--deletion",
        "data-wb-suggestion": "deletion",
      }),
      0,
    ];
  },
});

/** A node-attribute or mark change proposed by an agent (modification tracking). */
export const SuggestionModification = Mark.create({
  name: "modification",
  inclusive: false,
  excludes: "deletion insertion",

  addAttributes() {
    return {
      ...idAttribute,
      type: { default: null },
      attrName: { default: null },
      previousValue: { default: null },
      newValue: { default: null },
      ...attributionAttributes,
    };
  },

  // Paste-forgery hardening: never reconstructible from clipboard or imported HTML.
  parseHTML() {
    return [];
  },

  renderHTML({ HTMLAttributes }) {
    return [
      "span",
      mergeAttributes(HTMLAttributes, {
        class: "wb-cowork-suggestion wb-cowork-suggestion--modification",
        "data-wb-suggestion": "modification",
      }),
      0,
    ];
  },
});

/** The three suggestion marks as one array for the registration seam. */
export const suggestionMarks = [
  SuggestionInsertion,
  SuggestionDeletion,
  SuggestionModification,
];
