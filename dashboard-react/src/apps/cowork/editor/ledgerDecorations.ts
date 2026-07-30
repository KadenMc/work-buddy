import { Extension, type Editor } from "@tiptap/core";
import type { Node as ProseMirrorNode } from "@tiptap/pm/model";
import { Plugin, PluginKey, type Transaction } from "@tiptap/pm/state";
import { Decoration, DecorationSet } from "@tiptap/pm/view";

import { resolveQuoteAnchor } from "../suggestions/anchor";
import type { QuoteAnchor } from "../suggestions/types";

/**
 * The editor-visible namespaces used by review geometry and passage navigation.
 * Review owns proposal and claim focus. Expression and provenance identities remain
 * available in the DOM for inspection without collapsing those namespaces together.
 */
export type CoworkEditorAnchorKind =
  | "proposal"
  | "claim"
  | "expression"
  | "provenance"
  | "evaluation_result"
  | "passage";

export interface CoworkFlagDecoration {
  readonly proposalId: string;
  readonly quoteAnchor: QuoteAnchor;
}

export interface CoworkEditDecoration {
  readonly proposalId: string;
  readonly quoteAnchor: QuoteAnchor;
  readonly replacement: string;
  readonly changeType: "insertion" | "deletion" | "modification";
}

export interface CoworkExpressionDecoration {
  readonly expressionId: string;
  readonly spanId: string;
  readonly quote: string;
  readonly quoteAnchor?: QuoteAnchor;
  readonly claimRef: string;
  readonly claimStatus: string | null;
}

export interface CoworkClaimDecoration {
  readonly claimId: string;
  readonly expressionId: string;
  readonly spanId: string;
  readonly quote: string;
}

export interface CoworkProvenanceDecoration {
  readonly spanId: string;
  readonly quote: string;
  readonly quoteAnchor?: QuoteAnchor;
  readonly trustState: "human" | "ai_confirmed" | "ai_proposed";
  readonly producer: string | null;
  readonly approvalGestureId: string | null;
}

export interface CoworkEvaluationDecoration {
  readonly resultId: string;
  readonly quoteAnchor: QuoteAnchor;
  readonly resultKind:
    | "conforming"
    | "nonconforming"
    | "inconclusive"
    | "review_comment";
}

/**
 * One ledger pull projected into the editor. These records are deliberately data-only:
 * the plugin resolves them against the current ProseMirror document and creates
 * decorations, never schema marks or document/Yjs content.
 */
export interface CoworkLedgerDecorationProjection {
  readonly edits: readonly CoworkEditDecoration[];
  readonly flags: readonly CoworkFlagDecoration[];
  readonly expressions: readonly CoworkExpressionDecoration[];
  readonly claims: readonly CoworkClaimDecoration[];
  readonly provenance: readonly CoworkProvenanceDecoration[];
  /** Additive Verify projection; absent inputs are treated as no results. */
  readonly evaluations?: readonly CoworkEvaluationDecoration[];
}

export interface CoworkFocusedAnchor {
  readonly id: string;
  readonly kind: "proposal" | "claim" | "evaluation_result";
}

export interface CoworkPassageHighlight {
  readonly id: string;
  readonly from: number;
  readonly to: number;
}

interface CoworkLedgerDecorationState {
  readonly projection: CoworkLedgerDecorationProjection;
  readonly focused: CoworkFocusedAnchor | null;
  readonly flashFocused: boolean;
  readonly highlight: CoworkPassageHighlight | null;
  readonly decorations: DecorationSet;
}

type CoworkLedgerDecorationMeta =
  | {
      readonly type: "project";
      readonly projection: CoworkLedgerDecorationProjection;
    }
  | {
      readonly type: "focus";
      readonly focused: CoworkFocusedAnchor | null;
      readonly flash: boolean;
    }
  | {
      readonly type: "set-focus-flash";
      readonly flash: boolean;
    }
  | {
      readonly type: "highlight";
      readonly highlight: CoworkPassageHighlight;
    }
  | {
      readonly type: "clear-highlight";
      readonly id?: string;
    };

const EMPTY_PROJECTION: CoworkLedgerDecorationProjection = Object.freeze({
  edits: Object.freeze([]),
  flags: Object.freeze([]),
  expressions: Object.freeze([]),
  claims: Object.freeze([]),
  provenance: Object.freeze([]),
  evaluations: Object.freeze([]),
});

export const coworkLedgerDecorationsKey =
  new PluginKey<CoworkLedgerDecorationState>("coworkLedgerDecorations");

