import type { CoworkCapturedActionSnapshot } from "../targets";

type FetchLike = typeof fetch;

export interface CoworkVerifyExecutionSelection {
  readonly providerId: string;
  readonly modelId: string;
  readonly providerLabel: string;
  readonly modelLabel: string;
}

export interface CoworkVerifyStartReceipt {
  readonly actionSnapshotId: string;
  readonly runId: string;
  readonly jobId: string;
  readonly stage: string;
  readonly resultCount: number;
  readonly coordinationStatus: "pending" | "completed" | "unavailable";
}

export interface CoworkCothinkStartReceipt {
  readonly actionSnapshotId: string;
  readonly jobId: string;
  readonly status: string;
}

const endpoint = (
  documentId: string,
  storeId: string,
  suffix: string,
): string =>
  `/api/truth/doc/${encodeURIComponent(documentId)}/${suffix}?store_id=${encodeURIComponent(storeId)}`;

const errorMessage = async (
  response: Response,
  fallback: string,
): Promise<string> => {
  try {
    const payload = (await response.json()) as {
      readonly error?: unknown;
    };
    if (typeof payload.error === "string" && payload.error.trim().length > 0) {
      return payload.error;
    }
    if (
      typeof payload.error === "object" &&
      payload.error !== null &&
      "message" in payload.error &&
      typeof payload.error.message === "string"
    ) {
      return payload.error.message;
    }
  } catch {
    // Keep the stable user-facing fallback for malformed proxy/server output.
  }
  return fallback;
};

const executionPayload = (selection: CoworkVerifyExecutionSelection) => ({
  provider_id: selection.providerId,
  model_id: selection.modelId,
  provider_label: selection.providerLabel,
  model_label: selection.modelLabel,
});

/**
 * Same-origin transport for exact captured actions. The selection is copied
 * from the currently displayed provider/model pair at the click boundary and
 * becomes an explicit server authorization receipt; the server never silently
 * falls back to another execution profile.
 */
export class HttpCoworkVerifyClient {
  readonly #documentId: string;
  readonly #storeId: string;
  readonly #fetch: FetchLike;

  constructor({
    documentId,
    storeId,
    fetchImpl = fetch,
  }: {
    readonly documentId: string;
    readonly storeId: string;
    readonly fetchImpl?: FetchLike;
  }) {
    this.#documentId = documentId;
    this.#storeId = storeId;
    this.#fetch = fetchImpl;
  }

  async startVerify(
    capture: CoworkCapturedActionSnapshot,
    selection: CoworkVerifyExecutionSelection,
    options: {
      readonly userGoal: string;
      readonly protectedIntent: string;
      readonly recheckOfProposalIds?: readonly string[];
      readonly recheckOfRunId?: string;
      readonly recheckIntentId?: string;
    },
  ): Promise<CoworkVerifyStartReceipt> {
    const response = await this.#fetch(
      endpoint(this.#documentId, this.#storeId, "verify/runs"),
      {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          capture,
          execution: executionPayload(selection),
          user_goal: options.userGoal,
          protected_intent: options.protectedIntent,
          recheck_of_proposal_ids: options.recheckOfProposalIds ?? [],
          recheck_of_run_id: options.recheckOfRunId ?? null,
          recheck_intent_id: options.recheckIntentId ?? null,
        }),
      },
    );
    if (!response.ok) {
      throw new Error(
        await errorMessage(
          response,
          "Co-work Verify could not start on this exact document version.",
        ),
      );
    }
    const payload = (await response.json()) as Record<string, unknown>;
    if (
      typeof payload.action_snapshot_id !== "string" ||
      typeof payload.run_id !== "string" ||
      typeof payload.job_id !== "string"
    ) {
      throw new Error("Co-work Verify returned an invalid start receipt.");
    }
    return {
      actionSnapshotId: payload.action_snapshot_id,
      runId: payload.run_id,
      jobId: payload.job_id,
      stage: typeof payload.stage === "string" ? payload.stage : "preparing",
      resultCount:
        typeof payload.result_count === "number" ? payload.result_count : 0,
      coordinationStatus:
        payload.coordination_status === "unavailable" ||
        payload.coordination_status === "completed"
          ? payload.coordination_status
          : "pending",
    };
  }

  async startCothink(
    capture: CoworkCapturedActionSnapshot,
    selection: CoworkVerifyExecutionSelection,
  ): Promise<CoworkCothinkStartReceipt> {
    const response = await this.#fetch(
      endpoint(this.#documentId, this.#storeId, "cothink"),
      {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          capture,
          execution: executionPayload(selection),
          purpose: "Invite one useful alternative perspective.",
          protected_intent:
            "Support deliberation without presenting a defect claim.",
        }),
      },
    );
    if (!response.ok) {
      throw new Error(
        await errorMessage(
          response,
          "Co-think could not start on this exact document version.",
        ),
      );
    }
    const payload = (await response.json()) as Record<string, unknown>;
    if (
      typeof payload.action_snapshot_id !== "string" ||
      typeof payload.job_id !== "string"
    ) {
      throw new Error("Co-think returned an invalid start receipt.");
    }
    return {
      actionSnapshotId: payload.action_snapshot_id,
      jobId: payload.job_id,
      status: typeof payload.status === "string" ? payload.status : "prepared",
    };
  }
}
