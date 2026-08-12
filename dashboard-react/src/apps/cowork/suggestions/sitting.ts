import { CoworkHttpError, normalizeCoworkError } from "../providers/errors";
import { sha256Hex } from "../persistence/hashing";
import { coworkHumanAuthorityHeaders } from "../../../security/humanAuthority";
import type {
  DecisionItem,
  SittingDocumentCommit,
  SittingItemResult,
  SittingPrepareBody,
  SittingPrepared,
  SittingResponse,
} from "./types";

export interface CoworkSittingPrepareRequest {
  readonly documentId: string;
  readonly storeId: string;
  readonly body: SittingPrepareBody;
}

export interface CoworkSittingCommitRequest {
  readonly documentId: string;
  readonly storeId: string;
  readonly intentId: string;
  readonly documentCommit: SittingDocumentCommit | null;
}

export interface CoworkSittingTransport {
  prepare(request: CoworkSittingPrepareRequest): Promise<SittingPrepared>;
  commit(request: CoworkSittingCommitRequest): Promise<SittingResponse>;
  cancel(documentId: string, storeId: string, intentId: string): Promise<void>;
}

/** Validate fields whose requirements are knowable before the prepare request. */
export const validateSitting = (items: readonly DecisionItem[]): void => {
  if (items.length === 0) throw new Error("a sitting requires at least one decision");
  for (const item of items) {
    if (item.verb === "edit_confirm") {
      if (item.amend_content === undefined) {
        throw new Error(`edit_confirm on ${item.proposal_id} requires amend_content`);
      }
      if (item.amend_content.length > 0 && item.amend_content.trim().length === 0) {
        throw new Error(
          `amend_content on ${item.proposal_id} cannot be whitespace-only; use an empty string for deletion`,
        );
      }
    }
    if (item.verb === "redirect" && (item.redirect_note ?? "").trim().length === 0) {
      throw new Error(`redirect on ${item.proposal_id} requires redirect_note`);
    }
    if (
      item.verb === "reject_as_preference" &&
      (item.preference_text ?? "").trim().length === 0 &&
      (item.result_claim_id ?? "").trim().length === 0
    ) {
      throw new Error(
        `reject_as_preference on ${item.proposal_id} requires preference_text or result_claim_id`,
      );
    }
  }
};

/** Retained as a small hashing utility for callers and compatibility tests. */
export const buildMaterializePayload = async (renderedMarkdown: string) => ({
  rendered_markdown: renderedMarkdown,
  post_apply_content_sha256: await sha256Hex(new TextEncoder().encode(renderedMarkdown)),
});

export class CoworkSittingClient {
  readonly #transport: CoworkSittingTransport;

  constructor(transport: CoworkSittingTransport) {
    this.#transport = transport;
  }

  async prepare(request: CoworkSittingPrepareRequest): Promise<SittingPrepared> {
    validateSitting(request.body.items);
    return this.#transport.prepare(request);
  }

  commit(request: CoworkSittingCommitRequest): Promise<SittingResponse> {
    return this.#transport.commit(request);
  }

  cancel(documentId: string, storeId: string, intentId: string): Promise<void> {
    return this.#transport.cancel(documentId, storeId, intentId);
  }
}

const readJson = async (response: Response): Promise<unknown> => {
  try {
    return await response.json();
  } catch {
    return {};
  }
};

export class HttpCoworkSittingTransport implements CoworkSittingTransport {
  readonly #fetch: typeof fetch;

  constructor(fetchImpl: typeof fetch = globalThis.fetch.bind(globalThis)) {
    this.#fetch = fetchImpl;
  }