const rangeForQuote = (
  doc: ProseMirrorNode,
  exact: string,
  quoteAnchor?: QuoteAnchor,
): { readonly from: number; readonly to: number } | null =>
  resolveQuoteAnchor(
    doc,
    quoteAnchor ?? { exact, prefix: "", suffix: "" },
  );

const anchorAttributes = (
  kind: CoworkEditorAnchorKind,
  id: string,
  baseClass: string,
  focused: CoworkFocusedAnchor | null,
  flashFocused: boolean,
  extra: Readonly<Record<string, string>> = {},
): Record<string, string> => {
  const active =
    (kind === "proposal" ||
      kind === "claim" ||
      kind === "evaluation_result") &&
    focused?.kind === kind &&
    focused.id === id;
  return {
    class: [
      "wb-cowork-ledger-decoration",
      "wb-cowork-anchor",
      baseClass,
      active ? "wb-cowork-anchor--active" : "",
      active && flashFocused ? "wb-cowork-anchor--flash" : "",
    ]
      .filter(Boolean)
      .join(" "),
    "data-wb-anchor-kind": kind,
    "data-wb-anchor-id": id,
    ...extra,
  };
};

const inlineDecoration = (
  from: number,
  to: number,
  attributes: Record<string, string>,
  key: string,
): Decoration | null =>
  from < to
    ? Decoration.inline(from, to, attributes, {
        inclusiveStart: false,
        inclusiveEnd: false,
        key,
      })
    : null;

const proposalWidget = (
  pos: number,
  text: string,
  attributes: Record<string, string>,
  key: string,
  side: -1 | 1,
): Decoration | null => {
  if (text.length === 0) return null;
  return Decoration.widget(
    pos,
    () => {
      const element = document.createElement("span");
      for (const [name, value] of Object.entries(attributes)) {
        element.setAttribute(name, value);
      }
      element.setAttribute("contenteditable", "false");
      element.textContent = text;
      return element;
    },
    // The class list carries persistent/flash focus. Include it in the widget
    // identity so ProseMirror does not reuse stale DOM after a focus rebuild.
    { key: `${key}:${attributes["class"] ?? ""}`, side },
  );
};

const suggestionId = (raw: unknown): string | null =>
  typeof raw === "string" && raw.length > 0 ? raw : null;

const atomSuggestion = (
  raw: unknown,
): { readonly id: string; readonly type: string } | null => {
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) return null;
  const value = raw as Record<string, unknown>;
  return typeof value["id"] === "string" && typeof value["type"] === "string"
    ? { id: value["id"], type: value["type"] }
    : null;
};

