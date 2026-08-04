import type { RoutingDeliveryInput } from "../chat";
import { normalizeChatExecutionSnapshot } from "../../../dashboard/conversations";
import type {
  SittingItemResult as RailSittingItemResult,
  SittingResult as RailSittingResult,
  StagedDecision,
} from "../rail/contracts";
import type { SittingSubmission } from "../rail/provider";
import { RecoverableDecisionApplyError } from "../rail/applyRecovery";
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

const failedItemMessage = (item: SittingItemResult): string => {
  if (item.result === "rejected_stale_view") {
    return "This suggestion changed since you made the decision.";
  }
  const raw = item.error?.trim();
  if (raw === undefined || raw.length === 0) {
    return "This decision cannot be applied to the current document.";
  }
  const known: Readonly<Record<string, string>> = {
    stale_base: "The document changed around this suggestion.",
    target_missing: "The original passage is no longer in the current document.",
    target_ambiguous: "The original passage now appears in more than one place.",
    "proposal does not exist": "This suggestion is no longer available.",
    "proposal belongs to another document":
      "This suggestion belongs to a different document.",
    "proposal has no status history":
      "Co-work cannot determine this suggestion’s current status.",
    "duplicate proposal in sitting": "This suggestion was selected more than once.",
    "unsupported item fields": "This saved decision is no longer supported.",
  };
  if (known[raw] !== undefined) return known[raw];
  const closed = /^proposal is ([a-z_]+), not open$/u.exec(raw);
  if (closed !== null) {
    return `This suggestion is already ${closed[1].replace(/_/gu, " ")}.`;
  }
  if (raw.startsWith("unsupported verb:")) {
    return "This saved decision is no longer supported.";
  }
  if (/^[a-z_]+$/u.test(raw)) {
    return "Co-work could not safely apply this decision to the current document.";
  }
  const bounded = raw.slice(0, 240);
  return `${bounded.charAt(0).toUpperCase()}${bounded.slice(1)}${/[.!?]$/u.test(bounded) ? "" : "."}`;
};

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
      let execution: RoutingDeliveryInput["execution"];
      if (delivery.execution !== undefined) {
        try {
          execution = normalizeChatExecutionSnapshot(delivery.execution);
        } catch {
          execution = undefined;
        }
      }
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
        ...(execution === undefined
          ? {}
          : {
              execution,
              ...(delivery.conversation_id === undefined ||
              delivery.conversation_id === null
                ? {}
                : { conversationId: delivery.conversation_id }),
              ...(delivery.agent === undefined
                ? {}
                : { agent: delivery.agent }),
            }),
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
  /** Retire the key for an intent that is known not to have committed. */
  readonly onIntentAbandoned?: (fingerprint: string) => void;
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
  const prepare = () =>
    client.prepare({
      documentId: params.documentId,
      storeId: params.storeId,
      body: {
        items,
        expected_file_sha256: preflight.expectedFileSha256,
        expected_ydoc_head_sha256: preflight.expectedStructuredHeadSha256,
        idempotency_key: params.idempotencyKeyFor(fingerprint),
      },
    });
  let prepared = await prepare();

  // A deterministic recovery may have cancelled an earlier attempt using this
  // fingerprint. Retire that consumed key and transparently prepare once more.
  if (prepared.state === "cancelled") {
    params.onIntentAbandoned?.(fingerprint);
    prepared = await prepare();
    if (prepared.state === "cancelled") {
      params.onIntentAbandoned?.(fingerprint);
      throw new Error(
        "Co-work could not start a fresh decision attempt. Your decisions were not applied.",
      );
    }
  }

  const abandonPreparedIntent = async (): Promise<void> => {
    try {
      await client.cancel(params.documentId, params.storeId, prepared.intent_id);
    } catch {
      // The uncommitted intent expires even when best-effort cleanup is unavailable.
    } finally {
      // A cancelled or locally abandoned intent must never poison an identical retry.
      params.onIntentAbandoned?.(fingerprint);
    }
  };

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

  if (prepared.failed_items.length > 0) {
    await abandonPreparedIntent();
    throw new RecoverableDecisionApplyError(
      "Some decisions do not match the current document.",
      {
        availableProposalIds: prepared.admitted_items.map(
          (item) => item.proposal_id,
        ),
        blockers: prepared.failed_items.map((item) => ({
          proposalId: item.proposal_id,
          reason:
            item.result === "rejected_stale_view"
              ? "proposal_changed"
              : "not_currently_applicable",
          relatedProposalIds: [],
          message: failedItemMessage(item),
        })),
      },
    );
  }

  let staged:
    | Awaited<ReturnType<CoworkSittingWorkspace["prepare"]>>
    | null = null;
  try {
    if (prepared.requires_document_commit) {
      staged = await params.workspace.prepare(prepared.admitted_items, preflight.generation);
    }
    if (!params.workspace.isCurrent(preflight.generation)) {
      await abandonPreparedIntent();
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
  } catch (error) {
    if (error instanceof RecoverableDecisionApplyError) {
      // This exact all-items intent cannot be committed with the recovery subset.
      // Release it before Review offers a separately confirmed subset submission.
      await abandonPreparedIntent();
    }
    throw error;
  } finally {
    staged?.dispose();
  }
};