  async #request(url: string, init: RequestInit): Promise<Record<string, unknown>> {
    let response: Response;
    try {
      response = await this.#fetch(url, { credentials: "same-origin", ...init });
    } catch (error) {
      throw new CoworkHttpError(normalizeCoworkError(error));
    }
    const payload = await readJson(response);
    if (!response.ok) {
      throw new CoworkHttpError(normalizeCoworkError(payload, response.status));
    }
    return typeof payload === "object" && payload !== null
      ? (payload as Record<string, unknown>)
      : {};
  }

  async prepare(request: CoworkSittingPrepareRequest): Promise<SittingPrepared> {
    const authorityHeaders = await coworkHumanAuthorityHeaders(
      {
        operation: "review.sitting_prepare",
        storeId: request.storeId,
        documentId: request.documentId,
        body: request.body as unknown as Record<string, unknown>,
      },
      this.#fetch,
    );
    return (await this.#request(
      `/api/truth/doc/${encodeURIComponent(request.documentId)}/sitting/prepare?store_id=${encodeURIComponent(request.storeId)}`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authorityHeaders },
        body: JSON.stringify(request.body),
      },
    )) as unknown as SittingPrepared;
  }

  async commit(request: CoworkSittingCommitRequest): Promise<SittingResponse> {
    const url = `/api/truth/doc/${encodeURIComponent(request.documentId)}/sitting/${encodeURIComponent(request.intentId)}/commit?store_id=${encodeURIComponent(request.storeId)}`;
    if (request.documentCommit === null) {
      const authorityHeaders = await coworkHumanAuthorityHeaders(
        {
          operation: "review.sitting_commit",
          storeId: request.storeId,
          documentId: request.documentId,
          body: {
            intent_id: request.intentId,
            metadata: {},
            snapshot_sha256: null,
            rendered_sha256: null,
          },
        },
        this.#fetch,
      );
      return (await this.#request(url, {
        method: "PUT",
        headers: { "Content-Type": "application/json", ...authorityHeaders },
        body: "{}",
      })) as unknown as SittingResponse;
    }
    const form = new FormData();
    const metadata = {
        snapshot_sha256: request.documentCommit.snapshot_sha256,
        rendered_sha256: request.documentCommit.rendered_sha256,
      };
    form.append("metadata", JSON.stringify(metadata));
    form.append(
      "snapshot",
      new Blob([request.documentCommit.snapshot as BlobPart], {
        type: "application/octet-stream",
      }),
      "document.ydoc",
    );
    form.append(
      "markdown",
      new Blob([request.documentCommit.rendered_markdown], {
        type: "text/markdown;charset=utf-8",
      }),
      "document.md",
    );
    const authorityHeaders = await coworkHumanAuthorityHeaders(
      {
        operation: "review.sitting_commit",
        storeId: request.storeId,
        documentId: request.documentId,
        body: {
          intent_id: request.intentId,
          metadata,
          snapshot_sha256: await sha256Hex(request.documentCommit.snapshot),
          rendered_sha256: await sha256Hex(
            new TextEncoder().encode(request.documentCommit.rendered_markdown),
          ),
        },
      },
      this.#fetch,
    );
    return (await this.#request(url, {
      method: "PUT",
      headers: authorityHeaders,
      body: form,
    })) as unknown as SittingResponse;
  }

  async cancel(documentId: string, storeId: string, intentId: string): Promise<void> {
    const authorityHeaders = await coworkHumanAuthorityHeaders(
      {
        operation: "review.sitting_cancel",
        storeId,
        documentId,
        body: { intent_id: intentId },
      },
      this.#fetch,
    );
    await this.#request(
      `/api/truth/doc/${encodeURIComponent(documentId)}/sitting/${encodeURIComponent(intentId)}?store_id=${encodeURIComponent(storeId)}`,
      { method: "DELETE", headers: authorityHeaders },
    );
  }
}

interface MemoryIntent {
  readonly request: CoworkSittingPrepareRequest;
  readonly prepared: SittingPrepared;
  receipt: SittingResponse | null;
  cancelled: boolean;
}

const resultFor = (item: DecisionItem, failed = false): SittingItemResult => ({
  proposal_id: item.proposal_id,
  verb: item.verb,
  result: failed
    ? "rejected_stale_view"
    : item.verb === "redirect"
      ? "kept_open_redirected"
      : item.verb === "defer"
        ? "kept_open_deferred"
        : item.verb === "endorse"
          ? "kept_open_endorsed"
          : item.verb === "confirm" || item.verb === "edit_confirm"
            ? "applied"
            : "closed",
  base_ok: !failed,
  gesture_id: failed ? null : `gesture-${item.proposal_id}`,
  negation_claim_id: null,
  preference_claim_id: null,
  new_proposal_id: null,
  materialized: !failed && (item.verb === "confirm" || item.verb === "edit_confirm"),
  error: failed ? "canonical_sha256 no longer matches the shown proposal" : null,
});