function buildDecorations(
  doc: ProseMirrorNode,
  projection: CoworkLedgerDecorationProjection,
  focused: CoworkFocusedAnchor | null,
  flashFocused: boolean,
  highlight: CoworkPassageHighlight | null,
): DecorationSet {
  const decorations: Decoration[] = [];
  const claimsByExpression = new Map<string, CoworkClaimDecoration[]>();
  for (const claim of projection.claims) {
    const claims = claimsByExpression.get(claim.expressionId) ?? [];
    claims.push(claim);
    claimsByExpression.set(claim.expressionId, claims);
  }

  /*
   * Open edit proposals are pure view state. Existing prose is decorated in
   * place and proposed text is a non-editable widget; neither operation changes
   * the ProseMirror document or its collaborative Y.Doc.
   */
  for (const edit of projection.edits) {
    const range = resolveQuoteAnchor(doc, edit.quoteAnchor);
    if (range === null) continue;
    const common = {
      "data-wb-decoration": "edit-proposal",
      "data-wb-proposal-kind": "edit",
      "data-wb-change-type": edit.changeType,
    };
    const originalClass =
      edit.changeType === "insertion"
        ? "wb-cowork-suggestion--insertion"
        : "wb-cowork-suggestion--deletion";
    const original = inlineDecoration(
      range.from,
      range.to,
      anchorAttributes(
        "proposal",
        edit.proposalId,
        `wb-cowork-proposal-anchor ${originalClass}`,
        focused,
        flashFocused,
        common,
      ),
      `edit-original:${edit.proposalId}`,
    );
    if (original !== null) decorations.push(original);

    if (edit.changeType === "deletion") continue;
    const replacementAttributes = anchorAttributes(
      "proposal",
      edit.proposalId,
      [
        "wb-cowork-proposal-anchor",
        "wb-cowork-suggestion-widget",
        edit.changeType === "insertion"
          ? "wb-cowork-suggestion--insertion"
          : "wb-cowork-suggestion--modification",
      ].join(" "),
      focused,
      flashFocused,
      {
        ...common,
        "data-wb-decoration": "edit-proposal-replacement",
      },
    );

    if (edit.changeType === "insertion") {
      const quote = edit.quoteAnchor.exact;
      const quoteIndex = edit.replacement.indexOf(quote);
      if (quoteIndex >= 0) {
        const before = proposalWidget(
          range.from,
          edit.replacement.slice(0, quoteIndex),
          replacementAttributes,
          `edit-before:${edit.proposalId}`,
          -1,
        );
        const after = proposalWidget(
          range.to,
          edit.replacement.slice(quoteIndex + quote.length),
          replacementAttributes,
          `edit-after:${edit.proposalId}`,
          1,
        );
        if (before !== null) decorations.push(before);
        if (after !== null) decorations.push(after);
        continue;
      }
    }

    const replacement = proposalWidget(
      range.to,
      edit.replacement,
      replacementAttributes,
      `edit-replacement:${edit.proposalId}`,
      1,
    );
    if (replacement !== null) decorations.push(replacement);
  }

  // Suggestion marks already carry the proposed text. These extra wrappers add one
  // plain, namespace-qualified anchor identity so geometry never has to parse the
  // vendored mark's JSON-encoded data-id attribute.
  doc.descendants((node, pos) => {
    if (node.isText) {
      for (const mark of node.marks) {
        if (
          mark.type.name !== "insertion" &&
          mark.type.name !== "deletion" &&
          mark.type.name !== "modification"
        ) {
          continue;
        }
        const id = suggestionId(mark.attrs["id"]);
        if (id === null) continue;
        const decoration = inlineDecoration(
          pos,
          pos + node.nodeSize,
          anchorAttributes(
            "proposal",
            id,
            "wb-cowork-proposal-anchor",
            focused,
            flashFocused,
            { "data-wb-decoration": "suggestion-anchor" },
          ),
          `proposal:${id}:${String(pos)}`,
        );
        if (decoration !== null) decorations.push(decoration);
      }
    }

    const atom = atomSuggestion(node.attrs["wbSuggestion"]);
    if (atom !== null) {
      decorations.push(
        Decoration.node(
          pos,
          pos + node.nodeSize,
          anchorAttributes(
            "proposal",
            atom.id,
            "wb-cowork-proposal-anchor",
            focused,
            flashFocused,
            {
              "data-wb-decoration": "atom-suggestion-anchor",
              "data-wb-suggestion": atom.type,
            },
          ),
          { key: `proposal-atom:${atom.id}:${String(pos)}` },
        ),
      );
    }
    return true;
  });

  for (const flag of projection.flags) {
    const range = resolveQuoteAnchor(doc, flag.quoteAnchor);
    if (range === null) continue;
    const decoration = inlineDecoration(
      range.from,
      range.to,
      anchorAttributes(
        "proposal",
        flag.proposalId,
        "wb-cowork-flag-mark",
        focused,
        flashFocused,
        {
          "data-wb-decoration": "flag",
          "data-wb-proposal-kind": "flag",
        },
      ),
      `flag:${flag.proposalId}`,
    );
    if (decoration !== null) decorations.push(decoration);
  }

  /*
   * Evaluation evidence is a view-only annotation from the same authoritative
   * document pull as its Review card. The double underline distinguishes a
   * checked observation from proposals and provenance without relying on color.
   */
  for (const evaluation of projection.evaluations ?? []) {
    const range = resolveQuoteAnchor(doc, evaluation.quoteAnchor);
    if (range === null) continue;
    const decoration = inlineDecoration(
      range.from,
      range.to,
      anchorAttributes(
        "evaluation_result",
        evaluation.resultId,
        "wb-cowork-evaluation-mark",
        focused,
        flashFocused,
        {
          "data-wb-decoration": "evaluation-result",
          "data-wb-result-kind": evaluation.resultKind,
        },
      ),
      `evaluation:${evaluation.resultId}`,
    );
    if (decoration !== null) decorations.push(decoration);
  }

  for (const expression of projection.expressions) {
    const range = rangeForQuote(
      doc,
      expression.quote,
      expression.quoteAnchor,
    );
    if (range === null) continue;
    const expressionClaims =
      claimsByExpression.get(expression.expressionId) ?? [];
    const claimIds = expressionClaims.map((claim) => claim.claimId);
    const activeClaim =
      focused?.kind === "claim" && claimIds.includes(focused.id);
    const attributes: Record<string, string> = {
      "data-wb-decoration": "expression",
      "data-wb-expression-id": expression.expressionId,
      "data-wb-span-id": expression.spanId,
      "data-claim-ref": expression.claimRef,
    };
    if (claimIds.length > 0) {
      attributes["data-wb-claim-ids"] = JSON.stringify(claimIds);
    }
    if (expression.claimStatus !== null) {
      attributes["data-claim-status"] = expression.claimStatus;
    }
    const baseClass = [
      "wb-cowork-expression-mark",
      claimIds.length > 0 ? "wb-cowork-claim-anchor" : "",
      activeClaim ? "wb-cowork-anchor--active" : "",
      activeClaim && flashFocused ? "wb-cowork-anchor--flash" : "",
    ]
      .filter(Boolean)
      .join(" ");
    const decoration = inlineDecoration(
      range.from,
      range.to,
      anchorAttributes(
        "expression",
        expression.expressionId,
        baseClass,
        focused,
        flashFocused,
        attributes,
      ),
      `expression:${expression.expressionId}`,
    );
    if (decoration !== null) decorations.push(decoration);
  }

  // Claim geometry is an alias on its expression decoration. A claim_ref can name
  // multiple claims, and exact-overlap ProseMirror decorations merge attributes, so a
  // JSON string array truthfully preserves every claim identity on the one prose range.

  // Only confirmed AI provenance receives the confirmed-provenance treatment.
  // Human text is the unadorned baseline and proposed AI text is represented by
  // tracked-change/flag decorations.
  for (const span of projection.provenance) {
    if (span.trustState !== "ai_confirmed") continue;
    const range = rangeForQuote(doc, span.quote, span.quoteAnchor);
    if (range === null) continue;
    const attributes: Record<string, string> = {
      "data-wb-decoration": "provenance",
      "data-wb-trust": "ai-confirmed",
      "data-wb-span-id": span.spanId,
    };
    if (span.producer !== null) attributes["data-producer"] = span.producer;
    if (span.approvalGestureId !== null) {
      attributes["data-approval-gesture-id"] = span.approvalGestureId;
    }
    const provenanceAttributes = anchorAttributes(
      "provenance",
      span.spanId,
      "wb-cowork-provenance-tint",
      focused,
      flashFocused,
      attributes,
    );
    // Provenance can cover exactly the same range as an expression. It has no rail
    // geometry contract, so keep its identity in a dedicated attribute instead of
    // overwriting the expression's generalized anchor identity during DOM merging.
    delete provenanceAttributes["data-wb-anchor-kind"];
    delete provenanceAttributes["data-wb-anchor-id"];
    provenanceAttributes["data-wb-provenance-id"] = span.spanId;
    const decoration = inlineDecoration(
      range.from,
      range.to,
      provenanceAttributes,
      `provenance:${span.spanId}`,
    );
    if (decoration !== null) decorations.push(decoration);
  }

  if (
    highlight !== null &&
    highlight.from >= 0 &&
    highlight.to <= doc.content.size
  ) {
    const decoration = inlineDecoration(
      highlight.from,
      highlight.to,
      anchorAttributes(
        "passage",
        highlight.id,
        "wb-cowork-passage-highlight",
        focused,
        flashFocused,
        {
          "data-wb-decoration": "passage-highlight",
          "data-wb-highlight-active": "true",
        },
      ),
      `passage:${highlight.id}`,
    );
    if (decoration !== null) decorations.push(decoration);
  }

  return DecorationSet.create(doc, decorations);
}

