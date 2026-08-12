/**
 * The same-origin HTTP client for the R2 doc-open read (C1 surface section 1.3), the pull
 * the bridge runs to learn the open proposals, expressions, provenance, and drift for one
 * document. It is a thin fetch wrapper returning the raw R2 payload, so the pure mapper
 * (reviewMapping.ts) owns the translation and this owns only the transport. R2 is a
 * read-only GET, so no consent gate and no read-only rejection apply (those guard the
 * mutating routes). The seam mirrors HttpCoworkYdocTransport and HttpCoworkSittingTransport
 * so a same-origin fetch and an in-memory double are interchangeable.
 */

import type {
  R2DocPayload,
  R2VerifyExecutionPlan,
  R2VerificationConfiguration,
} from "./types";
import { mapVerifyExecutionPlan } from "./reviewMapping";
import type {
  VerifyCheckInput,
  VerifyCriterionDraftInput,
  VerifyRunInspection,
} from "../rail/contracts";
import { coworkHumanAuthorityHeaders } from "../../../security/humanAuthority";

type JsonObject = Record<string, unknown>;

export interface CothinkDiscussionReceipt {
  readonly conversationId: string;
  readonly messageId: string;
}

const objectValue = (value: unknown): JsonObject =>
  typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as JsonObject)
    : {};
const stringValue = (value: unknown): string =>
  typeof value === "string" ? value : "";
const nullableStringValue = (value: unknown): string | null =>
  typeof value === "string" ? value : null;
const numberValue = (value: unknown): number =>
  typeof value === "number" && Number.isFinite(value) ? value : 0;
const arrayValue = (value: unknown): readonly unknown[] =>
  Array.isArray(value) ? value : [];

/** The read seam the bridge depends on, satisfied by fetch or an in-memory double. */
export interface CoworkDocClient {
  /** R2 doc-open read for the bound document. */
  fetchDoc(): Promise<R2DocPayload>;
  setVerifyCriterionEnabled?(
    criterionKey: string,
    enabled: boolean,
    expectedActivationId: string | null,
  ): Promise<R2VerificationConfiguration>;
  actOnCothink?(
    itemId: string,
    action: "park" | "dismiss",
    canonicalSha256: string,
  ): Promise<void>;
  discussCothink?(
    itemId: string,
    canonicalSha256: string,
  ): Promise<CothinkDiscussionReceipt>;
  inspectVerifyRun?(runId: string): Promise<VerifyRunInspection>;
  createVerifyCriterionDraft?(
    draft: VerifyCriterionDraftInput,
  ): Promise<R2VerificationConfiguration>;
  createVerifyCheck?(
    check: VerifyCheckInput,
  ): Promise<R2VerificationConfiguration>;
}

export interface HttpCoworkDocClientOptions {
  readonly documentId: string;
  readonly storeId: string;
  /** Injectable for tests, else the global fetch bound to the window. */
  readonly fetchImpl?: typeof fetch;
}

export class HttpCoworkDocClient implements CoworkDocClient {
  readonly #documentId: string;
  readonly #storeId: string;
  readonly #fetch: typeof fetch;

  constructor(options: HttpCoworkDocClientOptions) {
    this.#documentId = options.documentId;
    this.#storeId = options.storeId;
    this.#fetch = options.fetchImpl ?? globalThis.fetch.bind(globalThis);
  }