/** Deterministic two-phase test double with idempotent prepare and commit receipts. */
export class InMemoryCoworkSittingTransport implements CoworkSittingTransport {
  readonly #failedProposalIds: ReadonlySet<string>;
  readonly #byKey = new Map<string, MemoryIntent>();
  readonly #byIntent = new Map<string, MemoryIntent>();
  #lastPrepareRequest: CoworkSittingPrepareRequest | null = null;
  #lastCommitRequest: CoworkSittingCommitRequest | null = null;

  constructor(failedProposalIds: readonly string[] = []) {
    this.#failedProposalIds = new Set(failedProposalIds);
  }

  get lastPrepareRequest(): CoworkSittingPrepareRequest | null {
    return this.#lastPrepareRequest;
  }

  get lastCommitRequest(): CoworkSittingCommitRequest | null {
    return this.#lastCommitRequest;
  }

  async prepare(request: CoworkSittingPrepareRequest): Promise<SittingPrepared> {
    this.#lastPrepareRequest = request;
    const existing = this.#byKey.get(request.body.idempotency_key);
    if (existing !== undefined) {
      if (existing.cancelled) {
        return { ...existing.prepared, state: "cancelled" };
      }
      return existing.receipt === null
        ? existing.prepared
        : { ...existing.prepared, state: "committed", result: existing.receipt };
    }
    const admitted = request.body.items.filter(
      (item) => !this.#failedProposalIds.has(item.proposal_id),
    );
    const failed = request.body.items
      .filter((item) => this.#failedProposalIds.has(item.proposal_id))
      .map((item) => resultFor(item, true));
    const intentId = `sitting-${this.#byIntent.size + 1}`;
    const prepared: SittingPrepared = {
      ok: true,
      intent_id: intentId,
      state: "prepared",
      expires_at: new Date(Date.now() + 15 * 60_000).toISOString(),
      expected_file_sha256: request.body.expected_file_sha256,
      expected_ydoc_head_sha256: request.body.expected_ydoc_head_sha256,
      expected_snapshot_sha256: "a".repeat(64),
      admitted_items: admitted,
      failed_items: failed,
      requires_document_commit: admitted.some(
        (item) => item.verb === "confirm" || item.verb === "edit_confirm",
      ),
    };
    const intent: MemoryIntent = { request, prepared, receipt: null, cancelled: false };
    this.#byKey.set(request.body.idempotency_key, intent);
    this.#byIntent.set(intentId, intent);
    return prepared;
  }

  async commit(request: CoworkSittingCommitRequest): Promise<SittingResponse> {
    this.#lastCommitRequest = request;
    const intent = this.#byIntent.get(request.intentId);
    if (intent === undefined || intent.cancelled) throw new Error("sitting intent unavailable");
    if (intent.receipt !== null) return intent.receipt;
    const prepared = intent.prepared;
    if (prepared.requires_document_commit !== (request.documentCommit !== null)) {
      throw new Error("sitting commit payload does not match the prepared intent");
    }
    const results = intent.request.body.items.map((item) =>
      resultFor(item, this.#failedProposalIds.has(item.proposal_id)),
    );
    const snapshotSha256 =
      request.documentCommit?.snapshot_sha256 ?? prepared.expected_snapshot_sha256;
    const receipt: SittingResponse = {
      ok: true,
      intent_id: request.intentId,
      partial: prepared.failed_items.length > 0,
      results,
      materialize:
        request.documentCommit === null
          ? null
          : {
              new_file_sha256: request.documentCommit.rendered_sha256,
              document_version_id: `version-${request.intentId}`,
            },
      structured_head_sha256: snapshotSha256,
      snapshot_sha256: snapshotSha256,
      routing_deliveries: prepared.admitted_items
        .filter((item) => item.verb === "redirect" || item.verb === "endorse")
        .map((item) => ({
          delivery_id: `delivery-${request.intentId}-${item.proposal_id}`,
          verb: item.verb as "redirect" | "endorse",
          proposal_id: item.proposal_id,
          delivered: true,
          conversation_id: `conversation-${request.intentId}`,
          message_id: `message-${request.intentId}-${item.proposal_id}`,
          reason: null,
          agent: {
            status: "running" as const,
            alive: true,
            started: true,
            error: null,
          },
          ...(item.redirect_note === undefined ? {} : { note: item.redirect_note }),
        })),
    };
    intent.receipt = receipt;
    return receipt;
  }

  async cancel(_documentId: string, _storeId: string, intentId: string): Promise<void> {
    const intent = this.#byIntent.get(intentId);
    if (intent !== undefined && intent.receipt === null) intent.cancelled = true;
  }
}