const mappedHighlight = (
  transaction: Transaction,
  current: CoworkPassageHighlight | null,
): CoworkPassageHighlight | null => {
  if (current === null || !transaction.docChanged) return current;
  const from = transaction.mapping.mapResult(current.from, 1);
  const to = transaction.mapping.mapResult(current.to, -1);
  if (from.deletedAcross || to.deletedAcross || from.pos >= to.pos) return null;
  return { ...current, from: from.pos, to: to.pos };
};

function createPluginState(
  doc: ProseMirrorNode,
  projection: CoworkLedgerDecorationProjection,
  focused: CoworkFocusedAnchor | null,
  flashFocused: boolean,
  highlight: CoworkPassageHighlight | null,
): CoworkLedgerDecorationState {
  return {
    projection,
    focused,
    flashFocused,
    highlight,
    decorations: buildDecorations(
      doc,
      projection,
      focused,
      flashFocused,
      highlight,
    ),
  };
}

export function coworkLedgerDecorationsPlugin(): Plugin<CoworkLedgerDecorationState> {
  return new Plugin<CoworkLedgerDecorationState>({
    key: coworkLedgerDecorationsKey,
    state: {
      init: (_config, state) =>
        createPluginState(state.doc, EMPTY_PROJECTION, null, false, null),
      apply(transaction, value) {
        const meta = transaction.getMeta(coworkLedgerDecorationsKey) as
          | CoworkLedgerDecorationMeta
          | undefined;
        if (meta === undefined && !transaction.docChanged) return value;

        /*
         * A keystroke already supplies a precise ProseMirror mapping. Mapping the
         * existing DecorationSet keeps every annotation attached to its passage
         * without re-indexing the full document once per annotation. Fresh quote
         * resolution is reserved for an R2 projection/focus/highlight change.
         */
        if (meta === undefined) {
          return {
            ...value,
            highlight: mappedHighlight(transaction, value.highlight),
            decorations: value.decorations.map(
              transaction.mapping,
              transaction.doc,
            ),
          };
        }

        let projection = value.projection;
        let focused = value.focused;
        let flashFocused = value.flashFocused;
        let highlight = mappedHighlight(transaction, value.highlight);

        if (meta?.type === "project") {
          projection = meta.projection;
        } else if (meta?.type === "focus") {
          focused = meta.focused;
          flashFocused = meta.focused === null ? false : meta.flash;
        } else if (meta?.type === "set-focus-flash") {
          flashFocused = focused === null ? false : meta.flash;
        } else if (meta?.type === "highlight") {
          highlight = meta.highlight;
        } else if (
          meta?.type === "clear-highlight" &&
          (meta.id === undefined || highlight?.id === meta.id)
        ) {
          highlight = null;
        }

        return createPluginState(
          transaction.doc,
          projection,
          focused,
          flashFocused,
          highlight,
        );
      },
    },
    props: {
      decorations(state) {
        return coworkLedgerDecorationsKey.getState(state)?.decorations ?? null;
      },
    },
  });
}

