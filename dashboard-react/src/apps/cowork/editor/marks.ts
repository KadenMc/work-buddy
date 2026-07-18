import { Mark, mergeAttributes } from "@tiptap/core";

/**
 * wb custom marks (section 6, "wb custom marks / attrs").
 *
 * Both marks declare `parseHTML: () => []`, so NO wb mark is ever reconstructible from
 * the clipboard or from imported HTML (paste-forgery defense, gate condition 2, PRD
 * risk A3). Import parses through MarkdownManager only, never the HTML parser, and all
 * suggestion / provenance / expression display re-derives from the ledger every render
 * (I12). SP-1 proved the default parseDOM mints forgeable marks and the hardened path
 * mints none, so these renderHTML-only marks carry the same posture.
 *
 * The three tracked-change suggestion marks (insertion / deletion / modification) are
 * NOT authored here: they come from the vendored suggestion engine (section 3), which
 * owns them end to end.
 */

/**
 * State 2 of the PRD section 7 trust tri-state: an "AI-written, human-confirmed" span.
 * Rendered with an accent token plus a left rule and a stamp (the redundant non-color
 * encoding required for forced-colors, section 5.4). The attrs carry the producer ref
 * and the approving gesture so provenance survives acceptance (I11).
 */
export const WbProvenanceTint = Mark.create({
  name: "wbProvenanceTint",
  inclusive: false,

  addAttributes() {
    return {
      producer: {
        default: null,
        rendered: true,
        renderHTML: (attributes) =>
          attributes.producer === null
            ? {}
            : { "data-producer": String(attributes.producer) },
      },
      approval_gesture_id: {
        default: null,
        rendered: true,
        renderHTML: (attributes) =>
          attributes.approval_gesture_id === null
            ? {}
            : { "data-approval-gesture-id": String(attributes.approval_gesture_id) },
      },
    };
  },

  // Paste-forgery defense: never reconstructible from clipboard or imported HTML.
  parseHTML() {
    return [];
  },

  renderHTML({ HTMLAttributes }) {
    return [
      "span",
      mergeAttributes(HTMLAttributes, {
        class: "wb-cowork-provenance-tint",
        "data-wb-trust": "ai-confirmed",
      }),
      0,
    ];
  },
});

/**
 * The claim-chip read-path scaffolding (section 6 item 3): a violet dashed-underline
 * span linking a passage to an existing claim, attrs { expression_id, claim_ref,
 * claim_status }. The frozen contract frames this as a read-only DECORATION the
 * review rail drives from ledger expression rows, never a user-authored stored mark.
 * This module provides the attr shape and the same `parseHTML: () => []` forgery defense,
 * so the wire attributes and the hardened parse posture are in place for the rail to
 * consume. The mark carries no input rules or commands, keeping it off the authoring
 * path in v1.
 */
export const WbExpressionMark = Mark.create({
  name: "wbExpressionMark",
  inclusive: false,

  addAttributes() {
    return {
      expression_id: {
        default: null,
        rendered: true,
        renderHTML: (attributes) =>
          attributes.expression_id === null
            ? {}
            : { "data-expression-id": String(attributes.expression_id) },
      },
      claim_ref: {
        default: null,
        rendered: true,
        renderHTML: (attributes) =>
          attributes.claim_ref === null
            ? {}
            : { "data-claim-ref": String(attributes.claim_ref) },
      },
      claim_status: {
        default: null,
        rendered: true,
        renderHTML: (attributes) =>
          attributes.claim_status === null
            ? {}
            : { "data-claim-status": String(attributes.claim_status) },
      },
    };
  },

  // Paste-forgery defense: never reconstructible from clipboard or imported HTML.
  parseHTML() {
    return [];
  },

  renderHTML({ HTMLAttributes }) {
    return [
      "span",
      mergeAttributes(HTMLAttributes, {
        class: "wb-cowork-expression-mark",
        "data-wb-expression": "claim-chip",
      }),
      0,
    ];
  },
});
