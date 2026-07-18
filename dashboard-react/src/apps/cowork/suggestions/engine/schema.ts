/**
 * Vendored from @handlewithcare/prosemirror-suggest-changes v0.1.8 (MIT).
 * Upstream https://github.com/handlewithcarecollective/prosemirror-suggest-changes
 * See the LICENSE and PROVENANCE.md files alongside this source.
 *
 * Modifications in this file:
 * 1. Import specifiers rewritten to @tiptap/pm/* (single hoisted ProseMirror instance)
 *    and relative .js extensions dropped for the dashboard-react bundler resolution.
 * 2. Attribution attrs added to the three suggestion mark specs (producer, epistemic),
 *    so provenance survives acceptance (SP-1 fork delta 2, C1 surface section 3). The
 *    engine grouping key stays `id`, which the adapter injects with the kernel
 *    proposal_id via generateId, so the mark id IS the proposal_id.
 * 3. parseDOM stripped to [] on every suggestion mark (paste-forgery hardening, SP-1
 *    fork delta 3, gate condition 2). No suggestion mark is reconstructible from
 *    clipboard or imported HTML, and display re-derives from the ledger every render.
 *    The live Tiptap marks in ../marks.ts carry the same posture. These raw MarkSpec
 *    objects are retained for upstream parity and are consumed only through the index
 *    re-export, since the running schema is built from the Tiptap wrappers.
 */

import { type MarkSpec } from "@tiptap/pm/model";
import { suggestionIdValidate } from "./generateId";

/** Attribution attrs shared by the three suggestion marks (SP-1 fork delta 2). */
const attributionAttrs = {
  producer: { default: null, validate: "string|null" },
  epistemic: { default: "ai_proposed", validate: "string" },
} as const;

export const deletion: MarkSpec = {
  inclusive: false,
  excludes: "insertion modification deletion",
  attrs: {
    id: { validate: suggestionIdValidate },
    ...attributionAttrs,
  },
  toDOM(mark, inline) {
    return [
      "del",
      {
        "data-id": JSON.stringify(mark.attrs["id"]),
        "data-inline": String(inline),
        "data-producer": mark.attrs["producer"] as string | null,
        "data-epistemic": mark.attrs["epistemic"] as string,
        ...(!inline && { style: "display: block" }),
      },
      0,
    ];
  },
  // Paste-forgery hardening: never reconstructible from clipboard or imported HTML.
  parseDOM: [],
};

export const insertion: MarkSpec = {
  inclusive: false,
  excludes: "deletion modification insertion",
  attrs: {
    id: { validate: suggestionIdValidate },
    ...attributionAttrs,
  },
  toDOM(mark, inline) {
    return [
      "ins",
      {
        "data-id": JSON.stringify(mark.attrs["id"]),
        "data-inline": String(inline),
        "data-producer": mark.attrs["producer"] as string | null,
        "data-epistemic": mark.attrs["epistemic"] as string,
        ...(!inline && { style: "display: block" }),
      },
      0,
    ];
  },
  // Paste-forgery hardening: never reconstructible from clipboard or imported HTML.
  parseDOM: [],
};

export const modification: MarkSpec = {
  inclusive: false,
  excludes: "deletion insertion",
  attrs: {
    id: { validate: suggestionIdValidate },
    type: { validate: "string" },
    attrName: { default: null, validate: "string|null" },
    previousValue: { default: null },
    newValue: { default: null },
    ...attributionAttrs,
  },
  toDOM(mark, inline) {
    return [
      inline ? "span" : "div",
      {
        "data-type": "modification",
        "data-id": JSON.stringify(mark.attrs["id"]),
        "data-mod-type": mark.attrs["type"] as string,
        "data-mod-prev-val": JSON.stringify(mark.attrs["previousValue"]),
        "data-mod-new-val": JSON.stringify(mark.attrs["newValue"]),
        "data-producer": mark.attrs["producer"] as string | null,
        "data-epistemic": mark.attrs["epistemic"] as string,
      },
      0,
    ];
  },
  // Paste-forgery hardening: never reconstructible from clipboard or imported HTML.
  parseDOM: [],
};

/**
 * Add the deletion, insertion, and modification marks to
 * the provided MarkSpec map.
 */
export function addSuggestionMarks<Marks extends string>(
  marks: Record<Marks, MarkSpec>,
): Record<Marks | "deletion" | "insertion" | "modification", MarkSpec> {
  return {
    ...marks,
    deletion,
    insertion,
    modification,
  };
}
