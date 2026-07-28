import type { RoutingDeliveryInput } from "../chat";
import type {
  SittingItemResult as RailSittingItemResult,
  SittingResult as RailSittingResult,
  StagedDecision,
} from "../rail/contracts";
import type { SittingSubmission } from "../rail/provider";
import {
  CoworkSittingClient,
  type CoworkSittingTransport,
} from "../suggestions/sitting";
import type {
  DecisionItem,
  SittingItemResult,
  SittingResponse,
} from "../suggestions/types";
import type { CoworkSittingWorkspace } from "./sittingWorkspace";

/** Translate one rail decision into the exact prepare wire item. */
export const toDecisionItem = (decision: StagedDecision): DecisionItem => ({
  proposal_id: decision.proposalId,
  verb: decision.verb,
  canonical_sha256: decision.canonicalSha256,
  ...(decision.amendContent === undefined ? {} : { amend_content: decision.amendContent }),
  ...(decision.redirectNote === undefined ? {} : { redirect_note: decision.redirectNote }),
  ...(decision.negationText === undefined ? {} : { negation_text: decision.negationText }),
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

export const toRailSittingResult = (response: SittingResponse): RailSittingResult => ({
  ok: response.ok,
  partial: response.partial,
  results: response.results.map(toRailItemResult),
});

export const routingDeliveriesFrom = (
  submitted: readonly StagedDecision[],
  response: SittingResponse,
): RoutingDeliveryInput[] => {
  if (response.routing_deliveries !== undefined) {
    return response.routing_deliveries.map((delivery) => {
      const state =
        delivery.delivered === false
          ? "failed"
          : delivery.delivered === true && delivery.agent?.status === "running"
            ? "delivered"
            : "queued";
      return {
        verb: delivery.verb,
        proposalId: delivery.proposal_id,
        state,
        ...(delivery.note === undefined || delivery.note === null
          ? {}
          : { note: delivery.note }),
        ...(delivery.reason === undefined || delivery.reason === null
          ? {}
          : { reason: delivery.reason }),
      };
    });
  }
  const noteByProposal = new Map(
    submitted.map((decision) => [decision.proposalId, decision.redirectNote]),
  );
  return response.results.flatMap((result): RoutingDeliveryInput[] => {
    if (result.verb !== "redirect" && result.verb !== "endorse") return [];
    const delivered =
      result.verb === "redirect"
        ? result.result === "kept_open_redirected"
        : result.result === "kept_open_endorsed";
    const note = result.verb === "redirect" ? noteByProposal.get(result.proposal_id) : undefined;
    return [
      {
        verb: result.verb,
        proposalId: result.proposal_id,
        // A legacy receipt confirms only the sitting result, not conversation
        // persistence or a running document agent.
        state: delivered ? "queued" : "failed",
        ...(note === undefined ? {} : { note }),
        ...(delivered || result.error === null ? {} : { reason: result.error }),
      },
    ];
  });
};
export interface SubmitCoworkSittingParams {
  readonly documentId: string;
  readonly storeId: string;
  readonly submission: SittingSubmission;
  readonly workspace: CoworkSittingWorkspace;
  readonly transport: CoworkSittingTransport;
  readonly idempotencyKeyFor: (fingerprint: string) => string;
  readonly onCommitted?: () => void;
  readonly onRoutingDelivery?: (delivery: RoutingDeliveryInput) => void;
}

/**
 * Prepare against a synchronized head, transform an isolated clone using only
 * server-admitted items, commit it, then pull the authoritative committed state and
 * advance the editor's persistence heads. A lost response is recoverable by repeating
 * prepare with the same idempotency key; a committed prepare receipt follows the same
 * refresh path instead of replaying decisions.
 */
export const submitCoworkSitting = async (
  params: SubmitCoworkSittingParams,
): Promise<RailSittingResult> => {
  if (params.submission.claimDecisions.length > 0) {
    throw new Error(
      "Live claim review is not available yet. No sitting decisions were submitted.",
    );
  }
  const items = params.submission.proposalDecisions.map(toDecisionItem);
  const preflight = await params.workspace.synchronize();
  const fingerprint = JSON.stringify({
    documentId: params.documentId,
    items,
    expectedFileSha256: preflight.expectedFileSha256,
    expectedStructuredHeadSha256: preflight.expectedStructuredHeadSha256,
  });
  const client = new CoworkSittingClient(params.transport);
  const prepared = await client.prepare({
    documentId: params.documentId,
    storeId: params.storeId,
    body: {
      items,
      expected_file_sha256: preflight.expectedFileSha256,
      expected_ydoc_head_sha256: preflight.expectedStructuredHeadSha256,
      idempotency_key: params.idempotencyKeyFor(fingerprint),
    },
  });

  if (prepared.state === "committed" && prepared.result !== undefined) {
    await params.workspace.refreshFromServer(prepared.result, preflight.generation);
    for (const delivery of routingDeliveriesFrom(
      params.submission.proposalDecisions,
      prepared.result,
    )) {
      params.onRoutingDelivery?.(delivery);
    }
    params.onCommitted?.();
    return toRailSittingResult(prepared.result);
  }

  let staged:
    | Awaited<ReturnType<CoworkSittingWorkspace["prepare"]>>
    | null = null;
  try {
    if (prepared.requires_document_commit) {
      staged = await params.workspace.prepare(prepared.admitted_items, preflight.generation);
    }
    if (!params.workspace.isCurrent(preflight.generation)) {
      await client.cancel(params.documentId, params.storeId, prepared.intent_id);
      throw new Error(
        "The document changed while the sitting was being prepared. Review the latest text and submit again.",
      );
    }
    const response = await client.commit({
      documentId: params.documentId,
      storeId: params.storeId,
      intentId: prepared.intent_id,
      documentCommit: prepared.requires_document_commit ? staged?.commit ?? null : null,
    });
    if (
      prepared.requires_document_commit &&
      staged !== null &&
      response.snapshot_sha256 !== staged.commit.snapshot_sha256
    ) {
      throw new Error("The committed Co-work snapshot did not match the prepared document.");
    }
    await params.workspace.refreshFromServer(response, preflight.generation);
    for (const delivery of routingDeliveriesFrom(
      params.submission.proposalDecisions,
      response,
    )) {
      params.onRoutingDelivery?.(delivery);
    }
    params.onCommitted?.();
    return toRailSittingResult(response);
  } finally {
    staged?.dispose();
  }
};