/** Runtime-only extension. It is intentionally absent from the Markdown manager. */
export const CoworkLedgerDecorations = Extension.create({
  name: "coworkLedgerDecorations",

  addProseMirrorPlugins() {
    return [coworkLedgerDecorationsPlugin()];
  },
});

const dispatchMeta = (
  editor: Editor,
  meta: CoworkLedgerDecorationMeta,
): boolean => {
  if (editor.isDestroyed || coworkLedgerDecorationsKey.getState(editor.state) === undefined) {
    return false;
  }
  editor.view.dispatch(
    editor.state.tr.setMeta(coworkLedgerDecorationsKey, meta),
  );
  return true;
};

export const projectCoworkLedgerDecorations = (
  editor: Editor,
  projection: CoworkLedgerDecorationProjection,
): boolean => dispatchMeta(editor, { type: "project", projection });

export const focusCoworkLedgerAnchor = (
  editor: Editor,
  focused: CoworkFocusedAnchor,
  flash = false,
): boolean => dispatchMeta(editor, { type: "focus", focused, flash });

export const clearCoworkLedgerAnchorFocus = (editor: Editor): boolean =>
  dispatchMeta(editor, { type: "focus", focused: null, flash: false });

export const setCoworkLedgerAnchorFlash = (
  editor: Editor,
  flash: boolean,
): boolean => dispatchMeta(editor, { type: "set-focus-flash", flash });

export const showCoworkPassageHighlight = (
  editor: Editor,
  highlight: CoworkPassageHighlight,
): boolean => dispatchMeta(editor, { type: "highlight", highlight });

export const clearCoworkPassageHighlight = (
  editor: Editor,
  id?: string,
): boolean => dispatchMeta(editor, { type: "clear-highlight", id });

/** Read-only test/diagnostic projection; callers cannot mutate plugin state. */
export const readCoworkLedgerDecorationState = (
  editor: Editor,
): {
  readonly focused: CoworkFocusedAnchor | null;
  readonly flashFocused: boolean;
  readonly highlight: CoworkPassageHighlight | null;
} | null => {
  const state = coworkLedgerDecorationsKey.getState(editor.state);
  return state === undefined
    ? null
    : {
        focused: state.focused,
        flashFocused: state.flashFocused,
        highlight: state.highlight,
      };
};