  #endpoint(): string {
    return `/api/truth/doc/${encodeURIComponent(this.#documentId)}?store_id=${encodeURIComponent(this.#storeId)}`;
  }

  #authority(
    operation: string,
    body: JsonObject,
  ): Promise<Record<string, string>> {
    return coworkHumanAuthorityHeaders(
      {
        operation,
        storeId: this.#storeId,
        documentId: this.#documentId,
        body,
      },
      this.#fetch,
    );
  }

  async fetchDoc(): Promise<R2DocPayload> {
    const response = await this.#fetch(this.#endpoint(), { method: "GET" });
    if (!response.ok) {
      throw new Error(`doc read failed with status ${String(response.status)}`);
    }
    return (await response.json()) as R2DocPayload;
  }

  async setVerifyCriterionEnabled(
    criterionKey: string,
    enabled: boolean,
    expectedActivationId: string | null,
  ): Promise<R2VerificationConfiguration> {
    const body = {
      enabled,
      expected_activation_id: expectedActivationId,
    };
    const authorityHeaders = await this.#authority(
      "verify.criterion_update",
      body,
    );
    const response = await this.#fetch(
      `/api/truth/doc/${encodeURIComponent(this.#documentId)}/verify/criteria/${encodeURIComponent(criterionKey)}?store_id=${encodeURIComponent(this.#storeId)}`,
      {
        method: "PATCH",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", ...authorityHeaders },
        body: JSON.stringify(body),
      },
    );
    if (!response.ok) {
      let message = "Verify checks could not be changed.";
      try {
        const payload = (await response.json()) as {
          error?: unknown;
          message?: unknown;
        };
        if (typeof payload.error === "string") message = payload.error;
        else if (typeof payload.message === "string") message = payload.message;
      } catch {
        // The stable recovery message remains more useful than malformed HTML.
      }
      throw new Error(message);
    }
    const payload = (await response.json()) as {
      configuration?: R2VerificationConfiguration;
    };
    if (payload.configuration === undefined) {
      throw new Error("Verify checks returned an invalid configuration.");
    }
    return payload.configuration;
  }

  async actOnCothink(
    itemId: string,
    action: "park" | "dismiss",
    canonicalSha256: string,
  ): Promise<void> {
    const body = { action, canonical_sha256: canonicalSha256 };
    const authorityHeaders = await this.#authority(
      "cothink.item_action",
      body,
    );
    const response = await this.#fetch(
      `/api/truth/doc/${encodeURIComponent(this.#documentId)}/cothink/items/${encodeURIComponent(itemId)}/actions?store_id=${encodeURIComponent(this.#storeId)}`,
      {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", ...authorityHeaders },
        body: JSON.stringify(body),
      },
    );
    if (!response.ok) {
      let message = "The Co-think item could not be updated.";
      try {
        const payload = (await response.json()) as { error?: unknown };
        if (typeof payload.error === "string") message = payload.error;
      } catch {
        // Preserve the bounded recovery message for malformed responses.
      }
      throw new Error(message);
    }
  }

  async discussCothink(
    itemId: string,
    canonicalSha256: string,
  ): Promise<CothinkDiscussionReceipt> {
    const body = { action: "discuss", canonical_sha256: canonicalSha256 };
    const authorityHeaders = await this.#authority(
      "cothink.item_action",
      body,
    );
    const response = await this.#fetch(
      `/api/truth/doc/${encodeURIComponent(this.#documentId)}/cothink/items/${encodeURIComponent(itemId)}/actions?store_id=${encodeURIComponent(this.#storeId)}`,
      {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", ...authorityHeaders },
        body: JSON.stringify(body),
      },
    );
    const payload = objectValue(await response.json().catch(() => ({})));
    if (
      !response.ok ||
      typeof payload.conversation_id !== "string" ||
      typeof payload.message_id !== "string"
    ) {
      throw new Error(
        typeof payload.error === "string"
          ? payload.error
          : "The Co-think discussion could not be saved in Chat.",
      );
    }
    return {
      conversationId: payload.conversation_id,
      messageId: payload.message_id,
    };
  }

  async inspectVerifyRun(runId: string): Promise<VerifyRunInspection> {
    const response = await this.#fetch(
      `/api/truth/doc/${encodeURIComponent(this.#documentId)}/verify/runs/${encodeURIComponent(runId)}?store_id=${encodeURIComponent(this.#storeId)}`,
      {
        method: "GET",
        credentials: "same-origin",
      },
    );
    if (!response.ok) {
      throw new Error("Verify run details could not be loaded.");
    }
    const payload = objectValue(await response.json());
    const detail = objectValue(payload.detail);
    const action = objectValue(detail.action);
    const plan = objectValue(detail.plan);
    if (
      stringValue(detail.run_id).length === 0 ||
      stringValue(action.action_snapshot_id).length === 0 ||
      stringValue(plan.plan_snapshot_id).length === 0
    ) {
      throw new Error("Verify run details returned an invalid response.");
    }
    return {
      schema: stringValue(detail.schema),
      runId: stringValue(detail.run_id),
      action: {
        actionSnapshotId: stringValue(action.action_snapshot_id),
        structuredHeadSha256: stringValue(action.structured_head_sha256),
        targetKind: stringValue(action.target_kind),
        contextBoundary: objectValue(action.context_boundary),
        egressBoundary: objectValue(action.egress_boundary),
      },
      plan: {
        planSnapshotId: stringValue(plan.plan_snapshot_id),
        canonicalSha256: stringValue(plan.canonical_sha256),
        definition: objectValue(plan.definition),
      },
      checks: arrayValue(detail.checks).map((rawCheck) => {
        const check = objectValue(rawCheck);
        const definition = objectValue(check.definition);
        return {
          checkExecutionId: stringValue(check.check_execution_id),
          status: stringValue(check.status),
          mechanism: stringValue(check.mechanism),
          definition: {
            stableKey: stringValue(definition.stable_key),
            version: numberValue(definition.version),
            title: stringValue(definition.title),
            limitations: arrayValue(definition.limitations).map(stringValue),
          },
        };
      }),
      results: arrayValue(detail.results).map((rawResult) => {
        const result = objectValue(rawResult);
        return {
          evaluationResultId: stringValue(result.evaluation_result_id),
          kind: stringValue(result.kind),
          message: stringValue(result.message),
          dispositions: arrayValue(result.dispositions).map(
            (rawDisposition) => {
              const disposition = objectValue(rawDisposition);
              return {
                decision: stringValue(disposition.decision),
                rationale: stringValue(disposition.rationale),
                policySnapshotSha256: nullableStringValue(
                  disposition.policy_snapshot_sha256,
                ),
              };
            },
          ),
          lineage: arrayValue(result.lineage).map((rawRelation) => {
            const relation = objectValue(rawRelation);
            return {
              relation: stringValue(relation.relation),
              targetKind: stringValue(relation.target_kind),
              targetRef: stringValue(relation.target_ref),
            };
          }),
        };
      }),
      coordination: arrayValue(detail.coordination).map((rawJob) => {
        const job = objectValue(rawJob);
        const executionPlan = objectValue(job.execution_plan);
        return {
          jobId: stringValue(job.job_id),
          role: stringValue(job.role),
          status: stringValue(job.status),
          provider: stringValue(job.provider),
          model: stringValue(job.model),
          egressClass: stringValue(job.egress_class),
          costCeilingUsd: numberValue(job.cost_ceiling_usd),
          executionPlan:
            Object.keys(executionPlan).length === 0
              ? null
              : mapVerifyExecutionPlan(
                  executionPlan as unknown as R2VerifyExecutionPlan,
                ),
          error: nullableStringValue(job.error),
        };
      }),
    };
  }

  async createVerifyCriterionDraft(
    draft: VerifyCriterionDraftInput,
  ): Promise<R2VerificationConfiguration> {
    const body = {
      title: draft.title,
      description: draft.description,
      evaluation_instructions: draft.evaluationInstructions,
      limitations: draft.limitations,
    };
    const authorityHeaders = await this.#authority(
      "verify.criterion_draft_create",
      body,
    );
    const response = await this.#fetch(
      `/api/truth/doc/${encodeURIComponent(this.#documentId)}/verify/criteria/drafts?store_id=${encodeURIComponent(this.#storeId)}`,
      {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", ...authorityHeaders },
        body: JSON.stringify(body),
      },
    );
    if (!response.ok) {
      throw new Error("The user-authored criterion draft could not be saved.");
    }
    const payload = objectValue(await response.json());
    if (
      typeof payload.configuration !== "object" ||
      payload.configuration === null
    ) {
      throw new Error("The criterion draft returned an invalid response.");
    }
    return payload.configuration as unknown as R2VerificationConfiguration;
  }

  async createVerifyCheck(
    check: VerifyCheckInput,
  ): Promise<R2VerificationConfiguration> {
    const body = {
      title: check.title,
      description: check.description,
      evaluation_instructions: check.evaluationInstructions,
      limitations: check.limitations,
    };
    const authorityHeaders = await this.#authority(
      "verify.check_create",
      body,
    );
    const response = await this.#fetch(
      `/api/truth/doc/${encodeURIComponent(this.#documentId)}/verify/checks?store_id=${encodeURIComponent(this.#storeId)}`,
      {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", ...authorityHeaders },
        body: JSON.stringify(body),
      },
    );
    if (!response.ok) {
      let message = "The verification check could not be created.";
      try {
        const payload = objectValue(await response.json());
        if (typeof payload.error === "string") message = payload.error;
      } catch {
        // Preserve the stable recovery message for malformed responses.
      }
      throw new Error(message);
    }
    const payload = objectValue(await response.json());
    if (
      typeof payload.configuration !== "object" ||
      payload.configuration === null
    ) {
      throw new Error("The verification check returned an invalid response.");
    }
    return payload.configuration as unknown as R2VerificationConfiguration;
  }
}
