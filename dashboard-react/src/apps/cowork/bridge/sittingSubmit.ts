/**
 * The submit path. A staged sitting flows: map the rail's staged decisions to R5
 * DecisionItems, apply each accept and reject to the editor's Y.Doc through
 * adapter.applyDecision (under the apply-origin tag, section 1.4), render the post-apply
 * Markdown into the materialize block when the sitting carries any accept, POST through
 * HttpCoworkSittingTransport, and map the R5 response back to the rail's SittingResult so the
 * health strip and cards reconcile on the reload the confirmation triggers.
 *
 * Routing verbs (redirect, defer, endorse) leave their marks in place: adapter.applyDecision
 * is a no-op for them, so the proposal stays open and its card and mark persist. The route
 * mints every gesture, never the client (section 1.5).
 *
 * The materialize renderer is a seam. This orchestration owns the submit wiring and drives
 * the confirmation. Byte-exact block-splice materialization (copying unedited blocks
 * verbatim, so an undecided proposal in another block never reaches the file) is the fidelity
 * suite's obligation (gate conditions 6 and 8, the production-materializer item), so the default
 * renderer the host supplies serializes the post-apply editor content and the server verifies
 * its hash. The seam lets that default swap for the reference block-splice materializer
 * without touching this orchestration.
 */

import {
  CoworkSittingClient,
  buildMaterializePayload,
  type CoworkSittingTransport,
} from "../suggestions/sitting";
import type {
  DecisionItem,
  SittingItemResult,
  SittingResponse,
} from "../suggestions/types";
import type {
  SittingItemResult as RailSittingItemResult,
  SittingResult as RailSittingResult,
} from "../rail/contracts";
import type { SittingSubmission } from "../rail/provider";
import type { StagedDecision } from "../rail/contracts";
import type { RoutingDeliveryInput } from "../chat";

/**
 * The commit-time application the submit path needs from the adapter: applying one accepted
 * or rejected decision to the editor's Y.Doc under the apply-origin tag. Depending on this
 * narrow capability rather than the whole adapter keeps the orchestration decoupled from the
 * engine, and WbTrackedChangesAdapterImpl.applyDecision satisfies it structurally.
 */
export interface DecisionApplier {
  applyDecision(item: DecisionItem): void;
}

/** Verbs that accept a tracked edit and so require the materialize block (section 1.5). */
const ACCEPT_VERBS = new Set<DecisionItem["verb"]>(["confirm", "edit_confirm"]);

/** Translate one rail staged decision (camelCase) into the R5 wire item (snake_case). */
export const toDecisionItem = (decision: StagedDecision): DecisionItem => ({
  proposal_id: decision.proposalId,
  verb: decision.verb,
  canonical_sha256: decision.canonicalSha256,
  ...(decision.amendContent === undefined
    ? {}
    : { amend_content: decision.amendContent }),
  ...(decision.redirectNote === undefined
    ? {}
    : { redirect_note: decision.redirectNote }),
  ...(decision.negationText === undefined
    ? {}
    : { negation_text: decision.negationText }),
  ...(decision.preferenceText === undefined
    ? {}
    : { preference_text: decision.preferenceText }),
});

const toRailItemResult = (result: SittingItemResult): RailSittingItemResult => ({
  proposalId: result.proposal_id,
  verb: result.verb,
  result: result.result,
  baseOk: result.base_ok,
  gestureId: result.gesture_id,
  error: result.error,
});

/** Map the R5 response back to the rail's SittingResult shape. */
export const toRailSittingResult = (
  response: SittingResponse,
): RailSittingResult => ({
  ok: response.ok,
  partial: response.partial,
  results: response.results.map(toRailItemResult),
});

/**
 * Derive the routing-note deliveries the Chat tab annotates from an R5 response. Only a
 * redirect or an endorse routes guidance into the document conversation, so those are the
 * two verbs mapped. A kept_open_redirected or kept_open_endorsed result is a delivery, any
 * other outcome on those verbs is a failed delivery carrying the item error as the reason.
 * The redirect note comes from the staged decision the human composed, the route echoes no
 * note back.
 */
export const routingDeliveriesFrom = (
  submitted: readonly StagedDecision[],
  response: SittingResponse,
): RoutingDeliveryInput[] => {
  const noteByProposal = new Map(
    submitted.map((decision) => [decision.proposalId, decision.redirectNote]),
  );
  const deliveries: RoutingDeliveryInput[] = [];
  for (const result of response.results) {
    if (result.verb !== "redirect" && result.verb !== "endorse") continue;
    const delivered =
      result.verb === "redirect"
        ? result.result === "kept_open_redirected"
        : result.result === "kept_open_endorsed";
    const note = result.verb === "redirect" ? noteByProposal.get(result.proposal_id) : undefined;
    deliveries.push({
      verb: result.verb,
      proposalId: result.proposal_id,
      state: delivered ? "delivered" : "failed",
      ...(note === undefined || note === null ? {} : { note }),
      ...(delivered || result.error === null || result.error === undefined
        ? {}
        : { reason: result.error }),
    });
  }
  return deliveries;
};

export interface SubmitCoworkSittingParams {
  readonly documentId: string;
  readonly storeId: string;
  readonly submission: SittingSubmission;
  /** The editor adapter, so accepts and rejects apply to the Y.Doc under apply-origin. */
  readonly adapter: DecisionApplier;
  /** The sitting transport (HttpCoworkSittingTransport live, in-memory in tests). */
  readonly transport: CoworkSittingTransport;
  /**
   * Render the post-apply document to Markdown for the materialize block. Called only when
   * the sitting contains an accept verb, after the accepts have been applied to the editor.
   */
  readonly renderMaterialized: () => Promise<string>;
  /**
   * Notified once per routed item (redirect or endorse) with its delivery outcome, so the
   * Chat tab can annotate the routing note. Optional, so a submit without a chat surface
   * skips it.
   */
  readonly onRoutingDelivery?: (delivery: RoutingDeliveryInput) => void;
}

/**
 * Apply the staged sitting to the editor, submit it through R5, and return the rail result.
 * The apply-then-post order is the frozen flow (section 1.5): the client applies accepted
 * edits locally, then posts the rendered Markdown plus its hash, and the server verifies the
 * hash and writes the file.
 */
export const submitCoworkSitting = async (
  params: SubmitCoworkSittingParams,
): Promise<RailSittingResult> => {
  const items = params.submission.proposalDecisions.map(toDecisionItem);

  // Apply each accept and reject to the editor's Y.Doc under the apply-origin tag. Routing
  // verbs no-op here and leave the proposal open.
  for (const item of items) {
    params.adapter.applyDecision(item);
  }

  const hasAccept = items.some((item) => ACCEPT_VERBS.has(item.verb));
  const materialize = hasAccept
    ? await buildMaterializePayload(await params.renderMaterialized())
    : null;

  const client = new CoworkSittingClient(params.transport);
  const response = await client.submit({
    documentId: params.documentId,
    storeId: params.storeId,
    baseDocSha256: params.submission.baseDocSha256,
    items,
    materialize,
  });

  const onRoutingDelivery = params.onRoutingDelivery;
  if (onRoutingDelivery !== undefined) {
    for (const delivery of routingDeliveriesFrom(
      params.submission.proposalDecisions,
      response,
    )) {
      onRoutingDelivery(delivery);
    }
  }

  return toRailSittingResult(response);
};
