import { Extension, type Editor } from "@tiptap/core";
import type { Node as ProseMirrorNode } from "@tiptap/pm/model";
import { Plugin, PluginKey, type Transaction } from "@tiptap/pm/state";
import { Decoration, DecorationSet } from "@tiptap/pm/view";

import {
  resolveProvenanceQuoteAnchorDetailed,
  resolveQuoteAnchor,
} from "../suggestions/anchor";
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

/**
 * The durable rail context currently projected over the editor. A lens changes
 * view-only decorations; it never changes the ProseMirror document, Y.Doc, or
 * the independent temporary passage highlight used by Chat and Working on.
 */
export type CoworkEditorLens = "neutral" | "review" | "truth" | "provenance";

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

export interface CoworkProvenanceOverlayDecoration {
  readonly targetId: string;
  readonly recordId: string;
  readonly quoteAnchor: QuoteAnchor | null;
  readonly isDocumentDefault: boolean;
  readonly authorship: "human" | "ai" | "mixed" | "unknown";
  readonly reviewStatus:
    "reviewed" | "not_reviewed" | "not_applicable" | "unknown";
  readonly currentness:
    "current" | "stale" | "requires_reanchor" | "unavailable";
  readonly resolution: "resolved" | "conflicted";
  readonly source: string;
  readonly sourceDetail: string;
  readonly contributors: string;
  readonly reviewers: string;
  readonly attester: string;
  readonly basis: string;
  readonly historyCount: number;
  readonly effectiveCount: number;
  readonly recordState: "recorded" | "unrecorded" | "pending";
  readonly authorshipFingerprint: string;
  readonly reviewFingerprint: string;
  readonly sourceFingerprint: string;
}

/**
 * A browser-local direct-entry capture that has not received an authoritative
 * server ledger projection yet. This is delivery state only: it deliberately
 * carries no authorship, reviewer, or attester claim.
 */
export interface CoworkPendingProvenanceDecoration {
  readonly captureId: string;
  readonly quoteAnchor: QuoteAnchor;
}

export interface CoworkEvaluationDecoration {
  readonly resultId: string;
  readonly quoteAnchor: QuoteAnchor;
  readonly resultKind:
    "conforming" | "nonconforming" | "inconclusive" | "review_comment";
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
  /** Rich, independent provenance lens projection. */
  readonly provenanceOverlay?: readonly CoworkProvenanceOverlayDecoration[];
  /** Additive Verify projection; absent inputs are treated as no results. */
  readonly evaluations?: readonly CoworkEvaluationDecoration[];
}

export interface CoworkFocusedAnchor {
  readonly id: string;
  readonly kind:
    "proposal" | "claim" | "expression" | "provenance" | "evaluation_result";
}

export interface CoworkPassageHighlight {
  readonly id: string;
  readonly from: number;
  readonly to: number;
}

interface CoworkLedgerDecorationState {
  readonly projection: CoworkLedgerDecorationProjection;
  readonly pendingProvenance: readonly CoworkPendingProvenanceDecoration[];
  readonly lens: CoworkEditorLens;
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
      readonly type: "set-lens";
      readonly lens: CoworkEditorLens;
    }
  | {
      readonly type: "set-pending-provenance";
      readonly pending: readonly CoworkPendingProvenanceDecoration[];
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
  resolveQuoteAnchor(doc, quoteAnchor ?? { exact, prefix: "", suffix: "" });

const anchorAttributes = (
  kind: CoworkEditorAnchorKind,
  id: string,
  baseClass: string,
  focused: CoworkFocusedAnchor | null,
  flashFocused: boolean,
  extra: Readonly<Record<string, string>> = {},
): Record<string, string> => {
  const active = focused?.kind === kind && focused.id === id;
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
  if (typeof raw !== "object" || raw === null || Array.isArray(raw))
    return null;
  const value = raw as Record<string, unknown>;
  return typeof value["id"] === "string" && typeof value["type"] === "string"
    ? { id: value["id"], type: value["type"] }
    : null;
};

interface ResolvedProvenanceOverlay {
  readonly target: CoworkProvenanceOverlayDecoration;
  readonly from: number;
  readonly to: number;
}

const provenanceAxes = (target: CoworkProvenanceOverlayDecoration): string =>
  JSON.stringify([
    target.authorshipFingerprint,
    target.reviewFingerprint,
    target.sourceFingerprint,
  ]);

const provenanceClass = (
  targets: readonly CoworkProvenanceOverlayDecoration[],
  conflicted: boolean,
): string => {
  if (conflicted) return "wb-cowork-provenance--conflict";
  const target = targets[0];
  const classes = [
    "wb-cowork-provenance-mark",
    target.recordState === "pending"
      ? ""
      : `wb-cowork-provenance--${target.authorship}`,
    target.recordState === "pending"
      ? ""
      : `wb-cowork-provenance--review-${target.reviewStatus.replace(/_/gu, "-")}`,
    `wb-cowork-provenance--${target.recordState}`,
  ];
  return classes.filter(Boolean).join(" ");
};

/**
 * Atomize the document's text ranges for the Provenance lens. Explicit spans
 * override the current document default; uncovered text is visibly unrecorded.
 * Incompatible explicit overlaps become one conflict segment instead of a
 * last-write-wins decoration.
 */
const provenanceOverlayDecorations = (
  doc: ProseMirrorNode,
  projection: CoworkLedgerDecorationProjection,
  pendingProvenance: readonly CoworkPendingProvenanceDecoration[],
  focused: CoworkFocusedAnchor | null,
  flashFocused: boolean,
): Decoration[] => {
  if (
    projection.provenanceOverlay === undefined &&
    pendingProvenance.length === 0
  ) {
    return [];
  }
  const overlays = projection.provenanceOverlay ?? [];
  const documentDefaults = overlays.filter(
    (item) => item.isDocumentDefault && item.currentness === "current",
  );
  const resolved: ResolvedProvenanceOverlay[] = overlays.flatMap((target) => {
    if (
      target.isDocumentDefault ||
      target.quoteAnchor === null ||
      target.currentness === "stale" ||
      target.currentness === "unavailable"
    ) {
      return [];
    }
    const resolution = resolveProvenanceQuoteAnchorDetailed(
      doc,
      target.quoteAnchor,
    );
    return resolution.state !== "unique"
      ? []
      : [{ target, from: resolution.from, to: resolution.to }];
  });
  const resolvedPending: ResolvedProvenanceOverlay[] =
    pendingProvenance.flatMap((pending) => {
      const resolution = resolveProvenanceQuoteAnchorDetailed(
        doc,
        pending.quoteAnchor,
      );
      if (resolution.state !== "unique") return [];
      return [
        {
          target: {
            targetId: `pending:${pending.captureId}`,
            recordId: `pending:${pending.captureId}`,
            quoteAnchor: pending.quoteAnchor,
            isDocumentDefault: false,
            authorship: "unknown",
            reviewStatus: "unknown",
            currentness: "current",
            resolution: "resolved",
            source: "direct_entry",
            sourceDetail: "Waiting for a durable provenance receipt",
            contributors: "Not asserted while recording",
            reviewers: "Not asserted while recording",
            attester: "Not asserted while recording",
            basis: "automatic_direct_entry_attribution",
            historyCount: 0,
            effectiveCount: 0,
            recordState: "pending",
            authorshipFingerprint: "pending",
            reviewFingerprint: "pending",
            sourceFingerprint: "pending",
          },
          from: resolution.from,
          to: resolution.to,
        },
      ];
    });
  const result: Decoration[] = [];
  doc.descendants((node, pos) => {
    if (!node.isText || node.nodeSize === 0) return true;
    const nodeFrom = pos;
    const nodeTo = pos + node.nodeSize;
    const boundaries = new Set<number>([nodeFrom, nodeTo]);
    for (const item of resolved) {
      if (item.to <= nodeFrom || item.from >= nodeTo) continue;
      boundaries.add(Math.max(nodeFrom, item.from));
      boundaries.add(Math.min(nodeTo, item.to));
    }
    for (const item of resolvedPending) {
      if (item.to <= nodeFrom || item.from >= nodeTo) continue;
      boundaries.add(Math.max(nodeFrom, item.from));
      boundaries.add(Math.min(nodeTo, item.to));
    }
    const ordered = [...boundaries].sort((a, b) => a - b);
    for (let index = 0; index < ordered.length - 1; index += 1) {
      const from = ordered[index];
      const to = ordered[index + 1];
      const covering = resolved.filter(
        (item) => item.from < to && item.to > from,
      );
      const pending = resolvedPending.filter(
        (item) => item.from < to && item.to > from,
      );
      let targets: readonly CoworkProvenanceOverlayDecoration[];
      if (covering.length > 0) {
        // A server-projected span is authoritative as soon as it appears. The
        // local pending marker may remain until outbox cleanup, but must never
        // obscure or conflict with the recorded receipt.
        targets = covering.map((item) => item.target);
      } else if (pending.length > 0) {
        targets = pending.map((item) => item.target);
      } else if (documentDefaults.length > 0) {
        targets = documentDefaults;
      } else {
        targets = [
          {
            targetId: `unrecorded:${String(from)}`,
            recordId: `unrecorded:${String(from)}`,
            quoteAnchor: null,
            isDocumentDefault: true,
            authorship: "unknown",
            reviewStatus: "unknown",
            currentness: "current",
            resolution: "resolved",
            source: "unrecorded",
            sourceDetail: "No source recorded",
            contributors: "No contributors recorded",
            reviewers: "No reviewers recorded",
            attester: "none",
            basis: "none",
            historyCount: 0,
            effectiveCount: 0,
            recordState: "unrecorded",
            authorshipFingerprint: "unrecorded",
            reviewFingerprint: "unrecorded",
            sourceFingerprint: "unrecorded",
          },
        ];
      }
      const incompatible = new Set(targets.map(provenanceAxes)).size > 1;
      const conflicted =
        incompatible ||
        targets.some((item) => item.resolution === "conflicted");
      const ids = targets.map((item) => item.targetId);
      const recordIds = targets.map((item) => item.recordId);
      const primary =
        focused?.kind === "provenance"
          ? (targets.find((item) => item.targetId === focused.id) ?? targets[0])
          : targets[0];
      const attester =
        new Set(targets.map((item) => item.attester)).size > 1
          ? "multiple"
          : primary.attester;
      const basis =
        new Set(targets.map((item) => item.basis)).size > 1
          ? "multiple"
          : primary.basis;
      const sourceDetail =
        new Set(targets.map((item) => item.sourceDetail)).size > 1
          ? "multiple sources"
          : primary.sourceDetail;
      const contributors =
        new Set(targets.map((item) => item.contributors)).size > 1
          ? "multiple contributor records"
          : primary.contributors;
      const reviewers =
        new Set(targets.map((item) => item.reviewers)).size > 1
          ? "multiple reviewer records"
          : primary.reviewers;
      const metadata = {
        "data-wb-decoration": "provenance-overlay",
        "data-wb-provenance-id": primary.targetId,
        "data-wb-provenance-ids": JSON.stringify(ids),
        "data-wb-provenance-record-ids": JSON.stringify(recordIds),
        "data-wb-authorship": conflicted ? "conflict" : primary.authorship,
        "data-wb-human-review": conflicted ? "conflict" : primary.reviewStatus,
        "data-wb-source": conflicted ? "conflict" : primary.source,
        "data-wb-source-detail": conflicted ? "multiple sources" : sourceDetail,
        "data-wb-contributors": conflicted
          ? "multiple contributor records"
          : contributors,
        "data-wb-reviewers": conflicted
          ? "multiple reviewer records"
          : reviewers,
        "data-wb-attester": conflicted ? "multiple" : attester,
        "data-wb-basis": conflicted ? "multiple" : basis,
        "data-wb-history-count": String(
          targets.reduce((count, item) => count + item.historyCount, 0),
        ),
        "data-wb-provenance-conflict": String(conflicted),
        "data-wb-provenance-record-state": primary.recordState,
        "data-wb-provenance-currentness":
          new Set(targets.map((item) => item.currentness)).size > 1
            ? "multiple target states"
            : primary.currentness,
      };
      const attributes =
        primary.recordState !== "recorded"
          ? {
              class: `wb-cowork-ledger-decoration ${provenanceClass(targets, conflicted)}`,
              ...metadata,
            }
          : anchorAttributes(
              "provenance",
              primary.targetId,
              provenanceClass(targets, conflicted),
              focused,
              flashFocused,
              metadata,
            );
      const decoration = inlineDecoration(
        from,
        to,
        attributes,
        `provenance-overlay:${ids.join(":")}:${String(from)}`,
      );
      if (decoration !== null) result.push(decoration);
    }
    return true;
  });
  return result;
};

function buildDecorations(
  doc: ProseMirrorNode,
  projection: CoworkLedgerDecorationProjection,
  pendingProvenance: readonly CoworkPendingProvenanceDecoration[],
  lens: CoworkEditorLens,
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
  if (lens === "provenance") {
    decorations.push(
      ...provenanceOverlayDecorations(
        doc,
        projection,
        pendingProvenance,
        focused,
        flashFocused,
      ),
    );
  }

  /*
   * Open edit proposals are pure view state. Existing prose is decorated in
   * place and proposed text is a non-editable widget; neither operation changes
   * the ProseMirror document or its collaborative Y.Doc.
   */
  for (const edit of lens === "review" ? projection.edits : []) {
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
  if (lens === "review")
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

  for (const flag of lens === "review" ? projection.flags : []) {
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
  for (const evaluation of lens === "review"
    ? (projection.evaluations ?? [])
    : []) {
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

  for (const expression of lens === "truth" ? projection.expressions : []) {
    const range = rangeForQuote(doc, expression.quote, expression.quoteAnchor);
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
  pendingProvenance: readonly CoworkPendingProvenanceDecoration[],
  lens: CoworkEditorLens,
  focused: CoworkFocusedAnchor | null,
  flashFocused: boolean,
  highlight: CoworkPassageHighlight | null,
): CoworkLedgerDecorationState {
  return {
    projection,
    pendingProvenance,
    lens,
    focused,
    flashFocused,
    highlight,
    decorations: buildDecorations(
      doc,
      projection,
      pendingProvenance,
      lens,
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
        createPluginState(
          state.doc,
          EMPTY_PROJECTION,
          [],
          "review",
          null,
          false,
          null,
        ),
      apply(transaction, value) {
        const meta = transaction.getMeta(coworkLedgerDecorationsKey) as
          CoworkLedgerDecorationMeta | undefined;
        if (meta === undefined && !transaction.docChanged) return value;

        /*
         * Review and Truth annotations can follow ProseMirror's precise mapping.
         * Provenance is different: exact selectors are the safety boundary, so a
         * document edit must resolve them again and must drop missing/ambiguous
         * spans instead of carrying a stale painted range forward.
         */
        if (meta === undefined) {
          const highlight = mappedHighlight(transaction, value.highlight);
          if (value.lens === "provenance") {
            return createPluginState(
              transaction.doc,
              value.projection,
              value.pendingProvenance,
              value.lens,
              value.focused,
              value.flashFocused,
              highlight,
            );
          }
          return {
            ...value,
            highlight,
            decorations: value.decorations.map(
              transaction.mapping,
              transaction.doc,
            ),
          };
        }

        let projection = value.projection;
        let pendingProvenance = value.pendingProvenance;
        let lens = value.lens;
        let focused = value.focused;
        let flashFocused = value.flashFocused;
        let highlight = mappedHighlight(transaction, value.highlight);

        if (meta?.type === "project") {
          projection = meta.projection;
        } else if (meta?.type === "set-pending-provenance") {
          pendingProvenance = meta.pending;
        } else if (meta?.type === "set-lens") {
          lens = meta.lens;
          const visible =
            focused === null ||
            (lens === "review" &&
              (focused.kind === "proposal" ||
                focused.kind === "evaluation_result")) ||
            (lens === "truth" &&
              (focused.kind === "claim" || focused.kind === "expression")) ||
            (lens === "provenance" && focused.kind === "provenance");
          if (!visible) {
            focused = null;
            flashFocused = false;
          }
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
          pendingProvenance,
          lens,
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
  if (
    editor.isDestroyed ||
    coworkLedgerDecorationsKey.getState(editor.state) === undefined
  ) {
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

/** Switch the editor's view-only ledger overlay without replaying navigation. */
export const setCoworkEditorLens = (
  editor: Editor,
  lens: CoworkEditorLens,
): boolean => dispatchMeta(editor, { type: "set-lens", lens });

/** Replace only the browser-local delivery projection; server ledger data is untouched. */
export const setCoworkPendingProvenance = (
  editor: Editor,
  pending: readonly CoworkPendingProvenanceDecoration[],
): boolean => dispatchMeta(editor, { type: "set-pending-provenance", pending });

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
  readonly lens: CoworkEditorLens;
  readonly focused: CoworkFocusedAnchor | null;
  readonly flashFocused: boolean;
  readonly highlight: CoworkPassageHighlight | null;
} | null => {
  const state = coworkLedgerDecorationsKey.getState(editor.state);
  return state === undefined
    ? null
    : {
        lens: state.lens,
        focused: state.focused,
        flashFocused: state.flashFocused,
        highlight: state.highlight,
      };
};
